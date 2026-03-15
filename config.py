"""
config.py — Paramètres globaux du bot de trading.
"""

# ─── INSTRUMENTS ACTIFS (IC Markets MT5) ─────────────────────────────────────
# Anciennement Capital.com — migré vers MT5. Liste inline pour éviter toute
# dépendance sur capital_client.py.
CAPITAL_INSTRUMENTS = [
    "GOLD", "SILVER", "J225", "EURJPY", "DE40",
    "UK100", "AU200", "BTCUSD", "GBPJPY", "TSLA",
]

# ─── INSTRUMENTS CAPITAL.COM CFD (DEMO) ───────────────────────────────────────
# 10 actifs Elite (Prop Firm validé — BK 1H)
SYMBOLS = CAPITAL_INSTRUMENTS   # alias legacy

# ─── GESTION DU RISQUE — Prop Firm Config (validée 5/5 seeds) ────────────────
RISK_PER_TRADE         = 0.0035  # 0.35% par trade (Prop Firm validé — backtest 5/5 seeds)
MAX_OPEN_TRADES        = 5       # Elite 10 BK only → max 5 positions simultanées
DAILY_DRAWDOWN_LIMIT   = 5.0    # 5% → pause (Prop Firm rule : max daily DD 5%)
MONTHLY_DRAWDOWN_LIMIT = 15.0   # 15% → circuit breaker mensuel
TOTAL_DRAWDOWN_LIMIT   = 10.0   # 10% → halt définitif depuis balance initiale (Prop Firm rule)

# ─── PROP FIRM MODE ───────────────────────────────────────────────────────────
# Active le protocole Prop Firm complet :
#   - Stratégie BK uniquement (TF et MR désactivés)
#   - Multi-TP : TP1 1.5R (40%) + TP2 2.5R (40%) + TP3 trailing (20%)
#   - Kill switches : journalier 5% + total 10%
PROP_FIRM_MODE         = True    # False = mode normal multi-stratégies
BK_ONLY_MODE           = True    # True = ignorer TF et MR même si signaux présents

# Multi-TP — R:R validés dans prop_firm_backtest.py (PnL +25k$, WR 61.5%, R:R 2.27)
MULTI_TP_TP1_R         = 1.5    # TP1 = entry ± 1.5R → close 40% + Break-Even activé
MULTI_TP_TP2_R         = 2.5    # TP2 = entry ± 2.5R → close 40%
MULTI_TP_TP3_TRAILING  = True   # TP3 = trailing stop sur les 20% restants
MULTI_TP_TP1_PORTION   = 0.40   # 40% de la position fermée au TP1
MULTI_TP_TP2_PORTION   = 0.40   # 40% au TP2
MULTI_TP_TP3_PORTION   = 0.20   # 20% en trailing final

# ─── LEVERAGE & MARGIN (Capital.com réglementaire) ────────────────────────────
# Source: Capital.com Trading Settings (screenshot verified 12/03/2026)
# margin_requirement = 1 / leverage_ratio (i.e. 30:1 → 3.33% margin)
MAX_EFFECTIVE_LEVERAGE = 3.0   # Plafond global: jamais > 3× le capital en valeur nominale

ASSET_MARGIN_REQUIREMENTS = {
    "crypto":       0.50,   # 2:1   → 50% marge requise
    "stocks":       0.20,   # 5:1   → 20% marge requise
    "commodities":  0.05,   # 20:1  → 5%  marge requise
    "indices":      0.05,   # 20:1  → 5%  marge requise
    "forex":        0.0333, # 30:1  → 3.33% marge requise
}

# Mapping instrument → asset_class (utilise le cat de ASSET_PROFILES comme source de vérité)
# Fallback si pas trouvé dans ASSET_PROFILES
ASSET_CLASS_FALLBACK = {
    "BTCUSD": "crypto", "ETHUSD": "crypto", "XRPUSD": "crypto",
    "LTCUSD": "crypto", "ADAUSD": "crypto", "DOGEUSD": "crypto",
    "SOLUSD": "crypto", "DOTUSD": "crypto",
    "US500": "indices", "US100": "indices", "DE40": "indices",
    "UK100": "indices", "JP225": "indices", "FR40": "indices", "EU50": "indices",
    "GOLD": "commodities", "SILVER": "commodities", "COPPER": "commodities",
    "OIL_BRENT": "commodities", "OIL_WTI": "commodities", "NATGAS": "commodities",
}

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

# ─── TELEGRAM (removed — will be rebuilt later) ────────────────────────
# Channels and notification config removed.

# ─── LOGS ────────────────────────────────────────────────────────────────────
LOG_DIR   = "logs"
LOG_LEVEL = "INFO"
