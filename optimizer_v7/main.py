#!/usr/bin/env python3
"""
main.py — Orchestration NEMESIS V7 : download, optimize, report, save
"""
import sys, os, json
import pandas as pd
from datetime import datetime

from .config import ASSETS, BAL0, RISK_PCT, MAX_TRADES, MIN_RR, BK_P, MR_P, TF_P, BK_MG, LOOKBACKS, ADX_OPTS
from .indicators import add_ind, resample_4h
from .backtest import backtest_single, calc_m


def main():
    try: import yfinance as yf
    except: print("pip3 install yfinance"); sys.exit(1)
    risk_eur = BAL0 * RISK_PCT

    print(f"\n{'='*90}")
    print(f"  ⚡ NEMESIS V7 — 4H MACHINE À TRADES")
    print(f"  {len(ASSETS)} actifs (winners v6) | 3 stratégies | R:R ≥ {MIN_RR}:1")
    print(f"  Capital: {BAL0:,.0f}€ | Risque: {RISK_PCT*100:.2f}% = {risk_eur:.0f}€/trade")
    print(f"  MAX_TRADES={MAX_TRADES} | Timeframe: 4H")
    print(f"{'='*90}")

    # Phase 1: Download 1h data + resample to 4H + optimize
    all_data={}; results={}
    for sym, info in ASSETS.items():
        ticker = info["ticker"]
        name = info["name"]
        cat = info["cat"]
        v6_strat = info["strat"]
        sp = info["spread"]
        print(f"\n  📥 {name:<12} ({ticker}) [v6={v6_strat}] ...", end=" ", flush=True)
        try:
            # yfinance: max 730 days for 1h data
            raw = yf.download(ticker, period="730d", interval="1h",
                              auto_adjust=True, progress=False, multi_level_index=False)
            if raw is None or raw.empty:
                print("❌ No data")
                continue
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            raw.columns = [str(c).lower() for c in raw.columns]
            raw.index = pd.to_datetime(raw.index, utc=True)
            raw = raw.dropna(subset=["close","high","low","open"])
            # Resample to 4H
            df_4h = resample_4h(raw)
            df_4h = add_ind(df_4h)
            if len(df_4h) < 200:
                print(f"❌ {len(df_4h)} bougies 4H trop court")
                continue
            all_data[sym] = df_4h
            print(f"{len(df_4h)} bougies 4H", end=" ", flush=True)
        except Exception as e:
            print(f"❌ {e}")
            continue

        # Test all 3 strategies, find best
        best_score=0; best_m=None; best_p=None; best_s=None
        for strat, profiles in [("BK", BK_P), ("MR", MR_P), ("TF", TF_P)]:
            for pf in profiles:
                margins = BK_MG if strat == "BK" else [0.05]
                for mg in margins:
                    for lb in LOOKBACKS:
                        for adx in ADX_OPTS:
                            pr = {"tp1":pf["tp1"],"tp2":pf["tp2"],"tp3":pf["tp3"],"slb":pf["slb"],
                                  "max_hold":pf["mh"],"adx_min":adx,"bk_margin":mg,"range_lb":lb}
                            if strat == "MR":
                                pr["rsi_lo"] = pf.get("rlo", 25)
                                pr["rsi_hi"] = pf.get("rhi", 75)
                            trades = backtest_single(df_4h, pr, sp, strat)
                            m = calc_m(trades, risk_eur)
                            if m["n"] >= 15 and m["wr"] >= 40 and m["pf"] > 1.15 and m["pnl"] > 0 and m["avg_rr"] >= MIN_RR:
                                sc = m["n"] * m["pf"] * (m["wr"]/50) * (1 + m["pnl"]/1000)
                                if sc > best_score:
                                    best_score=sc; best_m=m; best_p=pr.copy(); best_s=strat

        if best_m:
            print(f"| 🟢 [{best_s}] T={best_m['n']} WR={best_m['wr']:.0f}% PF={best_m['pf']:.2f} "
                  f"PnL={best_m['pnl']:+,.0f}€ R:R={best_m['avg_rr']:.1f}")
            results[sym] = {"name":name,"cat":cat,"spread":sp,"ticker":ticker,
                            "strat":best_s,"params":best_p,"metrics":best_m}
        else:
            print(f"| ❌ (4H)")

    # Summary
    n_bk = sum(1 for v in results.values() if v["strat"]=="BK")
    n_mr = sum(1 for v in results.values() if v["strat"]=="MR")
    n_tf = sum(1 for v in results.values() if v["strat"]=="TF")
    total_trades = sum(v["metrics"]["n"] for v in results.values())
    total_pnl = sum(v["metrics"]["pnl"] for v in results.values())

    print(f"\n{'='*90}")
    print(f"  📊 RÉSULTATS V7 — PHASE 1 (par actif, position fixe {risk_eur:.0f}€)")
    print(f"{'='*90}")
    print(f"  Actifs rentables   : {len(results)}/{len(ASSETS)} (BK:{n_bk} MR:{n_mr} TF:{n_tf})")
    print(f"  Trades totaux (4H) : {total_trades:,} (vs V6 daily: ~{sum(v['metrics']['n'] for v in ASSETS.values()):,})")
    print(f"  Trades/jour (avg)  : {total_trades/(730*0.7):.1f}")  # 0.7 = ratio trading days
    print(f"  PnL total (fixe)   : {total_pnl:+,.0f}€")
    print(f"  PnL/an             : {total_pnl/2:+,.0f}€")

    # Per-asset ranking
    print(f"\n  Top 15 actifs 4H:")
    for sym in sorted(results, key=lambda x: results[x]["metrics"]["pnl"], reverse=True)[:15]:
        r = results[sym]
        m = r["metrics"]
        print(f"    🟢 {r['name']:<12}[{r['strat']:>2}] T={m['n']:>4} WR={m['wr']:.0f}% "
              f"PF={m['pf']:.2f} PnL={m['pnl']:+,.0f}€ R:R={m['avg_rr']:.1f}")

    print(f"\n  Comparaison V6 daily vs V7 4H:")
    print(f"    V6 daily : {sum(v['metrics']['n'] for v in ASSETS.values()):>5} trades | "
          f"PnL {sum(v['metrics']['pnl'] for v in ASSETS.values()):+,.0f}€")
    print(f"    V7 4H    : {total_trades:>5} trades | PnL {total_pnl:+,.0f}€")
    ratio = total_trades / max(1, sum(v["metrics"]["n"] for v in ASSETS.values()))
    print(f"    Multiplicateur : ×{ratio:.1f} trades")

    # Rejected
    rejected = [s for s in ASSETS if s not in results]
    if rejected:
        print(f"\n  ❌ Rejetés en 4H ({len(rejected)}):")
        for s in rejected[:10]:
            print(f"    {ASSETS[s]['name']} ({ASSETS[s]['strat']} en daily)")

    print(f"\n{'='*90}")
    v = "🏆 MACHINE" if total_trades > 3000 else "✅ RENTABLE" if total_pnl > 10000 else "⚠️ MARGINAL"
    print(f"  {v} | {total_trades:,} trades | PnL: {total_pnl:+,.0f}€ | {total_pnl/2/BAL0*100:+.0f}%/an")
    print(f"{'='*90}\n")

    # Save
    p_out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "asset_profiles_v7.json")
    with open(p_out, "w") as f:
        json.dump({
            "v": "v7_4H", "tf": "4H",
            "generated": datetime.now().isoformat(),
            "capital": BAL0, "risk_pct": RISK_PCT,
            "n_profitable": len(results), "n_tested": len(ASSETS),
            "total_trades": total_trades, "total_pnl": round(total_pnl, 0),
            "bk": n_bk, "mr": n_mr, "tf": n_tf,
            "assets": {s: {"name":v["name"],"cat":v["cat"],"spread":v["spread"],
                           "ticker":v["ticker"],"strat":v["strat"],
                           "params":v["params"],"metrics":v["metrics"]}
                       for s, v in results.items()}
        }, f, indent=2, default=str)
    print(f"  💾 → {p_out}")


if __name__ == "__main__":
    main()
