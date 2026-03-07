"""
risk_manager.py — Gestion du risque avec système 3 TP + Break Even.

Logique :
  - TP1 = SL_distance × 1.0  → ferme 1/3 de la position → SL déplacé à BE
  - TP2 = SL_distance × 2.0  → ferme 1/3 de la position → SL reste à BE
  - TP3 = SL_distance × 3.0  → ferme le dernier 1/3 → trade terminé

Break Even (BE) : quand TP1 est touché, le SL est déplacé au prix d'entrée.
Cela rend les targets TP2 et TP3 SANS RISQUE.
"""

from loguru import logger
from config import (
    RISK_PER_TRADE,
    ATR_SL_MULTIPLIER,
    MAX_OPEN_TRADES,
    DAILY_DRAWDOWN_LIMIT,
)

# Ratios des 3 TPs par rapport à la distance SL
# R:R amélioré basé sur backtesting : TP larges pour compenser le win rate
TP1_RATIO = 1.5   # TP1 = SL × 1.5  (1:1.5 R:R)
TP2_RATIO = 3.0   # TP2 = SL × 3.0  (1:3.0 R:R)
TP3_RATIO = 5.0   # TP3 = SL × 5.0  (1:5.0 R:R)

# Fraction de la position fermée à chaque TP
TP_FRACTIONS = [1/3, 1/2, 1.0]  # 33% au TP1, 50% du reste au TP2, tout au TP3


class RiskManager:
    """Calcule les paramètres de risque avec 3 niveaux de Take-Profit."""

    def __init__(self, initial_balance: float):
        self.initial_balance      = initial_balance
        self.daily_start_balance  = initial_balance
        self._open_trades_count   = 0

    # ─── CONTRÔLE D'ACCÈS ────────────────────────────────────────────────────

    def can_open_trade(self, current_balance: float) -> bool:
        if self._open_trades_count >= MAX_OPEN_TRADES:
            logger.warning(f"⛔ Max {MAX_OPEN_TRADES} trades simultanés atteint.")
            return False

        drawdown = (current_balance - self.daily_start_balance) / self.daily_start_balance
        if drawdown <= DAILY_DRAWDOWN_LIMIT:
            logger.warning(f"⛔ Drawdown journalier atteint ({drawdown:.1%}). Bot en pause.")
            return False

        return True

    # ─── CALCULS SL / 3 TP ───────────────────────────────────────────────────

    def calculate_levels(
        self, entry_price: float, atr: float, side: str
    ) -> dict:
        """
        Calcule SL et les 3 niveaux de TP.

        Returns:
            dict avec sl, tp1, tp2, tp3, sl_distance
        """
        sl_distance = atr * ATR_SL_MULTIPLIER

        if side == "BUY":
            sl  = entry_price - sl_distance
            tp1 = entry_price + sl_distance * TP1_RATIO
            tp2 = entry_price + sl_distance * TP2_RATIO
            tp3 = entry_price + sl_distance * TP3_RATIO
        else:  # SELL
            sl  = entry_price + sl_distance
            tp1 = entry_price - sl_distance * TP1_RATIO
            tp2 = entry_price - sl_distance * TP2_RATIO
            tp3 = entry_price - sl_distance * TP3_RATIO

        levels = {
            "sl":          round(sl,  2),
            "tp1":         round(tp1, 2),
            "tp2":         round(tp2, 2),
            "tp3":         round(tp3, 2),
            "sl_distance": round(sl_distance, 2),
            "be":          round(entry_price, 2),   # Break Even = entrée
        }

        logger.info(
            f"📐 {side} @ {entry_price:.2f} | "
            f"SL={levels['sl']:.2f} | "
            f"TP1={levels['tp1']:.2f} | "
            f"TP2={levels['tp2']:.2f} | "
            f"TP3={levels['tp3']:.2f}"
        )
        return levels

    def position_size(self, balance: float, entry_price: float, sl_price: float) -> float:
        """Taille de la position totale (en unités crypto)."""
        capital_at_risk = balance * RISK_PER_TRADE
        sl_distance     = abs(entry_price - sl_price)

        if sl_distance == 0:
            logger.error("❌ Distance SL = 0.")
            return 0.0

        size = capital_at_risk / sl_distance
        logger.info(
            f"📦 Taille position : {size:.6f} BTC | "
            f"Capital risqué : {capital_at_risk:.2f} USDT"
        )
        return size

    # ─── COMPTEURS ───────────────────────────────────────────────────────────

    def on_trade_opened(self):
        self._open_trades_count += 1

    def on_trade_closed(self):
        self._open_trades_count = max(0, self._open_trades_count - 1)

    def reset_daily(self, current_balance: float):
        self.daily_start_balance = current_balance
        logger.info(f"🔄 Balance journalière reset : {current_balance:.2f} USDT")

    @property
    def open_trades_count(self) -> int:
        return self._open_trades_count
