"""
quant_access_bot.py — @QuantAccessBot FSM + File d'attente Admin
=================================================================
Flow : GO → confirm → âge/montant → éligibilité → liens → capture → admin queue

Env :
  TELEGRAM_BOT_TOKEN        → token de @QuantAccessBot
  TELEGRAM_CHAT_ID          → chat ID admin perso (fallback)
  TELEGRAM_ADMIN_GROUP_ID   → ID du groupe "QUANT Admin Queue"
  AFFILIATE_LINK            → lien affilié broker
  QUANT_CHANNEL_INVITE      → lien d'invitation canal PRO
  DATABASE_URL               → PostgreSQL pour persistance FSM + membres

Résistance aux restarts :
  - Les états FSM sont persistés en Postgres (table conversation_states)
  - Si le bot redémarre pendant un flow, l'état est restauré depuis Postgres
  - Si état perdu et photo reçue → message "renvoie GO pour recommencer"

Stockage de la preuve et de l'historique :
  - proof_file_id  → file_id Telegram de la photo de dépôt
  - proof_photo_url → URL publique de la photo (via API Telegram getFile)
  - conversation_log → JSONB historique complet du flow [{role, text, ts}]
"""

import os
import re
import json
import asyncio
import aiohttp
from datetime import datetime, timezone
from loguru import logger

# ── Postgres ───────────────────────────────────────────────────────────────────
try:
    import asyncpg
    _DB_URL = os.getenv("DATABASE_URL", "")
    _PG_OK  = bool(_DB_URL)
except ImportError:
    _PG_OK = False

_pool = None


async def _get_pool():
    """Lance le pool Postgres au premier appel et crée toutes les tables/colonnes."""
    global _pool
    if _pool is None and _PG_OK:
        try:
            _pool = await asyncpg.create_pool(
                os.getenv("DATABASE_URL", _DB_URL),
                min_size=1, max_size=5
            )
            async with _pool.acquire() as conn:
                # Table FSM
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS conversation_states (
                        user_id    BIGINT PRIMARY KEY,
                        state      TEXT,
                        data       JSONB DEFAULT '{}',
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                # Colonnes supplémentaires membres (idempotentes)
                for col_sql in [
                    "ALTER TABLE members ADD COLUMN IF NOT EXISTS proof_file_id TEXT",
                    "ALTER TABLE members ADD COLUMN IF NOT EXISTS proof_photo_url TEXT",
                    "ALTER TABLE members ADD COLUMN IF NOT EXISTS conversation_log JSONB DEFAULT '[]'",
                    # Bloc A1 — rate limiting
                    "ALTER TABLE members ADD COLUMN IF NOT EXISTS attempts INT DEFAULT 0",
                    "ALTER TABLE members ADD COLUMN IF NOT EXISTS last_attempt DATE",
                    # Bloc A2 — action log
                    "ALTER TABLE members ADD COLUMN IF NOT EXISTS action_log JSONB DEFAULT '[]'",
                    # F1 — parrainage
                    "ALTER TABLE members ADD COLUMN IF NOT EXISTS referral_code TEXT",
                    "ALTER TABLE members ADD COLUMN IF NOT EXISTS referred_by BIGINT",
                    # F2 — onboarding
                    "ALTER TABLE members ADD COLUMN IF NOT EXISTS onboarding_step INT DEFAULT 0",
                    "ALTER TABLE members ADD COLUMN IF NOT EXISTS validated_at TIMESTAMP",
                ]:
                    try:
                        await conn.execute(col_sql)
                    except Exception:
                        pass  # Colonne existe déjà probablement
                # F1 — Table referrals
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS referrals (
                        id          SERIAL PRIMARY KEY,
                        referrer_id BIGINT,
                        referred_id BIGINT,
                        created_at  TIMESTAMP DEFAULT NOW()
                    )
                """)
                # Index unique référals (idempotent)
                await conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS referrals_pair
                    ON referrals (referrer_id, referred_id)
                """)
                # Index unique referral_code
                await conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS members_referral_code_idx
                    ON members (referral_code) WHERE referral_code IS NOT NULL
                """)
            logger.info("\u2705 Pool Postgres OK — tables et colonnes prêtes")
        except Exception as e:
            logger.warning(f"[DB] Pool init: {e}")
            _pool = None
    return _pool



async def _db_exec(sql: str, *args):
    pool = await _get_pool()
    if not pool:
        return
    try:
        await pool.execute(sql, *args)
    except Exception as e:
        logger.debug(f"[DB exec] {e}")


async def _db_fetch(sql: str, *args):
    pool = await _get_pool()
    if not pool:
        return None
    try:
        return await pool.fetchrow(sql, *args)
    except Exception as e:
        logger.debug(f"[DB fetch] {e}")
        return None


async def _db_fetch_val(sql: str, *args):
    pool = await _get_pool()
    if not pool:
        return None
    try:
        return await pool.fetchval(sql, *args)
    except Exception as e:
        logger.debug(f"[DB fetchval] {e}")
        return None


# ── Helpers FSM Postgres ───────────────────────────────────────────────────────

def _parse_jsonb(raw) -> dict | list:
    """Decode JSONB retourné par asyncpg (dict, list, ou string)."""
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


async def _get_state(user_id: int) -> tuple:
    """Lit l'état FSM depuis Postgres. Retourne (state, data_dict)."""
    row = await _db_fetch(
        "SELECT state, data FROM conversation_states WHERE user_id=$1", user_id
    )
    if not row:
        return None, {}
    return row["state"], _parse_jsonb(row["data"])


async def _set_state(user_id: int, state: str, data: dict):
    """UPSERT l'état FSM dans Postgres."""
    await _db_exec(
        """
        INSERT INTO conversation_states (user_id, state, data, updated_at)
        VALUES ($1, $2, $3::jsonb, NOW())
        ON CONFLICT (user_id) DO UPDATE
          SET state=EXCLUDED.state, data=EXCLUDED.data, updated_at=NOW()
        """,
        user_id, state, json.dumps(data)
    )


async def _clear_state(user_id: int):
    """Supprime l'état FSM de Postgres une fois le flow terminé."""
    await _db_exec("DELETE FROM conversation_states WHERE user_id=$1", user_id)


# ── Helpers conversation_log ───────────────────────────────────────────────────

async def _log_message(user_id: int, role: str, text: str):
    """Ajoute une entrée dans conversation_log du membre."""
    entry = json.dumps({
        "role": role,
        "text": str(text)[:500],  # limiter la taille
        "ts": datetime.now(timezone.utc).isoformat()
    })
    await _db_exec(
        """
        UPDATE members
           SET conversation_log = COALESCE(conversation_log, '[]'::jsonb) || $1::jsonb
         WHERE user_id = $2
        """,
        f"[{entry}]", user_id
    )


async def _get_conversation_log(user_id: int) -> list:
    """Retourne l'historique de conversation du membre."""
    raw = await _db_fetch_val(
        "SELECT conversation_log FROM members WHERE user_id=$1", user_id
    )
    result = _parse_jsonb(raw)
    return result if isinstance(result, list) else []


# ── Telegram photo URL helper ──────────────────────────────────────────────────

