#!/usr/bin/env python3
"""
signals.py — Fonctions de signal: Breakout (BK), Mean-Reversion (MR), Trend-Following (TF)
"""


def sig_bk(row, rh, rl, pr):
    rng=rh-rl; c=row["close"]; o=row["open"]
    if rng<=0 or c<=0: return "HOLD",0
    rp=rng/c*100
    if rp<0.005 or rp>15: return "HOLD",0
    mg=rng*pr.get("bk_margin",0.05)
    up=c>rh+mg*0.3; dn=c<rl-mg*0.3
    pb_up=o>rh and c>rh-rng*0.1 and c>row.get("ema20",c)
    pb_dn=o<rl and c<rl+rng*0.1 and c<row.get("ema20",c)
    if up or pb_up: sig="BUY"
    elif dn or pb_dn: sig="SELL"
    else: return "HOLD",0
    if sig=="BUY" and row.get("ema20w",0)<row.get("ema50w",0): return "HOLD",0
    if sig=="SELL" and row.get("ema20w",0)>row.get("ema50w",0): return "HOLD",0
    sc=0
    if row.get("adx",0)>pr.get("adx_min",12): sc+=1
    if (sig=="BUY" and row.get("mom3",0)>0) or (sig=="SELL" and row.get("mom3",0)<0): sc+=1
    body=abs(c-o); crng=row["high"]-row["low"]
    if crng>0 and body/crng>=0.40: sc+=1
    vol=row.get("volume",0); vma=row.get("vol_ma",vol)
    if vol>0 and vma>0 and vol>vma*0.9: sc+=1
    if (sig=="BUY" and c>row.get("ema50",c)) or (sig=="SELL" and c<row.get("ema50",c)): sc+=1
    return (sig,sc) if sc>=1 else ("HOLD",0)


def sig_mr(row, rh, rl, pr):
    c=row["close"]; rsi=row.get("rsi",50)
    bb_lo=row.get("bb_lo",c); bb_up=row.get("bb_up",c)
    atr=row.get("atr",0)
    if atr<=0: return "HOLD",0
    if rsi<=pr.get("rsi_lo",30) and c<=bb_lo*1.005: sig="BUY"
    elif rsi>=pr.get("rsi_hi",70) and c>=bb_up*0.995: sig="SELL"
    else: return "HOLD",0
    sc=0
    if row.get("adx",0)<25: sc+=1
    body=abs(c-row["open"]); crng=row["high"]-row["low"]
    if crng>0 and body/crng>=0.35: sc+=1
    vol=row.get("volume",0); vma=row.get("vol_ma",vol)
    if vol>0 and vma>0 and vol>vma*0.8: sc+=1
    ema50=row.get("ema50",c)
    if c>0 and abs(c-ema50)/c<0.03: sc+=1
    return (sig,sc) if sc>=1 else ("HOLD",0)


def sig_tf(row, rh, rl, pr):
    ema20=row.get("ema20",0); ema50=row.get("ema50",0)
    macd=row.get("macd",0); macd_s=row.get("macd_s",0)
    adx=row.get("adx",0); c=row["close"]
    if ema20<=0 or ema50<=0: return "HOLD",0
    if ema20>ema50 and macd>macd_s and adx>18: sig="BUY"
    elif ema20<ema50 and macd<macd_s and adx>18: sig="SELL"
    else: return "HOLD",0
    if sig=="BUY" and row.get("ema20w",0)<row.get("ema50w",0): return "HOLD",0
    if sig=="SELL" and row.get("ema20w",0)>row.get("ema50w",0): return "HOLD",0
    sc=0
    if adx>25: sc+=1
    rsi=row.get("rsi",50)
    if (sig=="BUY" and 40<rsi<70) or (sig=="SELL" and 30<rsi<60): sc+=1
    vol=row.get("volume",0); vma=row.get("vol_ma",vol)
    if vol>0 and vma>0 and vol>vma*0.9: sc+=1
    if row.get("mom3",0)>0.5 and sig=="BUY": sc+=1
    if row.get("mom3",0)<-0.5 and sig=="SELL": sc+=1
    return (sig,sc) if sc>=1 else ("HOLD",0)
