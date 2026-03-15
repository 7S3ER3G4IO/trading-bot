"""
discord_admin_bot.py — QUANT Admin Bot Discord
================================================
Channels dynamiques par membre avec boutons d'action :
  - Catégorie "📋 EN ATTENTE" → channel #username-Xans créé automatiquement
  - Embed 1 : fiche membre + 4 boutons (Valider/Refuser/Message/+Info)
  - Embed 2 : photo de dépôt directement visible
  - Message 2 : historique complet de la conversation Telegram
  - /valider → déplace dans "✅ VALIDÉS" + DM Telegram lien PRO
  - /refuser → supprime le channel après 1h + DM Telegram refus
  - /kick    → révoque l'accès + log #members-crm
  - /stats   → dashboard complet depuis Postgres

Architecture : asyncio while-True loops (setup_hook discord.py v2)
"""

import os, asyncio, aiohttp, json
from datetime import datetime, time as dtime
from loguru import logger

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
import asyncpg

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
GUILD_ID      = int(os.getenv("DISCORD_GUILD_ID", "0"))
CH_SIGNALS    = int(os.getenv("DISCORD_SIGNALS_CH", "0"))
CH_ALERTS     = int(os.getenv("DISCORD_ALERTS_CH", "0"))
CH_STATS      = int(os.getenv("DISCORD_STATS_CH", "0"))
CH_CRM        = int(os.getenv("DISCORD_CRM_CH", "0"))
DATABASE_URL  = os.getenv("DATABASE_URL", "")
TG_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHANNEL_LINK  = os.getenv("QUANT_CHANNEL_INVITE", "https://t.me/+3fh4-ilGuCk2ZWY0")

CAT_PENDING_NAME   = "📋 EN ATTENTE"
CAT_VALIDATED_NAME = "✅ VALIDÉS"

_cat_pending_id:   int | None = int(os.getenv("DISCORD_CATEGORY_PENDING",   "0")) or None
_cat_validated_id: int | None = int(os.getenv("DISCORD_CATEGORY_VALIDATED", "0")) or None
_posted_signals: dict = {}
_posted_alerts:  set  = set()

DISCORD_API = "https://discord.com/api/v10"


# ── Discord REST helpers ───────────────────────────────────────────────────────

def _discord_headers():
    token = os.getenv("DISCORD_BOT_TOKEN", DISCORD_TOKEN)
    return {"Authorization": f"Bot {token}", "Content-Type": "application/json"}


async def _get_guild_channels() -> list:
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.get(f"{DISCORD_API}/guilds/{GUILD_ID}/channels",
                            headers=_discord_headers())
            data = await r.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"get_guild_channels: {e}")
        return []


async def _create_category(name: str) -> int | None:
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.post(f"{DISCORD_API}/guilds/{GUILD_ID}/channels",
                             headers=_discord_headers(),
                             json={"name": name, "type": 4})
            data = await r.json()
            cat_id = int(data.get("id", 0))
            logger.info(f"Catégorie créée : {name} ({cat_id})")
            return cat_id or None
    except Exception as e:
        logger.warning(f"create_category {name}: {e}")
        return None


async def _get_or_create_category_rest(name: str) -> int | None:
    channels = await _get_guild_channels()
    for c in channels:
        if c.get("type") == 4 and c.get("name") == name:
            return int(c["id"])
    return await _create_category(name)


async def _create_text_channel(name: str, category_id: int) -> int | None:
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.post(f"{DISCORD_API}/guilds/{GUILD_ID}/channels",
                             headers=_discord_headers(),
                             json={"name": name, "type": 0, "parent_id": str(category_id)})
            data = await r.json()
            ch_id = int(data.get("id", 0))
            return ch_id or None
    except Exception as e:
        logger.warning(f"create_text_channel {name}: {e}")
        return None


async def _send_to_channel(channel_id: int, embed: dict = None,
                            embeds: list = None, content: str = None) -> dict | None:
    """Envoie un message REST. Retourne la réponse JSON (contient l'id du message)."""
    try:
        payload = {}
        if content: payload["content"] = content
        if embeds:  payload["embeds"]  = embeds
        elif embed: payload["embeds"]  = [embed]
        async with aiohttp.ClientSession() as s:
            r = await s.post(f"{DISCORD_API}/channels/{channel_id}/messages",
                             headers=_discord_headers(), json=payload)
            return await r.json()
    except Exception as e:
        logger.warning(f"send_to_channel {channel_id}: {e}")
        return None


async def _update_message_components(channel_id: int, message_id: int, components: list):
    """Met à jour les components (boutons) d'un message existant."""
    try:
        async with aiohttp.ClientSession() as s:
            await s.patch(
                f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}",
                headers=_discord_headers(),
                json={"components": components}
            )
    except Exception as e:
        logger.warning(f"update_message_components: {e}")


async def _move_channel(channel_id: int, new_name: str, new_category_id: int):
    try:
        async with aiohttp.ClientSession() as s:
            await s.patch(f"{DISCORD_API}/channels/{channel_id}",
                          headers=_discord_headers(),
                          json={"name": new_name, "parent_id": str(new_category_id)})
    except Exception as e:
        logger.warning(f"move_channel {channel_id}: {e}")