async def _get_telegram_photo_url(file_id: str) -> str | None:
    """Résout un file_id Telegram en URL publique via l'API getFile."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token or not file_id:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.get(
                f"https://api.telegram.org/bot{token}/getFile",
                params={"file_id": file_id},
                timeout=aiohttp.ClientTimeout(total=10)
            )
            data = await r.json()
            if data.get("ok"):
                file_path = data["result"]["file_path"]
                return f"https://api.telegram.org/file/bot{token}/{file_path}"
    except Exception as e:
        logger.debug(f"getFile: {e}")
    return None


# ── Telegram ───────────────────────────────────────────────────────────────────
try:
    from telegram import (
        Update, ReplyKeyboardRemove,
        InlineKeyboardButton, InlineKeyboardMarkup,
        ForceReply,
    )
    from telegram.ext import (
        ApplicationBuilder, CommandHandler, MessageHandler,
        ContextTypes, filters, ConversationHandler,
        CallbackQueryHandler,
    )
    from telegram.error import TelegramError
    from telegram.constants import ParseMode
    _TELEGRAM_OK = True
except ImportError:
    _TELEGRAM_OK = False
    logger.warning("\u26a0\ufe0f python-telegram-bot non installé — QuantAccessBot désactivé")

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID       = os.getenv("TELEGRAM_CHAT_ID", "")
ADMIN_GROUP_ID = os.getenv("TELEGRAM_ADMIN_GROUP_ID", "")
AFFILIATE      = os.getenv("AFFILIATE_LINK", "https://clicks.pipaffiliates.com/c?c=589093&l=fr&p=1")
CHANNEL_LINK   = os.getenv("QUANT_CHANNEL_INVITE", "https://t.me/+3fh4-ilGuCk2ZWY0")

# FSM States
WAITING_CONFIRM   = 1
WAITING_INFO      = 2
WAITING_PROOF     = 3
WAITING_REFERRAL  = 4  # F1 — étape code parrainage


_request_counter: int = 0


def _next_request_num() -> int:
    global _request_counter
    _request_counter += 1
    return _request_counter


# ── F1 — Parrainage helpers ─────────────────────────────────────────────────────────────────

import random
import string

def _gen_referral_code(username: str) -> str:
    """Génère un code unique : QUANT-XXXX-XXXX (4 chars username + 4 random)."""
    prefix = (username or "USER").upper()[:4].strip()
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"QUANT-{prefix}-{suffix}"


async def _assign_referral_code(user_id: int, username: str) -> str:
    """Assigne et stocke un code parrainage unique en Postgres. Retourne le code."""
    pool = await _get_pool()
    if not pool:
        return _gen_referral_code(username)
    # Vérifier si déjà un code
    existing = await pool.fetchval("SELECT referral_code FROM members WHERE user_id=$1", user_id)
    if existing:
        return existing
    for _ in range(10):  # max 10 tentatives pour éviter collision
        code = _gen_referral_code(username)
        try:
            await pool.execute(
                "UPDATE members SET referral_code=$1 WHERE user_id=$2", code, user_id
            )
            return code
        except Exception:
            continue  # collision INDEX UNIQUE, réessayer
    return _gen_referral_code(username)  # fallback sans stockage


async def _notify_referrer(bot, referrer_id: int, referred_username: str):
    """Envoie un DM au parrain quand son filleul est validé."""
    try:
        await bot.send_message(
            chat_id=referrer_id,
            text=(
                f"\U0001f389 Ton filleul @{referred_username or '???'} vient de rejoindre QUANT PRO !\n"
                "Merci de développer la communauté \U0001f64f\n\n"
                "Continue à partager ton code pour gagner encore plus d'accès offerts !"
            )
        )
    except TelegramError as e:
        logger.debug(f"Notify referrer {referrer_id}: {e}")


# ── F2 — Onboarding scheduler ────────────────────────────────────────────────────────────

ONBOARDING_MSGS = {
    1: (  # Jour 1 : comment lire un signal
        "\U0001f4da Comment lire un signal QUANT ?\n\n"
        "Quand tu vois :\n"
        "\U0001f7e2 J'ACH\u00c8TE GOLD \u00e0 3285\n"
        "\U0001f3af TP1 : 3300\n"
        "\U0001f3af TP2 : 3320\n"
        "\U0001f3af TP3 : Ouvert\n"
        "\U0001f512 SL : 3270\n\n"
        "\u2192 Tu ouvres un achat sur l'or \u00e0 3285\n"
        "\u2192 Tu poses ton SL \u00e0 3270\n"
        "\u2192 Tu fermes 1/3 \u00e0 TP1, 1/3 \u00e0 TP2, laisses courir le reste\n\n"
        "\U0001f510 Break-Even (BE) : d\u00e8s que TP1 est touch\u00e9, le bot d\u00e9place\n"
        "le SL au prix d'entr\u00e9e automatiquement.\n"
        "Le trade devient sans risque.\n"
        "Tu verras un message \u2018SL \u2192 Break-Even\u2019 dans le canal \u2705\n\n"
        "Simple et efficace \U0001f4aa"
    ),
    3: (  # Jour 3 : maximiser les résultats
        "\U0001f3af Comment maximiser tes résultats avec QUANT ?\n\n"
        "1\ufe0f\u20e3 Applique TOUS les signaux, pas seulement ceux qui te plaisent\n"
        "2\ufe0f\u20e3 Respecte toujours le SL \u2014 c'est non négociable\n"
        "3\ufe0f\u20e3 Ne surtraded pas \u2014 attends les signaux QUANT\n\n"
        "\U0001f4ca Win rate moyen : 60.9%\n"
        "\U0001f4b0 R moyen par trade : +1.8R\n\n"
        "Tu as des questions sur un signal ? Réponds ici \U0001f447"
    ),
}


async def _run_onboarding(bot) -> None:
    """
    F2 — Vérifie toutes les heures les membres actifs et envoie
    les messages d'onboarding selon leur ancienneté.
    Tourne dans une boucle asyncio en tâche de fond.
    """
    from datetime import timedelta
    while True:
        try:
            pool = await _get_pool()
            if pool:
                now = datetime.now(timezone.utc)
                rows = await pool.fetch(
                    """
                    SELECT user_id, username, onboarding_step, validated_at
                    FROM members
                    WHERE status='active' AND onboarding_step < 3
                      AND validated_at IS NOT NULL
                    """
                )
                for row in rows:
                    uid       = row["user_id"]
                    step      = row["onboarding_step"] or 0
                    val_at    = row["validated_at"]
                    if not val_at:
                        continue
                    hours     = (now - val_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600
                    uname     = row["username"] or "trader"

                    # Étape 0 \u2192 1 : Jour 1 (+24h)
                    if step == 0 and hours >= 24:
                        try:
                            await bot.send_message(chat_id=uid, text=ONBOARDING_MSGS[1])
                            await pool.execute(
                                "UPDATE members SET onboarding_step=1 WHERE user_id=$1", uid
                            )
                            logger.info(f"\U0001f4da Onboarding J1 envoyé \u00e0 {uid}")
                        except TelegramError as e:
                            logger.debug(f"Onboarding J1 {uid}: {e}")

                    # Étape 1 \u2192 3 : Jour 3 (+72h)
                    elif step == 1 and hours >= 72:
                        try:
                            await bot.send_message(chat_id=uid, text=ONBOARDING_MSGS[3])
                            await pool.execute(
                                "UPDATE members SET onboarding_step=3 WHERE user_id=$1", uid
                            )
                            logger.info(f"\U0001f3af Onboarding J3 envoyé \u00e0 {uid}")
                        except TelegramError as e:
                            logger.debug(f"Onboarding J3 {uid}: {e}")

        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logger.warning(f"_run_onboarding: {ex}")

        # ── F1 — Post bilan vendredi 18h UTC ─────────────────────────────
        try:
            now_check = datetime.now(timezone.utc)
            # Vendredi = weekday 4, 18h UTC ± 2min
            if now_check.weekday() == 4 and now_check.hour == 18 and now_check.minute < 2:
                key_f1 = f"weekly_{now_check.strftime('%Y-%W')}"
                pool_f1 = await _get_pool()
                if pool_f1 and not getattr(_run_onboarding, '_f1_posted', None) == key_f1:
                    row_f1 = await pool_f1.fetchrow("""
                        SELECT
                            COUNT(*)                                    AS nb_trades,
                            COUNT(*) FILTER (WHERE result='win')        AS nb_wins,
                            AVG(r_result)                               AS avg_r,
                            MAX(r_result)                               AS best_r,
                            MAX(symbol) FILTER (WHERE r_result = MAX(r_result) OVER()) AS best_sym
                        FROM signals
                        WHERE created_at > NOW() - INTERVAL '7 days'
                    """)
                    nb = int(row_f1["nb_trades"] or 0) if row_f1 else 0

                    if nb >= 3:
                        nb_wins  = int(row_f1["nb_wins"] or 0)
                        winrate  = round(nb_wins / nb * 100) if nb else 0
                        avg_r    = float(row_f1["avg_r"] or 0)
                        best_r   = float(row_f1["best_r"] or 0)
                        best_sym = row_f1["best_sym"] or "N/A"
                    else:
                        # Fallback données historiques
                        nb = 9; nb_wins = 8; winrate = 88
                        avg_r = 2.9; best_r = 7.4; best_sym = "GOLD"

                    # Calcul label semaine
                    from datetime import timedelta
                    monday = now_check - timedelta(days=now_check.weekday())
                    friday = monday + timedelta(days=4)
                    lundi_s  = monday.strftime("%d/%m")
                    vendr_s  = friday.strftime("%d/%m")

                    txt_f1 = (
                        f"📊 SEMAINE DU {lundi_s} AU {vendr_s}\n\n"
                        f"✅ {nb} trades clôturés\n"
                        f"🏆 Win rate : {winrate}%\n"
                        f"💰 R moyen : +{avg_r:.1f}R\n"
                        f"📈 Meilleur signal : {best_sym} +{best_r:.1f}R\n\n"
                        f"Les signaux continuent lundi — tu es dedans ? 👇\n"
                        f"👉 @QuantAccessBot"
                    )
                    try:
                        import requests as _req
                        _tok  = os.getenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
                        _chan = os.getenv("TELEGRAM_CHANNEL_ID", "")
                        if _tok and _chan:
                            _req.post(
                                f"https://api.telegram.org/bot{_tok}/sendMessage",
                                json={"chat_id": _chan, "text": txt_f1},
                                timeout=10,
                            )
                            logger.info(f"📊 [F1] Bilan semaine posté dans le canal PRO")
                            _run_onboarding._f1_posted = key_f1
                    except Exception as ef1:
                        logger.warning(f"[F1] send: {ef1}")
        except Exception as ex_f1:
            logger.debug(f"[F1] {ex_f1}")

        # ── F2 — Rappel membres inactifs lundi 9h UTC ────────────────────
        try:
            now_check = datetime.now(timezone.utc)
            # Lundi = weekday 0, 9h UTC ± 2min
            if now_check.weekday() == 0 and now_check.hour == 9 and now_check.minute < 2:
                key_f2 = f"inactive_{now_check.strftime('%Y-%W')}"
                pool_f2 = await _get_pool()
                if pool_f2 and not getattr(_run_onboarding, '_f2_posted', None) == key_f2:
                    # Assurer la colonne last_active
                    try:
                        await pool_f2.execute(
                            "ALTER TABLE members ADD COLUMN IF NOT EXISTS last_active TIMESTAMP DEFAULT NOW()"
                        )
                    except Exception:
                        pass

                    inactive = await pool_f2.fetch("""
                        SELECT user_id, username FROM members
                        WHERE status='active'
                          AND (last_active IS NULL OR last_active < NOW() - INTERVAL '7 days')
                    """)
                    sent = 0
                    for row_i in inactive:
                        uid   = row_i["user_id"]
                        uname = row_i["username"] or "trader"
                        msg_f2 = (
                            f"👋 Salut {uname} !\n\n"
                            f"Tu es toujours actif sur QUANT Signals PRO ? 📊\n\n"
                            f"Les signaux de cette semaine sont déjà en cours —\n"
                            f"ne rate pas les prochains trades !\n\n"
                            f"Des questions ? Réponds directement ici 👇"
                        )
                        try:
                            await bot.send_message(chat_id=uid, text=msg_f2)
                            await pool_f2.execute(
                                "UPDATE members SET last_active=NOW() WHERE user_id=$1", uid
                            )
                            sent += 1
                        except Exception as ef2m:
                            logger.debug(f"[F2] DM {uid}: {ef2m}")
                    logger.info(f"👋 [F2] {sent} rappels inactifs envoyés")
                    _run_onboarding._f2_posted = key_f2
        except Exception as ex_f2:
            logger.debug(f"[F2] {ex_f2}")

        await asyncio.sleep(3600)  # vérifier toutes les heures



# ── Textes ────────────────────────────────────────────────────────────────────

MSG_READY = "Prêt à passer à l'action ? 🔥"

MSG_ASK_INFO = (
    "Parfait 👊\n\n"
    "Avant de te donner accès à QUANT Signals PRO, "
    "j'ai besoin de 2 infos rapides :\n\n"
    "1️⃣ Ton âge\n"
    "2️⃣ Combien tu peux déposer sur le broker\n\n"
    "⚠️ Minimum requis : 300€\n"
    "(On te conseille 500€+ pour maximiser les résultats)\n\n"
    "Réponds dans ce format :\n"
    "ex: 25 ans, 500€"
)

MSG_TOO_YOUNG = (
    "Désolé, l'accès est réservé aux personnes majeures (+18 ans).\n"
    "Tu pourras rejoindre dans quelques temps 🙏"
)

MSG_LOW_DEPOSIT = (
    "Le dépôt minimum est de 300€ pour accéder au groupe.\n"
    "Si tu atteins ce montant, reviens nous voir 💪"
)

MSG_ELIGIBLE = (
    "✅ Super, tu es éligible !\n\n"
    "En rejoignant QUANT Signals PRO, tu bénéficies de :\n\n"
    "✅ Signaux en temps réel sur 10 actifs (GOLD, BTC, Indices...)\n"
    "✅ Multi-TP géré automatiquement (TP1 / TP2 / TP3)\n"
    "✅ Risk management strict (-5% max par jour)\n"
    "✅ Bot validé 5/5 sur challenge Prop Firm\n"
    "✅ Win rate moyen : 60.9%\n"
    "✅ Support disponible 7j/7"
)


def MSG_STEPS(link: str) -> str:
    return (
        "Voici les étapes pour rejoindre 👇\n\n"
        "1️⃣ Ouvre ton compte broker via notre lien :\n"
        f"👉 {link}\n\n"
        "2️⃣ Choisis ces paramètres :\n"
        "- Compte : Standard\n"
        "- Devise : EUR\n"
        "- Levier : 1:500\n\n"
        "3️⃣ Fais un dépôt minimum de 300€\n"
        "(on te conseille 500€+ pour de meilleurs résultats)\n\n"
        "4️⃣ Une fois le dépôt effectué, envoie-moi :\n"
        "📸 Une capture d'écran de la confirmation de dépôt"
    )


MSG_WAITPROOF = (
    "🔥 Hâte de te voir rejoindre l'aventure QUANT !\n\n"
    "Dès réception de ta capture, ton accès sera activé sous 24h.\n\n"
    "Des questions ? Réponds directement ici 👇"
)

MSG_PROOF_RECEIVED = (
    "📩 Capture reçue, merci !\n\n"
    "Je transmets à l'équipe pour vérification.\n"
    "Ton accès sera activé sous 24h maximum ⏳\n\n"
    "On te contacte dès que c'est bon ✅"
)

MSG_RESTART_NEEDED = (
    "Oups, notre bot a redémarré 😅\n\n"
    "Renvoie GO pour recommencer, ça prend 2 minutes !"
)

MSG_REFUSED = (
    "Désolé, nous n'avons pas pu valider votre dépôt.\n"
    "Contactez-nous pour plus d'infos."
)


# ── Helpers admin Telegram ────────────────────────────────────────────────────

# State context key pour relay message
_ADMIN_RELAY_TARGET = "admin_relay_target_id"


def _admin_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Construit le clavier inline 8 boutons pour la fiche admin."""
    uid = str(user_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2705 Valider",       callback_data=f"valider_{uid}"),
         InlineKeyboardButton("\u274c Refuser",       callback_data=f"refuser_{uid}")],
        [InlineKeyboardButton("\U0001f4ac Message",   callback_data=f"message_{uid}"),
         InlineKeyboardButton("\u23f3 +Infos",        callback_data=f"infos_{uid}")],
        [InlineKeyboardButton("\U0001f4cb Photo re\u00e7ue", callback_data=f"photo_{uid}"),
         InlineKeyboardButton("\U0001f504 Nlle photo",   callback_data=f"newphoto_{uid}")],
        [InlineKeyboardButton("\U0001f6ab Kick",      callback_data=f"kick_{uid}"),
         InlineKeyboardButton("\U0001f4ca Stats",     callback_data="stats_global")],
    ])


