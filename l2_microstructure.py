"""
l2_microstructure.py — ⚡ APEX PREDATOR T1: Order Book Microstructure

Analyse le carnet d'ordres (Bid/Ask depth) pour détecter les murs
de liquidité et bloquer les trades contre-courant.

Capital.com ne fournit pas de L2 via WebSocket, donc ce module
utilise le spread bid/ask + volume profile comme proxy.

Fonctionnement:
  1. Récupère bid/ask live via REST API
  2. Calcule l'Order Book Imbalance (OBI) sur un historique glissant
  3. Détecte les murs (wall > 70% imbalance)
  4. Bloque les trades qui vont contre le mur

Usage dans bot_signals.py:
    allowed, reason = self.l2.check_entry(instrument, direction, df)
    if not allowed:
        logger.info(f"🧱 L2 Rejection: {reason}")
        return
"""

import time
import threading
from datetime import datetime, timezone
from collections import defaultdict, deque
from loguru import logger
import numpy as np


# ─── Configuration ────────────────────────────────────────────────────────────
IMBALANCE_WALL_THRESHOLD = 0.70   # 70% = mur détecté
IMBALANCE_HISTORY_SIZE   = 30     # Historique glissant (ticks)
REFRESH_INTERVAL_S       = 10     # Rafraîchissement en secondes
MIN_SPREAD_SAMPLES       = 5      # Minimum d'échantillons avant décision


# ─── Correlation map for spread-proxy L2 ─────────────────────────────────────
# Spread widening = ask pressure; narrowing = bid support
# Volume spike on one side = directional pressure


