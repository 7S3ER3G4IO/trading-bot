#!/usr/bin/env python3
"""
hf_pivot.py — ⚡ HIGH-VELOCITY MANDATE

Phase 1: Define The Liquid 20 (ban CHF, UK100, exotics)
Phase 2: Mutate all on 1h with volume-oriented fitness (PF×√T, min 40 trades)
Phase 3: Full portfolio backtest with conviction sizing

Usage:
    docker exec nemesis_bot python3 hf_pivot.py
"""

import sys, os, json, time, math, gc, itertools
sys.path.insert(0, ".")

import numpy as np
import pandas as pd

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")


# ═══════════════════════════════════════════════════════════════════════
#  THE LIQUID 20
# ═══════════════════════════════════════════════════════════════════════

# Toxic assets (permanent ban)
TOXIC = {
    "EURCHF", "GBPCHF", "CHFJPY", "AUDNZD", "NZDJPY", "NZDUSD",
    "UK100", "PAIR_GBPCHF_CADCHF", "PAIR_USDCHF_GBPCHF",
}

# The Liquid 20-25: high-volume, tight-spread assets
LIQUID_ASSETS = {
    # Crypto (24/7, high vol)
    "BTCUSD":     {"cat": "crypto"},
    "ETHUSD":     {"cat": "crypto"},
    "SOLUSD":     {"cat": "crypto"},
    "XRPUSD":     {"cat": "crypto"},
    "BNBUSD":     {"cat": "crypto"},
    "AVAXUSD":    {"cat": "crypto"},
    # Forex majors
    "EURUSD":     {"cat": "forex"},
    "GBPJPY":     {"cat": "forex"},
    "USDJPY":     {"cat": "forex"},
    "EURJPY":     {"cat": "forex"},
    "AUDJPY":     {"cat": "forex"},
    "GBPAUD":     {"cat": "forex"},
    # Commodities
    "GOLD":       {"cat": "commodities"},
    "SILVER":     {"cat": "commodities"},
    "OIL_CRUDE":  {"cat": "commodities"},
    "COPPER":     {"cat": "commodities"},
    "NATURALGAS": {"cat": "commodities"},
    # Indices
    "US500":      {"cat": "indices"},
    "US100":      {"cat": "indices"},
    "DE40":       {"cat": "indices"},
    # Stocks (high liquidity)
    "TSLA":       {"cat": "stocks"},
    "NVDA":       {"cat": "stocks"},
    "AAPL":       {"cat": "stocks"},
    "AMD":        {"cat": "stocks"},
}

INITIAL_CAPITAL = 10_000.0
COMMISSION_PCT  = 0.0004
MIN_TRADES_HF   = 40          # Minimum trades for HF validity
RULES_FILE      = "optimized_rules.json"

# Conviction
CONVICTION = {
    "ELITE":  {"min_pf": 2.0, "risk": 0.050},
    "STRONG": {"min_pf": 1.5, "risk": 0.040},
    "SOLID":  {"min_pf": 1.2, "risk": 0.030},
    "BASE":   {"min_pf": 0.0, "risk": 0.025},
}

# Sessions
SESS = {
    "crypto": [(0, 1440)], "forex": [(420, 1260)],
    "commodities": [(360, 1320)], "indices": [(420, 630), (780, 1200)],
    "stocks": [(780, 1200)],
}
ASIA = {"JPY", "AUD", "NZD"}


# ═══════════════════════════════════════════════════════════════════════
#  INDICATORS + SESSION
# ═══════════════════════════════════════════════════════════════════════

def indicators(df):
    c, h, l = df["close"], df["high"], df["low"]
    d = c.diff()
    g = d.clip(lower=0).rolling(14).mean()
    lo = (-d.clip(upper=0)).rolling(14).mean()
    rs = g / lo.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    pdm = h.diff().clip(lower=0); mdm = (-l.diff()).clip(lower=0)
    pdm[pdm < mdm] = 0; mdm[mdm < pdm] = 0
    trs = tr.rolling(14).mean()
    pdi = 100 * (pdm.rolling(14).mean() / trs.replace(0, np.nan))
    mdi = 100 * (mdm.rolling(14).mean() / trs.replace(0, np.nan))
    dx = (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan) * 100
    df["adx"] = dx.rolling(14).mean()
    df["ema9"] = c.ewm(span=9).mean(); df["ema21"] = c.ewm(span=21).mean()
    sma20 = c.rolling(20).mean(); std20 = c.rolling(20).std()
    df["zscore"] = (c - sma20) / std20.replace(0, np.nan)
    return df.dropna()

