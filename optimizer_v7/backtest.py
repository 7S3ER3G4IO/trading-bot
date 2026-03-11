#!/usr/bin/env python3
"""
backtest.py — Moteur de backtest single-asset et métriques
"""
import numpy as np
from .signals import sig_bk, sig_mr, sig_tf


def backtest_single(df, pr, sp, strat):
    trades=[]; opens=[]; lb=pr.get("range_lb",12); spread=sp/100.0
    for i in range(max(lb+30, 60), len(df)):
        row=df.iloc[i]; new_o=[]
        for ot in opens:
            hi,lo=float(row["high"]),float(row["low"]); cp=float(row["close"])
            held=i-ot["bar"]; closed=False
            if not ot.get("tp1"):
                if (hi>=ot["t1"]) if ot["d"]=="BUY" else (lo<=ot["t1"]):
                    ot["pnl_r"]+=ot["tp1r"]; ot["tp1"]=True; ot["sl"]=ot["e"]
            if ot.get("tp1") and not ot.get("tp2"):
                if (hi>=ot["t2"]) if ot["d"]=="BUY" else (lo<=ot["t2"]):
                    ot["pnl_r"]+=ot["tp2r"]; ot["tp2"]=True
            if ot.get("tp2") and not ot.get("tp3"):
                if (hi>=ot["t3"]) if ot["d"]=="BUY" else (lo<=ot["t3"]):
                    ot["pnl_r"]+=ot["tp3r"]; ot["tp3"]=True; ot["res"]="TP3"; closed=True
            if not closed:
                sl_hit=(lo<=ot["sl"]) if ot["d"]=="BUY" else (hi>=ot["sl"])
                if sl_hit:
                    if ot.get("tp1"): ot["res"]="TP1+BE"
                    else: ot["pnl_r"]=-1.0; ot["res"]="SL"
                    closed=True
            if not closed and held>=pr.get("max_hold",18):
                rd=ot.get("risk_dist",1)
                if rd>0:
                    rem=1+(0 if ot.get("tp1") else 1)+(0 if ot.get("tp2") else 1)
                    m_r=(cp-ot["e"])/rd if ot["d"]=="BUY" else (ot["e"]-cp)/rd
                    ot["pnl_r"]+=m_r*rem/3
                ot["res"]="TO"; closed=True
            if closed:
                sp_r=(ot["e"]*spread)/ot.get("risk_dist",1)*0.3 if ot.get("risk_dist",1)>0 else 0
                ot["pnl_r"]-=sp_r
                trades.append({"net_r":ot["pnl_r"],"res":ot["res"],"held":held})
            else: new_o.append(ot)
        opens=new_o
        if len(opens)>=3: continue  # Max 3 open trades per asset in backtest
        rh=float(df.iloc[max(0,i-lb):i]["high"].max())
        rl=float(df.iloc[max(0,i-lb):i]["low"].min()); rng=rh-rl
        if strat=="BK": sig,sc=sig_bk(row,rh,rl,pr)
        elif strat=="MR": sig,sc=sig_mr(row,rh,rl,pr)
        else: sig,sc=sig_tf(row,rh,rl,pr)
        if sig=="HOLD": continue
        entry=float(row["close"]); atr=float(row.get("atr",rng*0.5))
        if rng<=0 and atr<=0: continue
        if strat=="MR":
            sl_d=atr*pr.get("slb",0.5); rd=sl_d
            if sig=="BUY": sl=entry-sl_d; t1=entry+sl_d*pr["tp1"]; t2=entry+sl_d*pr["tp2"]; t3=entry+sl_d*pr["tp3"]
            else: sl=entry+sl_d; t1=entry-sl_d*pr["tp1"]; t2=entry-sl_d*pr["tp2"]; t3=entry-sl_d*pr["tp3"]
        elif strat=="TF":
            sl_d=atr*pr.get("slb",1.5); rd=sl_d
            if sig=="BUY": sl=entry-sl_d; t1=entry+sl_d*pr["tp1"]; t2=entry+sl_d*pr["tp2"]; t3=entry+sl_d*pr["tp3"]
            else: sl=entry+sl_d; t1=entry-sl_d*pr["tp1"]; t2=entry-sl_d*pr["tp2"]; t3=entry-sl_d*pr["tp3"]
        else:
            if rng<=0: continue
            if sig=="BUY": sl=rl-rng*pr.get("slb",0.10); t1=entry+rng*pr["tp1"]; t2=entry+rng*pr["tp2"]; t3=entry+rng*pr["tp3"]
            else: sl=rh+rng*pr.get("slb",0.10); t1=entry-rng*pr["tp1"]; t2=entry-rng*pr["tp2"]; t3=entry-rng*pr["tp3"]
            rd=abs(entry-sl)
        if rd<=0: continue
        opens.append({"bar":i,"d":sig,"e":entry,"sl":sl,"t1":t1,"t2":t2,"t3":t3,
                       "tp1r":pr["tp1"]/3,"tp2r":pr["tp2"]/3,"tp3r":pr["tp3"]/3,
                       "risk_dist":rd,"pnl_r":0.0,"tp1":False,"tp2":False,"tp3":False,"res":""})
    for ot in opens:
        cp=float(df.iloc[-1]["close"]); rd=ot.get("risk_dist",1)
        if rd>0:
            rem=1+(0 if ot.get("tp1") else 1)+(0 if ot.get("tp2") else 1)
            m_r=(cp-ot["e"])/rd if ot["d"]=="BUY" else (ot["e"]-cp)/rd
            ot["pnl_r"]+=m_r*rem/3
        trades.append({"net_r":ot["pnl_r"],"res":"FC","held":999})
    return trades


