#!/usr/bin/env python3
"""
global_sweep.py — 🌍 GLOBAL PROMETHEUS SWEEP

Sequential scan of ALL ~40 God Mode instruments:
  1. Backtest each on 1000 bars
  2. Triage: HEALTHY (PF≥1.2, trades≥10) vs SICK
  3. Mutate SICK with light grid (≤150 combos)
  4. Auto-save after each mutation + gc.collect()

Docker-safe: sequential, memory-managed, no OOM.

Usage:
    docker exec nemesis_bot python3 global_sweep.py
"""

import sys, os, json, time, math, gc, itertools
from datetime import datetime, timezone

sys.path.insert(0, ".")

import numpy as np
import pandas as pd

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

INITIAL_CAPITAL = 10_000.0
RISK_PCT        = 0.01
COMMISSION_PCT  = 0.0004
MIN_RR          = 1.5
RULES_FILE      = "optimized_rules.json"

# Triage thresholds
HEALTHY_PF      = 1.2
HEALTHY_MIN_TR  = 10
SICK_MAX_DD     = 15.0

# Mutation constraints
MAX_COMBOS      = 60      # keep grid ≤60 per asset (Docker safe)
MIN_MUTATION_TR = 5       # mutation must generate ≥5 trades

# Skip pair-trading (needs separate logic)
SKIP_PREFIXES   = ("PAIR_",)


# ═══════════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ═══════════════════════════════════════════════════════════════════════════════

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
    df["ema50"] = c.ewm(span=50).mean()

    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    df["zscore"] = (c - sma20) / std20.replace(0, np.nan)

    return df.dropna()


# ═══════════════════════════════════════════════════════════════════════════════
#  SESSION FILTER
# ═══════════════════════════════════════════════════════════════════════════════

SESS = {
    "crypto": [(0, 1440)],
    "forex": [(420, 1260)],
    "commodities": [(360, 1320)],
    "indices": [(420, 630), (780, 1200)],
    "stocks": [(780, 1200)],
}
ASIA_CCY = {"JPY", "AUD", "NZD"}

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


# ═══════════════════════════════════════════════════════════════════════════════
#  FAST BACKTESTER
# ═══════════════════════════════════════════════════════════════════════════════

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

            if strat in ("MR", "ML"):
                rsi = float(row.get("rsi", 50))
                zs  = float(row.get("zscore", 0))
                if rsi <= params.get("rsi_lo", 30) or zs <= -params.get("zscore_thresh", 2.0):
                    sig = "BUY"
                elif rsi >= params.get("rsi_hi", 70) or zs >= params.get("zscore_thresh", 2.0):
                    sig = "SELL"

            elif strat == "TF":
                ef = float(row.get("ema9", 0))
                es = float(row.get("ema21", 0))
                adx = float(row.get("adx", 0))
                if ef > es and adx > params.get("adx_min", 20):
                    sig = "BUY"
                elif ef < es and adx > params.get("adx_min", 20):
                    sig = "SELL"

            elif strat in ("BK", "M51"):
                rl = params.get("range_lb", 10)
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
                sl_d = rng * params.get("sl_buffer", 0.10)

                if c > hr + margin:
                    sig = "BUY"
                elif c < lr - margin:
                    sig = "SELL"

            if sig and sl_d > 0:
                tp_d = sl_d * params.get("tp_rr", MIN_RR)
                risk_a = cap * RISK_PCT
                sz = risk_a / sl_d
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
        "total": len(trades), "wins": len(wins), "losses": len(losses),
        "pf": round(gp / max(gl, 0.001), 3),
        "wr": round(len(wins) / len(trades), 3),
        "pnl": round(sum(t["pnl"] for t in trades), 2),
        "dd": round(dd, 2), "sharpe": round(sh, 2),
        "final": round(cap, 2),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  MUTATION GRIDS (light — ≤120 combos each)
# ═══════════════════════════════════════════════════════════════════════════════

def get_mutation_grid(strat):
    if strat in ("MR", "ML"):
        return {
            "strat": [strat],
            "rsi_lo": [30, 35, 40],
            "rsi_hi": [60, 65],
            "zscore_thresh": [1.5, 2.0],
            "sl_buffer": [1.5, 2.0],
            "tp_rr": [1.5, 2.0],
        }  # 3×2×2×2×2 = 48

    elif strat == "TF":
        return {
            "strat": ["TF"],
            "adx_min": [15, 20, 25],
            "sl_buffer": [1.0, 1.5, 2.0],
            "tp_rr": [1.5, 2.0],
        }  # 3×3×2 = 18

    elif strat in ("BK", "M51"):
        return {
            "strat": [strat],
            "range_lb": [10, 20, 40],
            "bk_margin": [0.03, 0.05],
            "adx_min": [0, 20],
            "sl_buffer": [0.10, 0.20],
            "tp_rr": [1.5, 2.5],
        }  # 3×2×2×2×2 = 48

    return None


