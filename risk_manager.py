"""
risk_manager.py — Gestion du risque : taille de position, SL/TP, drawdown maximal.
"""

from loguru import logger
from config import (
    RISK_PER_TRADE,
    ATR_SL_MULTIPLIER,
    RR_RATIO,
    MAX_OPEN_TRADES,
    DAILY_DRAWDOWN_LIMIT,
)


class RiskManager:
    """Calcule les paramètres de risque pour chaque trade."""

    def __init__(self, initial_balance: float):
        self.initial_balance   = initial_balance
        self.daily_start_balance = initial_balance
        self._open_trades_count  = 0

    # ─── CONTRÔLE D'ACCÈS ────────────────────────────────────────────────────

    def can_open_trade(self, current_balance: float) -> bool:
        """
        Vérifie si un nouveau trade peut être ouvert.
        Conditions :
          1. Nombre de trades ouverts < MAX_OPEN_TRADES
          2. Drawdown journalier non atteint
        """
        if self._open_trades_count >= MAX_OPEN_TRADES:
            logger.warning(
                f"⛔ Trade bloqué — maximum de {MAX_OPEN_TRADES} trades simultanés atteint."
            )
            return False

        drawdown = (current_balance - self.daily_start_balance) / self.daily_start_balance
        if drawdown <= DAILY_DRAWDOWN_LIMIT:
            logger.warning(
                f"⛔ Trade bloqué — drawdown journalier atteint ({drawdown:.1%}). "
                f"Limite : {DAILY_DRAWDOWN_LIMIT:.1%}"
            )
            return False

        return True

    # ─── CALCULS ─────────────────────────────────────────────────────────────

    def position_size(self, balance: float, entry_price: float, stop_loss_price: float) -> float:
        """
        Calcule la taille de la position (en unités de la cryptomonnaie).

        Formule :
          capital_risqué  = balance × RISK_PER_TRADE
          distance_sl     = |entry_price - stop_loss_price|
          taille          = capital_risqué / distance_sl
        """
        capital_at_risk  = balance * RISK_PER_TRADE
        sl_distance      = abs(entry_price - stop_loss_price)

        if sl_distance == 0:
            logger.error("❌ Distance Stop-Loss = 0, impossible de calculer la taille.")
            return 0.0

        size = capital_at_risk / sl_distance
        logger.info(
            f"📐 Position size : {size:.6f} BTC | "
            f"Capital risqué : {capital_at_risk:.2f} USDT | "
            f"Distance SL : {sl_distance:.2f} USDT"
        )
        return size

    def calculate_sl_tp(
        self, entry_price: float, atr: float, side: str
    ) -> tuple[float, float]:
        """
        Calcule le Stop-Loss et le Take-Profit basés sur l'ATR.

        Returns:
            (stop_loss_price, take_profit_price)
        """
        sl_distance = atr * ATR_SL_MULTIPLIER
        tp_distance = sl_distance * RR_RATIO

        if side == "BUY":
            stop_loss   = entry_price - sl_distance
            take_profit = entry_price + tp_distance
        else:  # SELL
            stop_loss   = entry_price + sl_distance
            take_profit = entry_price - tp_distance

        logger.info(
            f"🎯 {side} | Entry={entry_price:.2f} | "
            f"SL={stop_loss:.2f} (-{sl_distance:.2f}) | "
            f"TP={take_profit:.2f} (+{tp_distance:.2f}) | "
            f"R:R = 1:{RR_RATIO}"
        )
        return round(stop_loss, 2), round(take_profit, 2)

    # ─── COMPTEURS ───────────────────────────────────────────────────────────

    def on_trade_opened(self):
        self._open_trades_count += 1
        logger.debug(f"📈 Trades ouverts : {self._open_trades_count}/{MAX_OPEN_TRADES}")

    def on_trade_closed(self):
        self._open_trades_count = max(0, self._open_trades_count - 1)
        logger.debug(f"📉 Trades ouverts : {self._open_trades_count}/{MAX_OPEN_TRADES}")

    def reset_daily(self, current_balance: float):
        """Réinitialise le tracker journalier (à appeler chaque jour à minuit)."""
        self.daily_start_balance = current_balance
        logger.info(f"🔄 Balance journalière réinitialisée : {current_balance:.2f} USDT")

    @property
    def open_trades_count(self) -> int:
        return self._open_trades_count