async def _delete_channel_rest(channel_id: int):
    try:
        async with aiohttp.ClientSession() as s:
            await s.delete(f"{DISCORD_API}/channels/{channel_id}",
                           headers=_discord_headers())
        logger.info(f"🗑️ Channel {channel_id} supprimé")
    except Exception as e:
        logger.warning(f"delete_channel {channel_id}: {e}")


# ── Telegram helper ────────────────────────────────────────────────────────────

async def tg_send(user_id: int, text: str):
    try:
        tg_token = os.getenv("TELEGRAM_BOT_TOKEN", TG_TOKEN)
        tg_api   = f"https://api.telegram.org/bot{tg_token}"
        async with aiohttp.ClientSession() as s:
            await s.post(f"{tg_api}/sendMessage",
                         json={"chat_id": user_id, "text": text},
                         timeout=aiohttp.ClientTimeout(total=5))
    except Exception as e:
        logger.warning(f"tg_send {user_id}: {e}")


# ── Postgres pool ──────────────────────────────────────────────────────────────

_pool = None


async def get_pool():
    global _pool
    if _pool is None:
        db_url = os.getenv("DATABASE_URL", DATABASE_URL)
        _pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)
    return _pool


async def _parse_pg_jsonb(raw) -> list | dict:
    if raw is None:
        return []
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return []


# ── Discord UI — Boutons d'action membre ───────────────────────────────────────

class MessageModal(ui.Modal, title="💬 Envoyer un message Telegram"):
    """Modal pour envoyer un message personnalisé à l'utilisateur via Telegram."""
    message = ui.TextInput(
        label="Message à envoyer",
        style=discord.TextStyle.paragraph,
        placeholder="Ex: Votre dépôt semble insuffisant, pouvez-vous refaire un dépôt ?",
        max_length=1000
    )

    def __init__(self, user_id: int, username: str):
        super().__init__()
        self.user_id  = user_id
        self.username = username

    async def on_submit(self, interaction: discord.Interaction):
        text = self.message.value
        await tg_send(self.user_id, text)
        uname = f"@{self.username}" if self.username else f"ID:{self.user_id}"
        await interaction.response.send_message(
            f"💬 Message envoyé à {uname} :\n> {text[:200]}",
            ephemeral=True
        )
        logger.info(f"💬 Message Telegram envoyé à {self.user_id} par {interaction.user}")


