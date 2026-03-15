"""
monitor.py — Système de monitoring QUANT
=========================================
Tourne en boucle asyncio, vérifie toutes les 60 secondes :
  1. Containers Docker (5 requis) → restart auto si down
  2. Ressources système (CPU > 90%, RAM > 85%, Disque > 80%)
  3. Logs MT5 (nemesis_bot) — erreur ou silence > 10 min
  4. Kill switch (table alerts Postgres)
  5. API stats heartbeat (localhost:8080)
  + Bilan matinal 8h UTC

Toutes les alertes → Discord #monitoring uniquement (embeds colorés).
Aucune alerte Telegram.
"""

import os
import asyncio
import aiohttp
import psutil
import time
from datetime import datetime, timezone, timedelta
from loguru import logger

# ── Postgres ──────────────────────────────────────────────────────────────────
try:
    import asyncpg
    _DB_URL = os.getenv("DATABASE_URL", "")
    _PG_OK  = bool(_DB_URL)
except ImportError:
    _PG_OK = False

# ── Docker SDK ────────────────────────────────────────────────────────────────
try:
    import docker as docker_sdk
    _docker_client = docker_sdk.from_env()
    _DOCKER_OK = True
except Exception as _de:
    _docker_client = None
    _DOCKER_OK = False
    logger.warning(f"Docker SDK indisponible: {_de}")

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN    = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_MONITORING_CH = os.getenv("DISCORD_MONITORING_CH", "")
DISCORD_API          = "https://discord.com/api/v10"

CONTAINERS_REQUIS = [
    "nemesis_bot",
    "nemesis_postgres",
    "monitor",
    "market_brief",
    "social_visual",
    # nemesis_grafana est optionnel — dashboard uniquement, pas critique pour le trading
    # "nemesis_grafana",
]

# Seuils
CPU_THRESHOLD  = 90.0   # %
RAM_THRESHOLD  = 85.0   # %
DISK_THRESHOLD = 80.0   # %
CPU_COUNT_LIMIT = 3     # nb de checks > seuil avant alerte

# ── Anti-spam : dernière alerte par sujet ─────────────────────────────────────
_last_alert: dict[str, float] = {}
ALERT_COOLDOWN = 300  # 5 minutes


def _can_alert(key: str) -> bool:
    now = time.monotonic()
    last = _last_alert.get(key, 0)
    if now - last >= ALERT_COOLDOWN:
        _last_alert[key] = now
        return True
    return False


# ── Pool Postgres ─────────────────────────────────────────────────────────────
_pool = None


async def _get_pool():
    global _pool
    if _pool is None and _PG_OK:
        try:
            _pool = await asyncpg.create_pool(_DB_URL, min_size=1, max_size=3)
        except Exception as e:
            logger.warning(f"[DB] Pool init: {e}")
    return _pool


# ── Discord helpers ───────────────────────────────────────────────────────────

