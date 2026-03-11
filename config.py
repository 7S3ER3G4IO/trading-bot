"""
config.py — Paramètres globaux du bot de trading.
"""
from brokers.capital_client import CAPITAL_INSTRUMENTS

# ─── INSTRUMENTS CAPITAL.COM CFD (DEMO) ───────────────────────────────────────
# 39 actifs : Forex, Indices, Commodités, Crypto, Actions
SYMBOLS = CAPITAL_INSTRUMENTS   # alias legacy

# ─── GESTION DU RISQUE ────────────────────────────────────────────────────────
RISK_PER_TRADE       = 0.005   # 0.5% du capital par trade (haute fréquence)
MAX_OPEN_TRADES      = 20      # Capital.com CFD : max 20 positions simultanées (V8 HF)
DAILY_DRAWDOWN_LIMIT = -0.10   # -10% → pause (was -5%, caused false triggers on redeploy)

# Sessions de trading actives (heures UTC) — plage globale pour heartbeat/dashboard
# Le filtrage fin par catégorie d'actif est dans strategy.py (SESSION_WINDOWS)
SESSION_HOURS = list(range(6, 22))  # 06h-22h UTC (couvre crypto étendu)

# ─── CALENDRIER ÉCONOMIQUE ───────────────────────────────────────────────────
NEWS_PAUSE_BEFORE_MIN = 30    # Pause X min avant news HIGH impact
NEWS_PAUSE_AFTER_MIN  = 30    # Pause X min après news HIGH impact

# ─── BILAN JOURNALIER ────────────────────────────────────────────────────────
DAILY_REPORT_HOUR_UTC = 20    # 21h CET = 20h UTC

# ─── BOUCLE DU BOT ───────────────────────────────────────────────────────────
LOOP_INTERVAL_SECONDS = 30    # Scan toutes les 30s (12 paires = besoin plus rapide)

# ─── TELEGRAM CHANNELS ──────────────────────────────────────────────────
# Ancien canal wallet (legacy)
WALLET_CHANNEL_URL = "https://t.me/+kXWR58eDOGYxZDNk"
WALLET_CHAT_ID     = "-5200613208"

# Canaux Nemesis dédiés (multi-channel architecture)
CHANNELS = {
    "dashboard":   {"id": "-1003710848841", "url": "https://t.me/NemesisDashboard",      "name": "📊 Dashboard"},
    "trades":      {"id": "-1003754130921", "url": "https://t.me/Nemesis_Trades",         "name": "📋 Trades"},
    "performance": {"id": "-1003742483066", "url": "https://t.me/Nemesis_Performance",    "name": "📈 Performance"},
    "briefing":    {"id": "-1003876226636", "url": "https://t.me/Nemesis_Briefing",       "name": "☀️ Briefing"},
    "risk":        {"id": "-1003852577520", "url": "https://t.me/Nemesis_Risk",           "name": "🛡️ Risk"},
    "stats":       {"id": "-1003818313045", "url": "https://t.me/Nemesis_Stats",          "name": "🏆 Stats"},
}

# ─── LOGS ────────────────────────────────────────────────────────────────────
LOG_DIR   = "logs"
LOG_LEVEL = "INFO"
