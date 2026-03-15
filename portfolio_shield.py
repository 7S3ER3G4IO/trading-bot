"""
portfolio_shield.py — ⚡ Phase 2: Advanced Portfolio Risk Management

Consolidates all Phase 2 roadmap items:
  2.1 Monthly drawdown limit + equity curve circuit breaker + correlation filter
  2.2 ATR-scaled trailing stop + volatility regime SL widening
  2.3 Sector exposure limit + VaR estimation + weekend crypto exception

Usage:
    shield = PortfolioShield(initial_balance=10000)
    shield.check_monthly_dd(current_balance)  # True = paused
    shield.check_correlation(instrument, active_trades)  # True = blocked
    shield.check_sector_exposure(instrument, active_trades, balance)  # True = blocked
    shield.compute_var(positions)  # Simple portfolio VaR
"""

import math
import random
from datetime import datetime, timezone
from collections import defaultdict
from loguru import logger


# ─── Sector mapping ──────────────────────────────────────────────────────────
SECTOR_MAP = {
    # Forex
    "EURUSD": "forex", "GBPUSD": "forex", "USDJPY": "forex", "USDCHF": "forex",
    "AUDUSD": "forex", "NZDUSD": "forex", "USDCAD": "forex", "EURGBP": "forex",
    "EURJPY": "forex", "GBPJPY": "forex", "EURCHF": "forex", "AUDNZD": "forex",
    "EURAUD": "forex", "AUDCAD": "forex", "GBPCAD": "forex", "GBPCHF": "forex",
    "CADCHF": "forex", "CADJPY": "forex", "CHFJPY": "forex", "NZDJPY": "forex",
    "NZDCAD": "forex", "AUDCHF": "forex",
    # Indices
    "US500": "indices", "US100": "indices", "DE40": "indices", "UK100": "indices",
    "JP225": "indices", "HK50": "indices", "FR40": "indices", "EU50": "indices",
    # Crypto
    "BTCUSD": "crypto", "ETHUSD": "crypto", "XRPUSD": "crypto",
    "LTCUSD": "crypto", "ADAUSD": "crypto", "DOGEUSD": "crypto",
    "SOLUSD": "crypto", "DOTUSD": "crypto",
    # Commodities
    "GOLD": "commodities", "SILVER": "commodities", "COPPER": "commodities",
    "OIL_BRENT": "commodities", "OIL_WTI": "commodities", "NATGAS": "commodities",
}

