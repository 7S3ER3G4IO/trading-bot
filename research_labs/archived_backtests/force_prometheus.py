#!/usr/bin/env python3
"""
force_prometheus.py — 🔥 PROMETHEUS FORCED MUTATION

Brute-force grid optimization for EURUSD (Mean Reversion) and GOLD (Breakout).
Downloads real data from Capital.com, tests hundreds of parameter combinations,
selects the best, and rewrites optimized_rules.json.

Usage:
    docker exec nemesis_bot python3 force_prometheus.py
"""

import sys
import os
import json
import time
import math
import copy
import itertools
from datetime import datetime, timezone

sys.path.insert(0, ".")

import numpy as np
import pandas as pd

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

INITIAL_CAPITAL  = 10_000.0
RISK_PER_TRADE   = 0.01
COMMISSION_PCT   = 0.0004
MIN_RR           = 1.5
MIN_TRADES       = 8          # Minimum trades for validity
TARGET_PF        = 1.2        # Target Profit Factor
RULES_FILE       = "optimized_rules.json"


# ═══════════════════════════════════════════════════════════════════════════════
#  GRID DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════════

# EURUSD Mean Reversion: explore RSI thresholds, Z-score, SL buffer, TP R:R
EURUSD_GRID = {
    "strat": ["MR"],
    "rsi_lo":         [30, 35, 40],
    "rsi_hi":         [60, 65, 70],
    "zscore_thresh":  [1.5, 2.0],
    "sl_buffer":      [1.0, 1.5, 2.0],
    "tp_rr":          [1.5, 2.0, 2.5],
}

# GOLD Breakout: explore range lookback, BK margin, ADX filter, SL, TP
GOLD_GRID = {
    "strat": ["BK"],
    "range_lb":       [10, 20, 30, 40],
    "bk_margin":      [0.03, 0.05, 0.08],
    "adx_min":        [0, 20, 25],
    "sl_buffer":      [0.10, 0.15, 0.20],
    "tp_rr":          [1.5, 2.0, 2.5],
}


# ═══════════════════════════════════════════════════════════════════════════════
#  INDICATORS (same as pnl_backtester.py)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l = df["close"], df["high"], df["low"]
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))

    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()

    plus_dm = h.diff().clip(lower=0)
    minus_dm = (-l.diff()).clip(lower=0)
    plus_dm[plus_dm < minus_dm] = 0
    minus_dm[minus_dm < plus_dm] = 0
    tr_s = tr.rolling(14).mean()
    plus_di = 100 * (plus_dm.rolling(14).mean() / tr_s.replace(0, float("nan")))
    minus_di = 100 * (minus_dm.rolling(14).mean() / tr_s.replace(0, float("nan")))
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan")) * 100
    df["adx"] = dx.rolling(14).mean()

    df["ema9"] = c.ewm(span=9).mean()
    df["ema21"] = c.ewm(span=21).mean()

    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    df["bb_up"] = sma20 + 2 * std20
    df["bb_lo"] = sma20 - 2 * std20
    df["zscore"] = (c - sma20) / std20.replace(0, float("nan"))

    if "volume" in df.columns:
        df["vol_ma"] = df["volume"].rolling(20).mean()
        df["vol_ratio"] = df["volume"] / df["vol_ma"].replace(0, 1)
    else:
        df["vol_ratio"] = 1.0

    return df.dropna()


# ═══════════════════════════════════════════════════════════════════════════════
#  FAST VECTORIZED BACKTESTER
# ═══════════════════════════════════════════════════════════════════════════════