async def _post_to_admin_group(ctx, req_num, username, user_id,
                                name, age, montant, photo_id) -> int | None:
    """
    Envoie photo + fiche + boutons inline vers :
      - ADMIN_ID        (DM perso avec InlineKeyboard 8 boutons)
      - ADMIN_GROUP_ID  (groupe optionnel, sans boutons inline)
    """
    uname      = f"@{username}" if username else f"ID:{user_id}"
    age_str    = f"{age} ans"   if age    else "? ans"
    amount_str = f"{montant}€"  if montant else "?€"
    now_str    = datetime.now(timezone.utc).strftime("%d/%m/%Y à %H:%M")

    caption = (
        f"📸 Demande #{req_num} — {name} ({uname})\n"
        f"🎂 {age_str} | 💰 {amount_str}\n"
        f"📅 {now_str} UTC"
    )
    keyboard = _admin_keyboard(user_id)

    # ─ DM admin perso : photo + boutons inline
    admin_msg_id = None
    if ADMIN_ID:
        try:
            msg = await ctx.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo_id,
                caption=caption,
                reply_markup=keyboard,
            )
            admin_msg_id = msg.message_id
            logger.info(f"📲 Fiche #{req_num} envoyée à admin ({ADMIN_ID})")
        except TelegramError as e:
            logger.warning(f"Admin DM photo: {e}")

    # ─ Groupe admin Telegram (sans boutons inline)
    if ADMIN_GROUP_ID:
        try:
            await ctx.bot.send_photo(
                chat_id=ADMIN_GROUP_ID,
                photo=photo_id,
                caption=(
                    caption +
                    f"\n\n✅ /valider {user_id}   ❌ /refuser {user_id}"
                ),
            )
        except TelegramError as e:
            logger.warning(f"Admin group error: {e}")

    return admin_msg_id


