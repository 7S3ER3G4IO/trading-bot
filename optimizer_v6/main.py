"""
main.py — Phase 1 + Phase 2 : optimisation par actif puis portfolio backtest
"""
import sys, os, json
import pandas as pd, numpy as np
from datetime import datetime, timedelta

from .config import (CANDIDATES, BAL0, RISK_PCT, MAX_TRADES, MAX_PER_CCY,
                     BK_P, MR_P, TF_P, BK_MG, LOOKBACKS, ADX_OPTS)
from .indicators import add_ind
from .signals import sig_bk, sig_mr, sig_tf
from .backtest import backtest_single, calc_m, get_base_ccy


def main():
    try: import yfinance as yf
    except: print("pip3 install yfinance"); sys.exit(1)
    end=datetime.now(); start=end-timedelta(days=365*2+30)
    risk_eur = BAL0 * RISK_PCT

    print(f"\n{'='*90}")
    print(f"  ⚡ NEMESIS V6 — {len(CANDIDATES)} actifs | 3 stratégies | Kelly/8 | Weekly filter")
    print(f"  Capital: {BAL0:,.0f}€ | Risque: {RISK_PCT*100:.2f}% = {risk_eur:.0f}€/trade")
    print(f"{'='*90}")

    # Phase 1: Download + optimize per-asset
    all_data={}; results={}
    for sym,info in CANDIDATES.items():
        print(f"\n  📥 {info['n']:<12} ({info['t']}) ...", end=" ", flush=True)
        try:
            raw=yf.download(info["t"],start=start,end=end,interval="1d",auto_adjust=True,
                            progress=False,multi_level_index=False)
            if raw is None or raw.empty: print("❌ No data"); continue
            if isinstance(raw.columns,pd.MultiIndex): raw.columns=raw.columns.get_level_values(0)
            raw.columns=[str(c).lower() for c in raw.columns]
            raw.index=pd.to_datetime(raw.index,utc=True)
            raw=raw.dropna(subset=["close","high","low","open"])
            raw=add_ind(raw)
            if len(raw)<100: print(f"❌ {len(raw)}j trop court"); continue
            all_data[sym]=raw
            print(f"{len(raw)}j", end=" ", flush=True)
        except Exception as e: print(f"❌ {e}"); continue

        best_score=0; best_m=None; best_p=None; best_s=None
        # Test BK
        for pf in BK_P:
            for mg in BK_MG:
                for lb in LOOKBACKS:
                    for adx in ADX_OPTS:
                        pr={"tp1":pf["tp1"],"tp2":pf["tp2"],"tp3":pf["tp3"],"slb":pf["slb"],
                            "max_hold":pf["mh"],"adx_min":adx,"bk_margin":mg,"range_lb":lb}
                        trades=backtest_single(raw,pr,info["sp"],"BK")
                        m=calc_m(trades,risk_eur)
                        if m["n"]>=10 and m["wr"]>=45 and m["pf"]>1.15 and m["pnl"]>0 and m["avg_rr"]>=1.5:
                            sc=m["n"]*m["pf"]*(m["wr"]/50)*(1+m["pnl"]/1000)
                            if sc>best_score: best_score=sc; best_m=m; best_p=pr.copy(); best_s="BK"
        # Test MR
        for pf in MR_P:
            for lb in LOOKBACKS:
                for adx in ADX_OPTS:
                    pr={"tp1":pf["tp1"],"tp2":pf["tp2"],"tp3":pf["tp3"],"slb":pf["slb"],
                        "max_hold":pf["mh"],"adx_min":adx,"bk_margin":0.05,"range_lb":lb,
                        "rsi_lo":pf["rlo"],"rsi_hi":pf["rhi"]}
                    trades=backtest_single(raw,pr,info["sp"],"MR")
                    m=calc_m(trades,risk_eur)
                    if m["n"]>=10 and m["wr"]>=45 and m["pf"]>1.15 and m["pnl"]>0 and m["avg_rr"]>=1.5:
                        sc=m["n"]*m["pf"]*(m["wr"]/50)*(1+m["pnl"]/1000)
                        if sc>best_score: best_score=sc; best_m=m; best_p=pr.copy(); best_s="MR"
        # Test TF
        for pf in TF_P:
            for lb in LOOKBACKS:
                for adx in ADX_OPTS:
                    pr={"tp1":pf["tp1"],"tp2":pf["tp2"],"tp3":pf["tp3"],"slb":pf["slb"],
                        "max_hold":pf["mh"],"adx_min":adx,"bk_margin":0.05,"range_lb":lb}
                    trades=backtest_single(raw,pr,info["sp"],"TF")
                    m=calc_m(trades,risk_eur)
                    if m["n"]>=10 and m["wr"]>=45 and m["pf"]>1.15 and m["pnl"]>0 and m["avg_rr"]>=1.5:
                        sc=m["n"]*m["pf"]*(m["wr"]/50)*(1+m["pnl"]/1000)
                        if sc>best_score: best_score=sc; best_m=m; best_p=pr.copy(); best_s="TF"

        if best_m:
            print(f"| 🟢 [{best_s}] T={best_m['n']} WR={best_m['wr']:.0f}% PF={best_m['pf']:.2f} "
                  f"PnL={best_m['pnl']:+,.0f}€ R:R={best_m['avg_rr']:.1f}")
            results[sym]={"name":info["n"],"cat":info["cat"],"spread":info["sp"],
                          "ticker":info["t"],"strat":best_s,"params":best_p,"metrics":best_m}
        else:
            print(f"| ❌")

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 2: Portfolio Backtest Réaliste
    # ═══════════════════════════════════════════════════════════════════════════
    print(f"\n\n{'='*90}")
    print(f"  📊 PHASE 2 — PORTFOLIO BACKTEST ({len(results)} actifs)")
    print(f"  MAX_TRADES={MAX_TRADES} | Kelly/8={RISK_PCT*100:.2f}% | Trailing | Pyramiding")
    print(f"{'='*90}")

    all_dates=sorted(set().union(*(set(all_data[s].index) for s in results if s in all_data)))
    equity=BAL0; open_tr=[]; trade_log=[]; monthly_pnl={}
    n_signals=0; n_blocked=0; n_pyrs=0

    for di,date in enumerate(all_dates):
        if di<50: continue
        new_o=[]
        for ot in open_tr:
            sym=ot["sym"]
            if sym not in all_data or date not in all_data[sym].index: new_o.append(ot); continue
            row=all_data[sym].loc[date]; hi,lo,cp=float(row["high"]),float(row["low"]),float(row["close"])
            held=di-ot["bar"]; closed=False; atr_n=float(row.get("atr",ot["atr"]))
            if not ot["tp1"]:
                if (hi>=ot["t1"]) if ot["dir"]=="BUY" else (lo<=ot["t1"]):
                    ot["pnl_r"]+=ot["tp1r"]; ot["tp1"]=True; ot["sl"]=ot["entry"]
                    if not ot.get("is_pyr"):
                        pyr_sd=atr_n*0.5
                        if pyr_sd>0:
                            pe=float(ot["t1"])
                            if ot["dir"]=="BUY": ps=pe-pyr_sd; pt1=pe+pyr_sd; pt2=pe+pyr_sd*1.5; pt3=pe+pyr_sd*2
                            else: ps=pe+pyr_sd; pt1=pe-pyr_sd; pt2=pe-pyr_sd*1.5; pt3=pe-pyr_sd*2
                            new_o.append({"sym":sym,"dir":ot["dir"],"entry":pe,"sl":ps,"t1":pt1,"t2":pt2,"t3":pt3,
                                          "tp1r":1/3,"tp2r":1.5/3,"tp3r":2/3,"risk_dist":pyr_sd,
                                          "risk_eur":max(50,equity*0.005),"pnl_r":0,"tp1":False,"tp2":False,"tp3":False,
                                          "bar":di,"atr":atr_n,"is_pyr":True,"strat":ot["strat"]+"_PYR","res":""})
                            n_pyrs+=1
            if ot.get("tp1") and not ot.get("tp2"):
                if (hi>=ot["t2"]) if ot["dir"]=="BUY" else (lo<=ot["t2"]):
                    ot["pnl_r"]+=ot["tp2r"]; ot["tp2"]=True
            if ot.get("tp2") and not ot.get("tp3"):
                if (hi>=ot["t3"]) if ot["dir"]=="BUY" else (lo<=ot["t3"]):
                    ot["pnl_r"]+=ot["tp3r"]; ot["tp3"]=True
            # Trailing after TP3
            if ot.get("tp3"):
                td=atr_n*0.5
                if ot["dir"]=="BUY":
                    ns=hi-td
                    if ns>ot["sl"]: ot["sl"]=ns
                    if lo<=ot["sl"]:
                        ex=(ot["sl"]-ot["entry"])/ot["risk_dist"]/3 if ot["risk_dist"]>0 else 0
                        ot["pnl_r"]+=max(0,ex); ot["res"]="TRAIL"; closed=True
                else:
                    ns=lo+td
                    if ns<ot["sl"]: ot["sl"]=ns
                    if hi>=ot["sl"]:
                        ex=(ot["entry"]-ot["sl"])/ot["risk_dist"]/3 if ot["risk_dist"]>0 else 0
                        ot["pnl_r"]+=max(0,ex); ot["res"]="TRAIL"; closed=True
                if not closed: new_o.append(ot); continue
            if not closed and not ot.get("tp3"):
                sl_hit=(lo<=ot["sl"]) if ot["dir"]=="BUY" else (hi>=ot["sl"])
                if sl_hit:
                    if ot.get("tp1"): ot["res"]="TP1+BE"
                    else: ot["pnl_r"]=-1.0; ot["res"]="SL"
                    closed=True
            mh=results.get(sym,{}).get("params",{}).get("max_hold",5)
            if not closed and held>=mh and not ot.get("tp3"):
                rd=ot["risk_dist"]
                if rd>0:
                    rem=1+(0 if ot["tp1"] else 1)+(0 if ot["tp2"] else 1)
                    mr=(cp-ot["entry"])/rd if ot["dir"]=="BUY" else (ot["entry"]-cp)/rd
                    ot["pnl_r"]+=mr*rem/3
                ot["res"]="TO"; closed=True
            if closed:
                re=ot.get("risk_eur",risk_eur)
                pnl_e=ot["pnl_r"]*re
                sp_c=results.get(sym,{}).get("spread",0.02)/100*ot["entry"]
                sp_e=(sp_c/ot["risk_dist"]*re)*0.3 if ot["risk_dist"]>0 else 0
                pnl_e-=sp_e; equity+=pnl_e
                trade_log.append({"sym":sym,"net":round(pnl_e,2),"res":ot["res"],"held":held,
                                  "strat":ot.get("strat","?"),"is_pyr":ot.get("is_pyr",False)})
                mk=date.strftime("%Y-%m"); monthly_pnl[mk]=monthly_pnl.get(mk,0)+pnl_e
            else: new_o.append(ot)
        open_tr=new_o
        n_open=len([t for t in open_tr if not t.get("is_pyr")])
        if n_open>=MAX_TRADES: continue
        ccy_c={}
        for t in open_tr: bc=get_base_ccy(t["sym"]); ccy_c[bc]=ccy_c.get(bc,0)+1
        for sym in results:
            if n_open>=MAX_TRADES: break
            if any(t["sym"]==sym and not t.get("is_pyr") for t in open_tr): continue
            if sym not in all_data or date not in all_data[sym].index: continue
            df=all_data[sym]; idx=df.index.get_loc(date)
            if idx<50: continue
            row=df.iloc[idx]; pr=results[sym]["params"]; lb=pr.get("range_lb",5)
            rh=float(df.iloc[max(0,idx-lb):idx]["high"].max())
            rl=float(df.iloc[max(0,idx-lb):idx]["low"].min()); rng=rh-rl
            st=results[sym]["strat"]
            if st=="BK": sg,sc=sig_bk(row,rh,rl,pr)
            elif st=="MR": sg,sc=sig_mr(row,rh,rl,pr)
            else: sg,sc=sig_tf(row,rh,rl,pr)
            if sg=="HOLD": continue
            n_signals+=1
            bc=get_base_ccy(sym)
            if ccy_c.get(bc,0)>=MAX_PER_CCY: n_blocked+=1; continue
            r_eur=max(50,equity*RISK_PCT)
            entry=float(row["close"]); atr=float(row.get("atr",rng*0.5))
            if st=="MR":
                sd=atr*pr.get("slb",0.5); rd=sd
                if sg=="BUY": sl=entry-sd; t1=entry+sd*pr["tp1"]; t2=entry+sd*pr["tp2"]; t3=entry+sd*pr["tp3"]
                else: sl=entry+sd; t1=entry-sd*pr["tp1"]; t2=entry-sd*pr["tp2"]; t3=entry-sd*pr["tp3"]
            elif st=="TF":
                sd=atr*pr.get("slb",1.5); rd=sd
                if sg=="BUY": sl=entry-sd; t1=entry+sd*pr["tp1"]; t2=entry+sd*pr["tp2"]; t3=entry+sd*pr["tp3"]
                else: sl=entry+sd; t1=entry-sd*pr["tp1"]; t2=entry-sd*pr["tp2"]; t3=entry-sd*pr["tp3"]
            else:
                if rng<=0: continue
                if sg=="BUY": sl=rl-rng*pr.get("slb",0.1); t1=entry+rng*pr["tp1"]; t2=entry+rng*pr["tp2"]; t3=entry+rng*pr["tp3"]
                else: sl=rh+rng*pr.get("slb",0.1); t1=entry-rng*pr["tp1"]; t2=entry-rng*pr["tp2"]; t3=entry-rng*pr["tp3"]
                rd=abs(entry-sl)
            if rd<=0: continue
            open_tr.append({"sym":sym,"dir":sg,"entry":entry,"sl":sl,"t1":t1,"t2":t2,"t3":t3,
                            "tp1r":pr["tp1"]/3,"tp2r":pr["tp2"]/3,"tp3r":pr["tp3"]/3,"risk_dist":rd,
                            "risk_eur":r_eur,"pnl_r":0,"tp1":False,"tp2":False,"tp3":False,
                            "bar":di,"atr":atr,"is_pyr":False,"strat":st,"res":""})
            n_open+=1; ccy_c[bc]=ccy_c.get(bc,0)+1
    # Close remaining
    for ot in open_tr:
        sym=ot["sym"]
        if sym in all_data:
            ld=all_data[sym].index[-1]
            cp=float(all_data[sym].loc[ld]["close"]) if ld in all_data[sym].index else ot["entry"]
            rd=ot["risk_dist"]
            if rd>0:
                rem=1+(0 if ot["tp1"] else 1)+(0 if ot["tp2"] else 1)
                mr=(cp-ot["entry"])/rd if ot["dir"]=="BUY" else (ot["entry"]-cp)/rd
                ot["pnl_r"]+=mr*rem/3
            re=ot.get("risk_eur",risk_eur); pnl_e=ot["pnl_r"]*re; equity+=pnl_e
            trade_log.append({"sym":sym,"net":round(pnl_e,2),"res":"FC","held":999,"strat":ot.get("strat","?"),"is_pyr":ot.get("is_pyr",False)})

    # ── ANALYSE FINALE ──
    n=len(trade_log); wins=[t for t in trade_log if t["net"]>0]; losses=[t for t in trade_log if t["net"]<=0]
    wr=len(wins)/n*100 if n else 0; top=sum(t["net"] for t in trade_log)
    gw=sum(t["net"] for t in wins); gl=abs(sum(t["net"] for t in losses))
    pf=gw/gl if gl>0 else 99
    aw=np.mean([t["net"] for t in wins]) if wins else 0
    al=np.mean([t["net"] for t in losses]) if losses else 0
    pk=BAL0; dd=0; eq=BAL0
    for t in trade_log:
        eq+=t["net"]
        if eq>pk: pk=eq
        d=(pk-eq)/pk*100 if pk>0 else 0
        if d>dd: dd=d
    by_a={}
    for t in trade_log:
        if t["sym"] not in by_a: by_a[t["sym"]]={"n":0,"w":0,"pnl":0,"strat":t["strat"]}
        by_a[t["sym"]]["n"]+=1; by_a[t["sym"]]["pnl"]+=t["net"]
        if t["net"]>0: by_a[t["sym"]]["w"]+=1
    by_cat={c:{"n":0,"pnl":0} for c in ["forex","commodity","index","crypto","stock"]}
    for s,a in by_a.items():
        c=results.get(s,{}).get("cat","other")
        if c in by_cat: by_cat[c]["n"]+=a["n"]; by_cat[c]["pnl"]+=a["pnl"]
    mp=len([v for v in monthly_pnl.values() if v>0])
    mn=len([v for v in monthly_pnl.values() if v<=0])
    bk_c=sum(1 for v in results.values() if v["strat"]=="BK")
    mr_c=sum(1 for v in results.values() if v["strat"]=="MR")
    tf_c=sum(1 for v in results.values() if v["strat"]=="TF")
    by_res={}
    for t in trade_log: by_res[t["res"]]=by_res.get(t["res"],0)+1

    print(f"\n{'='*90}")
    print(f"  📊 RÉSULTATS V6 PORTFOLIO FINAL")
    print(f"{'='*90}")
    print(f"  Capital: {BAL0:,.0f}€ → {equity:,.0f}€ | PnL: {top:+,.0f}€ ({top/BAL0*100:+.1f}%)")
    print(f"  PnL/an: {top/2:+,.0f}€ | PnL/mois: {top/24:+,.0f}€")
    print(f"  Trades: {n} (dont {len([t for t in trade_log if t.get('is_pyr')])} pyramides)")
    print(f"  WR: {wr:.1f}% | PF: {pf:.2f} | R:R: {abs(aw/al):.1f}:1" if al else f"  WR: {wr:.1f}%")
    print(f"  Gain moy: {aw:+,.0f}€ | Perte moy: {al:+,.0f}€")
    print(f"  DD max: {dd:.1f}% | Mois+: {mp}/{mp+mn}")
    print(f"  Actifs: {len(results)} (BK:{bk_c} MR:{mr_c} TF:{tf_c})")
    print(f"  Signaux: {n_signals} | Bloqués: {n_blocked} | Pyramides: {n_pyrs}")
    print(f"\n  Par catégorie:")
    for c in ["forex","commodity","index","crypto","stock"]:
        if by_cat[c]["n"]>0: print(f"    {c:<10}: {by_cat[c]['n']:>3}t PnL={by_cat[c]['pnl']:+,.0f}€")
    print(f"\n  Top 15 actifs:")
    for sym in sorted(by_a,key=lambda x:by_a[x]["pnl"],reverse=True)[:15]:
        a=by_a[sym]; awr=a["w"]/a["n"]*100 if a["n"]>0 else 0
        nm=results.get(sym,{}).get("name",sym); st=a["strat"]
        print(f"    🟢 {nm:<12}[{st:>2}] T={a['n']:>3} WR={awr:.0f}% PnL={a['pnl']:+,.0f}€")
    print(f"\n  PnL mensuel:")
    for m in sorted(monthly_pnl.keys()):
        v=monthly_pnl[m]; print(f"    {m}: {v:+,.0f}€ {'🟢' if v>0 else '🔴'}")
    reject=[s for s in CANDIDATES if s not in results]
    if reject: print(f"\n  ❌ Rejetés ({len(reject)}): {', '.join(reject[:20])}")
    print(f"\n{'='*90}")
    v="🏆 EXCELLENT" if top>30000 else "✅ RENTABLE" if top>10000 else "⚠️ MARGINAL"
    print(f"  {v} | PnL: {top:+,.0f}€ | {top/2/BAL0*100:+.0f}%/an")
    print(f"{'='*90}\n")
    p_out=os.path.join(os.path.dirname(os.path.abspath(__file__)),"..","asset_profiles_v6.json")
    with open(p_out,"w") as f:
        json.dump({"v":"v6","generated":datetime.now().isoformat(),"capital":BAL0,"risk_pct":RISK_PCT,
                   "n_profitable":len(results),"n_tested":len(CANDIDATES),"total_pnl":round(top,0),
                   "equity_final":round(equity,0),"n_trades":n,"wr":round(wr,1),"pf":round(pf,2),
                   "bk":bk_c,"mr":mr_c,"tf":tf_c,
                   "assets":{s:{"name":v["name"],"cat":v["cat"],"spread":v["spread"],"ticker":v["ticker"],
                                "strat":v["strat"],"params":v["params"],"metrics":v["metrics"]}
                             for s,v in results.items()}},f,indent=2,default=str)
    print(f"  💾 → {p_out}")

if __name__=="__main__":
    main()