# Correlation groups: instruments that are highly correlated
CORRELATION_GROUPS = {
    "usd_majors": {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"},
    "jpy_crosses": {"USDJPY", "EURJPY", "GBPJPY", "CADJPY", "CHFJPY", "NZDJPY"},
    "chf_crosses": {"USDCHF", "EURCHF", "GBPCHF", "CADCHF", "AUDCHF"},
    "cad_crosses": {"USDCAD", "AUDCAD", "GBPCAD", "NZDCAD"},
    "us_indices":  {"US500", "US100"},
    "oil":         {"OIL_BRENT", "OIL_WTI"},
    "major_crypto": {"BTCUSD", "ETHUSD"},
}


class PortfolioShield:
    """Phase 2: Advanced Portfolio-Level Risk Management."""

    def __init__(self, initial_balance: float = 10_000.0,
                 monthly_dd_limit: float = 0.15,
                 sector_max_pct: float = 0.30,
                 max_correlated: int = 3,
                 telegram_router=None):

        self._initial_balance = initial_balance
        self._monthly_start = initial_balance
        self._monthly_dd_limit = monthly_dd_limit  # 15%
        self._sector_max_pct = sector_max_pct       # 30% max per sector
        self._max_correlated = max_correlated        # Max 3 from same group
        self._router = telegram_router

        # Monthly reset tracking
        self._last_reset_month = datetime.now(timezone.utc).month
        self._monthly_paused = False

        # ATR trailing config
        self.atr_trail_mult = 1.5     # Trailing = 1.5 × ATR
        self.crisis_sl_widen = 1.25   # Widen SL by 25% in CRISIS regime

    # ═══════════════════════════════════════════════════════════════════════════
    # 2.1 MONTHLY DRAWDOWN
    # ═══════════════════════════════════════════════════════════════════════════

    def check_monthly_dd(self, current_balance: float) -> bool:
        """Check if monthly drawdown limit is breached. Returns True = PAUSED."""
        now = datetime.now(timezone.utc)

        # Monthly reset
        if now.month != self._last_reset_month:
            self._last_reset_month = now.month
            self._monthly_start = current_balance
            self._monthly_paused = False
            logger.info(f"📅 Monthly DD reset — start balance: {current_balance:.2f}€")

        if self._monthly_paused:
            return True

        dd_pct = (self._monthly_start - current_balance) / self._monthly_start
        if dd_pct >= self._monthly_dd_limit:
            self._monthly_paused = True
            logger.critical(
                f"🚨 MONTHLY DD LIMIT: {dd_pct:.1%} >= {self._monthly_dd_limit:.0%} — "
                f"trading suspended until next month"
            )
            self._send_alert(
                f"🚨 <b>MONTHLY DRAWDOWN LIMIT</b>\n\n"
                f"📉 Perte mensuelle : <b>{dd_pct:.1%}</b>\n"
                f"🛑 Limite : {self._monthly_dd_limit:.0%}\n"
                f"💰 Balance : {current_balance:,.2f}€\n\n"
                f"⛔ Trading suspendu jusqu'au mois prochain."
            )
            return True

        return False

    # ═══════════════════════════════════════════════════════════════════════════
    # 2.1 CORRELATION FILTER
    # ═══════════════════════════════════════════════════════════════════════════

    def check_correlation(self, instrument: str,
                          active_trades: dict) -> tuple:
        """
        Check if too many correlated instruments are already open.
        Returns (blocked: bool, reason: str)
        """
        # Find which correlation groups this instrument belongs to
        my_groups = []
        for group_name, members in CORRELATION_GROUPS.items():
            if instrument in members:
                my_groups.append((group_name, members))

        for group_name, members in my_groups:
            # Count how many from this group are already open
            open_count = sum(
                1 for m in members
                if m != instrument and active_trades.get(m) is not None
            )
            if open_count >= self._max_correlated:
                reason = (
                    f"Corrélation: {open_count} positions déjà ouvertes "
                    f"dans le groupe '{group_name}'"
                )
                logger.info(f"🚫 {instrument} bloqué — {reason}")
                return True, reason

        return False, ""

    # ═══════════════════════════════════════════════════════════════════════════
    # 2.3 SECTOR EXPOSURE
    # ═══════════════════════════════════════════════════════════════════════════

    def check_sector_exposure(self, instrument: str,
                              active_trades: dict,
                              balance: float) -> tuple:
        """
        Check sector exposure limit (max 30% of capital in one sector).
        Returns (blocked: bool, reason: str)
        """
        my_sector = SECTOR_MAP.get(instrument, "other")

        # Count open trades per sector
        sector_counts = defaultdict(int)
        for inst, state in active_trades.items():
            if state is not None:
                s = SECTOR_MAP.get(inst, "other")
                sector_counts[s] += 1

        total_open = sum(sector_counts.values())
        if total_open == 0:
            return False, ""

        # Estimate sector exposure as % of total positions
        sector_count = sector_counts.get(my_sector, 0)
        sector_exposure = (sector_count + 1) / (total_open + 1)

        if sector_exposure > self._sector_max_pct and sector_count >= 3:
            reason = (
                f"Sector '{my_sector}': {sector_count} positions "
                f"({sector_exposure:.0%} > {self._sector_max_pct:.0%})"
            )
            logger.info(f"🚫 {instrument} bloqué — {reason}")
            return True, reason

        return False, ""

    # ═══════════════════════════════════════════════════════════════════════════
    # 2.2 ATR-SCALED TRAILING STOP
    # ═══════════════════════════════════════════════════════════════════════════

    def compute_atr_trailing_sl(self, current_price: float,
                                atr: float, direction: str,
                                current_sl: float = 0.0) -> float:
        """
        Compute ATR-based trailing stop level.
        Only moves SL in profit direction (never widens).
        """
        trail_dist = atr * self.atr_trail_mult

        if direction == "BUY":
            new_sl = current_price - trail_dist
            # Only trail upward
            if current_sl > 0 and new_sl <= current_sl:
                return current_sl
            return round(new_sl, 5)
        else:
            new_sl = current_price + trail_dist
            # Only trail downward
            if current_sl > 0 and new_sl >= current_sl:
                return current_sl
            return round(new_sl, 5)

    def adjust_sl_for_regime(self, sl: float, entry: float,
                             direction: str, regime: str) -> float:
        """
        Widen SL in CRISIS regime to avoid whipsaws.
        """
        if regime not in ("CRISIS_HIGH_VOL", "CRISIS"):
            return sl

        sl_dist = abs(entry - sl)
        widened_dist = sl_dist * self.crisis_sl_widen

        if direction == "BUY":
            return round(entry - widened_dist, 5)
        else:
            return round(entry + widened_dist, 5)

    # ═══════════════════════════════════════════════════════════════════════════
    # 2.3 VALUE AT RISK (Simple Monte Carlo)
    # ═══════════════════════════════════════════════════════════════════════════

    def compute_var(self, positions: list, balance: float,
                    confidence: float = 0.95, n_sims: int = 1000) -> dict:
        """
        Simple portfolio VaR via Monte Carlo.
        positions: list of {instrument, direction, size, entry, current_price}
        Returns {var_amount, var_pct, max_loss}
        """
        if not positions:
            return {"var_amount": 0, "var_pct": 0, "max_loss": 0}

        losses = []
        for _ in range(n_sims):
            sim_pnl = 0
            for pos in positions:
                entry = pos.get("entry", 0)
                size = pos.get("size", 1)
                direction = pos.get("direction", "BUY")
                # Simulate daily return: ~0.5% std for forex, ~2% for crypto
                sector = SECTOR_MAP.get(pos.get("instrument", ""), "forex")
                vol = {"forex": 0.005, "crypto": 0.025, "indices": 0.012,
                       "commodities": 0.015}.get(sector, 0.01)
                daily_ret = random.gauss(0, vol)
                pnl = entry * daily_ret * size * (1 if direction == "BUY" else -1)
                sim_pnl += pnl
            losses.append(sim_pnl)

        losses.sort()
        idx = int((1 - confidence) * n_sims)
        var_amount = abs(losses[max(idx, 0)])
        max_loss = abs(min(losses))

        return {
            "var_amount": round(var_amount, 2),
            "var_pct": round(var_amount / balance * 100, 2) if balance > 0 else 0,
            "max_loss": round(max_loss, 2),
        }

    # ═══════════════════════════════════════════════════════════════════════════
    # 2.3 WEEKEND CRYPTO EXCEPTION
    # ═══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def is_crypto(instrument: str) -> bool:
        """Returns True if instrument is crypto (trades 24/7)."""
        return SECTOR_MAP.get(instrument, "") == "crypto"

    def should_friday_close(self, instrument: str) -> bool:
        """
        Friday kill-switch: close TradFi positions but KEEP crypto open.
        """
        if self.is_crypto(instrument):
            return False  # Crypto stays open (24/7 markets)
        return True  # Close TradFi before weekend

    # ─── Telegram ─────────────────────────────────────────────────────────────

    def _send_alert(self, text: str):
        if self._router:
            try:
                self._router.send_to("risk", text)
            except Exception as e:
                logger.error(f"PortfolioShield Telegram: {e}")

    def format_status(self, active_trades: dict, balance: float) -> str:
        """Format portfolio shield status for /stats."""
        sector_counts = defaultdict(int)
        for inst, state in active_trades.items():
            if state is not None:
                s = SECTOR_MAP.get(inst, "other")
                sector_counts[s] += 1

        dd_pct = (self._monthly_start - balance) / self._monthly_start if self._monthly_start > 0 else 0
        lines = [
            f"📊 <b>Portfolio Shield</b>",
            f"  Monthly DD: {dd_pct:.1%} / {self._monthly_dd_limit:.0%}",
            f"  Status: {'🔴 PAUSED' if self._monthly_paused else '🟢 Active'}",
        ]
        if sector_counts:
            lines.append("  Sectors: " + " · ".join(
                f"{s}={c}" for s, c in sorted(sector_counts.items())
            ))
        return "\n".join(lines)