class MemberActions(ui.View):
    """Boutons d'action sur la fiche membre Discord."""

    def __init__(self, user_id: int, username: str, channel_id: int):
        super().__init__(timeout=None)  # persistant entre redémarrages
        self.user_id    = user_id
        self.username   = username
        self.channel_id = channel_id

    async def _get_member_row(self):
        pool = await get_pool()
        return await pool.fetchrow("SELECT * FROM members WHERE user_id=$1", self.user_id)

    def _disable_action_buttons(self):
        """Désactive les boutons valider/refuser après action."""
        for item in self.children:
            if hasattr(item, "custom_id") and item.custom_id in ("valider", "refuser"):
                item.disabled = True

    @ui.button(label="✅ Valider", style=discord.ButtonStyle.success,
               custom_id="valider", emoji="✅")
    async def btn_valider(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        row = await self._get_member_row()
        if not row:
            await interaction.followup.send("❌ Membre introuvable en base.", ephemeral=True)
            return
        # Idempotence — déjà validé ?
        if row["status"] == "active":
            await interaction.followup.send("ℹ️ Membre déjà validé — invitation non renvoyée.", ephemeral=True)
            return
        # Vérification lien d'invitation
        if not CHANNEL_LINK:
            await interaction.followup.send(
                "⚠️ QUANT_CHANNEL_INVITE manquant dans .env — validation impossible.", ephemeral=True
            )
            return

        # Envoyer lien PRO via Telegram + message bienvenue
        await tg_send(self.user_id, (
            f"\u2705 Bonne nouvelle ! Ton dépôt a été validé.\n\n"
            f"\U0001f449 Rejoins QUANT Signals PRO ici :\n{CHANNEL_LINK}\n\n"
            f"Bienvenue dans l'équipe \U0001f680"
        ))

        # Mettre à jour Postgres
        pool = await get_pool()
        now  = datetime.utcnow()
        await pool.execute(
            "UPDATE members SET status='active', validated_at=NOW() WHERE user_id=$1",
            self.user_id
        )

        # Bloc A2 — Log action dans Postgres action_log
        action_entry = json.dumps({"action": "validated", "by": str(interaction.user),
                                   "at": now.isoformat()})
        try:
            await pool.execute(
                "UPDATE members SET action_log = COALESCE(action_log,'[]'::jsonb) || $1::jsonb WHERE user_id=$2",
                f"[{action_entry}]", self.user_id
            )
        except Exception:
            pass

        # Déplacer le channel dans VALIDÉS
        global _cat_validated_id
        if _cat_validated_id and self.channel_id:
            safe = "".join(c if c.isalnum() or c == "-" else "-"
                           for c in (self.username or "user").lower())
            new_name = f"valide-{safe}"
            await _move_channel(self.channel_id, new_name, _cat_validated_id)

        # Bloc A2 — Log action dans le channel membre
        dc = bot.get_channel(self.channel_id)
        if dc:
            await dc.send(
                f"\U0001f4dd Action : \u2705 Validé par **@{interaction.user.display_name}** "
                f"le {now.strftime('%d/%m/%Y')} à {now.strftime('%H:%M')} UTC"
            )

        # Log CRM
        crm = bot.get_channel(CH_CRM)
        if crm:
            await crm.send(
                f"\u2705 **VALIDÉ** — @{self.username or self.user_id} "
                f"(`{self.user_id}`) par {interaction.user.mention}"
            )

        button.disabled = True
        button.label    = f"\u2705 Validé par {interaction.user.display_name}"
        # Trouver le bouton refuser et le désactiver aussi
        for item in self.children:
            if getattr(item, "custom_id", "") == "refuser":
                item.disabled = True
        await interaction.message.edit(view=self)
        await interaction.followup.send("\u2705 Membre validé — lien PRO envoyé sur Telegram !", ephemeral=True)
        logger.info(f"\u2705 {self.user_id} validé via bouton par {interaction.user}")

    @ui.button(label="❌ Refuser", style=discord.ButtonStyle.danger,
               custom_id="refuser", emoji="❌")
    async def btn_refuser(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        row = await self._get_member_row()
        if not row:
            await interaction.followup.send("❌ Membre introuvable en base.", ephemeral=True)
            return
        if row["status"] in ("refused", "kicked"):
            await interaction.followup.send("⚠️ Membre déjà refusé/kicked.", ephemeral=True)
            return

        await tg_send(self.user_id,
                      "Désolé, nous n'avons pas pu valider votre dépôt.\n"
                      "Contactez-nous pour plus d'infos.")

        pool = await get_pool()
        now  = datetime.utcnow()
        await pool.execute(
            "UPDATE members SET status='refused' WHERE user_id=$1", self.user_id
        )

        # Bloc A2 — Log action dans Postgres
        action_entry = json.dumps({"action": "refused", "by": str(interaction.user),
                                   "at": now.isoformat()})
        try:
            await pool.execute(
                "UPDATE members SET action_log = COALESCE(action_log,'[]'::jsonb) || $1::jsonb WHERE user_id=$2",
                f"[{action_entry}]", self.user_id
            )
        except Exception:
            pass

        # Supprimer le channel après 1h
        if self.channel_id:
            asyncio.create_task(_delete_channel_delayed(self.channel_id, delay_s=3600))

        # Bloc A2 — Log action dans le channel membre
        dc = bot.get_channel(self.channel_id)
        if dc:
            await dc.send(
                f"\U0001f4dd Action : \u274c Refusé par **@{interaction.user.display_name}** "
                f"le {now.strftime('%d/%m/%Y')} à {now.strftime('%H:%M')} UTC"
            )

        crm = bot.get_channel(CH_CRM)
        if crm:
            await crm.send(
                f"\u274c **REFUSÉ** — @{self.username or self.user_id} "
                f"(`{self.user_id}`) par {interaction.user.mention}"
            )

        button.disabled = True
        button.label    = f"\u274c Refusé par {interaction.user.display_name}"
        for item in self.children:
            if getattr(item, "custom_id", "") == "valider":
                item.disabled = True
        await interaction.message.edit(view=self)
        await interaction.followup.send("\u274c Membre refusé — channel supprimé dans 1h.", ephemeral=True)
        logger.info(f"\u274c {self.user_id} refusé via bouton par {interaction.user}")

    @ui.button(label="💬 Message Telegram", style=discord.ButtonStyle.primary,
               custom_id="message_tg", emoji="💬")
    async def btn_message(self, interaction: discord.Interaction, button: ui.Button):
        modal = MessageModal(self.user_id, self.username)
        await interaction.response.send_modal(modal)

    @ui.button(label="⏳ + d'infos", style=discord.ButtonStyle.secondary,
               custom_id="plus_infos", emoji="⏳")
    async def btn_infos(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)

        # Envoyer le message via Telegram
        await tg_send(
            self.user_id,
            "Merci pour votre dépôt ! 📸\n\n"
            "Pourriez-vous envoyer une capture plus nette "
            "montrant bien le montant et votre nom ?"
        )

        # FIX 4 — Remettre WAITING_PROOF en Postgres (partagé avec quant_access_bot)
        # → la prochaine photo du user sera traitée dans ce channel existant sans créer un nouveau
        try:
            pool = await get_pool()
            row  = await pool.fetchrow("SELECT * FROM members WHERE user_id=$1", self.user_id)
            if row:
                data_dict = {
                    "age":      row["age"]             or 0,
                    "amount":   row["amount_declared"] or 0,
                    "username": row["username"]        or "",
                }
                await pool.execute(
                    """
                    INSERT INTO conversation_states (user_id, state, data, updated_at)
                    VALUES ($1, 'WAITING_PROOF', $2::jsonb, NOW())
                    ON CONFLICT (user_id) DO UPDATE
                      SET state='WAITING_PROOF', data=EXCLUDED.data, updated_at=NOW()
                    """,
                    self.user_id, json.dumps(data_dict)
                )
                logger.info(f"⏳ WAITING_PROOF Postgres mis pour {self.user_id} via +infos")
        except Exception as ex:
            logger.warning(f"btn_infos WAITING_PROOF: {ex}")

        uname = f"@{self.username}" if self.username else f"ID:{self.user_id}"
        await interaction.followup.send(
            f"⏳ Demande d'infos supplémentaires envoyée à {uname} via Telegram.\n"
            f"La prochaine photo ira dans ce channel (WAITING_PROOF actif).",
            ephemeral=True
        )
        logger.info(f"⏳ +infos envoyé à {self.user_id} par {interaction.user}")


# ── Bot subclass avec setup_hook ───────────────────────────────────────────────

class QuantBot(commands.Bot):
    def __init__(self):
        intents                 = discord.Intents.default()
        intents.guilds          = True
        # NOTE: message_content privileged intent requires enabling in Discord Developer Portal
        # (https://discord.com/developers/applications/) before enabling here.
        # Leaving disabled to avoid PrivilegedIntentsRequired crash.
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        """setup_hook — appelé avant on_ready, bon moment pour démarrer les tasks."""
        print("\U0001f527 setup_hook: démarrage des pollers", flush=True)
        self.loop.create_task(_loop_pending(),  name="loop_pending")
        self.loop.create_task(_loop_signals(),  name="loop_signals")
        self.loop.create_task(_loop_alerts(),   name="loop_alerts")
        self.loop.create_task(_loop_inactive(), name="loop_inactive")  # Bloc B2
        if not daily_stats_task.is_running():
            daily_stats_task.start()
        print("\u2705 setup_hook: tasks créées", flush=True)


bot  = QuantBot()
tree = bot.tree


# ── Helpers canaux ─────────────────────────────────────────────────────────────

def _channel_name(username: str, age: int) -> str:
    safe = (username or "user").lower()
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in safe)
    return f"{safe}-{age}ans"


def _get_row_val(row, key, default=None):
    """Lit une valeur d'un asyncpg Record de façon safe."""
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return default


async def _build_member_embeds(row, uid: int, uname: str) -> list:
    """Construit la liste d'embeds discord.Embed pour la fiche membre."""
    joined = row["joined_at"].strftime("%d/%m/%Y %H:%M") if row["joined_at"] else "?"

    embed_info = discord.Embed(
        title="🆕 NOUVELLE DEMANDE",
        color=discord.Color.orange(),
        timestamp=datetime.utcnow()
    )
    embed_info.add_field(name="👤 Utilisateur",  value=f"@{uname} (`{uid}`)",              inline=False)
    embed_info.add_field(name="🎂 Âge",          value=f"{row['age'] or '?'} ans",          inline=True)
    embed_info.add_field(name="💰 Montant",       value=f"{row['amount_declared'] or '?'}€", inline=True)
    embed_info.add_field(name="📅 Reçu à",        value=joined,                              inline=True)
    embed_info.add_field(name="📋 Status",        value="EN ATTENTE ⏳",                     inline=False)

    embeds = [embed_info]

    photo_url = _get_row_val(row, "proof_photo_url")
    if photo_url:
        # FIX 2 — Photo inline dans l'embed (visible directement, pas en pièce jointe)
        embed_photo = discord.Embed(
            title="📸 Preuve de dépôt",
            color=discord.Color.blue(),
            description="Capture d'écran envoyée par l'utilisateur"
        )
        embed_photo.set_image(url=photo_url)
        embeds.append(embed_photo)

    return embeds, photo_url


async def _post_to_discord_channel(ch_id: int, uid: int, uname: str, row) -> bool:
    """Poste la fiche + boutons + historique dans un channel Discord."""
    embeds, photo_url = await _build_member_embeds(row, uid, uname)
    view        = MemberActions(uid, uname, ch_id)
    discord_ch  = bot.get_channel(ch_id)

    if discord_ch:
        await discord_ch.send(embeds=embeds, view=view)
        # Message 2 — historique conversation
        await _post_conversation_log(discord_ch, None, uid)
    else:
        # Fallback REST — boutons non disponibles
        payload_embeds = [e.to_dict() for e in embeds]
        await _send_to_channel(ch_id, embeds=payload_embeds)
        logger.warning(f"View/historique non envoyés (channel {ch_id} hors cache)")
    return bool(photo_url)


async def _post_conversation_log(discord_ch, ch_id_fallback, uid: int):
    """Poste l'historique de conversation Telegram dans le channel Discord."""
    try:
        pool    = await get_pool()
        raw_log = await pool.fetchval(
            "SELECT conversation_log FROM members WHERE user_id=$1", uid
        )
        entries = await _parse_pg_jsonb(raw_log)
        if not (isinstance(entries, list) and entries):
            return
        lines = ["💬 **CONVERSATION COMPLÈTE**\n"]
        for e in entries:
            if isinstance(e, dict):
                role = "👤 User" if e.get("role") == "user" else "🤖 Bot "
                lines.append(f"{role} : {(e.get('text') or '')[:200]}")
        text = "\n".join(lines)[:2000]
        if discord_ch:
            await discord_ch.send(text)
        elif ch_id_fallback:
            await _send_to_channel(ch_id_fallback, content=text)
    except Exception as ex:
        logger.debug(f"conversation_log post: {ex}")


async def _update_member_fiche(ch_id: int, row) -> None:
    """
    FIX 3 — Met à jour la fiche dans un channel Discord existant
    sans créer de nouveau channel. Poste simplement les embeds mis à jour.
    """
    uid   = row["user_id"]
    uname = row["username"] or f"ID:{uid}"
    logger.info(f"🔄 Mise à jour fiche dans channel existant {ch_id} pour {uid}")

    # FIX 2 — Si proof_photo_url encore vide, attendre 5s et réessayer
    photo_url = _get_row_val(row, "proof_photo_url")
    if not photo_url:
        logger.info(f"proof_photo_url manquant pour {uid}, attente 5s...")
        await asyncio.sleep(5)
        pool = await get_pool()
        fresh = await pool.fetchrow(
            "SELECT proof_photo_url FROM members WHERE user_id=$1", uid
        )
        if fresh:
            photo_url = _get_row_val(fresh, "proof_photo_url")
            # Créer un row avec l'URL à jour pour les embeds
            if photo_url:
                row = {**dict(row), "proof_photo_url": photo_url}

    await _post_to_discord_channel(ch_id, uid, uname, row)


async def _create_member_channel_rest(row) -> int | None:
    """Crée un channel Discord pour un membre pending. Embed info + photo + historique."""
    global _cat_pending_id
    if not _cat_pending_id:
        logger.warning("cat_pending_id non initialisé")
        return None

    uid    = row["user_id"]
    name   = _channel_name(row["username"] or "user", row["age"] or 0)
    uname  = row["username"] or f"ID:{uid}"

    # ── FIX 3 — Si discord_channel_id existe déjà → juste update la fiche ───
    existing_ch_id = _get_row_val(row, "discord_channel_id")
    if existing_ch_id:
        logger.info(f"Channel existant pour {uid} (ch:{existing_ch_id}) — mise à jour")
        await _update_member_fiche(existing_ch_id, row)
        return existing_ch_id

    # Éviter les doublons par nom de channel
    channels = await _get_guild_channels()
    for c in channels:
        if c.get("name") == name:
            ch_id_existing = int(c["id"])
            logger.info(f"Channel #{name} déjà existant ({ch_id_existing}) — mise à jour fiche")
            await _update_member_fiche(ch_id_existing, row)
            return ch_id_existing

    # ── Créer le channel ───────────────────────────────────────────────────────
    ch_id = await _create_text_channel(name, _cat_pending_id)
    if not ch_id:
        return None

    # FIX 2 — retry 5s si proof_photo_url pas encore disponible
    photo_url = _get_row_val(row, "proof_photo_url")
    if not photo_url:
        logger.info(f"proof_photo_url manquant pour {uid}, attente 5s...")
        await asyncio.sleep(5)
        pool = await get_pool()
        fresh = await pool.fetchrow(
            "SELECT proof_photo_url FROM members WHERE user_id=$1", uid
        )
        if fresh:
            new_url = _get_row_val(fresh, "proof_photo_url")
            if new_url:
                row = {**dict(row), "proof_photo_url": new_url}

    has_photo = await _post_to_discord_channel(ch_id, uid, uname, row)

    logger.info(f"📋 Channel créé : #{name} (id:{ch_id}) pour @{uname} | photo={'✅' if has_photo else '❌'}")
    return ch_id




async def _delete_channel_delayed(channel_id: int, delay_s: int):
    await asyncio.sleep(delay_s)
    await _delete_channel_rest(channel_id)


# ── Pollers while-True ─────────────────────────────────────────────────────────

async def _loop_pending():
    global _cat_pending_id, _cat_validated_id
    print("📋 _loop_pending démarré", flush=True)
    while True:
        try:
            print("🔄 poll_pending tick", flush=True)
            pool = await get_pool()

            if not _cat_pending_id:
                _cat_pending_id   = await _get_or_create_category_rest(CAT_PENDING_NAME)
                _cat_validated_id = await _get_or_create_category_rest(CAT_VALIDATED_NAME)
                print(f"📋 cat_pending={_cat_pending_id} | cat_validated={_cat_validated_id}", flush=True)
                logger.info(f"📋 cat_pending={_cat_pending_id} | cat_validated={_cat_validated_id}")

            rows = await pool.fetch(
                "SELECT * FROM members WHERE status='pending' AND discord_channel_id IS NULL ORDER BY joined_at"
            )
            if rows:
                print(f"📊 {len(rows)} membre(s) pending sans channel", flush=True)
                logger.info(f"📊 {len(rows)} membre(s) pending sans channel")

            for r in rows:
                ch_id = await _create_member_channel_rest(r)
                if ch_id:
                    await pool.execute(
                        "UPDATE members SET discord_channel_id=$1 WHERE user_id=$2",
                        ch_id, r["user_id"]
                    )
        except asyncio.CancelledError:
            print("⚠️ _loop_pending cancelled!", flush=True)
            raise
        except Exception as ex:
            print(f"❌ poll_pending error: {ex}", flush=True)
            logger.warning(f"poll_pending error: {ex}")
        await asyncio.sleep(30)


async def _loop_signals():
    """Bloc C1 — Poll signals Postgres toutes les 10s, ne poste que les nouveaux."""
    while True:
        try:
            pool = await get_pool()
            if not pool:
                await asyncio.sleep(10)
                continue
            # Ne lire QUE les signaux des 15 dernières secondes (nouveau + non posté)
            rows = await pool.fetch(
                "SELECT * FROM signals WHERE created_at > NOW() - INTERVAL '15 seconds' ORDER BY created_at"
            )
            ch = bot.get_channel(CH_SIGNALS)
            if ch and rows:
                colors = {
                    "open":       discord.Color.blue(),
                    "tp1":        discord.Color.green(),
                    "tp2":        discord.Color.green(),
                    "tp3":        discord.Color.green(),
                    "closed_win": discord.Color.green(),
                    "closed_be":  discord.Color.light_grey(),
                    "closed_loss": discord.Color.red(),
                }
                status_labels = {
                    "open": "OUVERT", "tp1": "TP1 TOUCHÉ \U0001f525",
                    "tp2": "TP2 TOUCHÉ \U0001f525", "tp3": "TP3 TOUCHÉ \U0001f525",
                    "closed_win": "CLOT. GAGNANT \u2705",
                    "closed_be": "BREAKEVEN \u2696️",
                    "closed_loss": "SL TOUCHÉ \u274c",
                }
                for r in rows:
                    sid = r["id"]
                    if sid in _posted_signals:
                        continue  # déjà posté
                    d = "\U0001f7e2 ACHAT" if r["direction"] == "BUY" else "\U0001f534 VENTE"
                    e = discord.Embed(
                        title=f"{d} {r['symbol']}",
                        color=discord.Color.green() if r["direction"] == "BUY" else discord.Color.red(),
                        timestamp=r["created_at"] or datetime.utcnow()
                    )
                    def fmt(v): return str(v).rstrip("0").rstrip(".") if v else "?"
                    e.add_field(name="\U0001f4cc Entrée", value=fmt(r["entry"]), inline=True)
                    e.add_field(name="\U0001f3af TP1",    value=fmt(r["tp1"]),   inline=True)
                    e.add_field(name="\U0001f3af TP2",    value=fmt(r["tp2"]),   inline=True)
                    e.add_field(name="\U0001f512 SL",     value=fmt(r["sl"]),    inline=True)
                    e.add_field(name="\U0001f4cb Status", value=status_labels.get(r["status"], r["status"].upper()), inline=True)
                    msg = await ch.send(embed=e)
                    _posted_signals[sid] = msg.id
                    logger.info(f"\U0001f4e3 Signal posté Discord: {r['symbol']} #{sid}")
        except Exception as ex:
            logger.warning(f"poll_signals error: {ex}")
        await asyncio.sleep(10)


async def _loop_alerts():
    """Bloc C2 — Poll alertes Postgres toutes les 10s, ne poste que les nouvelles."""
    while True:
        try:
            pool = await get_pool()
            if not pool:
                await asyncio.sleep(10)
                continue
            rows = await pool.fetch(
                "SELECT * FROM alerts WHERE created_at > NOW() - INTERVAL '15 seconds' ORDER BY created_at"
            )
            ch = bot.get_channel(CH_ALERTS)
            if ch and rows:
                icons = {
                    "KILL_SWITCH": "\u26d4",
                    "MT5_ERROR":   "\U0001f534",
                    "ERROR":       "\U0001f534",
                    "WARNING":     "\u26a0️",
                    "INFO":        "\u2139️",
                }
                colors_map = {
                    "KILL_SWITCH": discord.Color.orange(),
                    "MT5_ERROR":   discord.Color.red(),
                    "ERROR":       discord.Color.red(),
                    "WARNING":     discord.Color.yellow(),
                    "INFO":        discord.Color.blue(),
                }
                for r in rows:
                    aid = r["id"]
                    if aid in _posted_alerts:
                        continue
                    t = r["type"] or "INFO"
                    icon = icons.get(t, "ℹ️")
                    label = t.replace("_", " ")
                    e = discord.Embed(
                        title=f"{icon} {label}",
                        description=r["message"],
                        color=colors_map.get(t, discord.Color.blue()),
                        timestamp=r["created_at"] or datetime.utcnow()
                    )
                    await ch.send(embed=e)
                    _posted_alerts.add(aid)
                    logger.info(f"\u26A0 Alerte postée Discord: {t} #{aid}")
        except Exception as ex:
            logger.warning(f"poll_alerts error: {ex}")
        await asyncio.sleep(10)


async def _loop_inactive():
    """Bloc B2 — Rappel membres inactifs chaque lundi à 9h UTC."""
    while True:
        try:
            now = datetime.utcnow()
            # Calculer le prochain lundi 9h UTC
            days_until_monday = (7 - now.weekday()) % 7 or 7
            if now.weekday() == 0 and now.hour < 9:
                days_until_monday = 0
            next_monday = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if days_until_monday > 0:
                from datetime import timedelta
                next_monday += timedelta(days=days_until_monday)
            wait_s = (next_monday - now).total_seconds()
            logger.info(f"\U0001f4c5 Rappel inactifs dans {wait_s/3600:.1f}h (lundi 9h UTC)")
            await asyncio.sleep(max(wait_s, 60))

            # Envoyer rappel aux membres actifs inactifs depuis 7 jours
            pool = await get_pool()
            rows = await pool.fetch(
                "SELECT user_id, username FROM members "
                "WHERE status='active' AND (last_active IS NULL OR last_active < NOW() - INTERVAL '7 days')"
            )
            logger.info(f"\U0001f4e8 Rappel inactifs : {len(rows)} membres")
            for r in rows:
                await tg_send(r["user_id"],
                    "\U0001f44b Tu es toujours actif sur QUANT PRO ?\n"
                    "Les signaux de la semaine sont déjà là \U0001f4ca\n"
                    "Connecte-toi pour ne rien rater !"
                )
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logger.warning(f"_loop_inactive error: {ex}")
            await asyncio.sleep(3600)  # retry dans 1h si erreur


@tasks.loop(time=dtime(hour=22, minute=0))
async def daily_stats_task():
    await _post_stats(None)


async def _post_stats(interaction):
    """Bloc D1 — Dashboard complet avec données du mois."""
    try:
        pool    = await get_pool()
        # Membres
        started = await pool.fetchval("SELECT COUNT(*) FROM members") or 0
        pending = await pool.fetchval("SELECT COUNT(*) FROM members WHERE status='pending'") or 0
        active  = await pool.fetchval("SELECT COUNT(*) FROM members WHERE status='active'") or 0
        refused = await pool.fetchval("SELECT COUNT(*) FROM members WHERE status IN ('refused','kicked')") or 0
        conv    = round(active / started * 100, 1) if started else 0
        # Signaux du mois
        month_trades = await pool.fetchval(
            "SELECT COUNT(*) FROM signals WHERE created_at >= DATE_TRUNC('month', NOW())") or 0
        month_wins   = await pool.fetchval(
            "SELECT COUNT(*) FROM signals WHERE status='closed_win' AND created_at >= DATE_TRUNC('month', NOW())") or 0
        month_losses = await pool.fetchval(
            "SELECT COUNT(*) FROM signals WHERE status='closed_loss' AND created_at >= DATE_TRUNC('month', NOW())") or 0
        month_pnl    = await pool.fetchval(
            "SELECT COALESCE(SUM(pnl),0) FROM signals WHERE created_at >= DATE_TRUNC('month', NOW())") or 0
        wr = round(month_wins / month_trades * 100, 1) if month_trades else 0
        # Signaux today
        today_t = await pool.fetchval("SELECT COUNT(*) FROM signals WHERE created_at::date=CURRENT_DATE") or 0
        today_w = await pool.fetchval("SELECT COUNT(*) FROM signals WHERE status='closed_win' AND created_at::date=CURRENT_DATE") or 0

        e = discord.Embed(
            title=f"\U0001f4ca QUANT Dashboard — {datetime.utcnow().strftime('%d/%m/%Y')}",
            color=discord.Color.purple(), timestamp=datetime.utcnow()
        )
        e.add_field(name="👥 Total",      value=str(started),    inline=True)
        e.add_field(name="⏳ Pending",    value=str(pending),    inline=True)
        e.add_field(name="✅ Actifs",     value=str(active),     inline=True)
        e.add_field(name="❌ Refusés",    value=str(refused),    inline=True)
        e.add_field(name="📈 Conversion", value=f"{conv}%",      inline=True)
        e.add_field(name="\u200b",        value="\u200b",        inline=True)
        e.add_field(name="📉 Trades",     value=f"W:{month_wins} L:{month_losses} / {today_t}", inline=True)
        e.add_field(name="🎯 Win Rate",   value=f"{wr}%",        inline=True)
        e.add_field(name="💰 P&L",        value=f"{month_pnl:+.1f}R", inline=True)

        if interaction:
            await interaction.followup.send(embed=e)
        else:
            ch = bot.get_channel(CH_STATS)
            if ch: await ch.send(embed=e)
    except Exception as ex:
        logger.warning(f"_post_stats: {ex}")
        if interaction:
            await interaction.followup.send(f"❌ Erreur stats: {ex}")


# ── Slash Commands ─────────────────────────────────────────────────────────────

@tree.command(name="valider", description="Valide un membre → envoie lien PRO + déplace le channel")
@app_commands.describe(user_id="ID Telegram de l'utilisateur")
async def cmd_valider(interaction: discord.Interaction, user_id: str):
    await interaction.response.defer()
    try: uid = int(user_id)
    except ValueError:
        await interaction.followup.send("❌ user_id invalide"); return

    pool = await get_pool()
    row  = await pool.fetchrow("SELECT * FROM members WHERE user_id=$1", uid)
    if not row:
        await interaction.followup.send(f"❌ Aucun membre ID {uid}"); return
    if row["status"] not in ("pending", "started"):
        await interaction.followup.send(f"⚠️ Statut: {row['status']}"); return

    await tg_send(uid, (
        f"✅ Bonne nouvelle ! Ton dépôt a été validé.\n\n"
        f"👉 Rejoins QUANT Signals PRO ici :\n{CHANNEL_LINK}\n\n"
        f"Bienvenue dans l'équipe 🚀"
    ))
    await pool.execute("UPDATE members SET status='active', validated_at=NOW() WHERE user_id=$1", uid)

    ch_id = row["discord_channel_id"]
    if ch_id and _cat_validated_id:
        safe = "".join(c if c.isalnum() or c == "-" else "-"
                       for c in (row["username"] or "user").lower())
        await _move_channel(ch_id, f"valide-{safe}", _cat_validated_id)

    crm = bot.get_channel(CH_CRM)
    if crm:
        await crm.send(f"✅ **VALIDÉ** — @{row['username'] or uid} (`{uid}`) par {interaction.user.mention}")

    await interaction.followup.send(f"✅ `{uid}` validé — lien PRO envoyé, channel déplacé !")
    logger.info(f"✅ {uid} validé par {interaction.user}")


@tree.command(name="refuser", description="Refuse un membre → supprime le channel après 1h")
@app_commands.describe(user_id="ID Telegram de l'utilisateur")
async def cmd_refuser(interaction: discord.Interaction, user_id: str):
    await interaction.response.defer()
    try: uid = int(user_id)
    except ValueError:
        await interaction.followup.send("❌ user_id invalide"); return

    pool = await get_pool()
    row  = await pool.fetchrow("SELECT * FROM members WHERE user_id=$1", uid)
    if not row:
        await interaction.followup.send(f"❌ Aucun membre ID {uid}"); return

    await tg_send(uid, "Désolé, nous n'avons pas pu valider votre dépôt.\nContactez-nous pour plus d'infos.")
    await pool.execute("UPDATE members SET status='refused' WHERE user_id=$1", uid)

    ch_id = row["discord_channel_id"]
    if ch_id:
        asyncio.create_task(_delete_channel_delayed(ch_id, delay_s=3600))

    crm = bot.get_channel(CH_CRM)
    if crm:
        await crm.send(f"❌ **REFUSÉ** — @{row['username'] or uid} (`{uid}`) par {interaction.user.mention}")

    await interaction.followup.send(f"❌ `{uid}` refusé — channel supprimé dans 1h.")
    logger.info(f"❌ {uid} refusé par {interaction.user}")


@tree.command(name="kick", description="Révoque l'accès d'un membre")
@app_commands.describe(user_id="ID Telegram de l'utilisateur")
async def cmd_kick(interaction: discord.Interaction, user_id: str):
    await interaction.response.defer()
    try: uid = int(user_id)
    except ValueError:
        await interaction.followup.send("❌ user_id invalide"); return

    pool = await get_pool()
    row  = await pool.fetchrow("SELECT * FROM members WHERE user_id=$1", uid)
    if not row:
        await interaction.followup.send(f"❌ Aucun membre ID {uid}"); return

    await pool.execute("UPDATE members SET status='kicked' WHERE user_id=$1", uid)
    await tg_send(uid, "Votre accès QUANT PRO a été révoqué.")

    crm = bot.get_channel(CH_CRM)
    if crm:
        await crm.send(f"🚫 **KICKED** — @{row['username'] or uid} (`{uid}`) par {interaction.user.mention}")

    await interaction.followup.send(f"🚫 `{uid}` kicked.")
    logger.info(f"🚫 {uid} kicked par {interaction.user}")


@tree.command(name="stats", description="Dashboard QUANT")
async def cmd_stats(interaction: discord.Interaction):
    await interaction.response.defer()
    await _post_stats(interaction)


# ── Events ─────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    logger.info(f"🤖 Discord Admin Bot en ligne : {bot.user}")
    print(f"🤖 on_ready | cat_pending={_cat_pending_id} | cat_validated={_cat_validated_id}", flush=True)

    g_obj = discord.Object(id=GUILD_ID)
    tree.copy_global_to(guild=g_obj)
    synced = await tree.sync(guild=g_obj)
    logger.info(f"✅ {len(synced)} slash commands sync")


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv
    env = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env):
        load_dotenv(env, override=False)

    token = os.getenv("DISCORD_BOT_TOKEN", "")
    guild = int(os.getenv("DISCORD_GUILD_ID", "0"))
    if not token:
        logger.error("DISCORD_BOT_TOKEN manquant"); exit(1)
    print(f"🤖 Discord Admin Bot | Guild:{guild}")
    bot.run(token)
