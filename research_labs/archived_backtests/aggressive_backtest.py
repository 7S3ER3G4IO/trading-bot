#!/usr/bin/env python3
"""
aggressive_backtest.py — 🔥 APEX PORTFOLIO BACKTEST

Full portfolio simulation with Conviction Sizing (Adrenaline Dial):
  ELITE  PF≥2.5 → 5.0% risk
  STRONG PF≥1.8 → 4.0% risk
  SOLID  PF≥1.2 → 3.0% risk
  BASE   default → 2.5% risk

Reads all instruments from optimized_rules.json + GOD_MODE_RULES.
500 candles per asset, sequential, gc-managed.

Usage:
    docker exec nemesis_bot python3 aggressive_backtest.py
"""

import sys, os, json, time, math, gc
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
COMMISSION_PCT  = 0.0004
RULES_FILE      = "optimized_rules.json"
SKIP_PREFIXES   = ("PAIR_",)

# Conviction tiers (mirrors risk_manager.py)
CONVICTION = {
    "ELITE":  {"min_pf": 2.5, "risk": 0.050},
    "STRONG": {"min_pf": 1.8, "risk": 0.040},
    "SOLID":  {"min_pf": 1.2, "risk": 0.030},
    "BASE":   {"min_pf": 0.0, "risk": 0.025},
}

MARGIN_REQ = {
    "crypto": 0.50, "stocks": 0.20, "commodities": 0.05,
    "indices": 0.05, "forex": 0.0333,
}

# Sessions
SESS = {
    "crypto": [(0, 1440)], "forex": [(420, 1260)],
    "commodities": [(360, 1320)], "indices": [(420, 630), (780, 1200)],
    "stocks": [(780, 1200)],
}
ASIA = {"JPY", "AUD", "NZD"}


# ═══════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════

def get_tier(pf):
    for name, t in CONVICTION.items():
        if pf >= t["min_pf"]:
            return name, t["risk"]
    return "BASE", 0.025

def compute_indicators(df):
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

def session_ok(ts, instr, cat):
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
#  BACKTESTER WITH CONVICTION SIZING
# ═══════════════════════════════════════════════════════════════════════

def backtest(df, params, instr, cat, risk_pct):
    strat = params.get("strat", "MR")
    cap = INITIAL_CAPITAL
    peak = cap
    trades = []
    in_trade = False
    tr = {}
    eq = [cap]

    for i in range(50, len(df)):
        row = df.iloc[i]; idx = df.index[i]
        c = float(row["close"]); atr = float(row.get("atr", c * 0.01))
        if atr <= 0: continue

        if not in_trade:
            if not session_ok(idx, instr, cat): continue
            sig = None; sl_d = atr * params.get("sl_buffer", 1.5)

            if strat in ("MR", "ML"):
                rsi = float(row.get("rsi", 50)); zs = float(row.get("zscore", 0))
                if rsi <= params.get("rsi_lo", 35) or zs <= -params.get("zscore_thresh", 1.5): sig = "BUY"
                elif rsi >= params.get("rsi_hi", 65) or zs >= params.get("zscore_thresh", 1.5): sig = "SELL"
            elif strat == "TF":
                ef = float(row.get("ema9", 0)); es = float(row.get("ema21", 0)); adx = float(row.get("adx", 0))
                if ef > es and adx > params.get("adx_min", 20): sig = "BUY"
                elif ef < es and adx > params.get("adx_min", 20): sig = "SELL"
            elif strat in ("BK", "M51"):
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
                # Conviction sizing
                risk_amt = cap * risk_pct
                sz = risk_amt / sl_d
                if sz <= 0: continue
                notional = sz * c
                comm = notional * COMMISSION_PCT
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
                cap += pnl
                peak = max(peak, cap)
                eq.append(cap)
                trades.append({"pnl": pnl, "win": pnl > 0, "bars": i - tr["b"]})
                in_trade = False

    if not trades:
        return {"total": 0, "pf": 0, "wr": 0, "pnl": 0, "dd": 0, "sharpe": 0, "final": cap}

    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    gp = sum(t["pnl"] for t in wins) if wins else 0
    gl = abs(sum(t["pnl"] for t in losses)) if losses else 0.001
    pnls = np.array([t["pnl"] for t in trades])
    eq_arr = np.array(eq)
    pk = np.maximum.accumulate(eq_arr)
    dd = float(((pk - eq_arr) / pk * 100).max())
    sh = float((np.mean(pnls) / np.std(pnls)) * math.sqrt(252)) if np.std(pnls) > 0 else 0

    return {
        "total": len(trades), "wins": len(wins), "losses": len(losses),
        "pf": round(gp / max(gl, 0.001), 2),
        "wr": round(len(wins) / len(trades), 3),
        "pnl": round(sum(t["pnl"] for t in trades), 2),
        "dd": round(dd, 2), "sharpe": round(sh, 2),
        "final": round(cap, 2), "gp": round(gp, 2), "gl": round(gl, 2),
        "equity": eq,
    }


# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "🔥" * 25)
    print("  APEX PORTFOLIO BACKTEST")
    print("  Conviction Sizing | All Instruments | Aggressive Mode")
    print("🔥" * 25)
    print()

    from brokers.capital_client import CapitalClient
    capital = CapitalClient()
    if not capital.available:
        print("  ❌ Capital.com API not available"); return

    # Merge rules: GOD_MODE + optimized
    try:
        from god_mode import GOD_MODE_RULES
        all_instruments = dict(GOD_MODE_RULES)
    except: all_instruments = {}

    opt_rules = {}
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE) as f:
            opt_rules = json.load(f)

    # Merge optimized on top
    for instr, data in opt_rules.items():
        if isinstance(data, dict):
            if instr in all_instruments:
                all_instruments[instr].update(data)
            else:
                all_instruments[instr] = data

    # Build PF lookup for conviction
    pf_lookup = {}
    for instr, data in opt_rules.items():
        if isinstance(data, dict):
            pf = data.get("pf", data.get("profit_factor", 0))
            if pf: pf_lookup[instr] = float(pf)

    total = len(all_instruments)
    print(f"  📊 {total} instruments | €{INITIAL_CAPITAL:,.0f} capital")
    print(f"  💉 Conviction Sizing: ELITE=5% | STRONG=4% | SOLID=3% | BASE=2.5%")
    print()

    results = []
    all_equity = [INITIAL_CAPITAL]
    portfolio_cap = INITIAL_CAPITAL
    t0 = time.time()

    for idx, (instr, rule) in enumerate(sorted(all_instruments.items()), 1):
        if any(instr.startswith(p) for p in SKIP_PREFIXES):
            print(f"  {idx:>2d}/{total}  {instr:20s}  ⏭ SKIP")
            continue

        strat = rule.get("strat", "?")
        tf = rule.get("tf", "1d")
        cat = rule.get("cat", "forex")

        # Get conviction risk
        pf_val = pf_lookup.get(instr, 0)
        tier, risk_pct = get_tier(pf_val)

        # Fetch data
        time.sleep(0.8)
        df = capital.fetch_ohlcv(instr, timeframe=tf, count=500)
        if df is None or len(df) < 60:
            print(f"  {idx:>2d}/{total}  {instr:20s}  ⚠️  NO DATA")
            continue

        df = compute_indicators(df)

        # Build params
        params = {"strat": strat}
        for k in ("rsi_lo", "rsi_hi", "zscore_thresh", "sl_buffer", "tp_rr",
                   "range_lb", "bk_margin", "adx_min"):
            if k in rule: params[k] = rule[k]

        # Defaults
        if strat in ("MR", "ML"):
            params.setdefault("rsi_lo", 35); params.setdefault("rsi_hi", 65)
            params.setdefault("zscore_thresh", 1.5); params.setdefault("sl_buffer", 1.5)
            params.setdefault("tp_rr", 2.0)
        elif strat == "TF":
            params.setdefault("adx_min", 20); params.setdefault("sl_buffer", 1.5)
            params.setdefault("tp_rr", 2.0)
        elif strat in ("BK", "M51"):
            params.setdefault("range_lb", 20); params.setdefault("bk_margin", 0.03)
            params.setdefault("adx_min", 0); params.setdefault("sl_buffer", 0.15)
            params.setdefault("tp_rr", 2.0)

        # Run backtest
        r = backtest(df, params, instr, cat, risk_pct)

        icon = "🟢" if r["pf"] >= 1.2 and r["total"] >= 5 else "🟡" if r["total"] > 0 else "⚫"
        tier_icon = {"ELITE": "💎", "STRONG": "🔥", "SOLID": "✅", "BASE": "📊"}.get(tier, "📊")

        if r["total"] > 0:
            print(
                f"  {idx:>2d}/{total}  {instr:20s}  {icon} {tier_icon}{tier:6s} "
                f"R={risk_pct:.1%}  T={r['total']:>3d}  WR={r['wr']:.0%}  "
                f"PF={r['pf']:>5.2f}  PnL=€{r['pnl']:>+9,.0f}  DD={r['dd']:.1f}%"
            )
        else:
            print(f"  {idx:>2d}/{total}  {instr:20s}  ⚫ {tier_icon}{tier:6s}  0 trades")

        r["instr"] = instr
        r["tier"] = tier
        r["risk_pct"] = risk_pct
        results.append(r)

        del df; gc.collect()

    elapsed = time.time() - t0

    # ═══════════════════════════════════════════════════════════════════
    #  PORTFOLIO AGGREGATION
    # ═══════════════════════════════════════════════════════════════════

    active = [r for r in results if r["total"] > 0]
    if not active:
        print("  ❌ No trades across entire portfolio"); return

    total_pnl = sum(r["pnl"] for r in active)
    total_trades = sum(r["total"] for r in active)
    total_wins = sum(r["wins"] for r in active)
    total_losses = sum(r["losses"] for r in active)
    total_gp = sum(r.get("gp", 0) for r in active)
    total_gl = sum(r.get("gl", 0.001) for r in active)

    # Portfolio equity curve (sum of per-instrument PnL)
    max_eq_len = max(len(r.get("equity", [INITIAL_CAPITAL])) for r in active)
    portfolio_eq = np.full(max_eq_len, INITIAL_CAPITAL)
    for r in active:
        eq = r.get("equity", [INITIAL_CAPITAL])
        deltas = np.diff(eq) if len(eq) > 1 else np.array([0])
        for j, d in enumerate(deltas):
            if j + 1 < max_eq_len:
                portfolio_eq[j + 1:] += d

    pk = np.maximum.accumulate(portfolio_eq)
    portfolio_dd = float(((pk - portfolio_eq) / pk * 100).max())
    final_cap = INITIAL_CAPITAL + total_pnl
    total_return = total_pnl / INITIAL_CAPITAL * 100
    pf_global = round(total_gp / max(total_gl, 0.01), 2)
    wr_global = total_wins / max(total_trades, 1)

    # Top/Bottom assets
    sorted_by_pnl = sorted(active, key=lambda x: x["pnl"], reverse=True)

    # ═══════════════════════════════════════════════════════════════════
    #  TEAR SHEET
    # ═══════════════════════════════════════════════════════════════════

    print(f"\n\n  {'═' * 60}")
    print(f"  ║                                                          ║")
    print(f"  ║   🔥  APEX PORTFOLIO — INSTITUTIONAL TEAR SHEET  🔥     ║")
    print(f"  ║   Conviction Sizing | Aggressive Growth Mode            ║")
    print(f"  ║                                                          ║")
    print(f"  {'═' * 60}")
    print()

    # PORTFOLIO SUMMARY
    print(f"  ┌─── PORTFOLIO SUMMARY ─────────────────────────────────┐")
    print(f"  │  💰 Capital Initial:   €{INITIAL_CAPITAL:>12,.2f}            │")
    print(f"  │  💎 Capital Final:     €{final_cap:>12,.2f}            │")
    pnl_icon = "📈" if total_pnl > 0 else "📉"
    print(f"  │  {pnl_icon} PnL Net:            €{total_pnl:>+12,.2f}            │")
    print(f"  │  📊 Rendement:            {total_return:>+10.1f}%            │")
    print(f"  │  📉 Max Drawdown:          {portfolio_dd:>10.1f}%            │")
    print(f"  └─────────────────────────────────────────────────────────┘")
    print()

    # KEY METRICS
    print(f"  ┌─── MÉTRIQUES CLÉS ────────────────────────────────────┐")
    print(f"  │  📊 Total Trades:       {total_trades:>8d}                    │")
    print(f"  │  ✅ Wins:               {total_wins:>8d}                    │")
    print(f"  │  ❌ Losses:             {total_losses:>8d}                    │")
    wr_bar = "█" * int(wr_global * 20) + "░" * (20 - int(wr_global * 20))
    print(f"  │  🎯 Win Rate:          {wr_global:>8.1%}  {wr_bar}    │")
    pf_icon = "🟢" if pf_global >= 1.5 else "🟡" if pf_global >= 1.0 else "🔴"
    print(f"  │  {pf_icon} Profit Factor:     {pf_global:>8.2f}                    │")
    print(f"  │  📊 Instruments:        {len(active):>8d} active              │")
    print(f"  │  ⏱ Duration:           {elapsed:>7.0f}s                    │")
    print(f"  └─────────────────────────────────────────────────────────┘")
    print()

    # CONVICTION BREAKDOWN
    tier_stats = {}
    for r in active:
        t = r.get("tier", "BASE")
        if t not in tier_stats:
            tier_stats[t] = {"count": 0, "pnl": 0, "trades": 0}
        tier_stats[t]["count"] += 1
        tier_stats[t]["pnl"] += r["pnl"]
        tier_stats[t]["trades"] += r["total"]

    print(f"  ┌─── CONVICTION TIER BREAKDOWN ────────────────────────┐")
    print(f"  │  {'Tier':8s} │ {'Assets':>6s} │ {'Trades':>6s} │ {'PnL':>12s} │ {'Risk%':>6s} │")
    print(f"  │  {'─'*8} │ {'─'*6} │ {'─'*6} │ {'─'*12} │ {'─'*6} │")
    for t_name in ["ELITE", "STRONG", "SOLID", "BASE"]:
        if t_name in tier_stats:
            ts = tier_stats[t_name]
            r_pct = CONVICTION[t_name]["risk"]
            print(f"  │  {t_name:8s} │ {ts['count']:>6d} │ {ts['trades']:>6d} │ €{ts['pnl']:>+10,.0f} │ {r_pct:>5.1%} │")
    print(f"  └─────────────────────────────────────────────────────────┘")
    print()

    # TOP 5 CASH GENERATORS
    print(f"  ┌─── 🏆 TOP 5 CASH GENERATORS ─────────────────────────┐")
    for i, r in enumerate(sorted_by_pnl[:5], 1):
        t_icon = {"ELITE": "💎", "STRONG": "🔥", "SOLID": "✅", "BASE": "📊"}.get(r["tier"], "📊")
        print(
            f"  │  #{i} {r['instr']:16s} {t_icon}{r['tier']:6s}  "
            f"PnL=€{r['pnl']:>+9,.0f}  PF={r['pf']:.2f}  T={r['total']}"
        )
    print(f"  └─────────────────────────────────────────────────────────┘")
    print()

    # BOTTOM 3 DRAINS
    bottom = sorted_by_pnl[-3:]
    bottom.reverse()
    print(f"  ┌─── 📉 BOTTOM 3 (CASH DRAINS) ───────────────────────┐")
    for r in bottom:
        if r["pnl"] < 0:
            print(f"  │  {r['instr']:16s}  PnL=€{r['pnl']:>+9,.0f}  PF={r['pf']:.2f}  T={r['total']}")
    print(f"  └─────────────────────────────────────────────────────────┘")
    print()

    # VERDICT
    print(f"  ╔═══════════════════════════════════════════════════════╗")
    if total_pnl > 0 and pf_global >= 1.5:
        print(f"  ║  🏆 PORTEFEUILLE PROFITABLE — PRÊT AU DÉPLOIEMENT   ║")
    elif total_pnl > 0:
        print(f"  ║  ✅ PORTEFEUILLE EN PROFIT — OPTIMISABLE             ║")
    else:
        print(f"  ║  ⚠️  PORTEFEUILLE EN PERTE — REVIEW NÉCESSAIRE       ║")
    print(f"  ║                                                       ║")
    print(f"  ║  📊 PnL: €{total_pnl:>+10,.0f} ({total_return:>+.1f}%)                     ║")
    print(f"  ║  📉 Max DD: {portfolio_dd:.1f}%                                  ║")
    print(f"  ║  🎯 PF: {pf_global:.2f} | WR: {wr_global:.0%} | {total_trades} trades              ║")
    print(f"  ╚═══════════════════════════════════════════════════════╝")
    print()


if __name__ == "__main__":
    main()