async def _send_discord_embed(
    title: str,
    color: int,
    fields: list[dict],
    mention: str = "",
) -> None:
    """Envoie un embed coloré dans #monitoring Discord."""
    if not DISCORD_MONITORING_CH or not DISCORD_BOT_TOKEN:
        logger.warning("[Monitor] DISCORD_MONITORING_CH ou BOT_TOKEN manquant")
        return

    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y à %H:%M UTC")
    embed = {
        "title": title,
        "color": color,
        "fields": [{"name": f["name"], "value": f["value"], "inline": f.get("inline", True)} for f in fields],
        "footer": {"text": f"QUANT Monitor · {now_str}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    content = mention if mention else ""
    payload = {"content": content, "embeds": [embed]}

    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    url = f"{DISCORD_API}/channels/{DISCORD_MONITORING_CH}/messages"
    proxy = os.getenv("HTTPS_PROXY", "") or os.getenv("HTTP_PROXY", "")
    try:
        async with aiohttp.ClientSession() as s:
            resp = await s.post(
                url, json=payload, headers=headers,
                proxy=proxy.replace("socks5://", "http://") if proxy else None,
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if resp.status not in (200, 201):
                body = await resp.text()
                logger.warning(f"[Monitor] Discord {resp.status}: {body[:200]}")
    except Exception as e:
        logger.error(f"[Monitor] Discord send error: {e}")



# ── Couleurs ──────────────────────────────────────────────────────────────────
RED    = 0xE74C3C
ORANGE = 0xE67E22
GREEN  = 0x2ECC71
DARK_RED = 0x8B0000


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 1 — CONTAINERS DOCKER
# ═══════════════════════════════════════════════════════════════════════════════

async def check_containers() -> None:
    """Vérifie les status de tous les containers requis. Restart auto si down."""
    if not _DOCKER_OK:
        return

    loop = asyncio.get_event_loop()

    for name in CONTAINERS_REQUIS:
        try:
            container = await loop.run_in_executor(
                None, lambda n=name: _docker_client.containers.get(n)
            )
            status = container.status
        except Exception:
            status = "not_found"
            container = None

        if status != "running":
            alert_key = f"container_{name}"
            if _can_alert(alert_key):
                t_down = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
                await _send_discord_embed(
                    title="🔴 ALERTE — Container arrêté",
                    color=RED,
                    fields=[
                        {"name": "Container",    "value": f"`{name}`"},
                        {"name": "Statut",       "value": status},
                        {"name": "Détecté à",    "value": t_down},
                        {"name": "Action",       "value": "⚙️ Restart automatique en cours..."},
                    ],
                )
                logger.warning(f"[Monitor] Container {name} DOWN (status={status})")

                # Tentative de restart
                t_start = time.monotonic()
                try:
                    if container:
                        # Container existe mais est down → restart via SDK Docker
                        await loop.run_in_executor(None, container.restart)

                        # Re-vérifier après restart
                        await asyncio.sleep(5)
                        container2 = await loop.run_in_executor(
                            None, lambda n=name: _docker_client.containers.get(n)
                        )
                        downtime = int(time.monotonic() - t_start)
                        if container2.status == "running":
                            await _send_discord_embed(
                                title="✅ Récupération automatique",
                                color=GREEN,
                                fields=[
                                    {"name": "Container", "value": f"`{name}`"},
                                    {"name": "Downtime",  "value": f"~{downtime}s"},
                                    {"name": "Statut",    "value": "🟢 En ligne"},
                                ],
                            )
                            logger.info(f"[Monitor] ✅ {name} relancé en {downtime}s")
                        else:
                            await _send_discord_embed(
                                title="🆘 ÉCHEC DU RESTART",
                                color=DARK_RED,
                                fields=[
                                    {"name": "Container", "value": f"`{name}`"},
                                    {"name": "Statut",    "value": container2.status},
                                    {"name": "Action",    "value": "❗ Intervention manuelle requise"},
                                ],
                                mention="@here",
                            )
                            logger.error(f"[Monitor] 🆘 {name} ne redémarre pas")
                    else:
                        # FIX: container not_found → pas de subprocess docker
                        # On ne peut pas recréer un container supprimé sans docker compose
                        # Juste logguer — l'opérateur doit intervenir manuellement
                        logger.warning(
                            f"[Monitor] Container {name} introuvable (not_found) — "
                            f"recréation manuelle requise via docker compose"
                        )
                        await _send_discord_embed(
                            title="🆘 Container introuvable",
                            color=DARK_RED,
                            fields=[
                                {"name": "Container", "value": f"`{name}`"},
                                {"name": "Statut",    "value": "not_found (supprimé ?)"},
                                {"name": "Action",    "value": "❗ `docker compose up -d` requis manuellement"},
                            ],
                            mention="@here",
                        )

                except Exception as restart_err:
                    logger.error(f"[Monitor] Restart {name}: {restart_err}")
                    await _send_discord_embed(
                        title="🆘 ERREUR RESTART",
                        color=DARK_RED,
                        fields=[
                            {"name": "Container", "value": f"`{name}`"},
                            {"name": "Erreur",    "value": str(restart_err)[:200]},
                        ],
                        mention="@here",
                    )



# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 2 — RESSOURCES SYSTÈME
# ═══════════════════════════════════════════════════════════════════════════════

_cpu_high_count = 0  # compteur de checks consécutifs CPU > seuil


async def check_resources() -> None:
    """Vérifie CPU, RAM, Disque. Alerte si seuils dépassés."""
    global _cpu_high_count
    loop = asyncio.get_event_loop()

    cpu_pct  = await loop.run_in_executor(None, lambda: psutil.cpu_percent(interval=1))
    ram      = psutil.virtual_memory()
    disk     = psutil.disk_usage("/")

    ram_pct  = ram.percent
    disk_pct = disk.percent
    ram_gb   = ram.total / 1e9
    ram_used = ram.used  / 1e9
    disk_free = (disk.total - disk.used) / 1e9

    # CPU — exige 3 checks consécutifs > seuil
    if cpu_pct > CPU_THRESHOLD:
        _cpu_high_count += 1
    else:
        _cpu_high_count = 0

    resource_alert = (
        _cpu_high_count >= CPU_COUNT_LIMIT or
        ram_pct > RAM_THRESHOLD or
        disk_pct > DISK_THRESHOLD
    )

    if resource_alert and _can_alert("resources"):
        cpu_str  = f"**{cpu_pct:.1f}%**" + (" 🔴" if cpu_pct > CPU_THRESHOLD else " 🟢")
        ram_str  = f"**{ram_pct:.1f}%** ({ram_used:.1f}GB / {ram_gb:.1f}GB)" + (" 🔴" if ram_pct > RAM_THRESHOLD else " 🟢")
        disk_str = f"**{disk_pct:.1f}%** ({disk_free:.1f}GB libres)" + (" 🔴" if disk_pct > DISK_THRESHOLD else " 🟢")

        await _send_discord_embed(
            title="⚠️ Ressources système élevées",
            color=ORANGE,
            fields=[
                {"name": "CPU",    "value": cpu_str},
                {"name": "RAM",    "value": ram_str},
                {"name": "Disque", "value": disk_str},
            ],
        )
        logger.warning(f"[Monitor] Ressources: CPU={cpu_pct}% RAM={ram_pct}% Disk={disk_pct}%")


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 3 — LOGS MT5 (nemesis_bot)
# ═══════════════════════════════════════════════════════════════════════════════

_last_mt5_log_ts: float = time.time()


async def check_mt5_logs() -> None:
    """Lit les 50 dernières lignes de nemesis_bot. Alerte si ERROR ou silence > 10min."""
    global _last_mt5_log_ts

    if not _DOCKER_OK:
        return

    loop = asyncio.get_event_loop()
    try:
        container = await loop.run_in_executor(
            None, lambda: _docker_client.containers.get("nemesis_bot")
        )
        if container.status != "running":
            return

        raw_logs = await loop.run_in_executor(
            None, lambda: container.logs(tail=50, timestamps=True).decode("utf-8", errors="replace")
        )
    except Exception as e:
        logger.debug(f"[Monitor] MT5 logs: {e}")
        return

    lines = raw_logs.strip().splitlines()
    if lines:
        _last_mt5_log_ts = time.time()

    # Détecter des erreurs
    errors = [l for l in lines if "ERROR" in l or "CRITICAL" in l or "Exception" in l]
    if errors and _can_alert("mt5_error"):
        snippet = "\n".join(errors[-3:])[:500]
        await _send_discord_embed(
            title="⚠️ Erreur détectée dans nemesis_bot",
            color=ORANGE,
            fields=[
                {"name": "Container", "value": "`nemesis_bot`"},
                {"name": "Extrait",   "value": f"```\n{snippet}\n```", "inline": False},
            ],
        )
        logger.warning(f"[Monitor] MT5 erreur détectée: {errors[-1][:100]}")

    # Silence > 10 min
    silence_min = (time.time() - _last_mt5_log_ts) / 60
    if silence_min > 10 and _can_alert("mt5_silence"):
        await _send_discord_embed(
            title="⚠️ nemesis_bot — Aucune activité",
            color=ORANGE,
            fields=[
                {"name": "Container",         "value": "`nemesis_bot`"},
                {"name": "Silence depuis",     "value": f"{int(silence_min)} min"},
                {"name": "Dernier log connu",  "value": lines[-1][:100] if lines else "–"},
            ],
        )
        logger.warning(f"[Monitor] MT5 silence: {silence_min:.1f} min")


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 4 — KILL SWITCH (table alerts Postgres)
# ═══════════════════════════════════════════════════════════════════════════════

_seen_kill_switch_ids: set = set()


async def check_kill_switch() -> None:
    """Détecte tout nouveau kill switch dans la table alerts (< 2 min)."""
    pool = await _get_pool()
    if not pool:
        return

    try:
        rows = await pool.fetch("""
            SELECT id, message, created_at
            FROM alerts
            WHERE type='KILL_SWITCH'
              AND created_at > NOW() - INTERVAL '2 minutes'
            ORDER BY created_at DESC
        """)
    except Exception as e:
        logger.debug(f"[Monitor] kill switch query: {e}")
        return

    for row in rows:
        if row["id"] in _seen_kill_switch_ids:
            continue
        _seen_kill_switch_ids.add(row["id"])

        ts = row["created_at"].strftime("%H:%M:%S UTC") if row["created_at"] else "?"
        await _send_discord_embed(
            title="🆘 KILL SWITCH DÉCLENCHÉ",
            color=DARK_RED,
            fields=[
                {"name": "Raison", "value": str(row.get("message") or "N/A")[:200]},
                {"name": "Heure",  "value": ts},
            ],
            mention="@here",
        )
        logger.critical(f"[Monitor] 🆘 Kill Switch détecté: {row.get('message')}")




# ═══════════════════════════════════════════════════════════════════════════════
# BILAN MATINAL — 8H UTC
# ═══════════════════════════════════════════════════════════════════════════════

async def post_morning_summary() -> None:
    """Poste le bilan matinal dans #monitoring."""
    pool   = await _get_pool()
    loop   = asyncio.get_event_loop()

    # Stats traders
    trades_hier = 0
    wins_hier   = 0
    members_new = 0
    try:
        if pool:
            row = await pool.fetchrow("""
                SELECT
                    COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24h') as total,
                    COUNT(*) FILTER (WHERE status IN ('tp1','tp2','closed_win')
                      AND created_at >= NOW() - INTERVAL '24h') as wins
                FROM signals
            """)
            if row:
                trades_hier = int(row["total"] or 0)
                wins_hier   = int(row["wins"]  or 0)

            mem_row = await pool.fetchrow("""
                SELECT COUNT(*) as nb FROM members
                WHERE status='active' AND validated_at >= NOW() - INTERVAL '24h'
            """)
            if mem_row:
                members_new = int(mem_row["nb"] or 0)
    except Exception as e:
        logger.debug(f"[Monitor] Bilan DB: {e}")

    # Services status
    services_ok = []
    services_ko = []
    if _DOCKER_OK:
        for name in CONTAINERS_REQUIS:
            try:
                c = await loop.run_in_executor(None, lambda n=name: _docker_client.containers.get(n))
                if c.status == "running":
                    services_ok.append(name)
                else:
                    services_ko.append(f"{name} ({c.status})")
            except Exception:
                services_ko.append(f"{name} (not found)")

    services_str = (
        "🟢 Tous opérationnels" if not services_ko
        else f"🔴 Problèmes : {', '.join(services_ko)}"
    )
    wr_hier = f"{round(wins_hier/trades_hier*100,1)}%" if trades_hier else "–"

    cpu_pct = await loop.run_in_executor(None, lambda: psutil.cpu_percent(interval=1))
    ram_pct = psutil.virtual_memory().percent

    await _send_discord_embed(
        title="☀️ BILAN MATINAL QUANT",
        color=GREEN,
        fields=[
            {"name": "Services 24h",       "value": services_str,       "inline": False},
            {"name": "Trades hier",         "value": f"{trades_hier} | Win rate : {wr_hier}"},
            {"name": "Nouveaux membres",    "value": str(members_new)},
            {"name": "Ressources",          "value": f"CPU {cpu_pct:.1f}% | RAM {ram_pct:.1f}%"},
        ],
    )
    logger.info("[Monitor] ☀️ Bilan matinal posté")


# ═══════════════════════════════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════

async def startup_message() -> None:
    """Message de démarrage dans #monitoring."""
    await _send_discord_embed(
        title="🚀 Monitor QUANT démarré",
        color=GREEN,
        fields=[
            {"name": "Containers surveillés", "value": ", ".join(f"`{c}`" for c in CONTAINERS_REQUIS), "inline": False},
            {"name": "Intervalle",             "value": "60 secondes"},
            {"name": "Bilan matinal",          "value": "08:00 UTC chaque jour"},
        ],
    )


async def main_loop() -> None:
    """Boucle principale toutes les 60 secondes."""
    await _get_pool()
    await startup_message()

    morning_posted_date = None

    while True:
        now = datetime.now(timezone.utc)

        # Bilan matinal à 8h UTC
        today = now.date()
        if now.hour == 8 and now.minute < 2 and morning_posted_date != today:
            await post_morning_summary()
            morning_posted_date = today

        # Tous les checks en parallèle
        await asyncio.gather(
            check_containers(),
            check_resources(),
            check_mt5_logs(),
            check_kill_switch(),
            # check_api_stats() — RETIRÉ définitivement (service non déployé)
            return_exceptions=True,
        )

        await asyncio.sleep(60)


if __name__ == "__main__":
    logger.info("🖥️  QUANT Monitor v1.0 — démarrage")
    asyncio.run(main_loop())
