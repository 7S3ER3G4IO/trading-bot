"""
config.py — Paramètres globaux du bot de trading.
"""
from brokers.capital_client import CAPITAL_INSTRUMENTS

# ─── INSTRUMENTS CAPITAL.COM CFD (DEMO) ───────────────────────────────────────
# 39 actifs : Forex, Indices, Commodités, Crypto, Actions
SYMBOLS = CAPITAL_INSTRUMENTS   # alias legacy

# ─── GESTION DU RISQUE ────────────────────────────────────────────────────────
RISK_PER_TRADE       = 0.01    # 1% du capital par trade par symbole
ATR_SL_MULTIPLIER    = 1.0     # SL = 1.0 ATR
MAX_OPEN_TRADES      = 10      # Capital.com CFD : max 10 positions simultanées
DAILY_DRAWDOWN_LIMIT = -0.05   # -5% → pause

# Sessions de trading actives (heures UTC)
LONDON_HOURS  = list(range(7, 11))    # 7h-10h UTC
NY_HOURS      = list(range(13, 17))   # 13h-16h UTC
SESSION_HOURS = LONDON_HOURS + NY_HOURS

# ─── CALENDRIER ÉCONOMIQUE ───────────────────────────────────────────────────
NEWS_PAUSE_BEFORE_MIN = 30    # Pause X min avant news HIGH impact
NEWS_PAUSE_AFTER_MIN  = 30    # Pause X min après news HIGH impact

# ─── BILAN JOURNALIER ────────────────────────────────────────────────────────
DAILY_REPORT_HOUR_UTC = 20    # 21h CET = 20h UTC

# ─── BOUCLE DU BOT ───────────────────────────────────────────────────────────
LOOP_INTERVAL_SECONDS = 30    # Scan toutes les 30s (12 paires = besoin plus rapide)

# ─── TELEGRAM CHANNELS ──────────────────────────────────────────────────────
# Groupe Telegram créé pour les stats wallet en temps réel
WALLET_CHANNEL_URL = "https://t.me/+kXWR58eDOGYxZDNk"  # Nemesis Wallet group
WALLET_CHAT_ID     = "-5200613208"                       # Chat ID du groupe wallet

# ─── LOGS ────────────────────────────────────────────────────────────────────
LOG_DIR   = "logs"
LOG_LEVEL = "INFO"
