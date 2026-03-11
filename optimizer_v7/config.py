#!/usr/bin/env python3
"""
config.py — Constantes et profils de paramètres pour NEMESIS V7
"""
import os, json, warnings
warnings.filterwarnings("ignore")

# Charger les v6 winners
V6_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "asset_profiles_v6.json")
with open(V6_PATH) as f:
    V6 = json.load(f)

ASSETS = V6["assets"]  # 39 actifs gagnants de v6
BAL0 = 11_000.0
RISK_PCT = 0.0125  # Kelly/8
MAX_TRADES = 10
MAX_PER_CCY = 3
MIN_RR = 1.2  # R:R minimum (gain > perte garanti, adapté au 4H)

# Profils adaptés au 4H (lookback en bougies 4H, max_hold en bougies 4H)
# 1 jour = 6 bougies 4H. Lookback 5 jours = 30 bougies
BK_P = [
    {"tp1":1.5,"tp2":2.5,"tp3":4.0,"slb":0.10,"mh":18},   # 3j
    {"tp1":1.0,"tp2":2.0,"tp3":3.0,"slb":0.10,"mh":18},
    {"tp1":1.0,"tp2":2.0,"tp3":3.0,"slb":0.12,"mh":30},   # 5j
    {"tp1":1.5,"tp2":2.0,"tp3":3.5,"slb":0.12,"mh":18},
    {"tp1":1.0,"tp2":2.5,"tp3":4.5,"slb":0.15,"mh":30},
    {"tp1":2.0,"tp2":3.0,"tp3":5.0,"slb":0.15,"mh":42},   # 7j
]
MR_P = [
    {"tp1":1.5,"tp2":2.5,"tp3":3.0,"slb":0.50,"mh":18,"rlo":25,"rhi":75},
    {"tp1":1.0,"tp2":2.0,"tp3":3.0,"slb":0.50,"mh":18,"rlo":25,"rhi":75},
    {"tp1":1.0,"tp2":2.0,"tp3":3.0,"slb":0.40,"mh":12,"rlo":28,"rhi":72},
    {"tp1":2.0,"tp2":3.0,"tp3":4.0,"slb":0.60,"mh":30,"rlo":25,"rhi":75},
    {"tp1":1.5,"tp2":2.0,"tp3":3.5,"slb":0.50,"mh":18,"rlo":30,"rhi":70},
    {"tp1":1.5,"tp2":2.5,"tp3":3.0,"slb":0.60,"mh":18,"rlo":30,"rhi":70},
    # Profils 4H adaptés: TP plus proches (avg ~1.5-1.7R)
    {"tp1":0.8,"tp2":1.5,"tp3":2.5,"slb":0.40,"mh":12,"rlo":25,"rhi":75},
    {"tp1":0.8,"tp2":1.5,"tp3":2.5,"slb":0.50,"mh":18,"rlo":28,"rhi":72},
    {"tp1":1.0,"tp2":1.5,"tp3":2.0,"slb":0.40,"mh":12,"rlo":25,"rhi":75},
    {"tp1":1.0,"tp2":1.8,"tp3":2.5,"slb":0.50,"mh":18,"rlo":30,"rhi":70},
]
TF_P = [
    {"tp1":2.0,"tp2":3.0,"tp3":5.0,"slb":1.5,"mh":42},
    {"tp1":2.0,"tp2":4.0,"tp3":6.0,"slb":2.0,"mh":60},
    {"tp1":1.5,"tp2":2.5,"tp3":4.0,"slb":1.0,"mh":30},
    {"tp1":1.0,"tp2":2.0,"tp3":3.0,"slb":1.0,"mh":18},
]
BK_MG = [0.05, 0.08]
# Lookbacks 4H réduits pour vitesse
LOOKBACKS = [6, 18]  # 1j, 3j
ADX_OPTS = [12]
