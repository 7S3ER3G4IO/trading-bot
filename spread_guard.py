"""
spread_guard.py — ⚡ Tâche 2: Pre-Trade Spread Widening Filter

Avant chaque exécution d'ordre, vérifie que le spread bid-ask est dans les
limites acceptables. Si le spread est trop large (news, illiquidité, nuit),
l'ordre est rejeté pour éviter le slippage.

Usage:
    guard = SpreadGuard(capital_client)
    ok, spread_pct, reason = guard.check(instrument, entry_price)
    if not ok:
        logger.warning(f"Trade rejected: {reason}")
"""

from loguru import logger


# ─── Spread tolerance par classe d'actif (% du prix) ────────────────────────
# Au-delà de ces seuils, l'ordre est refusé
MAX_SPREAD_TOLERANCE = {
    "forex":        0.0005,   # 0.05% (≈ 5 pips sur EUR/USD @ 1.08)
    "indices":      0.0010,   # 0.10%
    "commodities":  0.0012,   # 0.12%
    "crypto":       0.0030,   # 0.30% (spreads larges sur crypto CFD)
    "stocks":       0.0020,   # 0.20%
}

# Mapping instrument → classe (fallback)
_ASSET_CLASS = {
    "BTCUSD": "crypto", "ETHUSD": "crypto", "XRPUSD": "crypto",
    "LTCUSD": "crypto", "ADAUSD": "crypto", "DOGEUSD": "crypto",
    "SOLUSD": "crypto", "DOTUSD": "crypto",
    "US500": "indices", "US100": "indices", "DE40": "indices",
    "UK100": "indices", "JP225": "indices", "FR40": "indices", "EU50": "indices",
    "GOLD": "commodities", "SILVER": "commodities", "COPPER": "commodities",
    "OIL_BRENT": "commodities", "OIL_WTI": "commodities", "NATGAS": "commodities",
}


class SpreadGuard:
    """Pre-trade spread validation."""

    def __init__(self, capital_client=None):
        self._capital = capital_client
        self._rejections = 0
        self._total_checks = 0

    def check(self, instrument: str, entry_price: float = 0.0) -> tuple:
        """
        Vérifie le spread actuel pour un instrument.

        Returns
        -------
        (ok: bool, spread_pct: float, reason: str)
          - ok=True → spread acceptable, trade autorisé
          - ok=False → spread trop large, trade rejeté
        """
        self._total_checks += 1

        # Get live bid/ask from broker
        bid, ask = self._get_bid_ask(instrument)
        if bid <= 0 or ask <= 0:
            # Can't read spread → fail-open (let trade through)
            return True, 0.0, "spread_unknown"

        spread = ask - bid
        mid_price = (bid + ask) / 2
        spread_pct = spread / mid_price if mid_price > 0 else 0

        # Determine asset class
        asset_class = self._get_class(instrument)
        max_spread = MAX_SPREAD_TOLERANCE.get(asset_class, 0.0010)

        if spread_pct > max_spread:
            self._rejections += 1
            reason = (
                f"Spread trop large: {spread_pct:.4%} > {max_spread:.4%} "
                f"({asset_class}) | bid={bid:.5f} ask={ask:.5f} spread={spread:.5f}"
            )
            logger.warning(f"🚫 SPREAD GUARD {instrument}: {reason}")
            return False, spread_pct, reason

        logger.debug(
            f"✅ Spread OK {instrument}: {spread_pct:.4%} ≤ {max_spread:.4%} "
            f"({asset_class}) | bid={bid:.5f} ask={ask:.5f}"
        )
        return True, spread_pct, "ok"

    def _get_bid_ask(self, instrument: str) -> tuple:
        """Get live bid/ask from Capital.com."""
        if not self._capital or not self._capital.available:
            return 0.0, 0.0
        try:
            # Capital.com: GET /markets/{epic}
            data = self._capital.get_market_info(instrument)
            if data:
                snap = data.get("snapshot", data)
                bid = float(snap.get("bid", 0))
                ask = float(snap.get("offer", snap.get("ask", 0)))
                return bid, ask
        except Exception as e:
            logger.debug(f"SpreadGuard bid/ask {instrument}: {e}")
        return 0.0, 0.0

    def _get_class(self, instrument: str) -> str:
        """Determine asset class for an instrument."""
        if instrument in _ASSET_CLASS:
            return _ASSET_CLASS[instrument]
        try:
            from brokers.capital_client import ASSET_PROFILES
            profile = ASSET_PROFILES.get(instrument, {})
            return profile.get("cat", "forex")
        except Exception:
            return "forex"

    @property
    def stats(self) -> dict:
        return {
            "total_checks": self._total_checks,
            "rejections": self._rejections,
            "rejection_rate": round(
                self._rejections / self._total_checks * 100, 1
            ) if self._total_checks > 0 else 0,
        }