def sess_ok(ts, instr, cat):
    if cat == "crypto": return True
    if hasattr(ts, "weekday") and ts.weekday() >= 5: return False
    mins = (ts.hour if hasattr(ts, "hour") else 0) * 60 + (ts.minute if hasattr(ts, "minute") else 0)
    ecat = cat
    if cat == "forex":
        for cc in ASIA:
            if cc in instr.upper(): ecat = "forex_asia"; break
    w = SESS.get(ecat, [(420, 1260)])
    if ecat == "forex_asia": w = [(0, 1260)]
    return any(s <= mins < e for s, e in w)


# ═══════════════════════════════════════════════════════════════════════
#  BACKTESTER
# ═══════════════════════════════════════════════════════════════════════

def backtest(df, params, instr, cat, risk_pct=0.025):
    strat = params.get("strat", "MR")
    cap = INITIAL_CAPITAL; peak = cap; trades = []; in_trade = False; tr = {}; eq = [cap]

    for i in range(50, len(df)):
        row = df.iloc[i]; idx = df.index[i]
        c = float(row["close"]); atr = float(row.get("atr", c * 0.01))
        if atr <= 0: continue

        if not in_trade:
            if not sess_ok(idx, instr, cat): continue
            sig = None; sl_d = atr * params.get("sl_buffer", 1.5)

            if strat == "MR":
                rsi = float(row.get("rsi", 50)); zs = float(row.get("zscore", 0))
                if rsi <= params.get("rsi_lo", 35) or zs <= -params.get("zscore_thresh", 1.5): sig = "BUY"
                elif rsi >= params.get("rsi_hi", 65) or zs >= params.get("zscore_thresh", 1.5): sig = "SELL"
            elif strat == "TF":
                ef = float(row.get("ema9", 0)); es = float(row.get("ema21", 0)); adx = float(row.get("adx", 0))
                if ef > es and adx > params.get("adx_min", 20): sig = "BUY"
                elif ef < es and adx > params.get("adx_min", 20): sig = "SELL"
            elif strat == "BK":
                rl = params.get("range_lb", 10)
                if i < rl + 5: continue
                rec = df.iloc[i - rl:i]; hr = float(rec["high"].max()); lr = float(rec["low"].min())
                rng = hr - lr
                if rng <= 0: continue
                adx = float(row.get("adx", 0))
                if params.get("adx_min", 0) > 0 and adx < params["adx_min"]: continue
                margin = rng * params.get("bk_margin", 0.03)
                sl_d = rng * params.get("sl_buffer", 0.10)
                if c > hr + margin: sig = "BUY"
                elif c < lr - margin: sig = "SELL"

            if sig and sl_d > 0:
                tp_d = sl_d * params.get("tp_rr", 1.5)
                sz = (cap * risk_pct) / sl_d
                if sz <= 0: continue
                comm = sz * c * COMMISSION_PCT
                if sig == "BUY": sl, tp = c - sl_d, c + tp_d
                else: sl, tp = c + sl_d, c - tp_d
                tr = {"e": c, "sl": sl, "tp": tp, "d": sig, "sz": sz, "cm": comm, "b": i}
                in_trade = True
        else:
            h = float(df.iloc[i]["high"]); l = float(df.iloc[i]["low"])
            hit_tp = (tr["d"] == "BUY" and h >= tr["tp"]) or (tr["d"] == "SELL" and l <= tr["tp"])
            hit_sl = (tr["d"] == "BUY" and l <= tr["sl"]) or (tr["d"] == "SELL" and h >= tr["sl"])
            if hit_tp or hit_sl:
                ep = tr["tp"] if hit_tp else tr["sl"]
                pnl = ((ep - tr["e"]) if tr["d"] == "BUY" else (tr["e"] - ep)) * tr["sz"] - tr["cm"]
                cap += pnl; peak = max(peak, cap); eq.append(cap)
                trades.append({"pnl": pnl, "win": pnl > 0})
                in_trade = False

    if not trades:
        return {"total": 0, "pf": 0, "wr": 0, "pnl": 0, "dd": 0, "sharpe": 0}
    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    gp = sum(t["pnl"] for t in wins) if wins else 0
    gl = abs(sum(t["pnl"] for t in losses)) if losses else 0.001
    pnls = np.array([t["pnl"] for t in trades])
    eq_a = np.array(eq); pk = np.maximum.accumulate(eq_a)
    dd = float(((pk - eq_a) / pk * 100).max())
    sh = float((np.mean(pnls) / np.std(pnls)) * math.sqrt(252)) if np.std(pnls) > 0 else 0
    return {
        "total": len(trades), "wins": len(wins), "losses": len(losses),
        "pf": round(gp / max(gl, 0.001), 3), "wr": round(len(wins) / len(trades), 3),
        "pnl": round(sum(t["pnl"] for t in trades), 2), "dd": round(dd, 2),
        "sharpe": round(sh, 2), "gp": round(gp, 2), "gl": round(gl, 2),
    }