def calc_m(trades, risk_eur=138):
    n=len(trades)
    if n==0: return {"n":0,"wr":0,"pf":0,"pnl":0,"avg_rr":0}
    wins=[t for t in trades if t["net_r"]>0]; losses=[t for t in trades if t["net_r"]<=0]
    wr=len(wins)/n*100
    gw=sum(t["net_r"] for t in wins)*risk_eur; gl=abs(sum(t["net_r"] for t in losses))*risk_eur
    pf=gw/gl if gl>0 else 99
    pnl=sum(t["net_r"] for t in trades)*risk_eur
    aw=np.mean([t["net_r"] for t in wins]) if wins else 0
    al=abs(np.mean([t["net_r"] for t in losses])) if losses else 1
    return {"n":n,"wr":round(wr,1),"pf":round(pf,2),"pnl":round(pnl,0),"avg_rr":round(aw/al,2) if al>0 else 0}


def get_base_ccy(sym):
    m={"EURUSD":"EUR","GBPUSD":"GBP","AUDUSD":"AUD","NZDUSD":"NZD","USDCAD":"USD","USDCHF":"USD",
       "GBPJPY":"GBP","EURGBP":"EUR","EURJPY":"EUR","EURAUD":"EUR","GBPAUD":"GBP","EURNZD":"EUR",
       "AUDCAD":"AUD","AUDNZD":"AUD","AUDJPY":"AUD","NZDJPY":"NZD","CHFJPY":"CHF","EURCAD":"EUR",
       "EURCHF":"EUR","GBPCAD":"GBP","GBPCHF":"GBP","CADCHF":"CAD","NZDCAD":"NZD","GBPNZD":"GBP",
       "CADJPY":"CAD","GOLD":"XAU","SILVER":"XAG","OIL_WTI":"OIL","OIL_BRENT":"OIL","NATGAS":"GAS",
       "COPPER":"COP","US500":"IDX","US100":"IDX","US30":"IDX","DE40":"IDX","FRA40":"IDX","UK100":"IDX",
       "JP225":"IDX","AUS200":"IDX","STOXX50":"IDX","BTCUSD":"BTC","ETHUSD":"ETH","SOLUSD":"SOL",
       "BNBUSD":"BNB","XRPUSD":"XRP","ADAUSD":"ADA","AVAXUSD":"AVX","LINKUSD":"LNK",
       "AAPL":"STK","TSLA":"STK","NVDA":"STK","AMZN":"STK","MSFT":"STK","META":"STK","GOOGL":"STK"}
    return m.get(sym,"OTH")
