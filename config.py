"""
config.py — Paramètres globaux du bot de trading.
"""
from brokers.capital_client import CAPITAL_INSTRUMENTS

# ─── INSTRUMENTS CAPITAL.COM CFD (DEMO) ───────────────────────────────────────
# 8 actifs tradés : Forex, Indices, Matières premières
# Source des données : Capital.com API (fetch_ohlcv)
SYMBOLS = CAPITAL_INSTRUMENTS   # alias legacy — utiliser CAPITAL_INSTRUMENTS directement
SYMBOL  = CAPITAL_INSTRUMENTS[0]  # Symbole principal (GOLD)

TIMEFRAME = "5m"         # Bougies 5 minutes — Session scalping (London + NY open)
HTF       = "1h"         # Higher TimeFrame (confirmation de tendance)
LIMIT     = 300          # Nombre de bougies à charger par instrument


# ─── STRATÉGIE — 6 FILTRES ───────────────────────────────────────────────────
EMA_FAST      = 9
EMA_SLOW      = 21
RSI_PERIOD    = 14
RSI_BUY_MAX   = 65
RSI_SELL_MIN  = 35
MACD_FAST     = 12
MACD_SLOW     = 26
MACD_SIGNAL   = 9
ATR_PERIOD    = 14

# Filtre ADX (force de tendance)
ADX_PERIOD    = 14
ADX_MIN       = 25      # Signal uniquement si ADX > 25 (tendance plus forte)

# Filtre Volume
VOLUME_MA_PERIOD = 20  # Volume > moyenne 20 bougies

# Filtre sessions crypto (heures UTC à éviter — faible liquidité)
AVOID_HOURS_UTC = list(range(23, 24)) + list(range(0, 6))  # 23h-5h UTC

# Sessions de scalping actives (backtesté optimal — scénario D)
LONDON_HOURS  = list(range(7, 11))    # 7h-10h UTC — fort volume pre-market
NY_HOURS      = list(range(13, 17))   # 13h-16h UTC — volume max journée
SESSION_HOURS = LONDON_HOURS + NY_HOURS  # Fenêtres actives pour le scalping

# ─── GESTION DU RISQUE ────────────────────────────────────────────────────────
RISK_PER_TRADE       = 0.01    # 1% du capital par trade par symbole
ATR_SL_MULTIPLIER    = 1.0     # SL = 1.0 ATR (tighter = meilleur R:R)
MIN_SCORE_LEGACY     = 5       # Legacy OANDA (non utilisé — strategy.py définit son propre MIN_SCORE=2/3)
MAX_OPEN_TRADES      = 2               # Capital.com CFD : max 2 positions simultanées
DAILY_DRAWDOWN_LIMIT = -0.05   # -5% → pause

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
