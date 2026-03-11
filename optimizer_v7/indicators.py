#!/usr/bin/env python3
"""
indicators.py — Calcul des indicateurs techniques et resampling 4H
"""
import pandas as pd
import numpy as np


def add_ind(df, p=14):
    df=df.copy(); c,h,l=df["close"],df["high"],df["low"]
    hl=h-l; hc=(h-c.shift()).abs(); lc=(l-c.shift()).abs()
    tr=pd.concat([hl,hc,lc],axis=1).max(axis=1)
    df["atr"]=tr.rolling(p).mean()
    pdm=h.diff().clip(lower=0); ndm=(-l.diff()).clip(lower=0)
    mask=pdm>ndm; pdm=pdm.where(mask,0); ndm=ndm.where(~mask,0)
    atr_s=tr.rolling(p).sum()
    pdi=100*pdm.rolling(p).sum()/atr_s.replace(0,np.nan)
    ndi=100*ndm.rolling(p).sum()/atr_s.replace(0,np.nan)
    dx=100*(pdi-ndi).abs()/(pdi+ndi).replace(0,np.nan)
    df["adx"]=dx.rolling(p).mean()
    df["mom3"]=c.pct_change(3)*100
    df["ema20"]=c.ewm(span=20).mean()
    df["ema50"]=c.ewm(span=50).mean()
    df["ema20w"]=c.ewm(span=100).mean()
    df["ema50w"]=c.ewm(span=250).mean()
    delta=c.diff()
    gain=delta.clip(lower=0).rolling(14).mean()
    loss=(-delta.clip(upper=0)).rolling(14).mean()
    rs=gain/(loss.replace(0,np.nan))
    df["rsi"]=100-100/(1+rs)
    df["bb_mid"]=c.rolling(20).mean()
    bb_std=c.rolling(20).std()
    df["bb_up"]=df["bb_mid"]+2*bb_std
    df["bb_lo"]=df["bb_mid"]-2*bb_std
    e12=c.ewm(span=12).mean(); e26=c.ewm(span=26).mean()
    df["macd"]=e12-e26; df["macd_s"]=df["macd"].ewm(span=9).mean()
    if "volume" in df.columns and df["volume"].sum()>0:
        df["vol_ma"]=df["volume"].rolling(20,min_periods=1).mean()
    else:
        df["volume"]=1.0; df["vol_ma"]=1.0
    return df.dropna()


def resample_4h(df_1h):
    """Agrège du 1h vers 4h."""
    r = df_1h.resample("4h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum"
    }).dropna(subset=["close"])
    return r
