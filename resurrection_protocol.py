#!/usr/bin/env python3
"""
resurrection_protocol.py — ⚡ THE RESURRECTION PROTOCOL

For each DEAD instrument (0 or <5 trades in the sweep), tests ALL 9
combinations of Strategy × Timeframe to find a viable heartbeat.

    Strategies: TF (Trend Following), MR (Mean Reversion), BK (Breakout)
    Timeframes: 1d, 4h, 1h

Stops searching when a combo produces ≥15 trades AND PF≥1.25.
Auto-saves the resurrected config to optimized_rules.json.

Usage:
    docker exec nemesis_bot python3 resurrection_protocol.py
"""

import sys, os, json, time, math, gc
from datetime import datetime, timezone

sys.path.insert(0, ".")

import numpy as np
import pandas as pd

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")


# ═══════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════

INITIAL_CAPITAL = 10_000.0
RISK_PCT        = 0.01
COMMISSION_PCT  = 0.0004
RULES_FILE      = "optimized_rules.json"

# Resurrection thresholds
MIN_TRADES_ALIVE = 15
MIN_PF_ALIVE     = 1.25

# The dead — from the sweep results
DEAD_INSTRUMENTS = [
    "AAPL", "EURUSD", "GBPCHF", "GOOGL", "J225",
    "META", "NATURALGAS", "OIL_BRENT", "NZDUSD", "UK100",
]

# Strategy × Timeframe matrix
STRATEGIES = ["TF", "MR", "BK"]
TIMEFRAMES = ["1d", "4h", "1h"]

# Default params per strategy (neutral starting point)
DEFAULT_PARAMS = {
    "TF": {"adx_min": 20, "sl_buffer": 1.5, "tp_rr": 2.0},
    "MR": {"rsi_lo": 35, "rsi_hi": 65, "zscore_thresh": 1.5, "sl_buffer": 1.5, "tp_rr": 2.0},
    "BK": {"range_lb": 20, "bk_margin": 0.03, "adx_min": 0, "sl_buffer": 0.15, "tp_rr": 2.0},
}

# Category lookup
CAT_MAP = {
    "AAPL": "stocks", "EURUSD": "forex", "GBPCHF": "forex",
    "GOOGL": "stocks", "J225": "indices", "META": "stocks",
    "NATURALGAS": "commodities", "OIL_BRENT": "commodities",
    "NZDUSD": "forex", "UK100": "indices",
}

# Session windows
SESS = {
    "crypto": [(0, 1440)],
    "forex": [(420, 1260)],
    "commodities": [(360, 1320)],
    "indices": [(420, 630), (780, 1200)],
    "stocks": [(780, 1200)],
}
ASIA_CCY = {"JPY", "AUD", "NZD"}


# ═══════════════════════════════════════════════════════════════════════
#  INDICATORS
# ═══════════════════════════════════════════════════════════════════════

def compute_indicators(df):
    c, h, l = df["close"], df["high"], df["low"]
    d = c.diff()
    g = d.clip(lower=0).rolling(14).mean()
    lo = (-d.clip(upper=0)).rolling(14).mean()
    rs = g / lo.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()

    pdm = h.diff().clip(lower=0)
    mdm = (-l.diff()).clip(lower=0)
    pdm[pdm < mdm] = 0
    mdm[mdm < pdm] = 0
    trs = tr.rolling(14).mean()
    pdi = 100 * (pdm.rolling(14).mean() / trs.replace(0, np.nan))
    mdi = 100 * (mdm.rolling(14).mean() / trs.replace(0, np.nan))
    dx = (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan) * 100
    df["adx"] = dx.rolling(14).mean()

    df["ema9"] = c.ewm(span=9).mean()
    df["ema21"] = c.ewm(span=21).mean()

    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    df["zscore"] = (c - sma20) / std20.replace(0, np.nan)

    return df.dropna()


# ═══════════════════════════════════════════════════════════════════════
#  SESSION FILTER
# ═══════════════════════════════════════════════════════════════════════

