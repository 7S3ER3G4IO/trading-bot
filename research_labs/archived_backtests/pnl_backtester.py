#!/usr/bin/env python3
"""
pnl_backtester.py — 📊 CORE ENGINE HISTORICAL BACKTEST

Backtest vectoriel du cœur mathématique (God Mode + Risk + Convexity + Session).
L2, Argus NLP, Emotional Core sont désactivés (non-disponibles sur historique).

Usage:
    docker exec nemesis_bot python3 pnl_backtester.py
"""

import sys
import os
import time
import json
import math
from datetime import datetime, timezone, timedelta

sys.path.insert(0, ".")

import numpy as np
import pandas as pd

# Suppress noisy logs during backtest
from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

INITIAL_CAPITAL  = 10_000.0
RISK_PER_TRADE   = 0.01           # 1% risk per trade
MAX_LEVERAGE     = 3.0            # Max effective leverage
MIN_RR           = 1.5            # Convexity gate
COMMISSION_PCT   = 0.0004         # 0.04% round-trip spread simulation

MARGIN_REQ = {
    "crypto": 0.50,
    "commodities": 0.05,
    "forex": 0.0333,
    "indices": 0.05,
}

# Instruments to backtest with their God Mode rules
BACKTEST_INSTRUMENTS = {
    "EURUSD": {
        "strat": "MR",       # ML uses MR-style signals (RSI/BB mean reversion)
        "tf": "1d",
        "cat": "forex",
        "rsi_lo": 30, "rsi_hi": 70,
        "zscore_thresh": 2.0,
        "sl_buffer": 1.5,    # ATR multiplier for SL
        "tp_rr": 1.5,        # R:R target
    },
    "BTCUSD": {
        "strat": "TF",       # Trend Following
        "tf": "1d",
        "cat": "crypto",
        "ema_fast": 9, "ema_slow": 21,
        "adx_min": 20,
        "sl_buffer": 1.5,
        "tp_rr": 1.5,
    },
    "GOLD": {
        "strat": "BK",       # Breakout
        "tf": "4h",
        "cat": "commodities",
        "range_lb": 6,
        "bk_margin": 0.03,   # 3% of range
        "sl_buffer": 0.10,   # % of range for SL
        "tp_rr": 1.5,
    },
}

# Session windows (from strategy.py)
SESSION_WINDOWS = {
    "crypto":      [(0, 1440)],         # 24/7
    "forex":       [(420, 1260)],       # 07-21h UTC
    "forex_asia":  [(0, 1260)],         # 00-21h UTC
    "commodities": [(360, 1320)],       # 06-22h UTC
    "indices":     [(420, 630), (780, 1200)],
}

ASIAN_CURRENCIES = {"JPY", "AUD", "NZD"}