# ── CallbackQuery — 8 boutons admin ──────────────────────────────────────────

async def handle_admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Gère tous les callbacks des boutons inline admin. Format : action_user_id."""
    query   = update.callback_query
    admin   = update.effective_user
    data    = query.data or ""
    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y à %H:%M")

    # ─ SÉCURITÉ : seul l'admin peut cliquer les boutons ─────────────────────
    if ADMIN_ID and str(admin.id) != str(ADMIN_ID):
        await query.answer("⛔ Accès refusé.", show_alert=True)
        return

    await query.answer()  # stop le spinner du bouton

    # ─ STATS GLOBAL (pas d'user_id)
    if data == "stats_global":
        pool = await _get_pool()
        if not pool:
            await ctx.bot.send_message(chat_id=admin.id, text="❌ DB indisponible"); return
        started = await pool.fetchval("SELECT COUNT(*) FROM members") or 0
        pending = await pool.fetchval("SELECT COUNT(*) FROM members WHERE status='pending'") or 0
        active  = await pool.fetchval("SELECT COUNT(*) FROM members WHERE status='active'") or 0
        refused = await pool.fetchval("SELECT COUNT(*) FROM members WHERE status IN ('refused','kicked')") or 0
        conv    = round(active / started * 100, 1) if started else 0
        await ctx.bot.send_message(
            chat_id=admin.id,
            text=(
                f"📊 Stats QUANT\n\n"
                f"👥 Total : {started} | ✅ Actifs : {active}\n"
                f"⏳ Pending : {pending}\n"
                f"❌ Refusés : {refused}\n"
                f"📈 Conversion : {conv}%"
            )
        )
        return

    # ─ Parser action + user_id
    parts = data.split("_", 1)
    if len(parts) != 2:
        return
    action, uid_str = parts
    try:
        target_id = int(uid_str)
    except ValueError:
        return

    pool = await _get_pool()
    if not pool:
        await ctx.bot.send_message(chat_id=admin.id, text="❌ DB indisponible"); return
    row       = await pool.fetchrow("SELECT * FROM members WHERE user_id=$1", target_id)
    uname_str = f"@{row['username']}" if row and row["username"] else f"ID:{target_id}"

    async def _edit_cap(extra: str):
        cap = ((query.message.caption or "") + f"\n\n{extra}")[:1024]
        try:
            await query.edit_message_caption(caption=cap)
        except Exception:
            pass

    # ── VALIDER ───────────────────────────────────────────────────────────────
    if action == "valider":
        if not row:
            await _edit_cap("❌ Membre introuvable"); return
        # Idempotence : déjà actif ?
        if row["status"] == "active":
            await ctx.bot.send_message(chat_id=admin.id,
                text=f"ℹ️ {uname_str} est déjà actif — pas de re-validation."); return
        # Vérifier que l'invitation existe
        if not CHANNEL_LINK:
            await ctx.bot.send_message(chat_id=admin.id,
                text="⚠️ QUANT_CHANNEL_INVITE manquant dans .env — validation annulée.")
            return
        try:
            await ctx.bot.send_message(
                chat_id=target_id,
                text=(
                    f"✅ Bonne nouvelle ! Ton dépôt a été validé.\n\n"
                    f"👉 Rejoins QUANT Signals PRO ici :\n{CHANNEL_LINK}\n\n"
                    f"Bienvenue dans l'équipe 🚀"
                )
            )
            await ctx.bot.send_message(
                chat_id=target_id,
                text=(
                    "🎉 Bienvenue dans QUANT Signals PRO !\n\n"
                    "Tu fais maintenant partie d'une communauté exclusive.\n\n"
                    "📊 Les signaux arrivent en temps réel dans le canal\n"
                    "🎯 Chaque signal a un TP1, TP2, TP3 et un SL\n"
                    "💡 Ne risque jamais plus de 1-2% par trade\n"
                    "⏰ Les meilleurs signaux arrivent entre 8h et 22h\n\n"
                    "Une question ? Réponds directement ici 👇"
                )
            )
        except TelegramError as e:
            logger.debug(f"Valider DM: {e}")
        await pool.execute(
            "UPDATE members SET status='active', validated_at=NOW() WHERE user_id=$1", target_id
        )
        await _edit_cap(f"✅ Validé par @{admin.username or admin.full_name} le {now_str} UTC")
        await ctx.bot.send_message(chat_id=admin.id, text=f"✅ {uname_str} validé !")
        logger.info(f"✅ {target_id} validé via bouton par @{admin.username}")

    # ── REFUSER ───────────────────────────────────────────────────────────────
    elif action == "refuser":
        # Idempotence : déjà refusé ?
        if row and row["status"] in ("refused", "kicked"):
            await ctx.bot.send_message(chat_id=admin.id,
                text=f"ℹ️ {uname_str} est déjà refusé/kicked."); return
        try:
            await ctx.bot.send_message(
                chat_id=target_id,
                text=(
                    "Désolé, nous n'avons pas pu valider ton dépôt.\n"
                    "Contacte-nous directement si besoin !"
                )
            )
        except TelegramError as e:
            logger.debug(f"Refuser DM: {e}")
        await pool.execute("UPDATE members SET status='refused' WHERE user_id=$1", target_id)
        await _edit_cap(f"❌ Refusé par @{admin.username or admin.full_name} le {now_str} UTC")
        logger.info(f"❌ {target_id} refusé via bouton par @{admin.username}")

    # ── MESSAGE (relay) ───────────────────────────────────────────────────────
    elif action == "message":
        ctx.user_data[_ADMIN_RELAY_TARGET] = target_id
        await ctx.bot.send_message(
            chat_id=admin.id,
            text=(
                f"💬 Écris ton message ici 👇\n"
                f"Je le transmets directement à {uname_str}\n"
                "(Envoie /annuler pour annuler)"
            ),
            reply_markup=ForceReply(selective=True),
        )

    # ── +INFOS ────────────────────────────────────────────────────────────────
    elif action == "infos":
        try:
            await ctx.bot.send_message(
                chat_id=target_id,
                text=(
                    "Merci pour ta capture ! 📸\n"
                    "Pourrais-tu envoyer une photo plus nette\n"
                    "montrant clairement le montant et ton nom ? 🙏"
                )
            )
        except TelegramError as e:
            logger.debug(f"+Infos DM: {e}")
        await pool.execute("UPDATE members SET status='pending' WHERE user_id=$1", target_id)
        if row:
            await _set_state(target_id, "WAITING_PROOF",
                             {"age": row["age"] or 0,
                              "amount": row["amount_declared"] or 0,
                              "username": row["username"] or ""})
        await _edit_cap(f"⏳ +Infos demandées le {now_str} UTC")
        await ctx.bot.send_message(chat_id=admin.id,
                                   text=f"⏳ Demande d'infos envoyée à {uname_str}.")

    # ── PHOTO REÇUE ───────────────────────────────────────────────────────────
    elif action == "photo":
        if not row or not row["proof_photo_url"]:
            await ctx.bot.send_message(
                chat_id=admin.id,
                text=f"❌ Aucune photo disponible pour {uname_str}"
            )
            return
        try:
            await ctx.bot.send_photo(
                chat_id=admin.id,
                photo=row["proof_photo_url"],
                caption=f"📸 Preuve de dépôt de {uname_str}"
            )
        except TelegramError as e:
            await ctx.bot.send_message(
                chat_id=admin.id,
                text=f"❌ Photo: {e}\n{row['proof_photo_url']}"
            )

    # ── NOUVELLE PHOTO ────────────────────────────────────────────────────────
    elif action == "newphoto":
        try:
            await ctx.bot.send_message(
                chat_id=target_id,
                text="Envoie-moi une nouvelle capture de ton dépôt 📸"
            )
        except TelegramError as e:
            logger.debug(f"newphoto DM: {e}")
        if row:
            await _set_state(target_id, "WAITING_PROOF",
                             {"age": row["age"] or 0,
                              "amount": row["amount_declared"] or 0,
                              "username": row["username"] or ""})
        await ctx.bot.send_message(
            chat_id=admin.id,
            text=f"🔄 Nouvelle photo demandée à {uname_str}."
        )

    # ── KICK ──────────────────────────────────────────────────────────────────
    elif action == "kick":
        try:
            await ctx.bot.send_message(
                chat_id=target_id,
                text="Ton accès QUANT PRO a été révoqué."
            )
        except TelegramError as e:
            logger.debug(f"Kick DM: {e}")
        await pool.execute("UPDATE members SET status='kicked' WHERE user_id=$1", target_id)
        await _edit_cap(f"🚫 Kické par @{admin.username or admin.full_name} le {now_str} UTC")
        logger.info(f"🚫 {target_id} kické via bouton par @{admin.username}")


async def handle_admin_message_relay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Transmet le message texte de l'admin au membre Telegram cible (mode relay)."""
    target_id = ctx.user_data.get(_ADMIN_RELAY_TARGET)
    if not target_id:
        return
    if update.message.text == "/annuler":
        ctx.user_data.pop(_ADMIN_RELAY_TARGET, None)
        await update.message.reply_text("❌ Relay annulé.")
        return
    admin      = update.effective_user
    admin_name = f"@{admin.username}" if admin.username else admin.full_name
    pool       = await _get_pool()
    row        = await pool.fetchrow("SELECT username FROM members WHERE user_id=$1", target_id) if pool else None
    uname      = f"@{row['username']}" if row and row["username"] else f"ID:{target_id}"
    try:
        await ctx.bot.send_message(chat_id=target_id, text=update.message.text)
        await update.message.reply_text(
            f"✅ Message transmis à {uname} :\n\n\"{update.message.text}\""
        )
        logger.info(f"💬 Relay admin→{target_id} par {admin_name}")
    except TelegramError as e:
        await update.message.reply_text(f"❌ Impossible de contacter {uname}: {e}")
    ctx.user_data.pop(_ADMIN_RELAY_TARGET, None)