def fast_backtest(df: pd.DataFrame, params: dict) -> dict:
    """Ultra-fast single-instrument backtester."""
    strat = params.get("strat", "MR")
    capital = INITIAL_CAPITAL
    trades = []
    in_trade = False
    trade = {}

    for i in range(50, len(df)):
        row = df.iloc[i]
        c = float(row["close"])
        atr = float(row.get("atr", c * 0.01))
        if atr <= 0:
            continue

        if not in_trade:
            signal = None

            if strat == "MR":
                rsi = float(row.get("rsi", 50))
                zscore = float(row.get("zscore", 0))
                if rsi <= params.get("rsi_lo", 30) or zscore <= -params.get("zscore_thresh", 2.0):
                    signal = "BUY"
                elif rsi >= params.get("rsi_hi", 70) or zscore >= params.get("zscore_thresh", 2.0):
                    signal = "SELL"

            elif strat == "BK":
                rl = params.get("range_lb", 6)
                if i < rl + 5:
                    continue
                recent = df.iloc[i - rl:i]
                high_r = float(recent["high"].max())
                low_r = float(recent["low"].min())
                rng = high_r - low_r
                if rng <= 0:
                    continue

                adx = float(row.get("adx", 0))
                adx_min = params.get("adx_min", 0)
                if adx_min > 0 and adx < adx_min:
                    continue

                margin = rng * params.get("bk_margin", 0.03)
                if c > high_r + margin:
                    signal = "BUY"
                elif c < low_r - margin:
                    signal = "SELL"

            if signal:
                if strat == "BK":
                    sl_dist = rng * params.get("sl_buffer", 0.10)
                else:
                    sl_dist = atr * params.get("sl_buffer", 1.5)

                if sl_dist <= 0:
                    continue

                tp_dist = sl_dist * params.get("tp_rr", MIN_RR)

                # Position sizing
                risk_amt = capital * RISK_PER_TRADE
                size = risk_amt / sl_dist if sl_dist > 0 else 0
                if size <= 0:
                    continue

                notional = size * c
                comm = notional * COMMISSION_PCT

                if signal == "BUY":
                    sl, tp = c - sl_dist, c + tp_dist
                else:
                    sl, tp = c + sl_dist, c - tp_dist

                trade = {"entry": c, "sl": sl, "tp": tp, "dir": signal,
                         "size": size, "comm": comm, "bar": i}
                in_trade = True

        else:
            h = float(df.iloc[i]["high"])
            l = float(df.iloc[i]["low"])

            hit_tp = (trade["dir"] == "BUY" and h >= trade["tp"]) or \
                     (trade["dir"] == "SELL" and l <= trade["tp"])
            hit_sl = (trade["dir"] == "BUY" and l <= trade["sl"]) or \
                     (trade["dir"] == "SELL" and h >= trade["sl"])

            if hit_tp or hit_sl:
                ep = trade["tp"] if hit_tp else trade["sl"]
                if trade["dir"] == "BUY":
                    pnl = (ep - trade["entry"]) * trade["size"]
                else:
                    pnl = (trade["entry"] - ep) * trade["size"]
                pnl -= trade["comm"]
                capital += pnl
                trades.append({"pnl": pnl, "win": pnl > 0, "bars": i - trade["bar"]})
                in_trade = False

    if not trades or len(trades) < MIN_TRADES:
        return {"total": len(trades), "pf": 0, "wr": 0, "pnl": 0, "sharpe": 0, "dd": 0}

    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    gp = sum(t["pnl"] for t in wins) if wins else 0
    gl = abs(sum(t["pnl"] for t in losses)) if losses else 0.001

    pnls = np.array([t["pnl"] for t in trades])
    eq = np.cumsum(pnls) + INITIAL_CAPITAL
    peak = np.maximum.accumulate(eq)
    dd = ((peak - eq) / peak * 100).max()

    sharpe = (np.mean(pnls) / np.std(pnls)) * math.sqrt(252) if np.std(pnls) > 0 else 0

    return {
        "total": len(trades),
        "wins": len(wins),
        "pf": round(gp / max(gl, 0.001), 3),
        "wr": round(len(wins) / len(trades), 3),
        "pnl": round(sum(t["pnl"] for t in trades), 2),
        "sharpe": round(sharpe, 3),
        "dd": round(dd, 2),
        "final_capital": round(capital, 2),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  GRID SEARCH ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def grid_search(df: pd.DataFrame, grid: dict, instrument: str) -> list[dict]:
    """Exhaustive grid search over all parameter combinations."""
    keys = list(grid.keys())
    values = list(grid.values())
    combos = list(itertools.product(*values))

    print(f"  🔬 {len(combos)} combinations to test...")

    results = []
    t0 = time.time()

    for combo in combos:
        params = dict(zip(keys, combo))
        result = fast_backtest(df, params)
        result["params"] = params
        results.append(result)

    elapsed = time.time() - t0
    print(f"  ⏱ Grid search complete: {elapsed:.1f}s ({len(combos) / max(elapsed, 0.001):.0f} tests/sec)")

    # Sort by composite score: PF * sqrt(trades) * (1 - dd/100)
    for r in results:
        r["score"] = r["pf"] * math.sqrt(max(r["total"], 1)) * max(0, 1 - r["dd"] / 100)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def print_top_results(results: list[dict], instrument: str, n: int = 5):
    """Print top N results."""
    valid = [r for r in results if r["total"] >= MIN_TRADES and r["pf"] > 0]
    if not valid:
        print(f"  ❌ No valid configurations found for {instrument}")
        return

    print(f"\n  🏆 TOP {n} CONFIGURATIONS — {instrument}")
    print(f"  {'─' * 70}")
    print(f"  {'#':>3s} │ {'Trades':>6s} │ {'WR':>5s} │ {'PF':>5s} │ {'PnL':>10s} │ {'Sharpe':>6s} │ {'DD%':>5s} │ Params")
    print(f"  {'─' * 70}")

    for i, r in enumerate(valid[:n]):
        p = r["params"]
        param_str = " | ".join(f"{k}={v}" for k, v in p.items() if k != "strat")
        pnl_str = f"€{r['pnl']:+,.0f}"
        pf_icon = "🟢" if r["pf"] >= TARGET_PF else "🟡"

        print(
            f"  {i+1:>3d} │ {r['total']:>6d} │ {r['wr']:>4.0%} │ "
            f"{pf_icon}{r['pf']:>4.2f} │ {pnl_str:>10s} │ {r['sharpe']:>+5.2f} │ "
            f"{r['dd']:>4.1f}% │ {param_str[:40]}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "🔥" * 25)
    print("  PROMETHEUS FORCED MUTATION")
    print("  Brute-Force Grid Optimization")
    print("🔥" * 25)
    print()

    from brokers.capital_client import CapitalClient
    capital = CapitalClient()
    if not capital.available:
        print("  ❌ Capital.com API not available")
        return

    # Load current rules
    current_rules = {}
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE) as f:
            current_rules = json.load(f)

    mutations_applied = {}

    # ═══════════════════════════════════════════════════════════════════════
    #  EURUSD OPTIMIZATION
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n  {'━' * 60}")
    print(f"  ┃  EURUSD — Mean Reversion (1D) — FIXING 0 TRADES          ┃")
    print(f"  {'━' * 60}")

    old_eurusd = {"rsi_lo": 30, "rsi_hi": 70, "zscore_thresh": 2.0, "sl_buffer": 1.5, "tp_rr": 1.5}
    print(f"  📋 Current: RSI {old_eurusd['rsi_lo']}/{old_eurusd['rsi_hi']} | Z={old_eurusd['zscore_thresh']} | SL={old_eurusd['sl_buffer']}× | TP={old_eurusd['tp_rr']}R")
    print(f"  ⚠️  Problem: 0 trades on 1000 daily bars")
    print()

    print(f"  📥 Fetching EURUSD 1D data...", end=" ")
    df_eur = capital.fetch_ohlcv("EURUSD", timeframe="1d", count=1000)
    if df_eur is not None and len(df_eur) > 100:
        print(f"✅ {len(df_eur)} bars")
        df_eur = compute_indicators(df_eur)

        # Baseline
        base = fast_backtest(df_eur, {**old_eurusd, "strat": "MR"})
        print(f"  📊 Baseline: {base['total']} trades | PF={base['pf']} | PnL=€{base['pnl']}")

        # Grid search
        eur_results = grid_search(df_eur, EURUSD_GRID, "EURUSD")
        print_top_results(eur_results, "EURUSD")

        # Select best
        best_eur = next((r for r in eur_results if r["total"] >= MIN_TRADES and r["pf"] >= TARGET_PF), None)
        if not best_eur:
            best_eur = next((r for r in eur_results if r["total"] >= MIN_TRADES), None)

        if best_eur:
            new_params = best_eur["params"]
            mutations_applied["EURUSD"] = {
                "old": old_eurusd,
                "new": new_params,
                "old_pf": base["pf"],
                "new_pf": best_eur["pf"],
                "old_trades": base["total"],
                "new_trades": best_eur["total"],
                "new_pnl": best_eur["pnl"],
            }
            print(f"\n  🔥 BEST MUTATION FOUND:")
            print(f"     RSI: {old_eurusd['rsi_lo']}/{old_eurusd['rsi_hi']} → {new_params['rsi_lo']}/{new_params['rsi_hi']}")
            print(f"     Z-Score: {old_eurusd['zscore_thresh']} → {new_params['zscore_thresh']}")
            print(f"     SL: {old_eurusd['sl_buffer']}× → {new_params['sl_buffer']}×")
            print(f"     TP: {old_eurusd['tp_rr']}R → {new_params['tp_rr']}R")
            print(f"     Trades: {base['total']} → {best_eur['total']}")
            print(f"     PF: {base['pf']} → {best_eur['pf']}")
            print(f"     PnL: €{base['pnl']} → €{best_eur['pnl']}")
    else:
        print("❌ No data")

    # ═══════════════════════════════════════════════════════════════════════
    #  GOLD OPTIMIZATION
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n  {'━' * 60}")
    print(f"  ┃  GOLD — Breakout (4H) — FIXING PF=0.86                   ┃")
    print(f"  {'━' * 60}")

    old_gold = {"range_lb": 6, "bk_margin": 0.03, "adx_min": 0, "sl_buffer": 0.10, "tp_rr": 1.5}
    print(f"  📋 Current: LB={old_gold['range_lb']} | BK%={old_gold['bk_margin']} | ADX>{old_gold['adx_min']} | SL={old_gold['sl_buffer']} | TP={old_gold['tp_rr']}R")
    print(f"  ⚠️  Problem: PF=0.86, 90 trades, -€448")
    print()

    print(f"  📥 Fetching GOLD 4H data...", end=" ")
    df_gold = capital.fetch_ohlcv("GOLD", timeframe="4h", count=1000)
    if df_gold is not None and len(df_gold) > 100:
        print(f"✅ {len(df_gold)} bars")
        df_gold = compute_indicators(df_gold)

        # Baseline
        base_gold = fast_backtest(df_gold, {**old_gold, "strat": "BK"})
        print(f"  📊 Baseline: {base_gold['total']} trades | PF={base_gold['pf']} | PnL=€{base_gold['pnl']}")

        # Grid search
        gold_results = grid_search(df_gold, GOLD_GRID, "GOLD")
        print_top_results(gold_results, "GOLD")

        # Select best
        best_gold = next((r for r in gold_results if r["total"] >= MIN_TRADES and r["pf"] >= TARGET_PF), None)
        if not best_gold:
            best_gold = next((r for r in gold_results if r["total"] >= MIN_TRADES), None)

        if best_gold:
            new_params_g = best_gold["params"]
            mutations_applied["GOLD"] = {
                "old": old_gold,
                "new": new_params_g,
                "old_pf": base_gold["pf"],
                "new_pf": best_gold["pf"],
                "old_trades": base_gold["total"],
                "new_trades": best_gold["total"],
                "new_pnl": best_gold["pnl"],
            }
            print(f"\n  🔥 BEST MUTATION FOUND:")
            print(f"     Range LB: {old_gold['range_lb']} → {new_params_g['range_lb']}")
            print(f"     BK Margin: {old_gold['bk_margin']} → {new_params_g['bk_margin']}")
            print(f"     ADX Min: {old_gold['adx_min']} → {new_params_g.get('adx_min', 0)}")
            print(f"     SL: {old_gold['sl_buffer']} → {new_params_g['sl_buffer']}")
            print(f"     TP: {old_gold['tp_rr']}R → {new_params_g['tp_rr']}R")
            print(f"     Trades: {base_gold['total']} → {best_gold['total']}")
            print(f"     PF: {base_gold['pf']} → {best_gold['pf']}")
            print(f"     PnL: €{base_gold['pnl']} → €{best_gold['pnl']}")
    else:
        print("❌ No data")

    # ═══════════════════════════════════════════════════════════════════════
    #  WRITE UPDATED RULES
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n  {'━' * 60}")
    print(f"  ┃  REWRITING RULES                                         ┃")
    print(f"  {'━' * 60}")

    if mutations_applied:
        for inst, mutation in mutations_applied.items():
            new_p = mutation["new"]
            entry = current_rules.get(inst, {})

            # Update strategy params
            for k, v in new_p.items():
                entry[k] = v

            entry["prometheus_forced"] = (
                f"Mutated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} | "
                f"PF: {mutation['old_pf']:.2f}→{mutation['new_pf']:.2f} | "
                f"Trades: {mutation['old_trades']}→{mutation['new_trades']}"
            )
            current_rules[inst] = entry

        with open(RULES_FILE, "w") as f:
            json.dump(current_rules, f, indent=2)
        print(f"  ✅ {RULES_FILE} rewritten with {len(mutations_applied)} mutations")
    else:
        print(f"  ⚠️  No mutations to apply")

    # ═══════════════════════════════════════════════════════════════════════
    #  FINAL COMPARISON
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n  {'━' * 60}")
    print(f"  ┃  BEFORE vs AFTER                                         ┃")
    print(f"  {'━' * 60}")

    print(f"\n  {'Instrument':12s} │ {'Metric':12s} │ {'Before':>10s} │ {'After':>10s} │ {'Delta':>10s}")
    print(f"  {'─' * 12} │ {'─' * 12} │ {'─' * 10} │ {'─' * 10} │ {'─' * 10}")

    for inst, m in mutations_applied.items():
        print(f"  {inst:12s} │ {'Trades':12s} │ {m['old_trades']:>10d} │ {m['new_trades']:>10d} │ {m['new_trades'] - m['old_trades']:>+10d}")
        pf_icon = "📈" if m['new_pf'] > m['old_pf'] else "📉"
        print(f"  {'':12s} │ {'Profit Factor':12s} │ {m['old_pf']:>10.2f} │ {m['new_pf']:>10.2f} │ {pf_icon} {m['new_pf'] - m['old_pf']:>+8.2f}")
        print(f"  {'':12s} │ {'PnL':12s} │ {'€0.00':>10s} │ €{m['new_pnl']:>+8.0f} │")

        old_p = m["old"]
        new_p = m["new"]
        for k in new_p:
            if k == "strat":
                continue
            if new_p.get(k) != old_p.get(k):
                print(f"  {'':12s} │ {k:12s} │ {str(old_p.get(k, '?')):>10s} │ {str(new_p[k]):>10s} │ {'✅ CHANGED':>10s}")
        print(f"  {'─' * 12} │ {'─' * 12} │ {'─' * 10} │ {'─' * 10} │ {'─' * 10}")

    print()
    print(f"  ╔═══════════════════════════════════════════════════════╗")
    print(f"  ║                                                     ║")
    print(f"  ║   🔥  PROMETHEUS MUTATION COMPLETE  🔥              ║")
    print(f"  ║   Rules rewritten. Machine evolved.                 ║")
    print(f"  ║                                                     ║")
    print(f"  ╚═══════════════════════════════════════════════════════╝")
    print()


if __name__ == "__main__":
    main()
