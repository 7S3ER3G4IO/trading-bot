"""
config.py — Constantes et profils optimizer_v6
"""
import sys, os, json, warnings
warnings.filterwarnings("ignore")
import pandas as pd, numpy as np
from datetime import datetime, timedelta

# ═══════════════════════════════════════════════════════════════════════════════
# ACTIFS: 50+ (Forex + Commodités + Indices + Crypto + Stocks)
# ═══════════════════════════════════════════════════════════════════════════════
CANDIDATES = {
    # Forex majors
    "EURUSD":{"t":"EURUSD=X","n":"EUR/USD","sp":0.008,"cat":"forex"},
    "GBPUSD":{"t":"GBPUSD=X","n":"GBP/USD","sp":0.012,"cat":"forex"},
    "AUDUSD":{"t":"AUDUSD=X","n":"AUD/USD","sp":0.010,"cat":"forex"},
    "NZDUSD":{"t":"NZDUSD=X","n":"NZD/USD","sp":0.015,"cat":"forex"},
    "USDCAD":{"t":"CAD=X","n":"USD/CAD","sp":0.012,"cat":"forex"},
    "USDCHF":{"t":"CHF=X","n":"USD/CHF","sp":0.012,"cat":"forex"},
    # Forex crosses
    "GBPJPY":{"t":"GBPJPY=X","n":"GBP/JPY","sp":0.025,"cat":"forex"},
    "EURGBP":{"t":"EURGBP=X","n":"EUR/GBP","sp":0.010,"cat":"forex"},
    "EURJPY":{"t":"EURJPY=X","n":"EUR/JPY","sp":0.020,"cat":"forex"},
    "EURAUD":{"t":"EURAUD=X","n":"EUR/AUD","sp":0.020,"cat":"forex"},
    "GBPAUD":{"t":"GBPAUD=X","n":"GBP/AUD","sp":0.025,"cat":"forex"},
    "EURNZD":{"t":"EURNZD=X","n":"EUR/NZD","sp":0.025,"cat":"forex"},
    "AUDCAD":{"t":"AUDCAD=X","n":"AUD/CAD","sp":0.015,"cat":"forex"},
    "AUDNZD":{"t":"AUDNZD=X","n":"AUD/NZD","sp":0.015,"cat":"forex"},
    "AUDJPY":{"t":"AUDJPY=X","n":"AUD/JPY","sp":0.020,"cat":"forex"},
    "NZDJPY":{"t":"NZDJPY=X","n":"NZD/JPY","sp":0.020,"cat":"forex"},
    "EURCHF":{"t":"EURCHF=X","n":"EUR/CHF","sp":0.015,"cat":"forex"},
    "GBPCAD":{"t":"GBPCAD=X","n":"GBP/CAD","sp":0.025,"cat":"forex"},
    "GBPCHF":{"t":"GBPCHF=X","n":"GBP/CHF","sp":0.025,"cat":"forex"},
    "CADCHF":{"t":"CADCHF=X","n":"CAD/CHF","sp":0.020,"cat":"forex"},
    "NZDCAD":{"t":"NZDCAD=X","n":"NZD/CAD","sp":0.020,"cat":"forex"},
    "EURCAD":{"t":"EURCAD=X","n":"EUR/CAD","sp":0.020,"cat":"forex"},
    "GBPNZD":{"t":"GBPNZD=X","n":"GBP/NZD","sp":0.030,"cat":"forex"},
    "CADJPY":{"t":"CADJPY=X","n":"CAD/JPY","sp":0.020,"cat":"forex"},
    "CHFJPY":{"t":"CHFJPY=X","n":"CHF/JPY","sp":0.020,"cat":"forex"},
    # Commodities
    "GOLD":{"t":"GC=F","n":"Gold","sp":0.025,"cat":"commodity"},
    "SILVER":{"t":"SI=F","n":"Silver","sp":0.035,"cat":"commodity"},
    "OIL_WTI":{"t":"CL=F","n":"WTI","sp":0.035,"cat":"commodity"},
    "OIL_BRENT":{"t":"BZ=F","n":"Brent","sp":0.035,"cat":"commodity"},
    "NATGAS":{"t":"NG=F","n":"Nat Gas","sp":0.050,"cat":"commodity"},
    "COPPER":{"t":"HG=F","n":"Copper","sp":0.040,"cat":"commodity"},
    # Indices
    "US500":{"t":"^GSPC","n":"S&P 500","sp":0.035,"cat":"index"},
    "US100":{"t":"^NDX","n":"NASDAQ","sp":0.045,"cat":"index"},
    "US30":{"t":"^DJI","n":"Dow Jones","sp":0.035,"cat":"index"},
    "DE40":{"t":"^GDAXI","n":"DAX 40","sp":0.045,"cat":"index"},
    "FRA40":{"t":"^FCHI","n":"CAC 40","sp":0.045,"cat":"index"},
    "UK100":{"t":"^FTSE","n":"FTSE 100","sp":0.035,"cat":"index"},
    "JP225":{"t":"^N225","n":"Nikkei","sp":0.055,"cat":"index"},
    "AUS200":{"t":"^AXJO","n":"ASX 200","sp":0.045,"cat":"index"},
    "STOXX50":{"t":"^STOXX50E","n":"Stoxx50","sp":0.045,"cat":"index"},
    # Crypto
    "BTCUSD":{"t":"BTC-USD","n":"Bitcoin","sp":0.10,"cat":"crypto"},
    "ETHUSD":{"t":"ETH-USD","n":"Ethereum","sp":0.12,"cat":"crypto"},
    "SOLUSD":{"t":"SOL-USD","n":"Solana","sp":0.15,"cat":"crypto"},
    "BNBUSD":{"t":"BNB-USD","n":"BNB","sp":0.12,"cat":"crypto"},
    "XRPUSD":{"t":"XRP-USD","n":"XRP","sp":0.15,"cat":"crypto"},
    "ADAUSD":{"t":"ADA-USD","n":"Cardano","sp":0.15,"cat":"crypto"},
    "AVAXUSD":{"t":"AVAX-USD","n":"Avalanche","sp":0.15,"cat":"crypto"},
    "LINKUSD":{"t":"LINK-USD","n":"Chainlink","sp":0.15,"cat":"crypto"},
    # Stocks CFD
    "AAPL":{"t":"AAPL","n":"Apple","sp":0.05,"cat":"stock"},
    "TSLA":{"t":"TSLA","n":"Tesla","sp":0.08,"cat":"stock"},
    "NVDA":{"t":"NVDA","n":"Nvidia","sp":0.08,"cat":"stock"},
    "AMZN":{"t":"AMZN","n":"Amazon","sp":0.05,"cat":"stock"},
    "MSFT":{"t":"MSFT","n":"Microsoft","sp":0.05,"cat":"stock"},
    "META":{"t":"META","n":"Meta","sp":0.06,"cat":"stock"},
    "GOOGL":{"t":"GOOGL","n":"Google","sp":0.05,"cat":"stock"},
}

