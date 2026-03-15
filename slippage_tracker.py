"""
slippage_tracker.py — Tracking du slippage d'exécution MT5
Compare le prix calculé (théorique) vs le prix d'exécution réel.
Stocke l'écart en pips dans trades.slippage_pips.

Usage dans bot_signals.py après placement d'ordre :
    from slippage_tracker import SlippageTracker
    tracker = SlippageTracker()
    tracker.record(instrument, expected_entry, actual_fill, direction, trade_id)
"""
import os
from typing import Optional
from loguru import logger

# Points décimaux par instrument (hérite de config si disponible)
try:
    from config import PRICE_DECIMALS
except ImportError:
    PRICE_DECIMALS = {}

PIP_SIZE = {
    "forex":       0.0001,
    "jpy":         0.01,
    "gold":        0.1,
    "silver":      0.01,
    "crypto":      1.0,
    "indices":     1.0,
    "commodities": 0.01,
}


def _get_pip_size(instrument: str) -> float:
    sym = instrument.upper()
    if "JPY" in sym:
        return 0.01
    if sym in ("XAUUSD", "GOLD"):
        return 0.1
    if sym in ("BTCUSD", "ETHUSD"):
        return 1.0
    if any(x in sym for x in ["US500", "US30", "US100", "DE40", "UK100", "J225", "AU200"]):
        return 1.0
    return 0.0001


class SlippageTracker:
    """Mesure et enregistre le slippage entre prix théorique et prix d'exécution."""

    def __init__(self):
        self._db = None
        self._history: list = []  # [(instrument, slippage_pips)]

    def _get_db(self):
        if self._db is None:
            try:
                from database import get_db
                self._db = get_db()
            except Exception:
                pass
        return self._db

    def record(
        self,
        instrument: str,
        expected_price: float,
        actual_price: float,
        direction: str,
        trade_id: Optional[int] = None,
    ) -> float:
        """
        Enregistre le slippage pour un trade.
        Retourne le slippage en pips (positif = défavorable).
        """
        pip = _get_pip_size(instrument)
        if pip == 0:
            return 0.0

        # Slippage positif = exécution moins bonne que prévu
        if direction.upper() == "BUY":
            slippage_raw = actual_price - expected_price    # positif = payé plus cher
        else:
            slippage_raw = expected_price - actual_price    # positif = vendu moins cher

        slippage_pips = round(slippage_raw / pip, 1)
        self._history.append((instrument, slippage_pips))

        if abs(slippage_pips) > 2:
            logger.warning(
                f"⚠️ Slippage élevé {instrument} {direction}: "
                f"{slippage_pips:+.1f} pips "
                f"(prévu={expected_price:.5f} exécuté={actual_price:.5f})"
            )
        else:
            logger.debug(
                f"📏 Slippage {instrument}: {slippage_pips:+.1f} pips"
            )

        # Persister en DB si on a un trade_id
        if trade_id:
            self._save_to_db(trade_id, slippage_pips)

        return slippage_pips

    def _save_to_db(self, trade_id: int, slippage_pips: float):
        db = self._get_db()
        if not db:
            return
        try:
            ph = "%s" if db._pg else "?"
            db._execute(
                f"UPDATE trades SET slippage_pips={ph} WHERE id={ph}",
                (slippage_pips, trade_id)
            )
        except Exception as e:
            logger.debug(f"SlippageTracker._save_to_db: {e}")

    def avg_slippage(self, instrument: str = "") -> float:
        """Slippage moyen en pips (filtrable par instrument)."""
        history = self._history
        if instrument:
            history = [(i, s) for i, s in history if i == instrument]
        if not history:
            return 0.0
        return round(sum(s for _, s in history) / len(history), 2)

    def summary(self) -> str:
        if not self._history:
            return "📏 Aucun slippage enregistré"
        avg = self.avg_slippage()
        worst = max(self._history, key=lambda x: abs(x[1]))
        return (
            f"📏 **Slippage Tracker**\n"
            f"Moyennen : {avg:+.1f} pips\n"
            f"Pire : {worst[0]} {worst[1]:+.1f} pips\n"
            f"Trades mesurés : {len(self._history)}"
        )


# ─── Stub pour compatibilité bot_init.py ─────────────────────────────────────
# SlippageInjector était utilisé en mode DEMO pour dégrader le prix artificiellement.
# Remplacé par SlippageTracker (mesure réelle au lieu d'injection fictive).

class SlippageInjector:
    """Stub de compatibilité — remplacé par SlippageTracker en prod."""

    def apply_market_slippage(self, entry: float, direction: str, ob_imbalance: float = 0.5) -> float:
        """En mode LIVE on retourne l'entry sans modification (slippage mesuré après exécution)."""
        return entry
