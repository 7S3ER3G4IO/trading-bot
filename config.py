"""
config.py — Paramètres globaux du bot de trading.
"""

# ─── MULTI-ASSET ──────────────────────────────────────────────────────────────
# Paires tradées simultanément (24/7 crypto — fonctionne weekend + semaine)
# Règle : liquidité élevée + historique de tendances claires sur Binance
SYMBOLS = [
    # Majeurs — liquidité maximale
    "BTC/USDT",   # Bitcoin       — référence du marché
    "ETH/USDT",   # Ethereum      — très liquide, bon trendy
    "BNB/USDT",   # Binance Coin  — tendances stables
    # Mid-caps — volatilité + volume = plus de signaux
    "SOL/USDT",   # Solana        — volatilité élevée, tendances rapides
    "XRP/USDT",   # Ripple        — très liquide, moves forts
    "ADA/USDT",   # Cardano       — tendances longues, peu de bruit
    "AVAX/USDT",  # Avalanche     — forte volatilité = bon ATR
    "LINK/USDT",  # Chainlink     — trending régulièrement
    "DOT/USDT",   # Polkadot      — tendances claires
    "DOGE/USDT",  # Dogecoin      — très liquide, moves explosifs
    "MATIC/USDT", # Polygon       — bon volume, trending
    "ATOM/USDT",  # Cosmos        — tendances nettes, moins de noise
]

SYMBOL    = SYMBOLS[0]   # Symbole principal (legacy)
TIMEFRAME = "15m"        # Bougies 15 minutes — OPTIMAL (5m = 4x plus de frais)
HTF       = "1h"         # Higher TimeFrame (confirmation de tendance)
LIMIT     = 200          # Nombre de bougies à charger


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
AVOID_HOURS_UTC = list(range(3, 5))   # 3h-5h UTC uniquement = creux absolu

# ─── GESTION DU RISQUE ────────────────────────────────────────────────────────
RISK_PER_TRADE       = 0.01    # 1% du capital par trade par symbole
ATR_SL_MULTIPLIER    = 1.0     # SL = 1.0 ATR (tighter = meilleur R:R)
MIN_SCORE            = 5       # Signal seulement si score >= 5/6
MAX_OPEN_TRADES      = 12              # 1 par symbole max (12 paires)
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
WALLET_CHANNEL_URL = "https://t.me/+kXWR58eDOGYxZDNk"  # AlphaTrader Wallet group
WALLET_CHAT_ID     = "-5200613208"                       # Chat ID du groupe wallet

# ─── LOGS ────────────────────────────────────────────────────────────────────
LOG_DIR   = "logs"
LOG_LEVEL = "INFO"