BAL0 = 11_000.0
RISK_PCT = 0.0125  # Kelly/8 ≈ 1.25%
MAX_TRADES = 8
MAX_PER_CCY = 3

# ── Profils par stratégie — R:R ≥ 2:1 STRICT ──────────────────────────────────
BK_P = [
    {"tp1":1.5,"tp2":2.5,"tp3":4.0,"slb":0.10,"mh":5},
    {"tp1":1.0,"tp2":2.0,"tp3":3.0,"slb":0.10,"mh":5},
    {"tp1":1.0,"tp2":2.0,"tp3":3.0,"slb":0.12,"mh":5},
    {"tp1":1.0,"tp2":2.0,"tp3":3.0,"slb":0.15,"mh":5},
    {"tp1":1.5,"tp2":2.0,"tp3":3.5,"slb":0.12,"mh":5},
    {"tp1":1.0,"tp2":2.5,"tp3":4.5,"slb":0.15,"mh":8},
    {"tp1":2.0,"tp2":3.0,"tp3":5.0,"slb":0.15,"mh":10},
]
MR_P = [
    {"tp1":1.5,"tp2":2.5,"tp3":3.0,"slb":0.50,"mh":5,"rlo":25,"rhi":75},
    {"tp1":1.5,"tp2":2.5,"tp3":3.0,"slb":0.60,"mh":5,"rlo":30,"rhi":70},
    {"tp1":1.0,"tp2":2.0,"tp3":3.0,"slb":0.50,"mh":5,"rlo":25,"rhi":75},
    {"tp1":1.0,"tp2":2.0,"tp3":3.0,"slb":0.40,"mh":3,"rlo":28,"rhi":72},
    {"tp1":2.0,"tp2":3.0,"tp3":4.0,"slb":0.60,"mh":5,"rlo":25,"rhi":75},
    {"tp1":1.5,"tp2":2.0,"tp3":3.5,"slb":0.50,"mh":5,"rlo":30,"rhi":70},
]
TF_P = [
    {"tp1":2.0,"tp2":3.0,"tp3":5.0,"slb":1.5,"mh":10},
    {"tp1":2.0,"tp2":4.0,"tp3":6.0,"slb":2.0,"mh":15},
    {"tp1":1.5,"tp2":2.5,"tp3":4.0,"slb":1.0,"mh":8},
    {"tp1":2.0,"tp2":3.5,"tp3":5.5,"slb":2.0,"mh":15},
    {"tp1":1.0,"tp2":2.0,"tp3":3.0,"slb":1.0,"mh":5},
]
BK_MG = [0.03, 0.05, 0.08, 0.10]
LOOKBACKS = [2, 3, 5]
ADX_OPTS = [10, 12]
