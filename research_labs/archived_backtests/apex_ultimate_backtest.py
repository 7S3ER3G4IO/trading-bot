#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════════╗
║     █████╗ ██████╗ ███████╗██╗  ██╗    ██╗   ██╗██╗  ████████╗██╗███╗   ███╗   ║
║    ██╔══██╗██╔══██╗██╔════╝╚██╗██╔╝    ██║   ██║██║  ╚══██╔══╝██║████╗ ████║   ║
║    ███████║██████╔╝█████╗   ╚███╔╝     ██║   ██║██║     ██║   ██║██╔████╔██║   ║
║    ██╔══██║██╔═══╝ ██╔══╝   ██╔██╗     ██║   ██║██║     ██║   ██║██║╚██╔╝██║   ║
║    ██║  ██║██║     ███████╗██╔╝ ██╗    ╚██████╔╝███████╗██║   ██║██║ ╚═╝ ██║   ║
║    ╚═╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝     ╚═════╝ ╚══════╝╚═╝   ╚═╝╚═╝     ╚═╝   ║
║                                                                                  ║
║    Risk 1.0% + Multi-TP (1.5R/2.5R/Trail) + Break-Even                          ║
║    10 Elite Assets × 1H Breakout × IC Markets ECN                                ║
╚══════════════════════════════════════════════════════════════════════════════════╝
"""

import os, sys, time, requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════
#  IC MARKETS ECN
# ═══════════════════════════════════════════════════════════════════════════
ECN_SPREAD_PIPS   = 0.1
ECN_COMM_PER_LOT  = 7.00
LOT_STD           = 100_000

# ═══════════════════════════════════════════════════════════════════════════
#  ULTIMATE STRATEGY PARAMS
# ═══════════════════════════════════════════════════════════════════════════
RISK_PCT    = 0.01      # 1.0% risk per trade (doubled from 0.5%)
SL_MULT     = 1.0       # SL = 1.0 × ATR
TP1_RR      = 1.5       # TP1 = 1.5R  → close 40%
TP2_RR      = 2.5       # TP2 = 2.5R  → close 40%
TP3_TRAIL   = 1.2       # TP3 = trailing stop at 1.2×ATR behind price → 20% rides
TP1_PCT     = 0.40      # 40% of position
TP2_PCT     = 0.40      # 40% of position
TP3_PCT     = 0.20      # 20% of position (trailing)
BK_LOOKBACK = 6
ATR_P       = 14
EMA_F       = 9
EMA_S       = 21

# ═══════════════════════════════════════════════════════════════════════════
#  INSTRUMENTS
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class Inst:
    epic: str; name: str; pip: float; contract: float; min_lot: float; cat: str

REG = {
    "EURUSD": Inst("EURUSD", "EUR/USD",  0.0001, 100_000, 0.01, "forex"),
    "GBPUSD": Inst("GBPUSD", "GBP/USD",  0.0001, 100_000, 0.01, "forex"),
    "GBPJPY": Inst("GBPJPY", "GBP/JPY",  0.01,   100_000, 0.01, "forex"),
    "EURJPY": Inst("EURJPY", "EUR/JPY",  0.01,   100_000, 0.01, "forex"),
    "AUDUSD": Inst("AUDUSD", "AUD/USD",  0.0001, 100_000, 0.01, "forex"),
    "AUDNZD": Inst("AUDNZD", "AUD/NZD",  0.0001, 100_000, 0.01, "forex"),
    "GOLD":   Inst("GOLD",   "Gold",     0.01,   100,     0.01, "commodity"),
    "SILVER": Inst("SILVER", "Silver",   0.001,  5000,    0.01, "commodity"),
    "DE40":   Inst("DE40",   "DAX 40",   0.01,   1,       1.0,  "index"),
    "ETHUSD": Inst("ETHUSD", "Ethereum", 0.01,   1,       0.01, "crypto"),
}

ELITE_EPICS = [
    "EURJPY", "AUDNZD", "ETHUSD", "GOLD", "GBPUSD",
    "GBPJPY", "SILVER", "EURUSD", "AUDUSD", "DE40",
]

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
                print("  ✅ Capital.com data feed OK")
        except Exception as e:
            print(f"  ❌ {e}")

    def fetch(self, epic, count=1000):
        try:
            r = requests.get(f"{self.url}/prices/{epic}", headers={
                "X-SECURITY-TOKEN": self.tok, "CST": self.cst,
                "Content-Type": "application/json",
            }, params={"resolution": "HOUR", "max": min(count, 1000)}, timeout=15)
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
#  INDICATORS
# ═══════════════════════════════════════════════════════════════════════════
def compute(df):
    h, l, c, v = df["high"], df["low"], df["close"], df["volume"]
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(ATR_P).mean()
    df["ema_f"] = c.ewm(span=EMA_F, adjust=False).mean()
    df["ema_s"] = c.ewm(span=EMA_S, adjust=False).mean()
    df["macd"] = df["ema_f"] - df["ema_s"]
    df["macd_sig"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_h"] = df["macd"] - df["macd_sig"]
    df["bk_hi"] = h.rolling(BK_LOOKBACK).max().shift(1)
    df["bk_lo"] = l.rolling(BK_LOOKBACK).min().shift(1)
    df["vol_r"] = v / v.rolling(20).mean().replace(0, 1)
    return df

# ═══════════════════════════════════════════════════════════════════════════
#  SIGNAL — Pure BK
# ═══════════════════════════════════════════════════════════════════════════
def signal(df, i):
    if i < 30: return "HOLD", ""
    row = df.iloc[i]
    c = row["close"]; atr = row["atr"]
    if pd.isna(atr) or atr <= 0: return "HOLD", ""
    ef = row["ema_f"]; es = row["ema_s"]
    mh = row["macd_h"]; bh = row["bk_hi"]; bl = row["bk_lo"]
    vr = row.get("vol_r", 1.0)
    if c > bh and ef > es and mh > 0 and vr > 0.8:
        return "BUY", "BK"
    if c < bl and ef < es and mh < 0 and vr > 0.8:
        return "SELL", "BK"
    return "HOLD", ""

# ═══════════════════════════════════════════════════════════════════════════
#  FEE CALC
# ═══════════════════════════════════════════════════════════════════════════
def ecn_fee(lots, inst, price):
    if inst.cat == "forex":
        comm = ECN_COMM_PER_LOT * lots
    else:
        notional = lots * price * inst.contract
        comm = (notional / LOT_STD) * ECN_COMM_PER_LOT
    return comm + ECN_SPREAD_PIPS * inst.pip * inst.contract * lots

# ═══════════════════════════════════════════════════════════════════════════
#  MULTI-TP POSITION
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class Position:
    inst: str; d: str; strat: str; entry: float
    sl: float; sl_orig: float
    tp1: float; tp2: float
    lots_total: float; lots_tp1: float; lots_tp2: float; lots_tp3: float
    comm_total: float
    atr_at_entry: float
    i_open: int
    # State
    tp1_hit: bool = False
    tp2_hit: bool = False
    be_active: bool = False
    trail_sl: float = 0.0
    # Results
    pnl_tp1: float = 0.0
    pnl_tp2: float = 0.0
    pnl_tp3: float = 0.0

@dataclass
class TradeResult:
    inst: str; d: str; strat: str; entry: float; sl: float
    lots: float; comm: float; i_open: int; i_close: int = 0
    pnl_g: float = 0; pnl_n: float = 0; result: str = ""
    tp1_hit: bool = False; tp2_hit: bool = False; tp3_exit: float = 0
    exit_type: str = ""


def backtest(df, inst: Inst, capital=25000.0):
    df = compute(df)
    trades = []
    eq = capital; peak = eq; mdd = 0
    pos = None

    for i in range(30, len(df)):
        row = df.iloc[i]
        hi, lo, close = row["high"], row["low"], row["close"]

        # ── Manage open position ──
        if pos is not None:
            is_buy = pos.d == "BUY"

            # ── TP1 check (1.5R) ──
            if not pos.tp1_hit:
                tp1_hit = (hi >= pos.tp1) if is_buy else (lo <= pos.tp1)
                sl_hit  = (lo <= pos.sl) if is_buy else (hi >= pos.sl)

                if sl_hit and not tp1_hit:
                    # Full SL — all 3 portions lost
                    pnl_g = (pos.sl - pos.entry if is_buy else pos.entry - pos.sl) * pos.lots_total * inst.contract
                    comm = pos.comm_total
                    tr = TradeResult(inst=pos.inst, d=pos.d, strat=pos.strat,
                                    entry=pos.entry, sl=pos.sl, lots=pos.lots_total,
                                    comm=comm, i_open=pos.i_open, i_close=i,
                                    pnl_g=pnl_g, pnl_n=pnl_g - comm,
                                    result="LOSS", exit_type="SL_FULL")
                    eq += tr.pnl_n; trades.append(tr)
                    if eq > peak: peak = eq
                    dd = (peak - eq) / peak * 100
                    if dd > mdd: mdd = dd
                    pos = None; continue

                if tp1_hit:
                    # TP1 hit → close 40%, activate BE
                    pos.tp1_hit = True
                    pos.be_active = True
                    pos.pnl_tp1 = (pos.tp1 - pos.entry if is_buy else pos.entry - pos.tp1) * pos.lots_tp1 * inst.contract
                    # Move SL to entry (Break-Even)
                    pos.sl = pos.entry
                    # Initialize trailing stop for TP3
                    pos.trail_sl = pos.entry
                    continue

            # ── TP2 check (2.5R) ──
            if pos.tp1_hit and not pos.tp2_hit:
                tp2_hit = (hi >= pos.tp2) if is_buy else (lo <= pos.tp2)
                be_hit  = (lo <= pos.sl) if is_buy else (hi >= pos.sl)  # SL is now BE

                if be_hit and not tp2_hit:
                    # BE hit on remaining 60% — TP1 profit locked, rest at 0
                    comm_remaining = ecn_fee(pos.lots_tp2 + pos.lots_tp3, inst, pos.entry)
                    pnl_g = pos.pnl_tp1  # only TP1 profit
                    comm = ecn_fee(pos.lots_tp1, inst, pos.entry) + comm_remaining
                    tr = TradeResult(inst=pos.inst, d=pos.d, strat=pos.strat,
                                    entry=pos.entry, sl=pos.sl, lots=pos.lots_total,
                                    comm=comm, i_open=pos.i_open, i_close=i,
                                    pnl_g=pnl_g, pnl_n=pnl_g - comm,
                                    result="BE", tp1_hit=True, exit_type="BE_AFTER_TP1")
                    eq += tr.pnl_n; trades.append(tr)
                    if eq > peak: peak = eq
                    dd = (peak - eq) / peak * 100
                    if dd > mdd: mdd = dd
                    pos = None; continue

                if tp2_hit:
                    pos.tp2_hit = True
                    pos.pnl_tp2 = (pos.tp2 - pos.entry if is_buy else pos.entry - pos.tp2) * pos.lots_tp2 * inst.contract
                    # Update trailing for TP3 to TP1 level
                    if is_buy:
                        pos.trail_sl = max(pos.trail_sl, pos.tp1)
                    else:
                        pos.trail_sl = min(pos.trail_sl, pos.tp1) if pos.trail_sl > 0 else pos.tp1
                    continue

            # ── TP3 — Trailing stop ──
            if pos.tp1_hit and pos.tp2_hit:
                atr_now = row["atr"] if not pd.isna(row["atr"]) else pos.atr_at_entry
                trail_dist = atr_now * TP3_TRAIL

                if is_buy:
                    new_trail = close - trail_dist
                    if new_trail > pos.trail_sl:
                        pos.trail_sl = new_trail
                    if lo <= pos.trail_sl:
                        # Trailing stop hit
                        exit_px = pos.trail_sl
                        pos.pnl_tp3 = (exit_px - pos.entry) * pos.lots_tp3 * inst.contract
                        pnl_g = pos.pnl_tp1 + pos.pnl_tp2 + pos.pnl_tp3
                        tr = TradeResult(inst=pos.inst, d=pos.d, strat=pos.strat,
                                        entry=pos.entry, sl=pos.sl, lots=pos.lots_total,
                                        comm=pos.comm_total, i_open=pos.i_open, i_close=i,
                                        pnl_g=pnl_g, pnl_n=pnl_g - pos.comm_total,
                                        result="WIN", tp1_hit=True, tp2_hit=True,
                                        tp3_exit=exit_px, exit_type="TP3_TRAIL")
                        eq += tr.pnl_n; trades.append(tr)
                        if eq > peak: peak = eq
                        dd = (peak - eq) / peak * 100
                        if dd > mdd: mdd = dd
                        pos = None; continue
                else:
                    new_trail = close + trail_dist
                    if pos.trail_sl == 0 or new_trail < pos.trail_sl:
                        pos.trail_sl = new_trail
                    if hi >= pos.trail_sl:
                        exit_px = pos.trail_sl
                        pos.pnl_tp3 = (pos.entry - exit_px) * pos.lots_tp3 * inst.contract
                        pnl_g = pos.pnl_tp1 + pos.pnl_tp2 + pos.pnl_tp3
                        tr = TradeResult(inst=pos.inst, d=pos.d, strat=pos.strat,
                                        entry=pos.entry, sl=pos.sl, lots=pos.lots_total,
                                        comm=pos.comm_total, i_open=pos.i_open, i_close=i,
                                        pnl_g=pnl_g, pnl_n=pnl_g - pos.comm_total,
                                        result="WIN", tp1_hit=True, tp2_hit=True,
                                        tp3_exit=exit_px, exit_type="TP3_TRAIL")
                        eq += tr.pnl_n; trades.append(tr)
                        if eq > peak: peak = eq
                        dd = (peak - eq) / peak * 100
                        if dd > mdd: mdd = dd
                        pos = None; continue

            continue  # position still open

        # ── New signal ──
        sig, strat = signal(df, i)
        if sig == "HOLD": continue
        atr = row["atr"]
        if pd.isna(atr) or atr <= 0: continue
        entry = close
        sl_d = atr * SL_MULT
        if sl_d <= 0: continue

        if sig == "BUY":
            sl = entry - sl_d
            tp1 = entry + sl_d * TP1_RR
            tp2 = entry + sl_d * TP2_RR
        else:
            sl = entry + sl_d
            tp1 = entry - sl_d * TP1_RR
            tp2 = entry - sl_d * TP2_RR

        # Position sizing at 1.0% risk
        total_lots = max(inst.min_lot, round((eq * RISK_PCT) / (sl_d * inst.contract), 2))
        lots_tp1 = max(inst.min_lot, round(total_lots * TP1_PCT, 2))
        lots_tp2 = max(inst.min_lot, round(total_lots * TP2_PCT, 2))
        lots_tp3 = max(inst.min_lot, round(total_lots * TP3_PCT, 2))
        actual_total = lots_tp1 + lots_tp2 + lots_tp3

        comm = ecn_fee(actual_total, inst, entry)

        # Fee gate
        expected = sl_d * TP1_RR * actual_total * inst.contract
        if expected > 0 and comm / expected > 0.50:
            continue

        pos = Position(
            inst=inst.epic, d=sig, strat=strat, entry=entry,
            sl=round(sl, 5), sl_orig=round(sl, 5),
            tp1=round(tp1, 5), tp2=round(tp2, 5),
            lots_total=actual_total, lots_tp1=lots_tp1,
            lots_tp2=lots_tp2, lots_tp3=lots_tp3,
            comm_total=comm, atr_at_entry=atr, i_open=i,
        )

    # Force close remaining
    if pos is not None:
        last_c = df.iloc[-1]["close"]
        is_buy = pos.d == "BUY"
        # Close whatever is still open
        remaining_lots = pos.lots_total
        pnl_g = pos.pnl_tp1 + pos.pnl_tp2
        if not pos.tp1_hit:
            pnl_g += (last_c - pos.entry if is_buy else pos.entry - last_c) * pos.lots_total * inst.contract
        elif not pos.tp2_hit:
            pnl_g += (last_c - pos.entry if is_buy else pos.entry - last_c) * (pos.lots_tp2 + pos.lots_tp3) * inst.contract
        else:
            pnl_g += (last_c - pos.entry if is_buy else pos.entry - last_c) * pos.lots_tp3 * inst.contract
        tr = TradeResult(inst=pos.inst, d=pos.d, strat=pos.strat, entry=pos.entry,
                        sl=pos.sl, lots=pos.lots_total, comm=pos.comm_total,
                        i_open=pos.i_open, i_close=len(df)-1,
                        pnl_g=pnl_g, pnl_n=pnl_g - pos.comm_total,
                        result="WIN" if pnl_g > pos.comm_total else "LOSS",
                        tp1_hit=pos.tp1_hit, tp2_hit=pos.tp2_hit, exit_type="FORCE_CLOSE")
        eq += tr.pnl_n; trades.append(tr)

    return trades, mdd


# ═══════════════════════════════════════════════════════════════════════════
#  TEAR SHEET
# ═══════════════════════════════════════════════════════════════════════════

def tear_sheet(all_trades, per_inst_mdd):
    W = 100
    print("\n" + "═"*W)
    print("     █████╗ ██████╗ ███████╗██╗  ██╗    ██╗   ██╗██╗  ████████╗██╗███╗   ███╗ █████╗ ████████╗███████╗")
    print("    ██╔══██╗██╔══██╗██╔════╝╚██╗██╔╝    ██║   ██║██║  ╚══██╔══╝██║████╗ ████║██╔══██╗╚══██╔══╝██╔════╝")
    print("    ███████║██████╔╝█████╗   ╚███╔╝     ██║   ██║██║     ██║   ██║██╔████╔██║███████║   ██║   █████╗  ")
    print("    ██╔══██║██╔═══╝ ██╔══╝   ██╔██╗     ██║   ██║██║     ██║   ██║██║╚██╔╝██║██╔══██║   ██║   ██╔══╝  ")
    print("    ██║  ██║██║     ███████╗██╔╝ ██╗    ╚██████╔╝███████╗██║   ██║██║ ╚═╝ ██║██║  ██║   ██║   ███████╗")
    print("    ╚═╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝     ╚═════╝ ╚══════╝╚═╝   ╚═╝╚═╝     ╚═╝╚═╝  ╚═╝   ╚═╝   ╚══════╝")
    print("    Risk 1.0% × Multi-TP (1.5R/2.5R/Trail) × Break-Even × 10 Elite × ECN")
    print("═"*W)

    print(f"\n  📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  🏦 IC Markets ECN | 💰 $25,000 | ⚡ Risk {RISK_PCT*100}%")
    print(f"  🎯 TP1={TP1_RR}R ({TP1_PCT*100:.0f}%) | TP2={TP2_RR}R ({TP2_PCT*100:.0f}%) | TP3=Trail ({TP3_PCT*100:.0f}%)")

    # Per instrument
    inst_data = {}
    for t in all_trades:
        if t.inst not in inst_data:
            inst_data[t.inst] = {"n": 0, "w": 0, "be": 0, "pnl_g": 0, "pnl_n": 0, "comm": 0,
                                 "tp1": 0, "tp2": 0, "trail": 0}
        d = inst_data[t.inst]
        d["n"] += 1
        d["pnl_g"] += t.pnl_g
        d["pnl_n"] += t.pnl_n
        d["comm"] += t.comm
        if t.result == "WIN": d["w"] += 1
        if t.result == "BE": d["be"] += 1
        if t.tp1_hit: d["tp1"] += 1
        if t.tp2_hit: d["tp2"] += 1
        if t.exit_type == "TP3_TRAIL": d["trail"] += 1

    print(f"\n  {'─'*94}")
    print(f"  {'ASSET':<14} {'#':>4} {'W':>4} {'BE':>3} {'L':>3} {'W/R':>6} "
          f"{'PnL BRUT':>11} {'COMM':>9} {'PnL NET':>11} {'TP1%':>5} {'TP2%':>5} {'TRAIL':>5}")
    print(f"  {'─'*94}")

    tot_n = tot_w = tot_be = 0; tot_g = tot_c = tot_net = 0.0

    for epic in ELITE_EPICS:
        d = inst_data.get(epic)
        if not d: continue
        wr = d["w"] / d["n"] * 100 if d["n"] > 0 else 0
        losses = d["n"] - d["w"] - d["be"]
        tp1_r = d["tp1"] / d["n"] * 100 if d["n"] > 0 else 0
        tp2_r = d["tp2"] / d["n"] * 100 if d["n"] > 0 else 0
        ic = "🟢" if d["pnl_n"] > 0 else "🔴"
        name = REG[epic].name
        print(f"  {ic} {name:<12} {d['n']:>3} {d['w']:>4} {d['be']:>3} {losses:>3} {wr:>5.1f}% "
              f"${d['pnl_g']:>9,.2f} ${d['comm']:>7,.2f} ${d['pnl_n']:>9,.2f} "
              f"{tp1_r:>4.0f}% {tp2_r:>4.0f}% {d['trail']:>4}")
        tot_n += d["n"]; tot_w += d["w"]; tot_be += d["be"]
        tot_g += d["pnl_g"]; tot_c += d["comm"]; tot_net += d["pnl_n"]

    print(f"  {'─'*94}")
    tot_losses = tot_n - tot_w - tot_be
    wr = tot_w / tot_n * 100 if tot_n > 0 else 0
    ic = "🟢" if tot_net > 0 else "🔴"
    print(f"  {ic} {'TOTAL':<12} {tot_n:>3} {tot_w:>4} {tot_be:>3} {tot_losses:>3} {wr:>5.1f}% "
          f"${tot_g:>9,.2f} ${tot_c:>7,.2f} ${tot_net:>9,.2f}")
    print(f"  {'═'*94}")

    # Exit type breakdown
    exit_types = {}
    for t in all_trades:
        et = t.exit_type
        if et not in exit_types:
            exit_types[et] = {"n": 0, "pnl": 0}
        exit_types[et]["n"] += 1
        exit_types[et]["pnl"] += t.pnl_n

    print(f"\n  📊 EXIT TYPE BREAKDOWN")
    print(f"  {'─'*60}")
    labels = {"SL_FULL": "❌ Stop Loss (full)", "BE_AFTER_TP1": "🛡️ Break-Even (after TP1)",
              "TP3_TRAIL": "🚀 Trailing Stop (TP3)", "FORCE_CLOSE": "⏹️ Force Close"}
    for et, d in sorted(exit_types.items(), key=lambda x: x[1]["pnl"], reverse=True):
        lbl = labels.get(et, et)
        ic = "🟢" if d["pnl"] > 0 else "🔴"
        print(f"  {ic} {lbl:<30} | {d['n']:>4} trades | PnL ${d['pnl']:>+10,.2f}")

    # Commission analysis
    print(f"\n  💸 COMMISSIONS")
    print(f"  {'─'*60}")
    print(f"  PnL Brut       : ${tot_g:>+12,.2f}")
    print(f"  Commissions    : ${tot_c:>+12,.2f}")
    print(f"  PnL Net        : ${tot_net:>+12,.2f}")
    if tot_g > 0:
        print(f"  Comm / Brut    :   {tot_c/tot_g*100:>8.1f}%")

    # ECN vs Retail
    retail_cost = sum(
        1.5 * REG.get(t.inst, REG["EURUSD"]).pip * REG.get(t.inst, REG["EURUSD"]).contract * t.lots
        for t in all_trades
    )
    print(f"\n  ⚡ ECN vs RETAIL")
    print(f"  {'─'*60}")
    print(f"  Retail (1.5 pip)   : ${retail_cost:>+12,.2f}")
    print(f"  ECN ($7/lot+0.1p)  : ${tot_c:>+12,.2f}")
    print(f"  Savings            : ${retail_cost - tot_c:>+12,.2f}")

    # Daily projections
    candles = 1000; days = candles / 24
    pnl_day = tot_net / days
    trades_day = tot_n / days
    roc = tot_net / 25000 * 100

    print(f"\n  📈 PERFORMANCE ({days:.0f} jours)")
    print(f"  {'─'*60}")
    print(f"  ROC              :   {roc:>+8.2f}%")
    print(f"  PnL / Jour       : ${pnl_day:>+10.2f}")
    print(f"  Trades / Jour    :   {trades_day:>8.1f}")
    print(f"  Avg PnL / Trade  : ${tot_net/tot_n if tot_n else 0:>+10.2f}")
    print(f"  Max Drawdown     :   {max(per_inst_mdd.values()) if per_inst_mdd else 0:>7.1f}%")

    # Comparison vs single-TP
    print(f"\n  🔬 vs SINGLE-TP (0.5% risk)")
    print(f"  {'─'*60}")
    print(f"  Single-TP 0.5%   : $+16,980 (+$404/j)")
    print(f"  Multi-TP 1.0%    : ${tot_net:>+,.0f} (${pnl_day:>+,.0f}/j)")
    improvement = ((tot_net - 16980) / 16980 * 100) if tot_net > 16980 else ((tot_net / 16980 - 1) * 100)
    print(f"  Delta            :   {improvement:>+.1f}%")

    v = "✅ APEX ULTIMATE PROFITABLE" if tot_net > 0 else "⚠️ NEEDS TUNING"
    print(f"\n  🏆 {v}")
    print("═"*W + "\n")


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════
def main():
    print("\n" + "═"*80)
    print("  APEX ULTIMATE — Risk 1.0% × Multi-TP × BE × Elite 10")
    print("═"*80)

    fetcher = Fetcher()
    all_trades = []
    mdd_map = {}

    for epic in ELITE_EPICS:
        inst = REG[epic]
        print(f"  📊 {inst.name:<12} ({epic})", end=" ", flush=True)
        df = fetcher.fetch(epic)
        if df is None or len(df) < 50:
            print("❌"); continue
        print(f"✅ {len(df)} bars", end="")
        trades, mdd = backtest(df, inst)
        all_trades.extend(trades)
        mdd_map[epic] = mdd
        pnl = sum(t.pnl_n for t in trades)
        wins = sum(1 for t in trades if t.result == "WIN")
        bes = sum(1 for t in trades if t.result == "BE")
        ic = "🟢" if pnl > 0 else "🔴"
        print(f" → {ic} {len(trades)} trades ({wins}W/{bes}BE) | ${pnl:+,.0f}")
        time.sleep(0.3)

    if all_trades:
        tear_sheet(all_trades, mdd_map)

if __name__ == "__main__":
    main()