def session_ok(ts, instr, cat):
    if cat == "crypto":
        return True
    if hasattr(ts, "weekday") and ts.weekday() >= 5:
        return False
    h = ts.hour if hasattr(ts, "hour") else 0
    m = ts.minute if hasattr(ts, "minute") else 0
    mins = h * 60 + m
    ecat = cat
    if cat == "forex":
        for cc in ASIA_CCY:
            if cc in instr.upper():
                ecat = "forex_asia"
                break
    windows = SESS.get(ecat, [(420, 1260)])
    if ecat == "forex_asia":
        windows = [(0, 1260)]
    return any(s <= mins < e for s, e in windows)


# ═══════════════════════════════════════════════════════════════════════
#  BACKTESTER
# ═══════════════════════════════════════════════════════════════════════

def backtest(df, params, instr, cat):
    strat = params.get("strat", "MR")
    cap = INITIAL_CAPITAL
    trades = []
    in_trade = False
    tr = {}

    for i in range(50, len(df)):
        row = df.iloc[i]
        idx = df.index[i]
        c = float(row["close"])
        atr = float(row.get("atr", c * 0.01))
        if atr <= 0:
            continue

        if not in_trade:
            if not session_ok(idx, instr, cat):
                continue

            sig = None
            sl_d = atr * params.get("sl_buffer", 1.5)

            if strat == "MR":
                rsi = float(row.get("rsi", 50))
                zs = float(row.get("zscore", 0))
                if rsi <= params.get("rsi_lo", 35) or zs <= -params.get("zscore_thresh", 1.5):
                    sig = "BUY"
                elif rsi >= params.get("rsi_hi", 65) or zs >= params.get("zscore_thresh", 1.5):
                    sig = "SELL"

            elif strat == "TF":
                ef = float(row.get("ema9", 0))
                es = float(row.get("ema21", 0))
                adx = float(row.get("adx", 0))
                if ef > es and adx > params.get("adx_min", 20):
                    sig = "BUY"
                elif ef < es and adx > params.get("adx_min", 20):
                    sig = "SELL"

            elif strat == "BK":
                rl = params.get("range_lb", 20)
                if i < rl + 5:
                    continue
                rec = df.iloc[i - rl:i]
                hr = float(rec["high"].max())
                lr = float(rec["low"].min())
                rng = hr - lr
                if rng <= 0:
                    continue
                adx = float(row.get("adx", 0))
                if params.get("adx_min", 0) > 0 and adx < params["adx_min"]:
                    continue
                margin = rng * params.get("bk_margin", 0.03)
                sl_d = rng * params.get("sl_buffer", 0.15)
                if c > hr + margin:
                    sig = "BUY"
                elif c < lr - margin:
                    sig = "SELL"

            if sig and sl_d > 0:
                tp_d = sl_d * params.get("tp_rr", 2.0)
                sz = (cap * RISK_PCT) / sl_d
                if sz <= 0:
                    continue
                comm = sz * c * COMMISSION_PCT
                if sig == "BUY":
                    sl, tp = c - sl_d, c + tp_d
                else:
                    sl, tp = c + sl_d, c - tp_d
                tr = {"e": c, "sl": sl, "tp": tp, "d": sig, "sz": sz, "cm": comm, "b": i}
                in_trade = True
        else:
            h = float(df.iloc[i]["high"])
            l = float(df.iloc[i]["low"])
            hit_tp = (tr["d"] == "BUY" and h >= tr["tp"]) or (tr["d"] == "SELL" and l <= tr["tp"])
            hit_sl = (tr["d"] == "BUY" and l <= tr["sl"]) or (tr["d"] == "SELL" and h >= tr["sl"])
            if hit_tp or hit_sl:
                ep = tr["tp"] if hit_tp else tr["sl"]
                pnl = ((ep - tr["e"]) if tr["d"] == "BUY" else (tr["e"] - ep)) * tr["sz"] - tr["cm"]
                cap += pnl
                trades.append({"pnl": pnl, "win": pnl > 0})
                in_trade = False

    if not trades:
        return {"total": 0, "pf": 0, "wr": 0, "pnl": 0, "dd": 0, "sharpe": 0}

    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    gp = sum(t["pnl"] for t in wins) if wins else 0
    gl = abs(sum(t["pnl"] for t in losses)) if losses else 0.001

    pnls = np.array([t["pnl"] for t in trades])
    eq = np.cumsum(pnls) + INITIAL_CAPITAL
    pk = np.maximum.accumulate(eq)
    dd = float(((pk - eq) / pk * 100).max())
    sh = float((np.mean(pnls) / np.std(pnls)) * math.sqrt(252)) if np.std(pnls) > 0 else 0

    return {
        "total": len(trades), "wins": len(wins),
        "pf": round(gp / max(gl, 0.001), 3),
        "wr": round(len(wins) / len(trades), 3),
        "pnl": round(sum(t["pnl"] for t in trades), 2),
        "dd": round(dd, 2), "sharpe": round(sh, 2),
    }


