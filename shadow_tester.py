"""
shadow_tester.py — 🔬 PROJECT PROMETHEUS T2: Ghost Backtest Lab

Moteur de backtest ultra-léger en mémoire (<5 secondes).
Teste des mutations de paramètres sur des données récentes.

Usage:
    tester = ShadowTester()
    result = tester.backtest(
        df=ohlcv_data,       # DataFrame avec OHLCV + indicators
        params={             # Paramètres de la stratégie
            "strat": "MR",
            "rsi_lo": 25,
            "rsi_hi": 75,
            "sl_buffer": 0.10,
            "tp1": 1.5,
        },
        direction_bias=None, # None = both, "BUY" or "SELL"
    )
    print(result["sharpe"], result["win_rate"], result["total_pnl"])
"""

import time
import math
from loguru import logger
import numpy as np

try:
    import pandas as pd
    _PD_OK = True
except ImportError:
    _PD_OK = False


class ShadowTester:
    """
    Ghost Backtest Lab — backtests strategy mutations in-memory.
    Designed for speed: <5 seconds per instrument per mutation.
    """

    def __init__(self):
        self._tests_run = 0
        self._total_time = 0.0

    def backtest(self, df, params: dict, direction_bias: str = None) -> dict:
        """
        Run a fast in-memory backtest.

        Parameters:
            df: DataFrame with OHLCV + indicators (rsi, atr, adx, bb_lo, bb_up, etc.)
            params: strategy parameters to test
            direction_bias: None (both), "BUY", or "SELL"

        Returns:
            {
                "total_trades": int,
                "wins": int,
                "losses": int,
                "win_rate": float,
                "total_pnl": float,
                "sharpe": float,
                "max_dd": float,
                "avg_r": float,
                "params": dict,
            }
        """
        t0 = time.time()

        if not _PD_OK or df is None or len(df) < 50:
            return self._empty_result(params)

        strat = params.get("strat", "BK")

        if strat == "MR":
            trades = self._simulate_mr(df, params, direction_bias)
        elif strat == "TF":
            trades = self._simulate_tf(df, params, direction_bias)
        else:
            trades = self._simulate_bk(df, params, direction_bias)

        result = self._compute_metrics(trades, params)

        elapsed = time.time() - t0
        self._tests_run += 1
        self._total_time += elapsed
        result["elapsed_ms"] = round(elapsed * 1000, 1)

        return result

    # ═══════════════════════════════════════════════════════════════════════
    #  STRATEGY SIMULATORS
    # ═══════════════════════════════════════════════════════════════════════

    def _simulate_mr(self, df, params: dict, bias: str = None) -> list[dict]:
        """Simulate Mean Reversion trades."""
        trades = []
        rsi_lo = params.get("rsi_lo", 25)
        rsi_hi = params.get("rsi_hi", 75)
        sl_buf = params.get("sl_buffer", 0.10)
        tp1_r = params.get("tp1", 1.5)
        zscore_thresh = params.get("zscore_thresh", 2.5)

        in_trade = False
        trade = {}

        for i in range(30, len(df) - 1):
            row = df.iloc[i]
            c = float(row["close"])
            rsi = float(row.get("rsi", 50))
            atr = float(row.get("atr", 0))
            zscore = float(row.get("zscore", 0))

            if atr <= 0:
                continue

            if not in_trade:
                sig = None
                # BUY conditions
                if (rsi <= rsi_lo or zscore <= -zscore_thresh) and (bias != "SELL"):
                    sig = "BUY"
                # SELL conditions
                elif (rsi >= rsi_hi or zscore >= zscore_thresh) and (bias != "BUY"):
                    sig = "SELL"

                if sig:
                    sl_dist = atr * sl_buf
                    if sl_dist <= 0:
                        continue
                    if sig == "BUY":
                        sl = c - sl_dist
                        tp = c + sl_dist * tp1_r
                    else:
                        sl = c + sl_dist
                        tp = c - sl_dist * tp1_r

                    trade = {"entry": c, "sl": sl, "tp": tp, "dir": sig, "bar": i}
                    in_trade = True
            else:
                # Check exit
                next_row = df.iloc[i]
                h = float(next_row["high"])
                l = float(next_row["low"])

                hit_tp = (trade["dir"] == "BUY" and h >= trade["tp"]) or \
                         (trade["dir"] == "SELL" and l <= trade["tp"])
                hit_sl = (trade["dir"] == "BUY" and l <= trade["sl"]) or \
                         (trade["dir"] == "SELL" and h >= trade["sl"])

                if hit_tp:
                    pnl = abs(trade["tp"] - trade["entry"])
                    risk = abs(trade["entry"] - trade["sl"])
                    r_mult = pnl / risk if risk > 0 else 0
                    trades.append({"pnl": pnl, "r": r_mult, "win": True, "bars": i - trade["bar"]})
                    in_trade = False
                elif hit_sl:
                    pnl = -abs(trade["entry"] - trade["sl"])
                    trades.append({"pnl": pnl, "r": -1.0, "win": False, "bars": i - trade["bar"]})
                    in_trade = False

        return trades

    def _simulate_tf(self, df, params: dict, bias: str = None) -> list[dict]:
        """Simulate Trend Following trades."""
        trades = []
        ema_fast = params.get("ema_fast", 9)
        ema_slow = params.get("ema_slow", 21)
        sl_buf = params.get("sl_buffer", 0.10)
        tp1_r = params.get("tp1", 1.5)
        adx_min = params.get("adx_min", 20)

        in_trade = False
        trade = {}

        for i in range(max(ema_slow + 5, 30), len(df) - 1):
            row = df.iloc[i]
            c = float(row["close"])
            atr = float(row.get("atr", 0))
            adx = float(row.get("adx", 0))

            if atr <= 0:
                continue

            # Compute EMAs inline (fast)
            ema_f = float(df["close"].iloc[i - ema_fast:i].ewm(span=ema_fast).mean().iloc[-1])
            ema_s = float(df["close"].iloc[i - ema_slow:i].ewm(span=ema_slow).mean().iloc[-1])

            if not in_trade:
                sig = None
                if ema_f > ema_s and adx > adx_min and bias != "SELL":
                    sig = "BUY"
                elif ema_f < ema_s and adx > adx_min and bias != "BUY":
                    sig = "SELL"

                if sig:
                    sl_dist = atr * sl_buf
                    if sl_dist <= 0:
                        continue
                    if sig == "BUY":
                        sl = c - sl_dist
                        tp = c + sl_dist * tp1_r
                    else:
                        sl = c + sl_dist
                        tp = c - sl_dist * tp1_r
                    trade = {"entry": c, "sl": sl, "tp": tp, "dir": sig, "bar": i}
                    in_trade = True
            else:
                h = float(df.iloc[i]["high"])
                l = float(df.iloc[i]["low"])

                hit_tp = (trade["dir"] == "BUY" and h >= trade["tp"]) or \
                         (trade["dir"] == "SELL" and l <= trade["tp"])
                hit_sl = (trade["dir"] == "BUY" and l <= trade["sl"]) or \
                         (trade["dir"] == "SELL" and h >= trade["sl"])

                if hit_tp:
                    pnl = abs(trade["tp"] - trade["entry"])
                    risk = abs(trade["entry"] - trade["sl"])
                    r_mult = pnl / risk if risk > 0 else 0
                    trades.append({"pnl": pnl, "r": r_mult, "win": True, "bars": i - trade["bar"]})
                    in_trade = False
                elif hit_sl:
                    pnl = -abs(trade["entry"] - trade["sl"])
                    trades.append({"pnl": pnl, "r": -1.0, "win": False, "bars": i - trade["bar"]})
                    in_trade = False

        return trades

    def _simulate_bk(self, df, params: dict, bias: str = None) -> list[dict]:
        """Simulate Breakout trades."""
        trades = []
        range_lb = params.get("range_lb", 6)
        bk_margin = params.get("bk_margin", 0.10)
        sl_buf = params.get("sl_buffer", 0.10)
        tp1_r = params.get("tp1", 1.5)

        in_trade = False
        trade = {}

        for i in range(range_lb + 5, len(df) - 1):
            row = df.iloc[i]
            c = float(row["close"])

            if not in_trade:
                recent = df.iloc[i - range_lb - 1:i]
                high_r = float(recent["high"].max())
                low_r = float(recent["low"].min())
                rng = high_r - low_r

                if rng <= 0 or (rng / c * 100) < 0.03:
                    continue

                margin = rng * bk_margin
                sig = None
                if c > high_r + margin and bias != "SELL":
                    sig = "BUY"
                elif c < low_r - margin and bias != "BUY":
                    sig = "SELL"

                if sig:
                    if sig == "BUY":
                        sl = low_r - rng * sl_buf
                        tp = c + rng * tp1_r
                    else:
                        sl = high_r + rng * sl_buf
                        tp = c - rng * tp1_r
                    trade = {"entry": c, "sl": sl, "tp": tp, "dir": sig, "bar": i}
                    in_trade = True
            else:
                h = float(df.iloc[i]["high"])
                l = float(df.iloc[i]["low"])

                hit_tp = (trade["dir"] == "BUY" and h >= trade["tp"]) or \
                         (trade["dir"] == "SELL" and l <= trade["tp"])
                hit_sl = (trade["dir"] == "BUY" and l <= trade["sl"]) or \
                         (trade["dir"] == "SELL" and h >= trade["sl"])

                if hit_tp:
                    pnl = abs(trade["tp"] - trade["entry"])
                    risk = abs(trade["entry"] - trade["sl"])
                    r_mult = pnl / risk if risk > 0 else 0
                    trades.append({"pnl": pnl, "r": r_mult, "win": True, "bars": i - trade["bar"]})
                    in_trade = False
                elif hit_sl:
                    pnl = -abs(trade["entry"] - trade["sl"])
                    trades.append({"pnl": pnl, "r": -1.0, "win": False, "bars": i - trade["bar"]})
                    in_trade = False

        return trades

    # ═══════════════════════════════════════════════════════════════════════
    #  METRICS
    # ═══════════════════════════════════════════════════════════════════════

    def _compute_metrics(self, trades: list[dict], params: dict) -> dict:
        """Compute Sharpe, win rate, max DD, etc."""
        if not trades:
            return self._empty_result(params)

        pnls = [t["pnl"] for t in trades]
        wins = [t for t in trades if t["win"]]

        total_pnl = sum(pnls)
        win_rate = len(wins) / len(trades) if trades else 0
        avg_r = sum(t["r"] for t in trades) / len(trades)

        # Sharpe ratio (annualized, assuming daily-ish trades)
        pnl_arr = np.array(pnls)
        if len(pnl_arr) > 1 and np.std(pnl_arr) > 0:
            sharpe = (np.mean(pnl_arr) / np.std(pnl_arr)) * math.sqrt(252)
        else:
            sharpe = 0.0

        # Max drawdown
        cumulative = np.cumsum(pnl_arr)
        peak = np.maximum.accumulate(cumulative)
        drawdown = peak - cumulative
        max_dd = float(np.max(drawdown)) if len(drawdown) > 0 else 0.0

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(trades) - len(wins),
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 6),
            "sharpe": round(sharpe, 3),
            "max_dd": round(max_dd, 6),
            "avg_r": round(avg_r, 2),
            "params": params,
        }

    def _empty_result(self, params: dict) -> dict:
        return {
            "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "total_pnl": 0, "sharpe": 0, "max_dd": 0, "avg_r": 0,
            "params": params,
        }

    @property
    def stats(self) -> dict:
        return {
            "tests_run": self._tests_run,
            "total_time_s": round(self._total_time, 2),
            "avg_time_ms": round(self._total_time / max(1, self._tests_run) * 1000, 1),
        }