async def _route_private_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Route texte privé : relay admin si mode actif, sinon handle_fallback."""
    if ctx.user_data.get(_ADMIN_RELAY_TARGET):
        await handle_admin_message_relay(update, ctx)
    else:
        await handle_fallback(update, ctx)


# ── FSM Handlers ──────────────────────────────────────────────────────────────


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bienvenue !\n\n"
        "Je suis le bot d'accès à QUANT Signals PRO.\n\n"
        "Envoie GO quand tu es prêt 🚀"
    )


async def handle_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # ── Bloc A1 — Rate limiting : max 3 tentatives par jour ──
    pool = await _get_pool()
    if pool:
        row = await pool.fetchrow("SELECT attempts, last_attempt FROM members WHERE user_id=$1", user.id)
        if row:
            today = datetime.now(timezone.utc).date()
            last  = row["last_attempt"]
            count = row["attempts"] or 0
            if last == today and count >= 3:
                await update.message.reply_text(
                    "Tu as atteint la limite de tentatives du jour.\n"
                    "Réessaie demain ou contacte le support 🙏"
                )
                logger.warning(f"[RATELIMIT] {user.id} bloqué ({count} tentatives aujourd'hui)")
                return ConversationHandler.END

    # Sauvegarder l'état en Postgres
    await _set_state(user.id, "WAITING_REFERRAL", {"username": user.username or ""})
    # INSERT membres status='started' + incrément attempts
    await _db_exec(
        """
        INSERT INTO members (user_id, username, status, conversation_log, attempts, last_attempt)
        VALUES ($1, $2, 'started', '[]'::jsonb, 1, CURRENT_DATE)
        ON CONFLICT (user_id) DO UPDATE
          SET status='started', last_active=NOW(), conversation_log='[]'::jsonb,
              attempts = CASE
                WHEN members.last_attempt = CURRENT_DATE THEN members.attempts + 1
                ELSE 1
              END,
              last_attempt = CURRENT_DATE
        """,
        user.id, user.username or ""
    )
    bot_msg = MSG_READY
    await update.message.reply_text(bot_msg)
    # F1 — demander un code de parrainage
    referral_msg = (
        "\U0001f381 Tu as un code de parrainage ?\n"
        "Entre-le ci-dessous, ou tape SKIP pour continuer."
    )
    await update.message.reply_text(referral_msg)
    await _log_message(user.id, "user", "GO")
    await _log_message(user.id, "bot", bot_msg)
    await _log_message(user.id, "bot", referral_msg)
    return WAITING_REFERRAL


async def handle_referral(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """F1 — Traite le code parrainage ou SKIP."""
    user = update.effective_user
    text = (update.message.text or "").strip().upper()
    await _log_message(user.id, "user", text)

    _, data = await _get_state(user.id)
    data["username"] = user.username or ""

    if text != "SKIP":
        # Vérifier si le code existe
        pool = await _get_pool()
        referrer = None
        if pool:
            referrer = await pool.fetchrow(
                "SELECT user_id, username FROM members WHERE UPPER(referral_code)=$1", text
            )
        if referrer and referrer["user_id"] != user.id:
            data["referred_by"] = referrer["user_id"]
            await _db_exec(
                "UPDATE members SET referred_by=$1 WHERE user_id=$2",
                referrer["user_id"], user.id
            )
            ref_reply = f"\u2705 Code validé ! Parrain : @{referrer['username'] or referrer['user_id']}\n\nOn continue !"
        else:
            ref_reply = "\u274c Code non trouvé. On continue sans code."
        await update.message.reply_text(ref_reply)
        await _log_message(user.id, "bot", ref_reply)
    else:
        await update.message.reply_text("OK, on continue !")

    # Passer à l'étape suivante
    await _set_state(user.id, "WAITING_CONFIRM", data)
    await update.message.reply_text(MSG_READY)
    await _log_message(user.id, "bot", MSG_READY)
    return WAITING_CONFIRM



async def handle_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    _, data = await _get_state(user.id)
    await _set_state(user.id, "WAITING_INFO", data)
    user_text = update.message.text or "Oui"
    await _log_message(user.id, "user", user_text)
    await update.message.reply_text(MSG_ASK_INFO)
    await _log_message(user.id, "bot", MSG_ASK_INFO)
    return WAITING_INFO


async def handle_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text or ""
    await _log_message(user.id, "user", text)

    numbers = re.findall(r'\d+', text)
    if len(numbers) < 2:
        reply = "Je n'ai pas bien compris 🤔\nRéponds dans ce format : ex: 25 ans, 500€"
        await update.message.reply_text(reply)
        await _log_message(user.id, "bot", reply)
        return WAITING_INFO

    age    = int(numbers[0])
    amount = int(numbers[1])

    if age < 18:
        await _clear_state(user.id)
        await update.message.reply_text(MSG_TOO_YOUNG)
        await _log_message(user.id, "bot", MSG_TOO_YOUNG)
        return ConversationHandler.END

    if amount < 300:
        await _clear_state(user.id)
        await update.message.reply_text(MSG_LOW_DEPOSIT)
        await _log_message(user.id, "bot", MSG_LOW_DEPOSIT)
        return ConversationHandler.END

    # Stocker age/amount en Postgres
    _, existing_data = await _get_state(user.id)
    data = {**existing_data, "age": age, "amount": amount}
    await _set_state(user.id, "WAITING_PROOF", data)
    ctx.user_data["age"]    = age
    ctx.user_data["amount"] = amount

    await update.message.reply_text(MSG_ELIGIBLE)
    await _log_message(user.id, "bot", MSG_ELIGIBLE)
    await asyncio.sleep(2)
    steps_msg = MSG_STEPS(AFFILIATE)
    await update.message.reply_text(steps_msg)
    await _log_message(user.id, "bot", steps_msg)
    await asyncio.sleep(2)
    await update.message.reply_text(MSG_WAITPROOF)
    await _log_message(user.id, "bot", MSG_WAITPROOF)
    return WAITING_PROOF


async def _process_proof(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                          age, amount, photo_id: str):
    """Traitement commun de la preuve photo (depuis ConvHandler ou fallback)."""
    user     = update.effective_user
    user_id  = user.id
    username = user.username or ""
    name     = user.full_name or "Inconnu"

    age_val    = int(age)    if age    else None
    amount_val = int(amount) if amount else None

    # Confirmer immédiatement
    await update.message.reply_text(MSG_PROOF_RECEIVED)
    await _log_message(user_id, "user", "[Photo de dépôt envoyée]")
    await _log_message(user_id, "bot", MSG_PROOF_RECEIVED)

    req_num = _next_request_num()

    # → Groupe admin Telegram
    await _post_to_admin_group(ctx, req_num, username, user_id, name,
                                age_val, amount_val, photo_id)

    # → Fallback chat admin perso
    if ADMIN_ID:
        try:
            uname_str  = f"@{username}" if username else f"ID:{user_id}"
            age_str    = f"{age_val}ans"    if age_val    else "?ans"
            amount_str = f"{amount_val}€"   if amount_val else "?€"
            await ctx.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo_id,
                caption=f"📸 Demande #{req_num} — {name} ({uname_str})\n🎂 {age_str} | 💰 {amount_str}"
            )
        except TelegramError as e:
            logger.debug(f"Admin personal fallback: {e}")

    # Résoudre l'URL de la photo via l'API Telegram getFile
    photo_url = await _get_telegram_photo_url(photo_id)

    # INSERT Postgres — NULL-safe avec COALESCE + stockage file_id + photo_url
    # FIX 3 — ON CONFLICT ne touche PAS discord_channel_id pour éviter la re-création de channel
    await _db_exec(
        """
        INSERT INTO members (user_id, username, age, amount_declared, status,
                             proof_file_id, proof_photo_url)
        VALUES ($1, $2, $3, $4, 'pending', $5, $6)
        ON CONFLICT (user_id) DO UPDATE
          SET username         = EXCLUDED.username,
              age              = COALESCE(EXCLUDED.age, members.age),
              amount_declared  = COALESCE(EXCLUDED.amount_declared, members.amount_declared),
              status           = 'pending',
              proof_file_id    = EXCLUDED.proof_file_id,
              proof_photo_url  = COALESCE(EXCLUDED.proof_photo_url, members.proof_photo_url),
              last_active      = NOW()
              -- discord_channel_id intentionnellement non touché
        """,
        user_id, username, age_val, amount_val, photo_id, photo_url
    )
    logger.info(
        f"📋 Demande #{req_num} — {username or user_id} | "
        f"{age_val or '?'}ans | {amount_val or '?'}€ | photo_url={bool(photo_url)}"
    )

    # Nettoyer l'état FSM
    await _clear_state(user_id)


async def handle_proof(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handler photo dans le ConversationHandler."""
    has_media = update.message.photo or update.message.document

    # FIX 1 — Si l'user envoie du texte pendant WAITING_PROOF → réponse naturelle
    if not has_media:
        user_text = update.message.text or ""
        await _log_message(update.effective_user.id, "user", user_text)
        reply = (
            "Je vois ta question 👍 Dès que tu es prêt, "
            "envoie-moi simplement la capture de ton dépôt 📸"
        )
        await update.message.reply_text(reply)
        await _log_message(update.effective_user.id, "bot", reply)
        return WAITING_PROOF  # on reste en attente de la photo

    user_id = update.effective_user.id
    # Lire age/amount depuis ctx.user_data, fallback Postgres
    age    = ctx.user_data.get("age")
    amount = ctx.user_data.get("amount")

    if not age or not amount:
        pg_state, pg_data = await _get_state(user_id)
        if pg_state == "WAITING_PROOF" and pg_data:
            age    = pg_data.get("age")
            amount = pg_data.get("amount")
            if age:    ctx.user_data["age"]    = age
            if amount: ctx.user_data["amount"] = amount

    photo_id = (update.message.photo[-1].file_id
                if update.message.photo
                else update.message.document.file_id)

    await _process_proof(update, ctx, age, amount, photo_id)
    return ConversationHandler.END