# ═══════════════════════════════════════════════════════════════════════
#  DEFIBRILLATOR DISPLAY
# ═══════════════════════════════════════════════════════════════════════

def defib_line(attempt, strat, tf, result, is_alive):
    icon = "💚" if is_alive else "💀"
    pf_s = f"{result['pf']:.2f}" if result['total'] > 0 else "N/A"
    return (
        f"    {icon} [{attempt:>2d}/9] {strat:3s}/{tf:3s}  "
        f"T={result['total']:>3d}  WR={result['wr']:.0%}  "
        f"PF={pf_s:>5s}  PnL=€{result['pnl']:>+8.0f}"
        + ("  ⚡ ALIVE!" if is_alive else "")
    )


# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "⚡" * 25)
    print("  THE RESURRECTION PROTOCOL")
    print("  Strategy Transplant for Dead Instruments")
    print("⚡" * 25)
    print()

    from brokers.capital_client import CapitalClient
    capital = CapitalClient()
    if not capital.available:
        print("  ❌ Capital.com API not available")
        return

    # Load rules
    opt_rules = {}
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE) as f:
            opt_rules = json.load(f)

    resurrected = []
    still_dead = []
    t_global = time.time()

    for idx, instr in enumerate(DEAD_INSTRUMENTS, 1):
        cat = CAT_MAP.get(instr, "forex")

        print(f"\n  {'━' * 56}")
        print(f"  ┃  {idx:>2d}/{len(DEAD_INSTRUMENTS)}  {instr:16s}  ⚡ DEFIBRILLATING...        ┃")
        print(f"  {'━' * 56}")

        found = False
        best_result = None
        best_strat = None
        best_tf = None
        best_params = None
        attempt = 0

        # Try all 9 combinations, stop on first success
        for tf in TIMEFRAMES:
            if found:
                break

            # Fetch data for this timeframe
            time.sleep(1)  # rate limit
            df = capital.fetch_ohlcv(instr, timeframe=tf, count=500)
            if df is None or len(df) < 60:
                for strat in STRATEGIES:
                    attempt += 1
                    print(f"    💀 [{attempt:>2d}/9] {strat:3s}/{tf:3s}  NO DATA")
                continue

            df = compute_indicators(df)

            for strat in STRATEGIES:
                attempt += 1
                params = {"strat": strat, **DEFAULT_PARAMS[strat]}
                result = backtest(df, params, instr, cat)

                is_alive = (
                    result["total"] >= MIN_TRADES_ALIVE
                    and result["pf"] >= MIN_PF_ALIVE
                )

                print(defib_line(attempt, strat, tf, result, is_alive))

                # Track best even if not alive yet
                if result["total"] >= 5:
                    score = result["pf"] * math.sqrt(result["total"])
                    if best_result is None or score > best_result.get("_score", 0):
                        best_result = {**result, "_score": score}
                        best_strat = strat
                        best_tf = tf
                        best_params = params

                if is_alive:
                    found = True
                    best_result = result
                    best_strat = strat
                    best_tf = tf
                    best_params = params
                    break

            # Free dataframe memory
            del df
            gc.collect()

        # Save result
        if best_result and best_strat:
            entry = opt_rules.get(instr, {})
            entry["strat"] = best_strat
            entry["tf"] = best_tf
            entry["cat"] = cat

            for k, v in best_params.items():
                if k != "strat":
                    entry[k] = v

            status = "RESURRECTED" if found else "STABILIZED"
            entry["prometheus_resurrection"] = (
                f"{status} {datetime.now(timezone.utc).strftime('%Y-%m-%d')} | "
                f"{best_strat}/{best_tf} | PF={best_result['pf']:.2f} | "
                f"T={best_result['total']}"
            )
            opt_rules[instr] = entry

            # Save immediately
            with open(RULES_FILE, "w") as f:
                json.dump(opt_rules, f, indent=2)

            if found:
                resurrected.append({
                    "instr": instr, "strat": best_strat, "tf": best_tf,
                    "pf": best_result["pf"], "trades": best_result["total"],
                    "wr": best_result["wr"], "pnl": best_result["pnl"],
                })
                print(f"\n    ⚡⚡⚡ {instr}: RESURRECTED on {best_strat}/{best_tf} "
                      f"(PF={best_result['pf']:.2f}, {best_result['total']} trades) — SAVED ✅")
            else:
                still_dead.append({
                    "instr": instr, "strat": best_strat, "tf": best_tf,
                    "pf": best_result["pf"], "trades": best_result["total"],
                })
                print(f"\n    🩹 {instr}: STABILIZED on {best_strat}/{best_tf} "
                      f"(PF={best_result['pf']:.2f}, {best_result['total']} trades) — best available, SAVED")
        else:
            still_dead.append({"instr": instr, "strat": "?", "tf": "?", "pf": 0, "trades": 0})
            print(f"\n    💀 {instr}: ALL COMBOS DEAD — no viable strategy found")

        gc.collect()

    elapsed = time.time() - t_global

    # ═══════════════════════════════════════════════════════════════════
    #  REPORT
    # ═══════════════════════════════════════════════════════════════════
    print(f"\n\n  {'━' * 56}")
    print(f"  ┃  RESURRECTION REPORT                                  ┃")
    print(f"  {'━' * 56}")
    print(f"  ⏱ Duration: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"  📊 Patients: {len(DEAD_INSTRUMENTS)}")
    print(f"  ⚡ Resurrected: {len(resurrected)}")
    print(f"  🩹 Stabilized: {len([s for s in still_dead if s['trades'] > 0])}")
    print(f"  💀 Still dead: {len([s for s in still_dead if s['trades'] == 0])}")

    if resurrected:
        print(f"\n  ┌─── ⚡ RESURRECTED ────────────────────────────────────┐")
        for r in sorted(resurrected, key=lambda x: x["pf"], reverse=True):
            print(
                f"  │  {r['instr']:16s}  {r['strat']:3s}/{r['tf']:3s}  "
                f"PF={r['pf']:>5.2f}  WR={r['wr']:.0%}  "
                f"T={r['trades']:>3d}  PnL=€{r['pnl']:>+8.0f}  │"
            )
        print(f"  └─────────────────────────────────────────────────────────┘")

    if still_dead:
        alive_sd = [s for s in still_dead if s["trades"] > 0]
        dead_sd = [s for s in still_dead if s["trades"] == 0]

        if alive_sd:
            print(f"\n  ┌─── 🩹 STABILIZED (best available, below threshold) ──┐")
            for s in alive_sd:
                print(f"  │  {s['instr']:16s}  {s['strat']:3s}/{s['tf']:3s}  PF={s['pf']:.2f}  T={s['trades']}  │")
            print(f"  └─────────────────────────────────────────────────────────┘")

        if dead_sd:
            print(f"\n  ┌─── 💀 TRULY DEAD (consider removing from portfolio) ──┐")
            for s in dead_sd:
                print(f"  │  {s['instr']:16s}  No viable strategy found            │")
            print(f"  └─────────────────────────────────────────────────────────┘")

    total_pnl = sum(r["pnl"] for r in resurrected)
    print(f"\n  ╔═══════════════════════════════════════════════════════╗")
    print(f"  ║  ⚡ {len(resurrected)}/{len(DEAD_INSTRUMENTS)} PATIENTS RESURRECTED                    ║")
    print(f"  ║  📊 Combined PnL: €{total_pnl:>+10,.0f}                       ║")
    print(f"  ║  💾 All configs saved to {RULES_FILE:20s}       ║")
    print(f"  ╚═══════════════════════════════════════════════════════╝")
    print()


if __name__ == "__main__":
    main()
