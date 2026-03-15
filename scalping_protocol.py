#!/usr/bin/env python3
"""
scalping_protocol.py — 🔪 THE MEAT GRINDER

Hyper-frequency scalping on 15m (most) / 5m (BTC, ETH).
Loose triggers + tight SL/TP + commission on every trade.
Target: 3000-5000 trades across 20 liquid assets.

Usage:
    docker exec nemesis_bot python3 scalping_protocol.py
"""

import sys, os, json, time, math, gc, itertools
sys.path.insert(0, ".")

import numpy as np
import pandas as pd

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")


# ═══════════════════════════════════════════════════════════════════════
#  THE LIQUID 20 — with assigned micro-timeframes
# ═══════════════════════════════════════════════════════════════════════

ASSETS = {
    # Crypto — 5m (ultra liquid, 24/7)
    "BTCUSD":     {"cat": "crypto",      "tf": "5m"},
    "ETHUSD":     {"cat": "crypto",      "tf": "5m"},
    "SOLUSD":     {"cat": "crypto",      "tf": "15m"},
    "XRPUSD":     {"cat": "crypto",      "tf": "15m"},
    "BNBUSD":     {"cat": "crypto",      "tf": "15m"},
    "AVAXUSD":    {"cat": "crypto",      "tf": "15m"},
    # Forex — 15m
    "EURUSD":     {"cat": "forex",       "tf": "15m"},
    "GBPJPY":     {"cat": "forex",       "tf": "15m"},
    "USDJPY":     {"cat": "forex",       "tf": "15m"},
    "EURJPY":     {"cat": "forex",       "tf": "15m"},
    "AUDJPY":     {"cat": "forex",       "tf": "15m"},
    "GBPAUD":     {"cat": "forex",       "tf": "15m"},
    # Commodities — 15m
    "GOLD":       {"cat": "commodities", "tf": "15m"},
    "SILVER":     {"cat": "commodities", "tf": "15m"},
    "OIL_CRUDE":  {"cat": "commodities", "tf": "15m"},
    "NATURALGAS": {"cat": "commodities", "tf": "15m"},
    # Indices — 15m
    "US500":      {"cat": "indices",     "tf": "15m"},
    "US100":      {"cat": "indices",     "tf": "15m"},
    "DE40":       {"cat": "indices",     "tf": "15m"},
    # Stocks — 15m
    "TSLA":       {"cat": "stocks",      "tf": "15m"},
}

INITIAL_CAPITAL = 10_000.0
COMMISSION_PCT  = 0.0004     # 0.04% round-trip per trade
MIN_TRADES      = 80         # Scalping minimum per asset
RULES_FILE      = "optimized_rules.json"

# Conviction (same as before)
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
#  SCALPING GRIDS — loose triggers, tight SL/TP
# ═══════════════════════════════════════════════════════════════════════

GRIDS = {
    "BK": {
        "strat": ["BK"],
        "range_lb": [6, 8, 10, 12, 15],
        "bk_margin": [0.01, 0.02, 0.03],
        "adx_min": [0, 10],
        "sl_buffer": [0.05, 0.08, 0.10],
        "tp_rr": [1.2, 1.5, 2.0],
    },  # 5×3×2×3×3 = 270
    "MR": {
        "strat": ["MR"],
        "rsi_lo": [35, 40, 45],
        "rsi_hi": [55, 60, 65],
        "zscore_thresh": [1.0, 1.3, 1.5],
        "sl_buffer": [0.8, 1.0, 1.2],
        "tp_rr": [1.2, 1.5],
    },  # 3×3×3×3×2 = 162
    "TF": {
        "strat": ["TF"],
        "adx_min": [10, 15, 20],
        "sl_buffer": [0.8, 1.0, 1.2],
        "tp_rr": [1.2, 1.5, 2.0],
    },  # 3×3×3 = 27
}

MAX_COMBOS = 150  # trim each grid to 150 max


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
#  BACKTESTER (scalping-optimized)
# ═══════════════════════════════════════════════════════════════════════