def grid_search(df, grid, instr, cat):
    keys = list(grid.keys())
    all_combos = list(itertools.product(*grid.values()))

    # Trim to MAX_COMBOS by sampling evenly
    if len(all_combos) > MAX_COMBOS:
        step = len(all_combos) / MAX_COMBOS
        all_combos = [all_combos[int(i * step)] for i in range(MAX_COMBOS)]

    best = None
    best_score = -999

    for combo in all_combos:
        p = dict(zip(keys, combo))
        r = backtest(df, p, instr, cat)
        if r["total"] < MIN_MUTATION_TR:
            continue
        # Score: PF × sqrt(trades) × (1 - DD/100)
        score = r["pf"] * math.sqrt(r["total"]) * max(0, 1 - r["dd"] / 100)
        if score > best_score:
            best_score = score
            best = {**r, "params": p, "score": score}

    return best, len(all_combos)


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "🌍" * 25)
    print("  GLOBAL PROMETHEUS SWEEP")
    print("  Scanning ALL instruments. Sequential. No OOM.")
    print("🌍" * 25)
    print()

    from brokers.capital_client import CapitalClient
    capital = CapitalClient()
    if not capital.available:
        print("  ❌ Capital.com API not available")
        return

    # Load all instruments from God Mode
    try:
        from god_mode import GOD_MODE_RULES
        instruments = GOD_MODE_RULES
    except Exception:
        print("  ❌ Cannot load GOD_MODE_RULES")
        return

    # Load current optimized rules
    opt_rules = {}
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE) as f:
            opt_rules = json.load(f)

    total = len(instruments)
    healthy = []
    sick = []
    mutated = []
    skipped = []
    no_data = []

    t_global = time.time()

    for idx, (instr, rule) in enumerate(sorted(instruments.items()), 1):
        strat = rule.get("strat", "?")
        tf = rule.get("tf", "1d")
        cat = rule.get("cat", "forex")

        # Skip pair-trading
        if any(instr.startswith(p) for p in SKIP_PREFIXES):
            print(f"  {idx:>2d}/{total}  {instr:20s}  ⏭ SKIP (pairs)")
            skipped.append(instr)
            gc.collect()
            continue

        # Map strat for backtest
        bt_strat = strat
        if strat == "PAIRS":
            skipped.append(instr)
            continue

        # Fetch data
        time.sleep(1)  # rate limit between API calls
        df = capital.fetch_ohlcv(instr, timeframe=tf, count=500)
        if df is None or len(df) < 60:
            print(f"  {idx:>2d}/{total}  {instr:20s}  ⚠️  NO DATA ({len(df) if df is not None else 0} bars)")
            no_data.append(instr)
            gc.collect()
            continue

        df = compute_indicators(df)
        bars = len(df)

        # Build baseline params from rule
        params = {"strat": bt_strat}
        for k in ("rsi_lo", "rsi_hi", "zscore_thresh", "sl_buffer", "tp_rr",
                   "range_lb", "bk_margin", "adx_min"):
            if k in rule:
                params[k] = rule[k]
            elif k in opt_rules.get(instr, {}):
                params[k] = opt_rules[instr][k]

        # Defaults for missing params
        if bt_strat in ("MR", "ML"):
            params.setdefault("rsi_lo", 30)
            params.setdefault("rsi_hi", 70)
            params.setdefault("zscore_thresh", 2.0)
            params.setdefault("sl_buffer", 1.5)
            params.setdefault("tp_rr", 1.5)
        elif bt_strat == "TF":
            params.setdefault("adx_min", 20)
            params.setdefault("sl_buffer", 1.5)
            params.setdefault("tp_rr", 1.5)
        elif bt_strat in ("BK", "M51"):
            params.setdefault("range_lb", 10)
            params.setdefault("bk_margin", 0.03)
            params.setdefault("adx_min", 0)
            params.setdefault("sl_buffer", 0.10)
            params.setdefault("tp_rr", 1.5)

        # Run baseline backtest
        result = backtest(df, params, instr, cat)

        # Triage
        is_healthy = (
            result["total"] >= HEALTHY_MIN_TR
            and result["pf"] >= HEALTHY_PF
            and result["dd"] <= SICK_MAX_DD
        )

        if is_healthy:
            pf_s = f"{result['pf']:.2f}"
            print(
                f"  {idx:>2d}/{total}  {instr:20s}  🟢 HEALTHY  "
                f"T={result['total']:>3d}  WR={result['wr']:.0%}  "
                f"PF={pf_s:>5s}  PnL=€{result['pnl']:>+8.0f}  "
                f"DD={result['dd']:.1f}%"
            )
            healthy.append({"instr": instr, **result})
        else:
            reason = []
            if result["total"] < HEALTHY_MIN_TR:
                reason.append(f"trades={result['total']}")
            if result["pf"] < HEALTHY_PF:
                reason.append(f"PF={result['pf']:.2f}")
            if result["dd"] > SICK_MAX_DD:
                reason.append(f"DD={result['dd']:.1f}%")

            print(
                f"  {idx:>2d}/{total}  {instr:20s}  🔴 SICK     "
                f"T={result['total']:>3d}  WR={result['wr']:.0%}  "
                f"PF={result['pf']:.2f}  ({', '.join(reason)})"
            )

            # Mutate
            grid = get_mutation_grid(bt_strat)
            if grid:
                best, n_tested = grid_search(df, grid, instr, cat)
                if best and best["pf"] > result["pf"]:
                    # Apply mutation
                    entry = opt_rules.get(instr, {})
                    old_pf = result["pf"]
                    new_pf = best["pf"]

                    for k, v in best["params"].items():
                        entry[k] = v

                    entry["cat"] = cat
                    entry["tf"] = tf
                    entry["prometheus_sweep"] = (
                        f"Swept {datetime.now(timezone.utc).strftime('%Y-%m-%d')} | "
                        f"PF: {old_pf:.2f}→{new_pf:.2f} | "
                        f"T: {result['total']}→{best['total']}"
                    )
                    opt_rules[instr] = entry

                    # Save immediately
                    with open(RULES_FILE, "w") as f:
                        json.dump(opt_rules, f, indent=2)

                    mutated.append({
                        "instr": instr, "old_pf": old_pf, "new_pf": new_pf,
                        "old_trades": result["total"], "new_trades": best["total"],
                        "new_pnl": best["pnl"], "params": best["params"],
                    })

                    print(
                        f"        → 🔥 MUTATED ({n_tested} tested) "
                        f"PF={old_pf:.2f}→{new_pf:.2f}  "
                        f"T={result['total']}→{best['total']}  "
                        f"PnL=€{best['pnl']:+.0f}  SAVED ✅"
                    )
                else:
                    sick.append({"instr": instr, **result})
                    print(f"        → ⚠️  No improvement found ({n_tested} tested)")
            else:
                sick.append({"instr": instr, **result})
                print(f"        → ⏭  No grid for strat={bt_strat}")

        # RAM cleanup
        del df
        gc.collect()

    elapsed = time.time() - t_global

    # ═══════════════════════════════════════════════════════════════════════
    #  FINAL REPORT
    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n  {'━' * 60}")
    print(f"  ┃  GLOBAL SWEEP REPORT                                    ┃")
    print(f"  {'━' * 60}")
    print(f"  ⏱ Duration: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"  📊 Total instruments: {total}")
    print(f"  🟢 HEALTHY:  {len(healthy)}")
    print(f"  🔥 MUTATED:  {len(mutated)}")
    print(f"  🔴 SICK (unfixable): {len(sick)}")
    print(f"  ⏭ SKIPPED:  {len(skipped)}")
    print(f"  ⚠️  NO DATA:  {len(no_data)}")

    if healthy:
        print(f"\n  ┌─── 🟢 HEALTHY INSTRUMENTS ─────────────────────────────┐")
        for h in sorted(healthy, key=lambda x: x["pf"], reverse=True):
            print(f"  │  {h['instr']:16s}  PF={h['pf']:>5.2f}  WR={h['wr']:.0%}  T={h['total']:>3d}  PnL=€{h['pnl']:>+8.0f}  │")
        print(f"  └─────────────────────────────────────────────────────────┘")

    if mutated:
        print(f"\n  ┌─── 🔥 MUTATED INSTRUMENTS ─────────────────────────────┐")
        for m in mutated:
            print(f"  │  {m['instr']:16s}  PF {m['old_pf']:.2f}→{m['new_pf']:.2f}  T {m['old_trades']}→{m['new_trades']}  PnL=€{m['new_pnl']:>+.0f}  │")
        print(f"  └─────────────────────────────────────────────────────────┘")

    if sick:
        print(f"\n  ┌─── 🔴 STILL SICK (need manual review) ────────────────┐")
        for s in sick:
            print(f"  │  {s['instr']:16s}  PF={s['pf']:.2f}  T={s['total']}  │")
        print(f"  └─────────────────────────────────────────────────────────┘")

    # Portfolio summary
    all_pnl = sum(h["pnl"] for h in healthy) + sum(m["new_pnl"] for m in mutated)
    print(f"\n  ╔═══════════════════════════════════════════════════════╗")
    print(f"  ║  📊 PORTFOLIO ESTIMATED PnL: €{all_pnl:>+10,.0f}            ║")
    print(f"  ║  🟢 {len(healthy)} healthy + 🔥 {len(mutated)} mutated = {len(healthy)+len(mutated)} optimized    ║")
    print(f"  ╚═══════════════════════════════════════════════════════╝")
    print()


if __name__ == "__main__":
    main()
