#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════════╗
║     █████╗ ██████╗ ███████╗██╗  ██╗    ██╗   ██╗██████╗                        ║
║    ██╔══██╗██╔══██╗██╔════╝╚██╗██╔╝    ██║   ██║╚════██╗                       ║
║    ███████║██████╔╝█████╗   ╚███╔╝     ██║   ██║ █████╔╝                       ║
║    ██╔══██║██╔═══╝ ██╔══╝   ██╔██╗     ╚██╗ ██╔╝██╔═══╝                        ║
║    ██║  ██║██║     ███████╗██╔╝ ██╗     ╚████╔╝ ███████╗                       ║
║    ╚═╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝      ╚═══╝  ╚══════╝                       ║
║                                                                                  ║
║    ALL OPTIMIZATIONS COMBINED:                                                   ║
║    ✅ Multi-TF (4H trend confirmation)                                           ║
║    ✅ Session Filter (no Asia for forex)                                          ║
║    ✅ Compounding (equity-based sizing)                                           ║
║    ✅ Volatility Filter (ATR > 50% avg)                                          ║
║    ✅ Per-Asset Risk Tuning                                                       ║
║    ✅ Multi-TP (1.5R/2.5R/Trail) + Break-Even                                   ║
║    ✅ 10 Elite Assets × 1H Breakout × IC Markets ECN                             ║
╚══════════════════════════════════════════════════════════════════════════════════╝
"""

import os, sys, time, requests
import pandas as pd
import numpy as np
from datetime import datetime
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════
#  IC MARKETS ECN
# ═══════════════════════════════════════════════════════════════════════════
ECN_SPREAD_PIPS   = 0.1
ECN_COMM_PER_LOT  = 7.00
LOT_STD           = 100_000

# ═══════════════════════════════════════════════════════════════════════════
#  STRATEGY PARAMS
# ═══════════════════════════════════════════════════════════════════════════
SL_MULT     = 1.0
TP1_RR      = 1.5;  TP1_PCT = 0.40
TP2_RR      = 2.5;  TP2_PCT = 0.40
TP3_TRAIL_M = 1.2;  TP3_PCT = 0.20
BK_LOOKBACK = 6
ATR_P       = 14
EMA_F       = 9
EMA_S       = 21

# ═══════════════════════════════════════════════════════════════════════════
#  INSTRUMENTS + PER-ASSET RISK TUNING
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class Inst:
    epic: str; name: str; pip: float; contract: float
    min_lot: float; cat: str
    risk: float             # per-asset risk % (tuned)
    vol_filter: float       # min vol_ratio for entry

REG = {
    # High WR → higher risk
    "GOLD":   Inst("GOLD",   "Gold",      0.01,   100,     0.01, "commodity", 0.015, 0.8),  # 42.5% WR → 1.5%
    "AUDUSD": Inst("AUDUSD", "AUD/USD",   0.0001, 100_000, 0.01, "forex",     0.012, 0.8),  # 32.7% → 1.2%
    "EURJPY": Inst("EURJPY", "EUR/JPY",   0.01,   100_000, 0.01, "forex",     0.012, 0.8),  # 29.8% WR but big wins
    "ETHUSD": Inst("ETHUSD", "Ethereum",  0.01,   1,       0.01, "crypto",    0.012, 0.8),  # 31.8% → 1.2%
    "GBPJPY": Inst("GBPJPY", "GBP/JPY",   0.01,  100_000, 0.01, "forex",     0.010, 0.8),  # 30.8% → 1.0%
    "SILVER": Inst("SILVER", "Silver",    0.001,  5000,    0.01, "commodity", 0.010, 0.8),  # 28.6% but BE saves
    "GBPUSD": Inst("GBPUSD", "GBP/USD",   0.0001, 100_000, 0.01, "forex",    0.010, 0.8),  # 30.4% → 1.0%
    "DE40":   Inst("DE40",   "DAX 40",    0.01,   1,       1.0,  "index",     0.010, 0.8),  # 24.4% → 1.0%
    # Lower WR → lower risk
    "AUDNZD": Inst("AUDNZD", "AUD/NZD",   0.0001, 100_000, 0.01, "forex",    0.008, 1.0),  # 24.5% → 0.8%, tighter vol
    "EURUSD": Inst("EURUSD", "EUR/USD",   0.0001, 100_000, 0.01, "forex",     0.008, 1.0),  # 24.1% → 0.8%, tighter vol
}

ELITE_EPICS = list(REG.keys())

# ═══════════════════════════════════════════════════════════════════════════
#  SESSION HOURS (UTC) — forex only filter
# ═══════════════════════════════════════════════════════════════════════════
# London: 7-16 UTC, New York: 12-21 UTC → combined active: 7-21 UTC
ACTIVE_START = 7   # UTC
ACTIVE_END   = 21  # UTC

# ═══════════════════════════════════════════════════════════════════════════
#  DATA FETCHER
# ═══════════════════════════════════════════════════════════════════════════
class Fetcher:
    def __init__(self):
        self.url = "https://demo-api-capital.backend-capital.com/api/v1"
        self.cst = self.tok = ""
        try:
            r = requests.post(f"{self.url}/session", headers={
                "X-CAP-API-KEY": os.getenv("CAPITAL_API_KEY",""),
                "Content-Type": "application/json",
            }, json={"identifier": os.getenv("CAPITAL_EMAIL",""),
                     "password": os.getenv("CAPITAL_PASSWORD","")}, timeout=10)
            if r.status_code == 200:
                self.cst = r.headers.get("CST","")
                self.tok = r.headers.get("X-SECURITY-TOKEN","")
                print("  ✅ Data feed OK")
        except Exception as e:
            print(f"  ❌ {e}")

    def fetch(self, epic, resolution="HOUR", count=1000):
        try:
            r = requests.get(f"{self.url}/prices/{epic}", headers={
                "X-SECURITY-TOKEN": self.tok, "CST": self.cst,
                "Content-Type": "application/json",
            }, params={"resolution": resolution, "max": min(count, 1000)}, timeout=15)
            if r.status_code != 200: return None
            prices = r.json().get("prices", [])
            if not prices: return None
            rows = []
            for p in prices:
                o = (float(p["openPrice"]["bid"]) + float(p["openPrice"]["ask"])) / 2
                h = (float(p["highPrice"]["bid"]) + float(p["highPrice"]["ask"])) / 2
                l = (float(p["lowPrice"]["bid"])  + float(p["lowPrice"]["ask"]))  / 2
                c = (float(p["closePrice"]["bid"]) + float(p["closePrice"]["ask"])) / 2
                v = int(p.get("lastTradedVolume", 0))
                rows.append({"ts": p["snapshotTime"], "open":o,"high":h,"low":l,"close":c,"volume":v})
            df = pd.DataFrame(rows)
            df["ts"] = pd.to_datetime(df["ts"])
            df.set_index("ts", inplace=True)
            df.sort_index(inplace=True)
            return df
        except: return None

# ═══════════════════════════════════════════════════════════════════════════
#  INDICATORS (1H entry)
# ═══════════════════════════════════════════════════════════════════════════
def compute(df):
    h, l, c, v = df["high"], df["low"], df["close"], df["volume"]
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    df["atr"]   = tr.rolling(ATR_P).mean()
    df["atr50"] = df["atr"].rolling(50).mean()  # for volatility filter
    df["ema_f"] = c.ewm(span=EMA_F, adjust=False).mean()
    df["ema_s"] = c.ewm(span=EMA_S, adjust=False).mean()
    df["macd"]  = df["ema_f"] - df["ema_s"]
    df["macd_sig"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_h"]   = df["macd"] - df["macd_sig"]
    df["bk_hi"] = h.rolling(BK_LOOKBACK).max().shift(1)
    df["bk_lo"] = l.rolling(BK_LOOKBACK).min().shift(1)
    df["vol_r"] = v / v.rolling(20).mean().replace(0, 1)
    return df

# ═══════════════════════════════════════════════════════════════════════════
#  4H TREND INDICATORS (higher timeframe confirmation)
# ═══════════════════════════════════════════════════════════════════════════
def compute_4h(df_1h):
    """Resample 1H to 4H and compute trend direction."""
    df_4h = df_1h.resample("4h").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum"
    }).dropna()
    df_4h["ema9"]  = df_4h["close"].ewm(span=9, adjust=False).mean()
    df_4h["ema21"] = df_4h["close"].ewm(span=21, adjust=False).mean()
    df_4h["trend"] = np.where(df_4h["ema9"] > df_4h["ema21"], 1,
                     np.where(df_4h["ema9"] < df_4h["ema21"], -1, 0))
    return df_4h

def get_4h_trend(df_4h, ts):
    """Get the 4H trend direction for a given 1H timestamp."""
    valid = df_4h.index[df_4h.index <= ts]
    if len(valid) == 0:
        return 0
    return int(df_4h.loc[valid[-1], "trend"])

# ═══════════════════════════════════════════════════════════════════════════
#  SIGNAL — Pure BK + all filters
# ═══════════════════════════════════════════════════════════════════════════
def signal(df, i, inst, df_4h):
    if i < 50: return "HOLD", ""
    row = df.iloc[i]
    c = row["close"]; atr = row["atr"]
    if pd.isna(atr) or atr <= 0: return "HOLD", ""

    # ── VOLATILITY FILTER: skip if ATR < 50% of its 50-period avg ──
    atr50 = row.get("atr50", atr)
    if not pd.isna(atr50) and atr50 > 0 and atr < atr50 * 0.50:
        return "HOLD", ""

    # ── SESSION FILTER: no Asia for forex ──
    if inst.cat == "forex":
        hour = row.name.hour if hasattr(row.name, 'hour') else 12
        if hour < ACTIVE_START or hour >= ACTIVE_END:
            return "HOLD", ""

    ef = row["ema_f"]; es = row["ema_s"]
    mh = row["macd_h"]; bh = row["bk_hi"]; bl = row["bk_lo"]
    vr = row.get("vol_r", 1.0)

    # ── VOLUME FILTER (per-asset tuned) ──
    if vr < inst.vol_filter:
        return "HOLD", ""

    # ── 4H TREND CONFIRMATION ──
    trend_4h = get_4h_trend(df_4h, row.name)

    # ── BREAKOUT LONG ──
    if c > bh and ef > es and mh > 0:
        if trend_4h >= 0:  # 4H not bearish
            return "BUY", "BK"

    # ── BREAKOUT SHORT ──
    if c < bl and ef < es and mh < 0:
        if trend_4h <= 0:  # 4H not bullish
            return "SELL", "BK"

    return "HOLD", ""

# ═══════════════════════════════════════════════════════════════════════════
#  FEE
# ═══════════════════════════════════════════════════════════════════════════
def ecn_fee(lots, inst, price):
    if inst.cat == "forex":
        c = ECN_COMM_PER_LOT * lots
    else:
        c = (lots * price * inst.contract / LOT_STD) * ECN_COMM_PER_LOT
    return c + ECN_SPREAD_PIPS * inst.pip * inst.contract * lots

# ═══════════════════════════════════════════════════════════════════════════
#  POSITION / TRADE
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class Pos:
    inst: str; d: str; strat: str; entry: float
    sl: float; tp1: float; tp2: float
    lots: float; l1: float; l2: float; l3: float
    comm: float; atr0: float; i_open: int
    tp1_hit: bool = False; tp2_hit: bool = False
    trail_sl: float = 0.0
    p1: float = 0.0; p2: float = 0.0; p3: float = 0.0

@dataclass
class TR:
    inst: str; d: str; entry: float; lots: float; comm: float
    i_open: int; i_close: int = 0
    pnl_g: float = 0; pnl_n: float = 0; result: str = ""
    tp1_hit: bool = False; tp2_hit: bool = False
    exit_type: str = ""

# ═══════════════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE (with compounding)
# ═══════════════════════════════════════════════════════════════════════════
def backtest(df_1h, inst: Inst, capital=25000.0):
    df = compute(df_1h)
    df_4h = compute_4h(df_1h)
    trades = []; eq = capital; peak = eq; mdd = 0; pos = None

    for i in range(50, len(df)):
        row = df.iloc[i]
        hi, lo, cl = row["high"], row["low"], row["close"]

        if pos is not None:
            buy = pos.d == "BUY"

            # ── TP1 ──
            if not pos.tp1_hit:
                t1 = (hi >= pos.tp1) if buy else (lo <= pos.tp1)
                sl = (lo <= pos.sl) if buy else (hi >= pos.sl)
                if sl and not t1:
                    pg = (pos.sl - pos.entry if buy else pos.entry - pos.sl) * pos.lots * inst.contract
                    tr = TR(inst=pos.inst, d=pos.d, entry=pos.entry, lots=pos.lots,
                            comm=pos.comm, i_open=pos.i_open, i_close=i,
                            pnl_g=pg, pnl_n=pg - pos.comm, result="LOSS", exit_type="SL")
                    eq += tr.pnl_n; trades.append(tr)
                    if eq > peak: peak = eq
                    dd = (peak - eq) / peak * 100
                    if dd > mdd: mdd = dd
                    pos = None; continue
                if t1:
                    pos.tp1_hit = True
                    pos.p1 = (pos.tp1 - pos.entry if buy else pos.entry - pos.tp1) * pos.l1 * inst.contract
                    pos.sl = pos.entry  # BE
                    pos.trail_sl = pos.entry
                    continue

            # ── TP2 ──
            if pos.tp1_hit and not pos.tp2_hit:
                t2 = (hi >= pos.tp2) if buy else (lo <= pos.tp2)
                be = (lo <= pos.sl) if buy else (hi >= pos.sl)
                if be and not t2:
                    pg = pos.p1
                    cm = ecn_fee(pos.l1, inst, pos.entry) + ecn_fee(pos.l2 + pos.l3, inst, pos.entry)
                    tr = TR(inst=pos.inst, d=pos.d, entry=pos.entry, lots=pos.lots,
                            comm=cm, i_open=pos.i_open, i_close=i,
                            pnl_g=pg, pnl_n=pg - cm, result="BE",
                            tp1_hit=True, exit_type="BE")
                    eq += tr.pnl_n; trades.append(tr)
                    if eq > peak: peak = eq
                    dd = (peak - eq) / peak * 100
                    if dd > mdd: mdd = dd
                    pos = None; continue
                if t2:
                    pos.tp2_hit = True
                    pos.p2 = (pos.tp2 - pos.entry if buy else pos.entry - pos.tp2) * pos.l2 * inst.contract
                    if buy: pos.trail_sl = max(pos.trail_sl, pos.tp1)
                    else: pos.trail_sl = pos.tp1
                    continue

            # ── TP3 trailing ──
            if pos.tp1_hit and pos.tp2_hit:
                a = row["atr"] if not pd.isna(row["atr"]) else pos.atr0
                td = a * TP3_TRAIL_M
                if buy:
                    ns = cl - td
                    if ns > pos.trail_sl: pos.trail_sl = ns
                    if lo <= pos.trail_sl:
                        pos.p3 = (pos.trail_sl - pos.entry) * pos.l3 * inst.contract
                        pg = pos.p1 + pos.p2 + pos.p3
                        tr = TR(inst=pos.inst, d=pos.d, entry=pos.entry, lots=pos.lots,
                                comm=pos.comm, i_open=pos.i_open, i_close=i,
                                pnl_g=pg, pnl_n=pg - pos.comm, result="WIN",
                                tp1_hit=True, tp2_hit=True, exit_type="TRAIL")
                        eq += tr.pnl_n; trades.append(tr)
                        if eq > peak: peak = eq
                        dd = (peak - eq) / peak * 100
                        if dd > mdd: mdd = dd
                        pos = None; continue
                else:
                    ns = cl + td
                    if pos.trail_sl == 0 or ns < pos.trail_sl: pos.trail_sl = ns
                    if hi >= pos.trail_sl:
                        pos.p3 = (pos.entry - pos.trail_sl) * pos.l3 * inst.contract
                        pg = pos.p1 + pos.p2 + pos.p3
                        tr = TR(inst=pos.inst, d=pos.d, entry=pos.entry, lots=pos.lots,
                                comm=pos.comm, i_open=pos.i_open, i_close=i,
                                pnl_g=pg, pnl_n=pg - pos.comm, result="WIN",
                                tp1_hit=True, tp2_hit=True, exit_type="TRAIL")
                        eq += tr.pnl_n; trades.append(tr)
                        if eq > peak: peak = eq
                        dd = (peak - eq) / peak * 100
                        if dd > mdd: mdd = dd
                        pos = None; continue
            continue

        # ── New signal ──
        sig, strat = signal(df, i, inst, df_4h)
        if sig == "HOLD": continue
        atr = row["atr"]
        if pd.isna(atr) or atr <= 0: continue
        entry = cl; sl_d = atr * SL_MULT
        if sl_d <= 0: continue

        if sig == "BUY":
            sl = entry - sl_d; t1 = entry + sl_d * TP1_RR; t2 = entry + sl_d * TP2_RR
        else:
            sl = entry + sl_d; t1 = entry - sl_d * TP1_RR; t2 = entry - sl_d * TP2_RR

        # ── COMPOUNDING: size based on CURRENT equity ──
        total = max(inst.min_lot, round((eq * inst.risk) / (sl_d * inst.contract), 2))
        l1 = max(inst.min_lot, round(total * TP1_PCT, 2))
        l2 = max(inst.min_lot, round(total * TP2_PCT, 2))
        l3 = max(inst.min_lot, round(total * TP3_PCT, 2))
        actual = l1 + l2 + l3
        cm = ecn_fee(actual, inst, entry)

        # Fee gate
        exp = sl_d * TP1_RR * actual * inst.contract
        if exp > 0 and cm / exp > 0.50: continue

        pos = Pos(inst=inst.epic, d=sig, strat=strat, entry=entry,
                  sl=round(sl,5), tp1=round(t1,5), tp2=round(t2,5),
                  lots=actual, l1=l1, l2=l2, l3=l3, comm=cm, atr0=atr, i_open=i)

    # Force close
    if pos is not None:
        lc = df.iloc[-1]["close"]; buy = pos.d == "BUY"
        pg = pos.p1 + pos.p2
        if not pos.tp1_hit:
            pg += (lc - pos.entry if buy else pos.entry - lc) * pos.lots * inst.contract
        elif not pos.tp2_hit:
            pg += (lc - pos.entry if buy else pos.entry - lc) * (pos.l2 + pos.l3) * inst.contract
        else:
            pg += (lc - pos.entry if buy else pos.entry - lc) * pos.l3 * inst.contract
        tr = TR(inst=pos.inst, d=pos.d, entry=pos.entry, lots=pos.lots,
                comm=pos.comm, i_open=pos.i_open, i_close=len(df)-1,
                pnl_g=pg, pnl_n=pg - pos.comm,
                result="WIN" if pg > pos.comm else "LOSS",
                tp1_hit=pos.tp1_hit, tp2_hit=pos.tp2_hit, exit_type="FC")
        eq += tr.pnl_n; trades.append(tr)
    return trades, mdd, eq

# ═══════════════════════════════════════════════════════════════════════════
#  TEAR SHEET
# ═══════════════════════════════════════════════════════════════════════════
def tear_sheet(all_trades, mdd_map, final_eqs):
    W = 100
    print("\n" + "═"*W)
    print("     █████╗ ██████╗ ███████╗██╗  ██╗    ██╗   ██╗██████╗     ███████╗██╗   ██╗██╗     ██╗     ")
    print("    ██╔══██╗██╔══██╗██╔════╝╚██╗██╔╝    ██║   ██║╚════██╗    ██╔════╝██║   ██║██║     ██║     ")
    print("    ███████║██████╔╝█████╗   ╚███╔╝     ██║   ██║ █████╔╝    █████╗  ██║   ██║██║     ██║     ")
    print("    ██╔══██║██╔═══╝ ██╔══╝   ██╔██╗     ╚██╗ ██╔╝██╔═══╝     ██╔══╝  ██║   ██║██║     ██║     ")
    print("    ██║  ██║██║     ███████╗██╔╝ ██╗     ╚████╔╝ ███████╗    ██║     ╚██████╔╝███████╗███████╗")
    print("    ╚═╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝      ╚═══╝  ╚══════╝    ╚═╝      ╚═════╝ ╚══════╝╚══════╝")
    print("    ALL OPTIMIZATIONS × Multi-TP × BE × Compounding × 4H Trend × Session Filter")
    print("═"*W)

    print(f"\n  📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  🏦 IC Markets ECN | 💰 $25,000 starting")
    print(f"  🎯 TP1={TP1_RR}R/{TP1_PCT*100:.0f}% | TP2={TP2_RR}R/{TP2_PCT*100:.0f}% | TP3=Trail/{TP3_PCT*100:.0f}%")
    print(f"  🔬 4H Trend ✅ | Session 7-21 UTC ✅ | Vol Filter ✅ | Compounding ✅")

    inst_d = {}
    for t in all_trades:
        if t.inst not in inst_d:
            inst_d[t.inst] = {"n":0,"w":0,"be":0,"pg":0,"pn":0,"cm":0,"t1":0,"t2":0,"tr":0}
        d = inst_d[t.inst]
        d["n"] += 1; d["pg"] += t.pnl_g; d["pn"] += t.pnl_n; d["cm"] += t.comm
        if t.result == "WIN": d["w"] += 1
        if t.result == "BE": d["be"] += 1
        if t.tp1_hit: d["t1"] += 1
        if t.tp2_hit: d["t2"] += 1
        if t.exit_type == "TRAIL": d["tr"] += 1

    print(f"\n  {'─'*94}")
    print(f"  {'ASSET':<14} {'RISK':>5} {'#':>4} {'W':>3} {'BE':>3} {'L':>3} {'W/R':>6} "
          f"{'PnL BRUT':>11} {'COMM':>9} {'PnL NET':>11} {'TP1%':>5} {'TRAIL':>5}")
    print(f"  {'─'*94}")

    tn=tw=tbe=0; tg=tc=tnet=0.0
    for epic in ELITE_EPICS:
        d = inst_d.get(epic)
        if not d: continue
        inst = REG[epic]
        wr = d["w"]/d["n"]*100 if d["n"]>0 else 0
        ls = d["n"]-d["w"]-d["be"]
        t1r = d["t1"]/d["n"]*100 if d["n"]>0 else 0
        ic = "🟢" if d["pn"]>0 else "🔴"
        print(f"  {ic} {inst.name:<12} {inst.risk*100:>4.1f}% {d['n']:>3} {d['w']:>3} {d['be']:>3} {ls:>3} "
              f"{wr:>5.1f}% ${d['pg']:>9,.2f} ${d['cm']:>7,.2f} ${d['pn']:>9,.2f} {t1r:>4.0f}% {d['tr']:>4}")
        tn+=d["n"]; tw+=d["w"]; tbe+=d["be"]; tg+=d["pg"]; tc+=d["cm"]; tnet+=d["pn"]

    print(f"  {'─'*94}")
    tl=tn-tw-tbe; wr=tw/tn*100 if tn>0 else 0
    ic = "🟢" if tnet > 0 else "🔴"
    print(f"  {ic} {'TOTAL':<12} {'':>5} {tn:>3} {tw:>3} {tbe:>3} {tl:>3} "
          f"{wr:>5.1f}% ${tg:>9,.2f} ${tc:>7,.2f} ${tnet:>9,.2f}")
    print(f"  {'═'*94}")

    # Exit types
    et_d = {}
    for t in all_trades:
        if t.exit_type not in et_d: et_d[t.exit_type]={"n":0,"p":0}
        et_d[t.exit_type]["n"]+=1; et_d[t.exit_type]["p"]+=t.pnl_n
    labels = {"SL":"❌ Stop Loss","BE":"🛡️ Break-Even","TRAIL":"🚀 Trailing","FC":"⏹️ Force Close"}
    print(f"\n  📊 EXIT TYPES")
    print(f"  {'─'*60}")
    for k,v in sorted(et_d.items(), key=lambda x: x[1]["p"], reverse=True):
        ic="🟢" if v["p"]>0 else "🔴"
        print(f"  {ic} {labels.get(k,k):<28} | {v['n']:>4} | PnL ${v['p']:>+10,.2f}")

    # Commissions
    print(f"\n  💸 COMMISSIONS")
    print(f"  {'─'*60}")
    print(f"  PnL Brut       : ${tg:>+12,.2f}")
    print(f"  Commissions    : ${tc:>+12,.2f}")
    print(f"  PnL Net        : ${tnet:>+12,.2f}")
    if tg > 0: print(f"  Comm / Brut    :   {tc/tg*100:>8.1f}%")

    # ECN vs Retail
    rc = sum(1.5 * REG.get(t.inst, REG["EURUSD"]).pip * REG.get(t.inst, REG["EURUSD"]).contract * t.lots
             for t in all_trades)
    print(f"\n  ⚡ ECN vs RETAIL")
    print(f"  {'─'*60}")
    print(f"  Savings        : ${rc - tc:>+12,.2f}")

    # Performance
    days = 1000/24
    print(f"\n  📈 PERFORMANCE ({days:.0f} jours)")
    print(f"  {'─'*60}")
    print(f"  ROC              :   {tnet/25000*100:>+8.2f}%")
    print(f"  PnL / Jour       : ${tnet/days:>+10.2f}")
    print(f"  Trades / Jour    :   {tn/days:>8.1f}")
    print(f"  Avg PnL / Trade  : ${tnet/tn if tn else 0:>+10.2f}")
    print(f"  Max Drawdown     :   {max(mdd_map.values()) if mdd_map else 0:>7.1f}%")

    # Compounding effect
    avg_final = np.mean(list(final_eqs.values())) if final_eqs else 25000
    print(f"\n  📈 COMPOUNDING")
    print(f"  {'─'*60}")
    print(f"  Avg Final Equity : ${avg_final:>10,.2f}")

    # vs Previous
    print(f"\n  🔬 EVOLUTION")
    print(f"  {'─'*60}")
    print(f"  V1 Ultimate (no filters) : $+23,547 ($565/j)")
    print(f"  V2 Full Optim            : ${tnet:>+,.0f} (${tnet/days:>+,.0f}/j)")
    delta = ((tnet - 23547) / 23547 * 100)
    print(f"  Delta                    :   {delta:>+.1f}%")

    v = "✅ APEX V2 — MACHINE À IMPRIMER" if tnet > 0 else "⚠️ NEEDS TUNING"
    print(f"\n  🏆 {v}")
    print("═"*W + "\n")

# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════
def main():
    print("\n" + "═"*80)
    print("  APEX V2 — ALL OPTIMIZATIONS COMBINED")
    print("  4H Trend + Session Filter + Compounding + Vol Filter + Per-Asset Risk")
    print("═"*80)

    f = Fetcher()
    all_trades = []; mdd_map = {}; eq_map = {}

    for epic in ELITE_EPICS:
        inst = REG[epic]
        print(f"  📊 {inst.name:<12} ({epic}) risk={inst.risk*100:.1f}%", end=" ", flush=True)
        df = f.fetch(epic, "HOUR", 1000)
        if df is None or len(df) < 60:
            print("❌"); continue
        print(f"✅ {len(df)}h", end="")
        trades, mdd, final_eq = backtest(df, inst)
        all_trades.extend(trades); mdd_map[epic] = mdd; eq_map[epic] = final_eq
        pnl = sum(t.pnl_n for t in trades)
        w = sum(1 for t in trades if t.result=="WIN")
        b = sum(1 for t in trades if t.result=="BE")
        ic = "🟢" if pnl > 0 else "🔴"
        print(f" → {ic} {len(trades)}t ({w}W/{b}BE) | ${pnl:+,.0f} | eq=${final_eq:,.0f}")
        time.sleep(0.3)

    if all_trades:
        tear_sheet(all_trades, mdd_map, eq_map)

if __name__ == "__main__":
    main()