class L2Microstructure:
    """
    Order Book Microstructure Analyzer.

    Simule l'analyse L2 via les données bid/ask de Capital.com.
    Détecte les déséquilibres (imbalance) et bloque les trades
    qui vont contre un mur de liquidité.
    """

    def __init__(self, capital_client=None, telegram_router=None):
        self._capital = capital_client
        self._router = telegram_router

        # Historique bid/ask par instrument
        self._bid_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=IMBALANCE_HISTORY_SIZE)
        )
        self._ask_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=IMBALANCE_HISTORY_SIZE)
        )
        self._spread_history: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=IMBALANCE_HISTORY_SIZE)
        )
        self._volume_profile: dict[str, dict] = {}  # instrument → {buy_vol, sell_vol}

        # Stats
        self._checks = 0
        self._rejections = 0
        self._walls_detected = 0

    # ═══════════════════════════════════════════════════════════════════════
    #  CORE: Snapshot bid/ask and compute imbalance
    # ═══════════════════════════════════════════════════════════════════════

    def snapshot(self, instrument: str) -> dict:
        """
        Capture un snapshot bid/ask de Capital.com.

        Returns:
            {"bid": float, "ask": float, "spread": float, "mid": float,
             "imbalance": float (-1 to +1), "wall": str|None}
        """
        result = {
            "bid": 0, "ask": 0, "spread": 0, "mid": 0,
            "imbalance": 0.0, "wall": None, "timestamp": time.time(),
        }

        if not self._capital or not self._capital.available:
            return result

        try:
            px = self._capital.get_current_price(instrument)
            if not px:
                return result

            bid = float(px.get("bid", 0))
            ask = float(px.get("ask", 0))
            mid = float(px.get("mid", (bid + ask) / 2 if bid and ask else 0))
            spread = abs(ask - bid)

            result["bid"] = bid
            result["ask"] = ask
            result["mid"] = mid
            result["spread"] = spread

            # Store history
            self._bid_history[instrument].append(bid)
            self._ask_history[instrument].append(ask)
            self._spread_history[instrument].append(spread)

            # Compute imbalance from spread dynamics
            result["imbalance"] = self._compute_imbalance(instrument)
            result["wall"] = self._detect_wall(instrument, result["imbalance"])

            return result

        except Exception as e:
            logger.debug(f"L2 snapshot {instrument}: {e}")
            return result

    def _compute_imbalance(self, instrument: str) -> float:
        """
        Compute Order Book Imbalance (OBI) from bid/ask history.

        OBI = (bid_pressure - ask_pressure) / (bid_pressure + ask_pressure)
        Range: -1.0 (sellers dominating) to +1.0 (buyers dominating)

        Proxy approach (no L2 data):
        - Compare bid/ask movements vs midpoint
        - Spread narrowing when price rises = buying pressure
        - Spread widening when price falls = selling pressure
        """
        bids = list(self._bid_history[instrument])
        asks = list(self._ask_history[instrument])
        spreads = list(self._spread_history[instrument])

        if len(bids) < MIN_SPREAD_SAMPLES:
            return 0.0

        bids_arr = np.array(bids)
        asks_arr = np.array(asks)
        spreads_arr = np.array(spreads)

        # Method 1: Bid/Ask movement ratio
        # If bids are rising faster than asks → buying pressure
        bid_changes = np.diff(bids_arr)
        ask_changes = np.diff(asks_arr)

        if len(bid_changes) == 0:
            return 0.0

        # Positive = bid rising more = buy pressure
        # Negative = ask rising more = sell pressure
        bid_strength = np.sum(np.maximum(bid_changes, 0))
        ask_strength = np.sum(np.maximum(ask_changes, 0))
        bid_weakness = np.sum(np.abs(np.minimum(bid_changes, 0)))
        ask_weakness = np.sum(np.abs(np.minimum(ask_changes, 0)))

        buy_pressure = bid_strength + ask_weakness  # Bids up + Asks retreating
        sell_pressure = ask_strength + bid_weakness  # Asks up + Bids retreating

        total = buy_pressure + sell_pressure
        if total < 1e-10:
            return 0.0

        obi = (buy_pressure - sell_pressure) / total

        # Method 2: Spread trend (narrowing = confidence, widening = fear)
        if len(spreads) >= 5:
            recent_spread = np.mean(spreads_arr[-5:])
            older_spread = np.mean(spreads_arr[:5]) if len(spreads_arr) > 5 else recent_spread
            if older_spread > 0:
                spread_trend = (older_spread - recent_spread) / older_spread
                # Blend: 70% price-based OBI, 30% spread trend
                obi = 0.7 * obi + 0.3 * np.clip(spread_trend, -1, 1)

        return float(np.clip(obi, -1.0, 1.0))

    def _detect_wall(self, instrument: str, imbalance: float) -> str | None:
        """Detect buy/sell walls from imbalance."""
        if imbalance >= IMBALANCE_WALL_THRESHOLD:
            self._walls_detected += 1
            return "BUY_WALL"   # Strong buying pressure (wall of bids)
        elif imbalance <= -IMBALANCE_WALL_THRESHOLD:
            self._walls_detected += 1
            return "SELL_WALL"  # Strong selling pressure (wall of asks)
        return None

    # ═══════════════════════════════════════════════════════════════════════
    #  GATE: Check if entry is safe
    # ═══════════════════════════════════════════════════════════════════════

    def check_entry(self, instrument: str, direction: str,
                    df=None) -> tuple[bool, str]:
        """
        Pre-trade gate: check Order Book Imbalance.

        Returns (allowed: bool, reason: str)
        """
        self._checks += 1

        snap = self.snapshot(instrument)

        # Not enough data yet → fail-open (allow trade)
        hist_len = len(self._bid_history[instrument])
        if hist_len < MIN_SPREAD_SAMPLES:
            return True, f"L2 warmup ({hist_len}/{MIN_SPREAD_SAMPLES})"

        imbalance = snap["imbalance"]
        wall = snap["wall"]

        # Volume profile from OHLCV (proxy for order flow)
        vol_bias = 0.0
        if df is not None and len(df) >= 5:
            try:
                last5 = df.tail(5)
                buy_vol = float(last5[last5["close"] > last5["open"]]["volume"].sum())
                sell_vol = float(last5[last5["close"] <= last5["open"]]["volume"].sum())
                total_vol = buy_vol + sell_vol
                if total_vol > 0:
                    vol_bias = (buy_vol - sell_vol) / total_vol
                    self._volume_profile[instrument] = {
                        "buy_vol": buy_vol, "sell_vol": sell_vol,
                        "bias": vol_bias,
                    }
            except Exception:
                pass

        # Decision matrix
        # BUY signal + SELL_WALL → BLOCK (buying into sellers)
        if direction == "BUY" and wall == "SELL_WALL":
            self._rejections += 1
            reason = (
                f"L2 Rejection: Sell Wall detected | "
                f"OBI={imbalance:+.2f} ({abs(imbalance):.0%} ask dominance) | "
                f"Vol bias={vol_bias:+.2f}"
            )
            logger.warning(f"🧱 {instrument} {reason}")
            self._send_alert(instrument, direction, reason)
            return False, reason

        # SELL signal + BUY_WALL → BLOCK (selling into buyers)
        if direction == "SELL" and wall == "BUY_WALL":
            self._rejections += 1
            reason = (
                f"L2 Rejection: Buy Wall detected | "
                f"OBI={imbalance:+.2f} ({abs(imbalance):.0%} bid dominance) | "
                f"Vol bias={vol_bias:+.2f}"
            )
            logger.warning(f"🧱 {instrument} {reason}")
            self._send_alert(instrument, direction, reason)
            return False, reason

        # Wall aligned with direction → BOOST confidence (log only)
        if (direction == "BUY" and wall == "BUY_WALL") or \
           (direction == "SELL" and wall == "SELL_WALL"):
            logger.info(
                f"🟢 L2 Confirmed: {instrument} {direction} aligned with {wall} "
                f"| OBI={imbalance:+.2f}"
            )

        return True, f"L2 OK (OBI={imbalance:+.2f})"

    def update_volume_profile(self, instrument: str, df) -> dict:
        """Update volume profile from OHLCV data."""
        if df is None or len(df) < 10:
            return {}
        try:
            last10 = df.tail(10)
            buy_candles = last10[last10["close"] > last10["open"]]
            sell_candles = last10[last10["close"] <= last10["open"]]

            buy_vol = float(buy_candles["volume"].sum())
            sell_vol = float(sell_candles["volume"].sum())
            total = buy_vol + sell_vol

            profile = {
                "buy_vol": buy_vol,
                "sell_vol": sell_vol,
                "bias": (buy_vol - sell_vol) / total if total > 0 else 0,
                "buy_count": len(buy_candles),
                "sell_count": len(sell_candles),
            }
            self._volume_profile[instrument] = profile
            return profile
        except Exception:
            return {}

    # ─── Status ──────────────────────────────────────────────────────────

    def format_status(self) -> str:
        return (
            f"🧱 <b>L2 Microstructure</b>\n"
            f"  📊 Checks: {self._checks}\n"
            f"  ⛔ Rejections: {self._rejections}\n"
            f"  🧱 Walls detected: {self._walls_detected}"
        )

    @property
    def stats(self) -> dict:
        return {
            "checks": self._checks,
            "rejections": self._rejections,
            "walls_detected": self._walls_detected,
            "instruments_tracked": len(self._bid_history),
        }

    def _send_alert(self, instrument: str, direction: str, reason: str):
        if self._router:
            try:
                self._router.send_to("risk",
                    f"🧱 <b>L2 WALL BLOCK</b>\n\n"
                    f"📊 {instrument} {direction}\n"
                    f"📋 {reason}"
                )
            except Exception:
                pass