def backtest(df, params, instr, cat, risk_pct=0.025):
    strat = params.get("strat", "MR")
    cap = INITIAL_CAPITAL; trades = []; in_trade = False; tr = {}; eq = [cap]

    for i in range(30, len(df)):
        row = df.iloc[i]; idx = df.index[i]
        c = float(row["close"]); atr = float(row.get("atr", c * 0.001))
        if atr <= 0: continue

        if not in_trade:
            if not sess_ok(idx, instr, cat): continue
            sig = None; sl_d = atr * params.get("sl_buffer", 1.0)

            if strat == "MR":
                rsi = float(row.get("rsi", 50)); zs = float(row.get("zscore", 0))
                if rsi <= params.get("rsi_lo", 40) or zs <= -params.get("zscore_thresh", 1.0): sig = "BUY"
                elif rsi >= params.get("rsi_hi", 60) or zs >= params.get("zscore_thresh", 1.0): sig = "SELL"
            elif strat == "TF":
                ef = float(row.get("ema9", 0)); es = float(row.get("ema21", 0)); adx = float(row.get("adx", 0))
                if ef > es and adx > params.get("adx_min", 15): sig = "BUY"
                elif ef < es and adx > params.get("adx_min", 15): sig = "SELL"
            elif strat == "BK":
                rl = params.get("range_lb", 10)
                if i < rl + 3: continue
                rec = df.iloc[i - rl:i]; hr = float(rec["high"].max()); lr = float(rec["low"].min())
                rng = hr - lr
                if rng <= 0: continue
                adx = float(row.get("adx", 0))
                if params.get("adx_min", 0) > 0 and adx < params["adx_min"]: continue
                margin = rng * params.get("bk_margin", 0.02)
                sl_d = rng * params.get("sl_buffer", 0.08)
                if c > hr + margin: sig = "BUY"
                elif c < lr - margin: sig = "SELL"

            if sig and sl_d > 0:
                tp_d = sl_d * params.get("tp_rr", 1.5)
                sz = (cap * risk_pct) / sl_d
                if sz <= 0: continue
                comm = sz * c * COMMISSION_PCT  # REAL commission on every trade
                if sig == "BUY": sl, tp = c - sl_d, c + tp_d
                else: sl, tp = c + sl_d, c - tp_d
                tr = {"e": c, "sl": sl, "tp": tp, "d": sig, "sz": sz, "cm": comm}
                in_trade = True
        else:
            h = float(df.iloc[i]["high"]); l = float(df.iloc[i]["low"])
            hit_tp = (tr["d"] == "BUY" and h >= tr["tp"]) or (tr["d"] == "SELL" and l <= tr["tp"])
            hit_sl = (tr["d"] == "BUY" and l <= tr["sl"]) or (tr["d"] == "SELL" and h >= tr["sl"])
            if hit_tp or hit_sl:
                ep = tr["tp"] if hit_tp else tr["sl"]
                pnl = ((ep - tr["e"]) if tr["d"] == "BUY" else (tr["e"] - ep)) * tr["sz"] - tr["cm"]
                cap += pnl; eq.append(cap)
                trades.append({"pnl": pnl, "win": pnl > 0})
                in_trade = False

    if not trades:
        return {"total": 0, "pf": 0, "wr": 0, "pnl": 0, "dd": 0, "sharpe": 0, "gp": 0, "gl": 0}
    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    gp = sum(t["pnl"] for t in wins) if wins else 0
    gl = abs(sum(t["pnl"] for t in losses)) if losses else 0.001
    pnls = np.array([t["pnl"] for t in trades])
    eq_a = np.array(eq); pk = np.maximum.accumulate(eq_a)
    dd = float(((pk - eq_a) / pk * 100).max())
    sh = float((np.mean(pnls) / np.std(pnls)) * math.sqrt(252 * 24)) if np.std(pnls) > 0 else 0
    return {
        "total": len(trades), "wins": len(wins), "losses": len(losses),
        "pf": round(gp / max(gl, 0.001), 3), "wr": round(len(wins) / len(trades), 3),
        "pnl": round(sum(t["pnl"] for t in trades), 2), "dd": round(dd, 2),
        "sharpe": round(sh, 2), "gp": round(gp, 2), "gl": round(gl, 2),
    }


# ═══════════════════════════════════════════════════════════════════════
#  MUTATION ENGINE
# ═══════════════════════════════════════════════════════════════════════

def mutate(df, instr, cat):
    best = None; best_score = -999; total_tested = 0

    for strat_name, grid in GRIDS.items():
        keys = list(grid.keys())
        combos = list(itertools.product(*grid.values()))
        # Trim
        if len(combos) > MAX_COMBOS:
            step = len(combos) / MAX_COMBOS
            combos = [combos[int(i * step)] for i in range(MAX_COMBOS)]

        for combo in combos:
            total_tested += 1
            p = dict(zip(keys, combo))
            r = backtest(df, p, instr, cat)

            if r["total"] < MIN_TRADES:
                continue

            # Volume-oriented fitness: PF × √trades
            score = r["pf"] * math.sqrt(r["total"]) * max(0, 1 - r["dd"] / 100)
            if score > best_score:
                best_score = score
                best = {**r, "params": p, "score": score}

    return best, total_tested


def get_tier(pf):
    for name, t in CONVICTION.items():
        if pf >= t["min_pf"]: return name, t["risk"]
    return "BASE", 0.025


# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "🔪" * 25)
    print("  THE SCALPING PROTOCOL — MEAT GRINDER")
    print("  15m / 5m | Loose Triggers | Tight SL/TP")
    print("🔪" * 25)
    print()

    from brokers.capital_client import CapitalClient
    api = CapitalClient()
    if not api.available:
        print("  ❌ API not available"); return

    total = len(ASSETS)
    print(f"  📊 Assets: {total}")
    print(f"  ⏱ Timeframes: 5m (BTC/ETH), 15m (rest)")
    print(f"  🎯 Min trades/asset: {MIN_TRADES}")
    print(f"  💸 Commission: {COMMISSION_PCT:.2%} per trade (REAL)")
    print()

    # PHASE 1: OPTIMIZE EACH
    print(f"  {'━' * 56}")
    print(f"  ┃  PHASE 1: SCALP OPTIMIZATION                          ┃")
    print(f"  {'━' * 56}\n")

    results = {}
    t0 = time.time()

    for idx, (instr, meta) in enumerate(sorted(ASSETS.items()), 1):
        cat = meta["cat"]; tf = meta["tf"]

        time.sleep(0.8)
        df = api.fetch_ohlcv(instr, timeframe=tf, count=500)
        if df is None or len(df) < 80:
            print(f"  {idx:>2d}/{total}  {instr:14s}  ⚠️ NO DATA ({len(df) if df is not None else 0})")
            continue

        df = indicators(df)
        best, n = mutate(df, instr, cat)

        if best and best["total"] >= MIN_TRADES and best["pf"] > 0.7:
            tier, risk = get_tier(best["pf"])
            icon = "🟢" if best["pf"] >= 1.2 else "🟡"
            print(
                f"  {idx:>2d}/{total}  {instr:14s}  {icon} {best['params']['strat']:3s}/{tf:3s}  "
                f"T={best['total']:>4d}  WR={best['wr']:.0%}  PF={best['pf']:>5.2f}  "
                f"PnL=€{best['pnl']:>+9,.0f}  DD={best['dd']:.0f}%"
            )
            results[instr] = {"r": best, "cat": cat, "tf": tf, "tier": tier, "risk": risk, "params": best["params"]}
        else:
            bt = best["total"] if best else 0
            print(f"  {idx:>2d}/{total}  {instr:14s}  ⚫ No viable scalp ({bt}T, {n} tested)")

        del df; gc.collect()

    mut_time = time.time() - t0
    print(f"\n  ⏱ Optimization: {mut_time:.0f}s ({mut_time/60:.1f}min)")

    # PHASE 2: SAVE
    opt = {}
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE) as f: opt = json.load(f)

    for instr, d in results.items():
        e = opt.get(instr, {})
        e["strat"] = d["params"]["strat"]; e["tf"] = d["tf"]; e["cat"] = d["cat"]
        for k, v in d["params"].items():
            if k != "strat": e[k] = v
        e["pf"] = d["r"]["pf"]
        e["scalp_mandate"] = f"SCALP {time.strftime('%Y-%m-%d')} | {d['params']['strat']}/{d['tf']} | PF={d['r']['pf']:.2f} T={d['r']['total']}"
        opt[instr] = e
    with open(RULES_FILE, "w") as f: json.dump(opt, f, indent=2)
    print(f"  💾 {len(results)} configs saved")

    # PHASE 3: TEAR SHEET
    active = [v for v in results.values() if v["r"]["total"] >= MIN_TRADES]
    if not active:
        print("  ❌ No viable scalp assets"); return

    total_pnl = sum(v["r"]["pnl"] for v in active)
    total_trades = sum(v["r"]["total"] for v in active)
    total_wins = sum(v["r"]["wins"] for v in active)
    total_losses = sum(v["r"]["losses"] for v in active)
    total_gp = sum(v["r"]["gp"] for v in active)
    total_gl = sum(v["r"]["gl"] for v in active)
    pf_g = round(total_gp / max(total_gl, 0.01), 2)
    wr_g = total_wins / max(total_trades, 1)
    max_dd = max(v["r"]["dd"] for v in active)
    avg_dd = sum(v["r"]["dd"] for v in active) / len(active)
    total_ret = total_pnl / INITIAL_CAPITAL * 100
    commission_total = total_trades * 0.04  # approximate % of PnL eaten by commission
    avg_tpa = total_trades / len(active)

    ranked = sorted(active, key=lambda v: v["r"]["pnl"], reverse=True)

    print(f"\n  {'═' * 60}")
    print(f"  ║                                                          ║")
    print(f"  ║   🔪  SCALPING TEAR SHEET — THE MEAT GRINDER  🔪       ║")
    print(f"  ║   15m/5m | Loose Triggers | Commission Included        ║")
    print(f"  ║                                                          ║")
    print(f"  {'═' * 60}\n")

    print(f"  ┌─── PORTFOLIO SUMMARY ─────────────────────────────────┐")
    print(f"  │  💰 Capital:           €{INITIAL_CAPITAL:>12,.0f}              │")
    pi = "📈" if total_pnl > 0 else "📉"
    print(f"  │  {pi} PnL Net:            €{total_pnl:>+12,.0f}              │")
    print(f"  │  📊 Rendement:            {total_ret:>+10.1f}%              │")
    print(f"  │  📉 Max DD (worst):       {max_dd:>10.1f}%              │")
    print(f"  │  📉 Avg DD:               {avg_dd:>10.1f}%              │")
    print(f"  └─────────────────────────────────────────────────────────┘\n")

    print(f"  ┌─── MÉTRIQUES CLÉS ────────────────────────────────────┐")
    print(f"  │  ⚡ Total Trades:       {total_trades:>8,d}  (target: 3000+)    │")
    print(f"  │  ✅ Wins:               {total_wins:>8,d}                    │")
    print(f"  │  ❌ Losses:             {total_losses:>8,d}                    │")
    wr_bar = "█" * int(wr_g * 20) + "░" * (20 - int(wr_g * 20))
    print(f"  │  🎯 Win Rate:          {wr_g:>8.1%}  {wr_bar}    │")
    pf_i = "🟢" if pf_g >= 1.2 else "🟡" if pf_g >= 1.0 else "🔴"
    print(f"  │  {pf_i} Profit Factor:     {pf_g:>8.2f}                    │")
    print(f"  │  📊 Active Assets:     {len(active):>8d}                    │")
    print(f"  │  ⚡ Trades/Asset:      {avg_tpa:>8.0f}                    │")
    print(f"  │  💸 Commission Impact: ~{COMMISSION_PCT*100:.02f}%/trade              │")
    print(f"  └─────────────────────────────────────────────────────────┘\n")

    # Tier breakdown
    ts = {}
    for v in active:
        t = v["tier"]
        if t not in ts: ts[t] = {"n": 0, "pnl": 0, "trades": 0}
        ts[t]["n"] += 1; ts[t]["pnl"] += v["r"]["pnl"]; ts[t]["trades"] += v["r"]["total"]

    print(f"  ┌─── CONVICTION BREAKDOWN ─────────────────────────────┐")
    for tn in ["ELITE", "STRONG", "SOLID", "BASE"]:
        if tn in ts:
            s = ts[tn]; rp = CONVICTION[tn]["risk"]
            print(f"  │  {tn:8s}  {s['n']:>2d} assets  {s['trades']:>5d}T  €{s['pnl']:>+10,.0f}  R={rp:.1%} │")
    print(f"  └─────────────────────────────────────────────────────────┘\n")

    # Top 5
    print(f"  ┌─── 🏆 TOP 5 SCALPERS ───────────────────────────────┐")
    for i, v in enumerate(ranked[:5], 1):
        nm = [k for k, val in results.items() if val is v][0]
        print(f"  │  #{i} {nm:12s} {v['params']['strat']:3s}/{v['tf']:3s}  T={v['r']['total']:>4d}  PF={v['r']['pf']:.2f}  PnL=€{v['r']['pnl']:>+9,.0f} │")
    print(f"  └─────────────────────────────────────────────────────────┘\n")

    # Bottom 3
    bottom = [v for v in ranked[-3:] if v["r"]["pnl"] < 0]
    bottom.reverse()
    if bottom:
        print(f"  ┌─── 📉 BOTTOM PERFORMERS ─────────────────────────────┐")
        for v in bottom:
            nm = [k for k, val in results.items() if val is v][0]
            print(f"  │  {nm:12s}  T={v['r']['total']:>4d}  PF={v['r']['pf']:.2f}  PnL=€{v['r']['pnl']:>+9,.0f}              │")
        print(f"  └─────────────────────────────────────────────────────────┘\n")

    # Verdict
    print(f"  ╔═══════════════════════════════════════════════════════╗")
    if total_pnl > 0 and pf_g >= 1.2 and total_trades >= 3000:
        print(f"  ║  🔪 SCALPING PROFITABLE + HIGH VELOCITY ✅            ║")
    elif total_pnl > 0 and total_trades >= 2000:
        print(f"  ║  ⚡ SCALPING POSITIVE — VELOCITY OK                  ║")
    elif total_pnl > 0:
        print(f"  ║  🟡 POSITIVE BUT LOW VELOCITY                        ║")
    else:
        print(f"  ║  ⚠️  SCALPING NET NEGATIVE                            ║")
    print(f"  ║  PnL: €{total_pnl:>+10,.0f} ({total_ret:>+.1f}%)                       ║")
    print(f"  ║  PF: {pf_g:.2f} | WR: {wr_g:.0%} | {total_trades:,d} trades                  ║")
    print(f"  ╚═══════════════════════════════════════════════════════╝\n")


if __name__ == "__main__":
    main()