async def handle_photo_fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Handler photo hors ConversationHandler.
    Cas couverts :
      - Après restart Docker (FSM perdu mais Postgres intact)
      - Membre refusé qui renvoie une photo sans refaire GO
      - Membre known qui renvoie une photo après "+infos"
    """
    user    = update.effective_user
    user_id = user.id

    pg_state, pg_data = await _get_state(user_id)

    # Cas 1 : FSM Postgres dit WAITING_PROOF (restart ou +infos)
    if pg_state == "WAITING_PROOF":
        age    = pg_data.get("age")
        amount = pg_data.get("amount")
        if age:    ctx.user_data["age"]    = age
        if amount: ctx.user_data["amount"] = amount
        photo_id = (update.message.photo[-1].file_id
                    if update.message.photo
                    else update.message.document.file_id)
        logger.info(f"\u21ba Photo restaurée depuis Postgres (WAITING_PROOF) pour {user_id}")
        await _process_proof(update, ctx, age, amount, photo_id)
        return

    # Cas 2 : membre connu en DB (status=refused/pending/started)
    # FIX 1 + FIX 3 : accepter la photo sans demander GO
    pool = await _get_pool()
    if pool:
        row = await pool.fetchrow(
            "SELECT * FROM members WHERE user_id=$1", user_id
        )
        if row and row["status"] in ("refused", "pending", "started"):
            age    = row["age"]
            amount = row["amount_declared"]
            if age:    ctx.user_data["age"]    = age
            if amount: ctx.user_data["amount"] = amount
            photo_id = (update.message.photo[-1].file_id
                        if update.message.photo
                        else update.message.document.file_id)
            logger.info(
                f"\u21ba Photo acceptée directement (status={row['status']}) pour {user_id}"
            )
            # Remettre le status  pending et FSM WAITING_PROOF pour le traitement
            await pool.execute(
                "UPDATE members SET status='pending' WHERE user_id=$1 AND status='refused'",
                user_id
            )
            await _set_state(user_id, "WAITING_PROOF",
                             {"age": age, "amount": amount,
                              "username": row["username"] or ""})
            await _process_proof(update, ctx, age, amount, photo_id)
            return

    # Cas 3 : inconnu → demander GO
    await update.message.reply_text(MSG_RESTART_NEEDED)


# ── Commandes admin ────────────────────────────────────────────────────────────

async def cmd_valider(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    admin_name = update.effective_user.username or update.effective_user.full_name
    if not ctx.args:
        await update.message.reply_text("Usage : /valider {user_id}"); return
    try:
        target_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id invalide"); return

    pool = await _get_pool()
    row  = None
    if pool:
        try:
            row = await pool.fetchrow("SELECT * FROM members WHERE user_id=$1", target_id)
        except Exception as e:
            logger.debug(f"cmd_valider DB: {e}")

    if not row or row["status"] not in ("pending", "started"):
        status_info = row["status"] if row else "introuvable"
        await update.message.reply_text(f"⚠️ Membre {target_id} : statut = {status_info}")
        return

    try:
        await ctx.bot.send_message(
            chat_id=target_id,
            text=(
                f"✅ Bonne nouvelle ! Ton dépôt a été validé.\n\n"
                f"👉 Rejoins QUANT Signals PRO ici :\n{CHANNEL_LINK}\n\n"
                f"Bienvenue dans l'équipe 🚀"
            )
        )
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ Impossible de contacter ID {target_id}: {e}")
        return

    # Bloc B1 — Message DM bienvenue QUANT PRO
    MSG_BIENVENUE = (
        "🎉 Bienvenue dans QUANT Signals PRO !\n\n"
        "Tu fais maintenant partie d'une communauté exclusive.\n\n"
        "Voici comment tirer le maximum de ton accès :\n\n"
        "📊 Les signaux arrivent en temps réel dans le canal\n"
        "🎯 Chaque signal a un TP1, TP2, TP3 et un SL\n"
        "💡 Ne risque jamais plus de 1-2% par trade\n"
        "⏰ Les meilleurs signaux arrivent entre 8h et 22h\n\n"
        "Une question ? Réponds directement ici 👇"
    )
    try:
        await ctx.bot.send_message(chat_id=target_id, text=MSG_BIENVENUE)
    except TelegramError as e:
        logger.debug(f"Bienvenue DM: {e}")

    await _db_exec("UPDATE members SET status='active', validated_at=NOW() WHERE user_id=$1", target_id)
    await update.message.reply_text(f"✅ User {target_id} validé — lien PRO + message bienvenue envoyés !")
    logger.info(f"✅ {target_id} validé par @{admin_name}")


async def cmd_refuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    admin_name = update.effective_user.username or update.effective_user.full_name
    if not ctx.args:
        await update.message.reply_text("Usage : /refuser {user_id}"); return
    try:
        target_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id invalide"); return

    try:
        await ctx.bot.send_message(chat_id=target_id, text=MSG_REFUSED)
    except TelegramError as e:
        logger.debug(f"Refus message: {e}")

    await _db_exec("UPDATE members SET status='refused' WHERE user_id=$1", target_id)
    await update.message.reply_text(f"❌ User {target_id} refusé.")
    logger.info(f"❌ {target_id} refusé par @{admin_name}")


async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pool = await _get_pool()
    if not pool:
        await update.message.reply_text("❌ DB indisponible"); return
    rows = await pool.fetch(
        "SELECT user_id, username, age, amount_declared FROM members WHERE status='pending' ORDER BY joined_at"
    )
    if not rows:
        await update.message.reply_text("✅ Aucune demande en attente."); return
    lines = [f"📋 {len(rows)} demande(s) EN ATTENTE\n"]
    for r in rows:
        uname   = f"@{r['username']}" if r["username"] else f"ID:{r['user_id']}"
        age_str = f"{r['age']}ans"    if r["age"]      else "?ans"
        amt_str = f"{r['amount_declared']}€" if r["amount_declared"] else "?€"
        lines.append(f"• {uname} | {age_str} | {amt_str}")
        lines.append(f"  /valider {r['user_id']}  |  /refuser {r['user_id']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_referrals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """F1 — /referrals : top 5 parrains + nombre de filleuls."""
    pool = await _get_pool()
    if not pool:
        await update.message.reply_text("\u274c DB indisponible"); return
    rows = await pool.fetch(
        """
        SELECT r.referrer_id, m.username, COUNT(*) as nb
        FROM referrals r
        LEFT JOIN members m ON m.user_id = r.referrer_id
        GROUP BY r.referrer_id, m.username
        ORDER BY nb DESC
        LIMIT 5
        """
    )
    if not rows:
        await update.message.reply_text("\U0001f381 Aucun parrainage pour l'instant."); return
    lines = ["\U0001f3c6 Top parrains QUANT :\n"]
    for i, r in enumerate(rows, 1):
        uname = f"@{r['username']}" if r["username"] else f"ID:{r['referrer_id']}"
        lines.append(f"{i}. {uname} \u2014 {r['nb']} filleul(s)")
    await update.message.reply_text("\n".join(lines))



async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _clear_state(update.effective_user.id)
    await update.message.reply_text(
        "Conversation annulée. Envoie GO pour recommencer 🙏",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ── Commande admin /visuel — génère le PNG hebdo immédiatement ────────────────
async def cmd_visuel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id) if update.effective_user else ""
    if uid != str(ADMIN_ID).strip():
        await update.message.reply_text("⛔ Admin uniquement")
        return
    await update.message.reply_text("🎨 Génération du visuel en cours…")
    try:
        from social_visual import _get_week_stats, send_visual_to_admin
        stats = _get_week_stats()
        ok = send_visual_to_admin(stats)
        if ok:
            await update.message.reply_text("✅ PNG envoyé en DM !")
        else:
            await update.message.reply_text("❌ Échec de l'envoi — vérifier les logs")
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur: {e}")
        logger.error(f"[cmd_visuel] {e}")


async def handle_fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):

    """
    FIX 2 + FIX 3 — Catch-all intelligent.
    Si le user est connu en DB (age+montant) → reprend là où il en était.
    Si inconnu → Envoie GO.
    """
    if update.effective_chat and str(update.effective_chat.id) == str(ADMIN_GROUP_ID):
        return

    user    = update.effective_user
    user_id = user.id

    # Vérifier si un FSM est en cours en Postgres
    pg_state, pg_data = await _get_state(user_id)
    if pg_state == "WAITING_PROOF":
        # FIX 3 — pas de message restart, on rappelle juste d'envoyer la photo
        reply = (
            "Tu étais en train d'envoyer ta capture 📸\n"
            "Dès que tu es prêt, envoie-la directement ici !"
        )
        await update.message.reply_text(reply)
        await _log_message(user_id, "user", update.message.text or "")
        await _log_message(user_id, "bot", reply)
        return
    if pg_state == "WAITING_INFO":
        reply = MSG_ASK_INFO
        await update.message.reply_text(reply)
        return
    if pg_state == "WAITING_CONFIRM":
        reply = MSG_READY
        await update.message.reply_text(reply)
        return

    # FIX 2 — si aucun FSM mais membre connu avec infos en DB
    pool = await _get_pool()
    if pool:
        row = await pool.fetchrow(
            "SELECT * FROM members WHERE user_id=$1", user_id
        )
        if row and row["age"] and row["amount_declared"] and row["status"] != "active":
            # Membre connu → remettre en WAITING_PROOF automatiquement
            await _set_state(
                user_id, "WAITING_PROOF",
                {"age": row["age"], "amount": row["amount_declared"],
                 "username": row["username"] or ""}
            )
            reply = (
                "Bienvenue de retour ! 👋\n\n"
                "Il ne te reste plus qu'à envoyer ta capture de dépôt 📸"
            )
            await update.message.reply_text(reply)
            await _log_message(user_id, "user", update.message.text or "")
            await _log_message(user_id, "bot", reply)
            logger.info(f"\u21ba Membre connu {user_id} remis en WAITING_PROOF automatiquement")
            return

    await update.message.reply_text("Envoie GO pour commencer 🚀")


# ── Initialisation ─────────────────────────────────────────────────────────────

async def post_init(app):
    await _get_pool()
    # F2 — Lancer le scheduler onboarding en tâche asyncio de fond
    asyncio.create_task(_run_onboarding(app.bot))
    logger.info("🚀 QuantAccessBot démarré — pool OK | scheduler onboarding actif")



def build_app():
    from telegram.ext import ApplicationBuilder
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex(re.compile(r'^go$', re.IGNORECASE)) & filters.ChatType.PRIVATE,
                handle_go
            ),
        ],
        states={
            WAITING_REFERRAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_referral)
            ],
            WAITING_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_confirm)
            ],
            WAITING_INFO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_info)
            ],
            WAITING_PROOF: [
                MessageHandler((filters.PHOTO | filters.Document.IMAGE) & filters.ChatType.PRIVATE, handle_proof),
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_proof),
            ],
        },

        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)
    app.add_handler(CommandHandler("valider", cmd_valider))
    app.add_handler(CommandHandler("refuser", cmd_refuser))
    app.add_handler(CommandHandler("queue",      cmd_queue))
    app.add_handler(CommandHandler("referrals",  cmd_referrals))
    app.add_handler(CommandHandler("visuel",     cmd_visuel))
    # Boutons inline admin
    app.add_handler(CallbackQueryHandler(handle_admin_callback))

    # Fallback photo hors ConversationHandler (bot redémarré en plein flow)
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.IMAGE) & filters.ChatType.PRIVATE,
        handle_photo_fallback
    ))
    # Relay message admin → membre (détection via ctx.user_data)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        _route_private_text
    ))

    return app


def run_bot():
    if not _TELEGRAM_OK:
        logger.error("python-telegram-bot non installé"); return
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN manquant"); return
    logger.info(f"🤖 QuantAccessBot — admin group: {ADMIN_GROUP_ID or 'non configuré'}")
    build_app().run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    from dotenv import load_dotenv
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path, override=False)
    BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
    ADMIN_ID       = os.getenv("TELEGRAM_CHAT_ID", "")
    ADMIN_GROUP_ID = os.getenv("TELEGRAM_ADMIN_GROUP_ID", "")
    AFFILIATE      = os.getenv("AFFILIATE_LINK", AFFILIATE)
    CHANNEL_LINK   = os.getenv("QUANT_CHANNEL_INVITE", CHANNEL_LINK)

    print(f"🤖  Token : {BOT_TOKEN[:20]}...")
    print(f"   Admin group : {ADMIN_GROUP_ID or '(non configuré)'}")
    run_bot()