# ═══════════════════════════════════════════════════════════════════════
#  HF MUTATION GRIDS (1h timeframe, volume-oriented)
# ═══════════════════════════════════════════════════════════════════════

GRIDS = {
    "TF": {
        "strat": ["TF"],
        "adx_min": [15, 20, 25],
        "sl_buffer": [1.0, 1.5, 2.0],
        "tp_rr": [1.5, 2.0],
    },  # 18
    "MR": {
        "strat": ["MR"],
        "rsi_lo": [30, 35, 40],
        "rsi_hi": [60, 65],
        "zscore_thresh": [1.5, 2.0],
        "sl_buffer": [1.0, 1.5],
        "tp_rr": [1.5, 2.0],
    },  # 48
    "BK": {
        "strat": ["BK"],
        "range_lb": [6, 10, 15, 20],
        "bk_margin": [0.02, 0.03, 0.05],
        "adx_min": [0, 15],
        "sl_buffer": [0.08, 0.12],
        "tp_rr": [1.5, 2.0],
    },  # 96
}


def mutate(df, instr, cat):
    """Try all strats × params on 1h data. Volume-oriented fitness."""
    best = None
    best_score = -999
    total_tested = 0

    for strat_name, grid in GRIDS.items():
        keys = list(grid.keys())
        combos = list(itertools.product(*grid.values()))
        for combo in combos:
            total_tested += 1
            p = dict(zip(keys, combo))
            r = backtest(df, p, instr, cat)

            # HF FITNESS: PF × √trades, REJECT if <40 trades
            if r["total"] < MIN_TRADES_HF:
                continue

            score = r["pf"] * math.sqrt(r["total"]) * max(0, 1 - r["dd"] / 100)
            if score > best_score:
                best_score = score
                best = {**r, "params": p, "score": score}

    return best, total_tested


def get_tier(pf):
    for name, t in CONVICTION.items():
        if pf >= t["min_pf"]:
            return name, t["risk"]
    return "BASE", 0.025


# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "⚡" * 25)
    print("  HIGH-VELOCITY MANDATE")
    print("  Liquid 20 | 1H Intraday | Volume Fitness | Aggressive")
    print("⚡" * 25)
    print()

    from brokers.capital_client import CapitalClient
    capital_api = CapitalClient()
    if not capital_api.available:
        print("  ❌ Capital.com API not available"); return

    total = len(LIQUID_ASSETS)
    print(f"  📊 Liquid Assets: {total}")
    print(f"  🚫 Toxic Banned: {len(TOXIC)} ({', '.join(sorted(TOXIC)[:5])}...)")
    print(f"  ⏱ Timeframe: 1H (forced)")
    print(f"  🎯 Min trades: {MIN_TRADES_HF}")
    print(f"  📐 Fitness: PF × √T × (1 - DD/100)")
    print()

    # ═══════════════════════ PHASE 1+2: MUTATE ═══════════════════════
    print(f"  {'━' * 56}")
    print(f"  ┃  PHASE 1+2: OPTIMISATION 1H POUR CHAQUE ACTIF         ┃")
    print(f"  {'━' * 56}\n")

    results = {}
    t0 = time.time()

    for idx, (instr, meta) in enumerate(sorted(LIQUID_ASSETS.items()), 1):
        cat = meta["cat"]
        time.sleep(0.8)
        df = capital_api.fetch_ohlcv(instr, timeframe="1h", count=500)
        if df is None or len(df) < 100:
            print(f"  {idx:>2d}/{total}  {instr:16s}  ⚠️ NO DATA")
            continue

        df = indicators(df)
        best, n = mutate(df, instr, cat)

        if best and best["total"] >= MIN_TRADES_HF and best["pf"] > 0.8:
            tier, risk = get_tier(best["pf"])
            icon = "🟢" if best["pf"] >= 1.2 else "🟡"
            print(
                f"  {idx:>2d}/{total}  {instr:16s}  {icon} {best['params']['strat']:3s}/1h  "
                f"T={best['total']:>3d}  WR={best['wr']:.0%}  PF={best['pf']:>5.2f}  "
                f"PnL=€{best['pnl']:>+8,.0f}  DD={best['dd']:.0f}%  ({n} tested)"
            )
            results[instr] = {
                "r": best, "cat": cat, "tier": tier, "risk": risk,
                "params": best["params"],
            }
        else:
            trades_found = best["total"] if best else 0
            print(f"  {idx:>2d}/{total}  {instr:16s}  ⚫ No viable HF config (best={trades_found}T, {n} tested)")

        del df; gc.collect()

    mut_elapsed = time.time() - t0
    print(f"\n  ⏱ Mutation phase: {mut_elapsed:.0f}s")

    # ═══════════════════ PHASE 3: SAVE RULES ═══════════════════════
    opt_rules = {}
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE) as f:
            opt_rules = json.load(f)

    for instr, data in results.items():
        entry = opt_rules.get(instr, {})
        entry["strat"] = data["params"]["strat"]
        entry["tf"] = "1h"
        entry["cat"] = data["cat"]
        for k, v in data["params"].items():
            if k != "strat":
                entry[k] = v
        entry["pf"] = data["r"]["pf"]
        entry["hf_mandate"] = f"HFv1 {time.strftime('%Y-%m-%d')} | {data['params']['strat']}/1h | PF={data['r']['pf']:.2f} T={data['r']['total']}"
        opt_rules[instr] = entry

    with open(RULES_FILE, "w") as f:
        json.dump(opt_rules, f, indent=2)
    print(f"\n  💾 {len(results)} configs saved to {RULES_FILE}")

    # ═══════════════════ PHASE 4: TEAR SHEET ═══════════════════════
    print(f"\n  {'━' * 56}")
    print(f"  ┃  PHASE 3: PORTFOLIO TEAR SHEET                        ┃")
    print(f"  {'━' * 56}\n")

    active = [v for v in results.values() if v["r"]["total"] >= MIN_TRADES_HF]
    if not active:
        print("  ❌ No viable HF assets"); return

    total_pnl = sum(v["r"]["pnl"] for v in active)
    total_trades = sum(v["r"]["total"] for v in active)
    total_wins = sum(v["r"]["wins"] for v in active)
    total_losses = sum(v["r"]["losses"] for v in active)
    total_gp = sum(v["r"]["gp"] for v in active)
    total_gl = sum(v["r"]["gl"] for v in active)
    pf_global = round(total_gp / max(total_gl, 0.01), 2)
    wr_global = total_wins / max(total_trades, 1)
    avg_dd = sum(v["r"]["dd"] for v in active) / len(active)
    max_dd = max(v["r"]["dd"] for v in active)
    final_cap = INITIAL_CAPITAL + total_pnl
    total_return = total_pnl / INITIAL_CAPITAL * 100

    # Sort by PnL
    ranked = sorted(active, key=lambda v: v["r"]["pnl"], reverse=True)

    print(f"  ╔═══════════════════════════════════════════════════════════╗")
    print(f"  ║                                                         ║")
    print(f"  ║   ⚡  HIGH-VELOCITY TEAR SHEET  ⚡                     ║")
    print(f"  ║   1H Intraday | Liquid 20 | Aggressive Growth          ║")
    print(f"  ║                                                         ║")
    print(f"  ╚═══════════════════════════════════════════════════════════╝\n")

    print(f"  ┌─── PORTFOLIO SUMMARY ─────────────────────────────────┐")
    print(f"  │  💰 Capital:           €{INITIAL_CAPITAL:>12,.0f}              │")
    pi = "📈" if total_pnl > 0 else "📉"
    print(f"  │  {pi} PnL Net:            €{total_pnl:>+12,.0f}              │")
    print(f"  │  📊 Rendement:            {total_return:>+10.1f}%              │")
    print(f"  │  📉 Max DD (worst):       {max_dd:>10.1f}%              │")
    print(f"  │  📉 Avg DD:               {avg_dd:>10.1f}%              │")
    print(f"  └─────────────────────────────────────────────────────────┘\n")

    print(f"  ┌─── MÉTRIQUES CLÉS ────────────────────────────────────┐")
    print(f"  │  📊 Total Trades:       {total_trades:>8,d}                    │")
    print(f"  │  ✅ Wins:               {total_wins:>8,d}                    │")
    print(f"  │  ❌ Losses:             {total_losses:>8,d}                    │")
    wr_bar = "█" * int(wr_global * 20) + "░" * (20 - int(wr_global * 20))
    print(f"  │  🎯 Win Rate:          {wr_global:>8.1%}  {wr_bar}    │")
    pf_i = "🟢" if pf_global >= 1.2 else "🟡" if pf_global >= 1.0 else "🔴"
    print(f"  │  {pf_i} Profit Factor:     {pf_global:>8.2f}                    │")
    print(f"  │  📊 Active Assets:     {len(active):>8d}                    │")
    tpd = total_trades / max(len(active), 1)
    print(f"  │  ⚡ Trades/Asset:      {tpd:>8.0f}                    │")
    print(f"  └─────────────────────────────────────────────────────────┘\n")

    # Tier breakdown
    tier_stats = {}
    for v in active:
        t = v["tier"]
        if t not in tier_stats: tier_stats[t] = {"n": 0, "pnl": 0, "trades": 0}
        tier_stats[t]["n"] += 1
        tier_stats[t]["pnl"] += v["r"]["pnl"]
        tier_stats[t]["trades"] += v["r"]["total"]

    print(f"  ┌─── CONVICTION BREAKDOWN ─────────────────────────────┐")
    for tn in ["ELITE", "STRONG", "SOLID", "BASE"]:
        if tn in tier_stats:
            ts = tier_stats[tn]
            rp = CONVICTION[tn]["risk"]
            print(f"  │  {tn:8s}  {ts['n']:>2d} assets  {ts['trades']:>4d} trades  €{ts['pnl']:>+9,.0f}  R={rp:.1%} │")
    print(f"  └─────────────────────────────────────────────────────────┘\n")

    # Top 5
    print(f"  ┌─── 🏆 TOP 5 GENERATORS ──────────────────────────────┐")
    for i, v in enumerate(ranked[:5], 1):
        instr = [k for k, val in results.items() if val is v][0]
        print(f"  │  #{i} {instr:14s} {v['params']['strat']:3s}/1h  PnL=€{v['r']['pnl']:>+8,.0f}  PF={v['r']['pf']:.2f}  T={v['r']['total']:>3d} │")
    print(f"  └─────────────────────────────────────────────────────────┘\n")

    # Bottom 3
    bottom = ranked[-3:]
    bottom.reverse()
    print(f"  ┌─── 📉 BOTTOM 3 ──────────────────────────────────────┐")
    for v in bottom:
        instr = [k for k, val in results.items() if val is v][0]
        if v["r"]["pnl"] < 0:
            print(f"  │  {instr:14s} PnL=€{v['r']['pnl']:>+8,.0f}  PF={v['r']['pf']:.2f}  T={v['r']['total']:>3d}              │")
    print(f"  └─────────────────────────────────────────────────────────┘\n")

    # Verdict
    print(f"  ╔═══════════════════════════════════════════════════════╗")
    if total_pnl > 0 and pf_global >= 1.2:
        print(f"  ║  🏆 HIGH-VELOCITY PORTFOLIO: PROFITABLE              ║")
    elif total_pnl > 0:
        print(f"  ║  ✅ HV PORTFOLIO: POSITIVE — ROOM TO GROW             ║")
    else:
        print(f"  ║  ⚠️  HV PORTFOLIO: NEGATIVE — NEEDS REFINEMENT        ║")
    print(f"  ║  PnL: €{total_pnl:>+10,.0f} ({total_return:>+.1f}%)                       ║")
    print(f"  ║  PF: {pf_global:.2f} | WR: {wr_global:.0%} | {total_trades:,d} trades                  ║")
    print(f"  ║  Assets: {len(active)} active / {total} liquid pool              ║")
    print(f"  ╚═══════════════════════════════════════════════════════╝\n")


if __name__ == "__main__":
    main()
