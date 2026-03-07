"""
config.py — Paramètres globaux du bot de trading.
Modifiez ces valeurs pour ajuster le comportement du bot.
"""

# ─── MARCHÉ ──────────────────────────────────────────────────────────────────
SYMBOL       = "BTC/USDT"    # Paire tradée
TIMEFRAME    = "15m"          # Bougies 15 minutes
LIMIT        = 200            # Nombre de bougies à récupérer pour les indicateurs

# ─── STRATÉGIE ────────────────────────────────────────────────────────────────
EMA_FAST     = 9              # EMA rapide
EMA_SLOW     = 21             # EMA lente
RSI_PERIOD   = 14             # Période RSI
RSI_BUY_MAX  = 65             # RSI max pour acheter (évite surachat)
RSI_SELL_MIN = 35             # RSI min pour vendre (évite survente)
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIGNAL  = 9
ATR_PERIOD   = 14             # Période ATR pour stop-loss dynamique

# ─── GESTION DU RISQUE ────────────────────────────────────────────────────────
RISK_PER_TRADE      = 0.01    # 1% du capital par trade
ATR_SL_MULTIPLIER   = 1.5    # Stop-Loss = ATR × 1.5
RR_RATIO            = 2.0    # Take-Profit = Stop-Loss × 2.0 (R:R 1:2)
MAX_OPEN_TRADES     = 3       # Trades simultanés maximum
DAILY_DRAWDOWN_LIMIT= -0.05  # -5% du capital → pause automatique

# ─── BOUCLE DU BOT ───────────────────────────────────────────────────────────
LOOP_INTERVAL_SECONDS = 60    # Vérification toutes les 60 secondes

# ─── LOGS ────────────────────────────────────────────────────────────────────
LOG_DIR      = "logs"
LOG_LEVEL    = "INFO"
