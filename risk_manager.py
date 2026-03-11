"""
risk_manager.py — Gestion du risque avec système 3 TP + Break Even.

Logique :
  - 3 positions ouvertes simultanément (même taille)
  - TP1 → ferme pos1 → SL pos2+pos3 déplacé à BE (entry ±1pip)
  - TP2 → SL pos3 déplacé à TP1 (lock-in)
  - TP3 → dernier 1/3 fermé → trade terminé

Break Even (BE) : quand TP1 est touché, le SL est déplacé à entry ±1pip.
"""

from loguru import logger
from config import (
    RISK_PER_TRADE,
    MAX_OPEN_TRADES,
    DAILY_DRAWDOWN_LIMIT,
)


class RiskManager:
    """Contrôle d'accès au trading + compteurs de positions."""

    def __init__(self, initial_balance: float):
        self.initial_balance      = initial_balance
        self.daily_start_balance  = initial_balance
        self._open_trades_count   = 0
        self._open_instruments: set = set()

    # ─── CONTRÔLE D'ACCÈS ────────────────────────────────────────────────────

    def can_open_trade(self, current_balance: float, instrument: str = "") -> bool:
        if self._open_trades_count >= MAX_OPEN_TRADES:
            logger.warning(f"⛔ Max {MAX_OPEN_TRADES} trades simultanés atteint.")
            return False

        if instrument and instrument in self._open_instruments:
            logger.warning(f"⛔ {instrument} : trade déjà ouvert sur cet instrument.")
            return False

        drawdown = (current_balance - self.daily_start_balance) / self.daily_start_balance
        if drawdown <= DAILY_DRAWDOWN_LIMIT:
            logger.warning(f"⛔ Drawdown journalier atteint ({drawdown:.1%}). Bot en pause.")
            return False

        return True

    # ─── COMPTEURS ───────────────────────────────────────────────────────────

    def on_trade_opened(self, instrument: str = ""):
        self._open_trades_count += 1
        if instrument:
            self._open_instruments.add(instrument)

    def on_trade_closed(self, instrument: str = ""):
        self._open_trades_count = max(0, self._open_trades_count - 1)
        self._open_instruments.discard(instrument)

    def reset_daily(self, current_balance: float):
        self.daily_start_balance = current_balance
        logger.info(f"🔄 Balance journalière reset : {current_balance:.2f} €")

    @property
    def open_trades_count(self) -> int:
        return self._open_trades_count