# ═══════════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ═══════════════════════════════════════════════════════════════════════════════

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all needed indicators."""
    c = df["close"]
    h = df["high"]
    l = df["low"]

    # RSI
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))

    # ATR
    tr = pd.concat([
        h - l,
        (h - c.shift()).abs(),
        (l - c.shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()

    # ADX
    plus_dm = h.diff().clip(lower=0)
    minus_dm = (-l.diff()).clip(lower=0)
    plus_dm[plus_dm < minus_dm] = 0
    minus_dm[minus_dm < plus_dm] = 0
    tr_smooth = tr.rolling(14).mean()
    plus_di = 100 * (plus_dm.rolling(14).mean() / tr_smooth.replace(0, float("nan")))
    minus_di = 100 * (minus_dm.rolling(14).mean() / tr_smooth.replace(0, float("nan")))
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan")) * 100
    df["adx"] = dx.rolling(14).mean()

    # EMA
    df["ema9"] = c.ewm(span=9).mean()
    df["ema21"] = c.ewm(span=21).mean()
    df["ema50"] = c.ewm(span=50).mean()

    # Bollinger Bands
    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    df["bb_up"] = sma20 + 2 * std20
    df["bb_lo"] = sma20 - 2 * std20

    # Z-Score
    df["zscore"] = (c - sma20) / std20.replace(0, float("nan"))

    # Volume MA
    if "volume" in df.columns:
        df["vol_ma"] = df["volume"].rolling(20).mean()
        df["vol_ratio"] = df["volume"] / df["vol_ma"].replace(0, 1)
    else:
        df["vol_ratio"] = 1.0

    return df.dropna()


# ═══════════════════════════════════════════════════════════════════════════════
#  SESSION FILTER
# ═══════════════════════════════════════════════════════════════════════════════

def is_session_ok(timestamp, instrument: str, category: str) -> bool:
    """Check if a timestamp falls within the trading session."""
    if category == "crypto":
        return True

    # Weekend check
    if hasattr(timestamp, 'weekday'):
        if timestamp.weekday() >= 5:
            return False
    elif hasattr(timestamp, 'dayofweek'):
        if timestamp.dayofweek >= 5:
            return False

    h = timestamp.hour if hasattr(timestamp, 'hour') else 0
    m = timestamp.minute if hasattr(timestamp, 'minute') else 0
    minutes = h * 60 + m

    # Asian currency detection
    effective_cat = category
    if category == "forex":
        instr_upper = instrument.upper()
        for ccy in ASIAN_CURRENCIES:
            if ccy in instr_upper:
                effective_cat = "forex_asia"
                break

    windows = SESSION_WINDOWS.get(effective_cat, [(420, 1260)])
    for start, end in windows:
        if start <= minutes < end:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
#  STRATEGY SIGNALS
# ═══════════════════════════════════════════════════════════════════════════════

def generate_signal(row, prev_rows, params: dict) -> str | None:
    """Generate BUY/SELL signal based on strategy."""
    strat = params["strat"]

    if strat == "MR":
        rsi = row.get("rsi", 50)
        zscore = row.get("zscore", 0)
        rsi_lo = params.get("rsi_lo", 30)
        rsi_hi = params.get("rsi_hi", 70)
        z_thresh = params.get("zscore_thresh", 2.0)

        if rsi <= rsi_lo or zscore <= -z_thresh:
            return "BUY"
        elif rsi >= rsi_hi or zscore >= z_thresh:
            return "SELL"

    elif strat == "TF":
        ema_f = row.get("ema9", 0)
        ema_s = row.get("ema21", 0)
        adx = row.get("adx", 0)
        adx_min = params.get("adx_min", 20)

        if ema_f > ema_s and adx > adx_min:
            return "BUY"
        elif ema_f < ema_s and adx > adx_min:
            return "SELL"

    elif strat == "BK":
        if prev_rows is None or len(prev_rows) < 6:
            return None

        range_lb = params.get("range_lb", 6)
        bk_margin = params.get("bk_margin", 0.03)

        recent = prev_rows.tail(range_lb)
        high_r = float(recent["high"].max())
        low_r = float(recent["low"].min())
        rng = high_r - low_r
        c = float(row["close"])

        if rng <= 0:
            return None

        margin = rng * bk_margin
        if c > high_r + margin:
            return "BUY"
        elif c < low_r - margin:
            return "SELL"

    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  BACKTESTER CORE
# ═══════════════════════════════════════════════════════════════════════════════

class BacktestEngine:
    """Vectorial backtester with position sizing and risk management."""

    def __init__(self, capital: float = INITIAL_CAPITAL):
        self.initial_capital = capital
        self.capital = capital
        self.peak_capital = capital
        self.trades: list[dict] = []
        self.equity_curve: list[float] = [capital]

    def run(self, df: pd.DataFrame, instrument: str, params: dict) -> dict:
        """Run backtest on a single instrument."""
        category = params["cat"]
        in_trade = False
        trade = {}

        for i in range(50, len(df)):
            row = df.iloc[i]
            idx = df.index[i]

            if not in_trade:
                # Session filter
                if not is_session_ok(idx, instrument, category):
                    continue

                # Look for signal
                prev = df.iloc[max(0, i - 20):i]
                signal = generate_signal(row, prev, params)
                if signal is None:
                    continue

                # Compute SL/TP
                c = float(row["close"])
                atr = float(row.get("atr", c * 0.01))
                if atr <= 0:
                    continue

                strat = params["strat"]
                if strat == "BK":
                    range_lb = params.get("range_lb", 6)
                    recent = df.iloc[max(0, i - range_lb):i]
                    rng = float(recent["high"].max() - recent["low"].min())
                    if rng <= 0:
                        continue
                    sl_dist = rng * params.get("sl_buffer", 0.10)
                else:
                    sl_dist = atr * params.get("sl_buffer", 1.5)

                if sl_dist <= 0:
                    continue

                tp_dist = sl_dist * params.get("tp_rr", MIN_RR)

                if signal == "BUY":
                    sl = c - sl_dist
                    tp = c + tp_dist
                else:
                    sl = c + sl_dist
                    tp = c - tp_dist

                # Convexity gate
                rr = tp_dist / sl_dist if sl_dist > 0 else 0
                if rr < MIN_RR - 0.001:
                    continue

                # Position sizing
                risk_amount = self.capital * RISK_PER_TRADE
                margin_req = MARGIN_REQ.get(category, 0.05)
                max_notional = self.capital / margin_req
                raw_size = risk_amount / sl_dist if sl_dist > 0 else 0
                max_size = max_notional / c if c > 0 else 0

                # Leverage check
                effective_size = min(raw_size, max_size)
                notional = effective_size * c
                eff_leverage = notional / self.capital if self.capital > 0 else 0
                if eff_leverage > MAX_LEVERAGE:
                    effective_size = (MAX_LEVERAGE * self.capital) / c

                if effective_size <= 0:
                    continue

                # Commission
                commission = notional * COMMISSION_PCT

                trade = {
                    "entry": c,
                    "sl": sl,
                    "tp": tp,
                    "direction": signal,
                    "size": effective_size,
                    "bar_entry": i,
                    "rr": rr,
                    "commission": commission,
                    "entry_time": idx,
                }
                in_trade = True

            else:
                # Check exits
                h = float(df.iloc[i]["high"])
                l = float(df.iloc[i]["low"])
                c = float(df.iloc[i]["close"])

                hit_tp = False
                hit_sl = False

                if trade["direction"] == "BUY":
                    if h >= trade["tp"]:
                        hit_tp = True
                    elif l <= trade["sl"]:
                        hit_sl = True
                else:
                    if l <= trade["tp"]:
                        hit_tp = True
                    elif h >= trade["sl"]:
                        hit_sl = True

                if hit_tp or hit_sl:
                    if hit_tp:
                        exit_price = trade["tp"]
                        exit_reason = "TP"
                    else:
                        exit_price = trade["sl"]
                        exit_reason = "SL"

                    # PnL
                    if trade["direction"] == "BUY":
                        pnl = (exit_price - trade["entry"]) * trade["size"]
                    else:
                        pnl = (trade["entry"] - exit_price) * trade["size"]

                    pnl -= trade["commission"]

                    self.capital += pnl
                    self.peak_capital = max(self.peak_capital, self.capital)
                    self.equity_curve.append(self.capital)

                    self.trades.append({
                        "instrument": instrument,
                        "direction": trade["direction"],
                        "entry": trade["entry"],
                        "exit": exit_price,
                        "pnl": round(pnl, 2),
                        "r_multiple": round(pnl / (trade["commission"] + abs(trade["entry"] - trade["sl"]) * trade["size"]) if trade["size"] > 0 else 0, 2),
                        "exit_reason": exit_reason,
                        "win": pnl > 0,
                        "bars_held": i - trade["bar_entry"],
                        "commission": round(trade["commission"], 2),
                        "entry_time": str(trade.get("entry_time", "")),
                    })

                    in_trade = False

        return self._compute_stats(instrument)

    def _compute_stats(self, instrument: str) -> dict:
        """Compute performance metrics for one instrument."""
        inst_trades = [t for t in self.trades if t["instrument"] == instrument]
        if not inst_trades:
            return {"instrument": instrument, "total_trades": 0}

        wins = [t for t in inst_trades if t["win"]]
        losses = [t for t in inst_trades if not t["win"]]

        gross_profit = sum(t["pnl"] for t in wins) if wins else 0
        gross_loss = abs(sum(t["pnl"] for t in losses)) if losses else 0.001

        return {
            "instrument": instrument,
            "total_trades": len(inst_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(inst_trades),
            "total_pnl": round(sum(t["pnl"] for t in inst_trades), 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0,
            "avg_win": round(gross_profit / max(1, len(wins)), 2),
            "avg_loss": round(-gross_loss / max(1, len(losses)), 2),
            "avg_bars_held": round(sum(t["bars_held"] for t in inst_trades) / len(inst_trades), 1),
            "best_trade": round(max(t["pnl"] for t in inst_trades), 2),
            "worst_trade": round(min(t["pnl"] for t in inst_trades), 2),
            "total_commissions": round(sum(t["commission"] for t in inst_trades), 2),
        }

    def global_stats(self) -> dict:
        """Compute portfolio-level stats."""
        if not self.trades:
            return {}

        equity = np.array(self.equity_curve)
        peak = np.maximum.accumulate(equity)
        dd = (peak - equity) / peak * 100
        max_dd = float(np.max(dd))

        wins = [t for t in self.trades if t["win"]]
        losses = [t for t in self.trades if not t["win"]]
        gp = sum(t["pnl"] for t in wins)
        gl = abs(sum(t["pnl"] for t in losses))

        # Monthly return
        total_return = (self.capital - self.initial_capital) / self.initial_capital * 100

        # Sharpe approximation
        pnls = [t["pnl"] for t in self.trades]
        if len(pnls) > 1 and np.std(pnls) > 0:
            sharpe = (np.mean(pnls) / np.std(pnls)) * math.sqrt(252)
        else:
            sharpe = 0.0

        return {
            "total_trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(self.trades),
            "total_pnl": round(self.capital - self.initial_capital, 2),
            "total_return_pct": round(total_return, 2),
            "final_capital": round(self.capital, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "profit_factor": round(gp / max(gl, 0.01), 2),
            "sharpe_ratio": round(sharpe, 2),
            "avg_trade": round(sum(pnls) / len(pnls), 2),
            "total_commissions": round(sum(t["commission"] for t in self.trades), 2),
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  TEAR SHEET DISPLAY
# ═══════════════════════════════════════════════════════════════════════════════

def print_tear_sheet(engine: BacktestEngine, inst_results: list[dict]):
    gs = engine.global_stats()
    if not gs:
        print("  ❌ No trades generated")
        return

    print()
    print("  ╔═══════════════════════════════════════════════════════════╗")
    print("  ║                                                         ║")
    print("  ║   📊  INSTITUTIONAL TEAR SHEET  📊                     ║")
    print("  ║   God Mode Core Engine Backtest                        ║")
    print("  ║                                                         ║")
    print("  ╚═══════════════════════════════════════════════════════════╝")
    print()

    # Portfolio summary
    print("  ┌─── PORTFOLIO SUMMARY ─────────────────────────────────┐")
    print(f"  │  💰 Capital initial:    €{engine.initial_capital:>12,.2f}            │")
    print(f"  │  💎 Capital final:      €{gs['final_capital']:>12,.2f}            │")
    icon = "📈" if gs['total_pnl'] > 0 else "📉"
    print(f"  │  {icon} PnL Total:          €{gs['total_pnl']:>+12,.2f}            │")
    print(f"  │  📊 Rendement:            {gs['total_return_pct']:>+10.2f}%            │")
    print(f"  │  📉 Max Drawdown:          {gs['max_drawdown_pct']:>10.2f}%            │")
    print(f"  │  💸 Commissions:        €{gs['total_commissions']:>12,.2f}            │")
    print(f"  └─────────────────────────────────────────────────────────┘")
    print()

    # Key metrics
    print("  ┌─── MÉTRIQUES CLÉS ────────────────────────────────────┐")
    print(f"  │  📊 Total Trades:       {gs['total_trades']:>8d}                    │")
    print(f"  │  ✅ Wins:               {gs['wins']:>8d}                    │")
    print(f"  │  ❌ Losses:             {gs['losses']:>8d}                    │")
    wr = gs['win_rate']
    wr_bar = "█" * int(wr * 20) + "░" * (20 - int(wr * 20))
    print(f"  │  🎯 Win Rate:          {wr:>8.1%}  {wr_bar}    │")

    pf = gs['profit_factor']
    pf_icon = "🟢" if pf >= 1.5 else "🟡" if pf >= 1.0 else "🔴"
    print(f"  │  {pf_icon} Profit Factor:     {pf:>8.2f}                    │")

    sh = gs['sharpe_ratio']
    sh_icon = "🟢" if sh >= 1.0 else "🟡" if sh >= 0.5 else "🔴"
    print(f"  │  {sh_icon} Sharpe Ratio:      {sh:>8.2f}                    │")
    print(f"  │  📊 Avg Trade:         €{gs['avg_trade']:>+8.2f}                    │")
    print(f"  └─────────────────────────────────────────────────────────┘")
    print()

    # Per-instrument breakdown
    print("  ┌─── DÉTAIL PAR INSTRUMENT ─────────────────────────────┐")
    print(f"  │  {'Instrument':12s} │ Trades │ WR%    │ PnL €      │ PF   │")
    print(f"  │  {'─'*12} │ {'─'*6} │ {'─'*6} │ {'─'*10} │ {'─'*4} │")

    for r in inst_results:
        if r["total_trades"] == 0:
            print(f"  │  {r['instrument']:12s} │ {'0':>6s} │ {'N/A':>6s} │ {'€0.00':>10s} │ {'N/A':>4s} │")
        else:
            pnl_str = f"€{r['total_pnl']:+,.2f}"
            pf_str = f"{r['profit_factor']:.2f}"
            wr_str = f"{r['win_rate']:.0%}"
            print(f"  │  {r['instrument']:12s} │ {r['total_trades']:>6d} │ {wr_str:>6s} │ {pnl_str:>10s} │ {pf_str:>4s} │")

    print(f"  └─────────────────────────────────────────────────────────┘")
    print()

    # Verdict
    print("  ┌─── VERDICT ───────────────────────────────────────────┐")
    if gs['total_pnl'] > 0 and gs['profit_factor'] >= 1.5:
        print("  │  🏆 STRATÉGIE PROFITABLE                              │")
        print(f"  │  PF={pf:.2f} ≥ 1.5 ✅ | Max DD={gs['max_drawdown_pct']:.1f}% | WR={wr:.0%}        │")
    elif gs['total_pnl'] > 0:
        print("  │  ✅ STRATÉGIE EN PROFIT (mais PF < 1.5)                │")
        print(f"  │  PF={pf:.2f} | Optimisation possible                   │")
    else:
        print("  │  ⚠️  STRATÉGIE EN PERTE — OPTIMISATION NÉCESSAIRE      │")
        print(f"  │  PnL={gs['total_pnl']:+.2f}€ | PF={pf:.2f}                          │")
    print(f"  └─────────────────────────────────────────────────────────┘")
    print()

    # Trade log excerpt (last 10)
    if engine.trades:
        print("  ┌─── DERNIERS TRADES (max 10) ─────────────────────────┐")
        for t in engine.trades[-10:]:
            icon = "✅" if t["win"] else "❌"
            print(
                f"  │  {icon} {t['instrument']:8s} {t['direction']:4s} "
                f"€{t['pnl']:+8.2f} ({t['exit_reason']:2s}) "
                f"{t['bars_held']:3d} bars │"
            )
        print(f"  └─────────────────────────────────────────────────────────┘")
        print()


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "📊" * 25)
    print("  CORE ENGINE HISTORICAL BACKTEST")
    print("  God Mode + Risk + Convexity 1.5 + Session Filter")
    print("📊" * 25)
    print()
    print(f"  💰 Capital: €{INITIAL_CAPITAL:,.2f}")
    print(f"  📐 Min R:R: {MIN_RR}")
    print(f"  📊 Risk/Trade: {RISK_PER_TRADE:.1%}")
    print(f"  📊 Max Leverage: {MAX_LEVERAGE}x")
    print(f"  📊 Commission: {COMMISSION_PCT:.2%} round-trip")
    print()

    # Connect to broker for data
    from brokers.capital_client import CapitalClient
    capital = CapitalClient()

    if not capital.available:
        print("  ❌ Capital.com API not available — cannot fetch data")
        return

    engine = BacktestEngine(INITIAL_CAPITAL)
    inst_results = []

    for instrument, params in BACKTEST_INSTRUMENTS.items():
        tf = params["tf"]
        cat = params["cat"]

        print(f"  ━━━ {instrument} ({params['strat']}, {tf}) ━━━")

        # Fetch data
        count = 1000
        print(f"  📥 Fetching {count} candles ({tf})...", end=" ")
        df = capital.fetch_ohlcv(instrument, timeframe=tf, count=count)

        if df is None or len(df) < 100:
            print(f"❌ Only {len(df) if df is not None else 0} bars")
            inst_results.append({"instrument": instrument, "total_trades": 0})
            continue

        print(f"✅ {len(df)} bars ({df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')})")

        # Compute indicators
        df = compute_indicators(df)
        print(f"  📊 Indicators computed: {len(df)} valid bars")

        # Run backtest
        result = engine.run(df, instrument, params)
        inst_results.append(result)

        if result["total_trades"] > 0:
            icon = "📈" if result["total_pnl"] > 0 else "📉"
            print(
                f"  {icon} {result['total_trades']} trades | "
                f"WR={result['win_rate']:.0%} | "
                f"PnL=€{result['total_pnl']:+,.2f} | "
                f"PF={result['profit_factor']:.2f}"
            )
        else:
            print(f"  ⚠️  No trades generated")
        print()

    # Final tear sheet
    print_tear_sheet(engine, inst_results)

    print("  ℹ️  Note: L2, Argus NLP, Emotional Core, Hedging sont désactivés")
    print("  ℹ️  Ce backtest reflète uniquement le cœur mathématique")
    print()


if __name__ == "__main__":
    main()
