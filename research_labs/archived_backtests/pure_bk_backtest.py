#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════════╗
║   ██████╗ ██████╗ ███████╗ █████╗ ██╗  ██╗ ██████╗ ██╗   ██╗████████╗          ║
║   ██╔══██╗██╔══██╗██╔════╝██╔══██╗██║ ██╔╝██╔═══██╗██║   ██║╚══██╔══╝          ║
║   ██████╔╝██████╔╝█████╗  ███████║█████╔╝ ██║   ██║██║   ██║   ██║             ║
║   ██╔══██╗██╔══██╗██╔══╝  ██╔══██║██╔═██╗ ██║   ██║██║   ██║   ██║             ║
║   ██████╔╝██║  ██║███████╗██║  ██║██║  ██╗╚██████╔╝╚██████╔╝   ██║             ║
║   ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝    ╚═╝             ║
║              S I N G U L A R I T Y   —   P U R E   B K                          ║
║                                                                                  ║
║    Division 1  "THE FLEET"    — 1H Breakout Only × 22 assets                     ║
║    Division 2  "THE SNIPERS"  — 5m Confirmed BK × BTCUSD + GOLD                 ║
║    Broker: IC Markets True ECN (Raw Spread)                                      ║
║    MR & TF DISABLED — Breakout isolation test                                    ║
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
#  IC MARKETS ECN FEE STRUCTURE
# ═══════════════════════════════════════════════════════════════════════════
ECN_SPREAD_PIPS   = 0.1
ECN_COMM_PER_LOT  = 7.00   # $7/Standard Lot (100k) RT
LOT_STD           = 100_000

# ═══════════════════════════════════════════════════════════════════════════
#  INSTRUMENT REGISTRY
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class Inst:
    epic: str; name: str; pip: float; contract: float; min_lot: float; cat: str

REG = {
    # Forex Majors
    "EURUSD":  Inst("EURUSD", "EUR/USD",   0.0001, 100_000, 0.01, "forex"),
    "GBPUSD":  Inst("GBPUSD", "GBP/USD",   0.0001, 100_000, 0.01, "forex"),
    "USDJPY":  Inst("USDJPY", "USD/JPY",   0.01,   100_000, 0.01, "forex"),
    "GBPJPY":  Inst("GBPJPY", "GBP/JPY",   0.01,   100_000, 0.01, "forex"),
    "EURJPY":  Inst("EURJPY", "EUR/JPY",   0.01,   100_000, 0.01, "forex"),
    "AUDUSD":  Inst("AUDUSD", "AUD/USD",   0.0001, 100_000, 0.01, "forex"),
    "NZDUSD":  Inst("NZDUSD", "NZD/USD",   0.0001, 100_000, 0.01, "forex"),
    "EURGBP":  Inst("EURGBP", "EUR/GBP",   0.0001, 100_000, 0.01, "forex"),
    "EURAUD":  Inst("EURAUD", "EUR/AUD",   0.0001, 100_000, 0.01, "forex"),
    "GBPAUD":  Inst("GBPAUD", "GBP/AUD",   0.0001, 100_000, 0.01, "forex"),
    "AUDNZD":  Inst("AUDNZD", "AUD/NZD",   0.0001, 100_000, 0.01, "forex"),
    "AUDJPY":  Inst("AUDJPY", "AUD/JPY",   0.01,   100_000, 0.01, "forex"),
    "NZDJPY":  Inst("NZDJPY", "NZD/JPY",   0.01,   100_000, 0.01, "forex"),
    "AUDCAD":  Inst("AUDCAD", "AUD/CAD",   0.0001, 100_000, 0.01, "forex"),
    "GBPCAD":  Inst("GBPCAD", "GBP/CAD",   0.0001, 100_000, 0.01, "forex"),
    # Commodities
    "GOLD":    Inst("GOLD",   "Gold",      0.01,   100,     0.01, "commodity"),
    "SILVER":  Inst("SILVER", "Silver",    0.001,  5000,    0.01, "commodity"),
    # Indices
    "US500":   Inst("US500",  "S&P 500",   0.01,   1,       1.0,  "index"),
    "US100":   Inst("US100",  "NASDAQ",    0.01,   1,       1.0,  "index"),
    "US30":    Inst("US30",   "Dow Jones", 0.01,   1,       1.0,  "index"),
    "DE40":    Inst("DE40",   "DAX 40",    0.01,   1,       1.0,  "index"),
    # Crypto
    "BTCUSD":  Inst("BTCUSD", "Bitcoin",   0.01,   1,       0.01, "crypto"),
    "ETHUSD":  Inst("ETHUSD", "Ethereum",  0.01,   1,       0.01, "crypto"),
}

# ═══════════════════════════════════════════════════════════════════════════
#  DIVISION ASSIGNMENTS
# ═══════════════════════════════════════════════════════════════════════════

FLEET_EPICS = [
    "EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "EURJPY",
    "AUDUSD", "NZDUSD", "EURGBP", "EURAUD", "GBPAUD",
    "AUDNZD", "AUDJPY", "NZDJPY", "AUDCAD", "GBPCAD",
    "GOLD", "SILVER",
    "US500", "US100", "US30", "DE40",
    "ETHUSD",
]

SNIPER_EPICS = ["BTCUSD", "GOLD"]

# ═══════════════════════════════════════════════════════════════════════════
#  DIVISION 1 PARAMS — "THE FLEET" (1H Day Trading, proven +96k€ config)
# ═══════════════════════════════════════════════════════════════════════════
FLEET_TF        = "HOUR"
FLEET_TF_LABEL  = "1H"
FLEET_RSI_P     = 14
FLEET_ATR_P     = 14
FLEET_EMA_F     = 9
FLEET_EMA_S     = 21
FLEET_BK_LB     = 6          # 6-candle range breakout
FLEET_SL_MULT   = 1.0        # SL = 1.0 × ATR
FLEET_TP_RR     = 2.5        # TP = 2.5R (large targets for 1H)
FLEET_RSI_LO    = 30
FLEET_RSI_HI    = 70
FLEET_RISK      = 0.005      # 0.5% per trade

# ═══════════════════════════════════════════════════════════════════════════
#  DIVISION 2 PARAMS — "THE SNIPERS" (5m Scalping, confirmed BK only)
# ═══════════════════════════════════════════════════════════════════════════
SNIPER_TF       = "MINUTE_5"
SNIPER_TF_LABEL = "5m"
SNIPER_RSI_P    = 9
SNIPER_ATR_P    = 10
SNIPER_EMA_F    = 9
SNIPER_EMA_S    = 21
SNIPER_BK_LB    = 12         # 12-candle range
SNIPER_SL_MULT  = 1.0        # SL = 1.0 × ATR
SNIPER_TP_RR    = 2.0        # TP = 2.0R
SNIPER_ADX_MIN  = 25         # MANDATORY: no trade in range
SNIPER_BK_CONF  = 0.5        # Breakout = close > range + 0.5×ATR
SNIPER_RISK     = 0.004      # 0.4% per trade (tighter for scalping)

# ═══════════════════════════════════════════════════════════════════════════
#  DATA FETCHER
# ═══════════════════════════════════════════════════════════════════════════
class Fetcher:
    def __init__(self):
        self.url = "https://demo-api-capital.backend-capital.com/api/v1"
        self.cst = self.tok = ""
        self._auth()

    def _auth(self):
        try:
            r = requests.post(f"{self.url}/session", headers={
                "X-CAP-API-KEY": os.getenv("CAPITAL_API_KEY", ""),
                "Content-Type": "application/json",
            }, json={"identifier": os.getenv("CAPITAL_EMAIL", ""),
                     "password": os.getenv("CAPITAL_PASSWORD", "")}, timeout=10)
            if r.status_code == 200:
                self.cst = r.headers.get("CST", "")
                self.tok = r.headers.get("X-SECURITY-TOKEN", "")
                print("  ✅ Capital.com data feed OK")
            else:
                print(f"  ⚠️ Auth {r.status_code}")
        except Exception as e:
            print(f"  ❌ {e}")

    def fetch(self, epic, resolution="HOUR", count=1000):
        try:
            r = requests.get(f"{self.url}/prices/{epic}", headers={
                "X-SECURITY-TOKEN": self.tok, "CST": self.cst,
                "Content-Type": "application/json",
            }, params={"resolution": resolution, "max": min(count, 1000)}, timeout=15)
            if r.status_code != 200:
                return None
            prices = r.json().get("prices", [])
            if not prices:
                return None
            rows = []
            for p in prices:
                o = (float(p["openPrice"]["bid"]) + float(p["openPrice"]["ask"])) / 2
                h = (float(p["highPrice"]["bid"]) + float(p["highPrice"]["ask"])) / 2
                l = (float(p["lowPrice"]["bid"])  + float(p["lowPrice"]["ask"]))  / 2
                c = (float(p["closePrice"]["bid"]) + float(p["closePrice"]["ask"])) / 2
                v = int(p.get("lastTradedVolume", 0))
                rows.append({"ts": p["snapshotTime"], "open": o, "high": h,
                             "low": l, "close": c, "volume": v})
            df = pd.DataFrame(rows)
            df["ts"] = pd.to_datetime(df["ts"])
            df.set_index("ts", inplace=True)
            df.sort_index(inplace=True)
            return df
        except Exception as e:
            return None


# ═══════════════════════════════════════════════════════════════════════════
#  INDICATOR ENGINE
# ═══════════════════════════════════════════════════════════════════════════

def compute(df, rsi_p=14, atr_p=14, ema_f=9, ema_s=21, bk_lb=6):
    h, l, c, v = df["high"], df["low"], df["close"], df["volume"]
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(atr_p).mean()

    # RSI
    d = c.diff()
    g = d.where(d > 0, 0).rolling(rsi_p).mean()
    lo = (-d.where(d < 0, 0)).rolling(rsi_p).mean()
    rs = g / lo.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # EMA
    df["ema_f"] = c.ewm(span=ema_f, adjust=False).mean()
    df["ema_s"] = c.ewm(span=ema_s, adjust=False).mean()

    # MACD
    df["macd"] = df["ema_f"] - df["ema_s"]
    df["macd_sig"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_h"] = df["macd"] - df["macd_sig"]

    # Breakout range (shifted to avoid lookahead)
    df["bk_hi"] = h.rolling(bk_lb).max().shift(1)
    df["bk_lo"] = l.rolling(bk_lb).min().shift(1)

    # Volume ratio
    df["vol_r"] = v / v.rolling(20).mean().replace(0, 1)

    # ADX (14-period)
    plus_dm = h.diff()
    minus_dm = -l.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
    atr14 = tr.rolling(14).mean()
    plus_di = 100 * (plus_dm.rolling(14).mean() / atr14.replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(14).mean() / atr14.replace(0, np.nan))
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
    df["adx"] = dx.rolling(14).mean()

    # Bollinger
    df["bb_m"] = c.rolling(20).mean()
    df["bb_u"] = df["bb_m"] + 2 * c.rolling(20).std()
    df["bb_d"] = df["bb_m"] - 2 * c.rolling(20).std()

    return df


# ═══════════════════════════════════════════════════════════════════════════
#  DIVISION 1 — THE FLEET (1H Day Trading)
# ═══════════════════════════════════════════════════════════════════════════

def fleet_signal(df, i):
    """PURE BREAKOUT — MR and TF disabled for isolation test."""
    if i < 30:
        return "HOLD", ""
    row  = df.iloc[i]
    c = row["close"]; atr = row["atr"]
    if pd.isna(atr) or atr <= 0:
        return "HOLD", ""

    ef = row["ema_f"]; es = row["ema_s"]
    mh = row["macd_h"]; bh = row["bk_hi"]; bl = row["bk_lo"]
    vr = row.get("vol_r", 1.0)

    # ── BREAKOUT ONLY ── range break + EMA alignment + volume
    if c > bh and ef > es and mh > 0 and vr > 0.8:
        return "BUY", "BK"
    if c < bl and ef < es and mh < 0 and vr > 0.8:
        return "SELL", "BK"

    # MR — DISABLED
    # TF — DISABLED

    return "HOLD", ""


# ═══════════════════════════════════════════════════════════════════════════
#  DIVISION 2 — THE SNIPERS (5m Scalping, ADX + Confirmed BK)
# ═══════════════════════════════════════════════════════════════════════════

def sniper_signal(df, i):
    """Apex Sniper signal — ADX filter + confirmed breakout only."""
    if i < 30:
        return "HOLD", ""
    row = df.iloc[i]
    c = row["close"]; atr = row["atr"]; adx = row.get("adx", 0)

    if pd.isna(atr) or atr <= 0 or pd.isna(adx):
        return "HOLD", ""

    # ── MANDATORY: ADX > 25 — no trade in ranging market ──
    if adx < SNIPER_ADX_MIN:
        return "HOLD", ""

    ef = row["ema_f"]; es = row["ema_s"]
    mh = row["macd_h"]
    bh = row["bk_hi"]; bl = row["bk_lo"]
    vr = row.get("vol_r", 1.0)

    # ── CONFIRMED BREAKOUT ── close > range + 0.5×ATR (no fakeouts)
    confirm_buffer = atr * SNIPER_BK_CONF

    if c > (bh + confirm_buffer) and ef > es and mh > 0 and vr > 0.9:
        return "BUY", "SNIPER"

    if c < (bl - confirm_buffer) and ef < es and mh < 0 and vr > 0.9:
        return "SELL", "SNIPER"

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
    spread = ECN_SPREAD_PIPS * inst.pip * inst.contract * lots
    return comm + spread


# ═══════════════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE (unified)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    inst: str; div: str; d: str; strat: str; entry: float; sl: float; tp: float
    lots: float; comm: float; i_open: int
    i_close: int = 0; exit_px: float = 0; pnl_g: float = 0
    pnl_n: float = 0; result: str = ""

@dataclass
class DivResult:
    name: str; tf: str
    n: int = 0; wins: int = 0; wr: float = 0
    pnl_g: float = 0; comm: float = 0; pnl_n: float = 0
    pf: float = 0; mdd: float = 0; sharpe: float = 0
    trades: List[Trade] = field(default_factory=list)
    per_inst: dict = field(default_factory=dict)


def run_backtest(df, inst: Inst, division: str, sig_fn, sl_mult, tp_rr,
                 risk, rsi_p, atr_p, ema_f, ema_s, bk_lb, tf_label,
                 capital=25000.0) -> List[Trade]:
    """Run backtest for a single instrument, return trades."""
    df = compute(df, rsi_p=rsi_p, atr_p=atr_p, ema_f=ema_f, ema_s=ema_s, bk_lb=bk_lb)
    trades = []
    eq = capital; pos = None

    for i in range(30, len(df)):
        row = df.iloc[i]

        # Manage position
        if pos is not None:
            if pos.d == "BUY":
                if row["low"] <= pos.sl:
                    pos.exit_px = pos.sl
                    pos.pnl_g = (pos.sl - pos.entry) * pos.lots * inst.contract
                    pos.pnl_n = pos.pnl_g - pos.comm; pos.result = "LOSS"; pos.i_close = i
                elif row["high"] >= pos.tp:
                    pos.exit_px = pos.tp
                    pos.pnl_g = (pos.tp - pos.entry) * pos.lots * inst.contract
                    pos.pnl_n = pos.pnl_g - pos.comm; pos.result = "WIN"; pos.i_close = i
                else:
                    continue
            else:
                if row["high"] >= pos.sl:
                    pos.exit_px = pos.sl
                    pos.pnl_g = (pos.entry - pos.sl) * pos.lots * inst.contract
                    pos.pnl_n = pos.pnl_g - pos.comm; pos.result = "LOSS"; pos.i_close = i
                elif row["low"] <= pos.tp:
                    pos.exit_px = pos.tp
                    pos.pnl_g = (pos.entry - pos.tp) * pos.lots * inst.contract
                    pos.pnl_n = pos.pnl_g - pos.comm; pos.result = "WIN"; pos.i_close = i
                else:
                    continue
            eq += pos.pnl_n; trades.append(pos); pos = None; continue

        # New signal
        sig, strat = sig_fn(df, i)
        if sig == "HOLD":
            continue

        atr = row["atr"]
        if pd.isna(atr) or atr <= 0:
            continue

        entry = row["close"]
        sl_d = atr * sl_mult
        if sl_d <= 0:
            continue

        if sig == "BUY":
            sl = entry - sl_d; tp = entry + sl_d * tp_rr
        else:
            sl = entry + sl_d; tp = entry - sl_d * tp_rr

        lots = max(inst.min_lot, round((eq * risk) / (sl_d * inst.contract), 2))
        comm = ecn_fee(lots, inst, entry)

        # Fee gate: skip if fees > 50% of expected reward
        expected_reward = sl_d * tp_rr * lots * inst.contract
        if expected_reward > 0 and comm / expected_reward > 0.50:
            continue

        pos = Trade(inst=inst.epic, div=division, d=sig, strat=strat, entry=entry,
                    sl=round(sl, 5), tp=round(tp, 5), lots=lots, comm=comm, i_open=i)

    # Force close
    if pos is not None:
        last_c = df.iloc[-1]["close"]
        pos.exit_px = last_c
        pos.pnl_g = ((last_c - pos.entry) if pos.d == "BUY" else (pos.entry - last_c)) * pos.lots * inst.contract
        pos.pnl_n = pos.pnl_g - pos.comm
        pos.result = "WIN" if pos.pnl_n > 0 else "LOSS"
        pos.i_close = len(df) - 1; eq += pos.pnl_n; trades.append(pos)

    return trades


# ═══════════════════════════════════════════════════════════════════════════
#  TEAR SHEET
# ═══════════════════════════════════════════════════════════════════════════

def build_div_result(name, tf, trades):
    r = DivResult(name=name, tf=tf, trades=trades)
    r.n = len(trades)
    if r.n == 0:
        return r
    r.wins = sum(1 for t in trades if t.result == "WIN")
    r.wr = r.wins / r.n * 100
    r.pnl_g = sum(t.pnl_g for t in trades)
    r.comm = sum(t.comm for t in trades)
    r.pnl_n = sum(t.pnl_n for t in trades)
    w = sum(t.pnl_n for t in trades if t.result == "WIN")
    l = abs(sum(t.pnl_n for t in trades if t.result == "LOSS"))
    r.pf = w / max(l, 0.01)
    # Max DD
    eq = 25000; peak = eq; mdd = 0
    for t in trades:
        eq += t.pnl_n
        if eq > peak: peak = eq
        dd = (peak - eq) / peak * 100
        if dd > mdd: mdd = dd
    r.mdd = mdd
    # Per-instrument
    for t in trades:
        if t.inst not in r.per_inst:
            r.per_inst[t.inst] = {"n": 0, "wins": 0, "pnl_n": 0, "comm": 0}
        r.per_inst[t.inst]["n"] += 1
        r.per_inst[t.inst]["pnl_n"] += t.pnl_n
        r.per_inst[t.inst]["comm"] += t.comm
        if t.result == "WIN":
            r.per_inst[t.inst]["wins"] += 1
    return r


def print_tear_sheet(fleet: DivResult, snipers: DivResult):
    W = 100
    all_trades = fleet.trades + snipers.trades
    tot_n = fleet.n + snipers.n
    tot_w = fleet.wins + snipers.wins
    tot_g = fleet.pnl_g + snipers.pnl_g
    tot_c = fleet.comm + snipers.comm
    tot_net = fleet.pnl_n + snipers.pnl_n

    print("\n" + "═" * W)
    print("   ██╗  ██╗██╗   ██╗██████╗ ██████╗ ██╗██████╗      █████╗ ██████╗ ███████╗██╗  ██╗")
    print("   ██║  ██║╚██╗ ██╔╝██╔══██╗██╔══██╗██║██╔══██╗    ██╔══██╗██╔══██╗██╔════╝╚██╗██╔╝")
    print("   ███████║ ╚████╔╝ ██████╔╝██████╔╝██║██║  ██║    ███████║██████╔╝█████╗   ╚███╔╝ ")
    print("   ██╔══██║  ╚██╔╝  ██╔══██╗██╔══██╗██║██║  ██║    ██╔══██║██╔═══╝ ██╔══╝   ██╔██╗ ")
    print("   ██║  ██║   ██║   ██████╔╝██║  ██║██║██████╔╝    ██║  ██║██║     ███████╗██╔╝ ██╗")
    print("   ╚═╝  ╚═╝   ╚═╝   ╚═════╝ ╚═╝  ╚═╝╚═╝╚═════╝    ╚═╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝")
    print("   UNIFIED HEDGE FUND TEAR SHEET — IC Markets True ECN")
    print("═" * W)

    print(f"\n  📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  🏦 IC Markets ECN | 💰 $25,000 | 📊 {ECN_SPREAD_PIPS} pip | 💸 ${ECN_COMM_PER_LOT}/lot RT")

    # ── DIVISION SUMMARY ──
    print(f"\n  {'─' * 94}")
    print(f"  {'DIVISION':<28} {'TF':<5} {'TRADES':>6} {'WINS':>5} {'W/R':>6} "
          f"{'PnL BRUT':>11} {'COMM':>10} {'PnL NET':>11} {'PF':>5} {'MDD':>6}")
    print(f"  {'─' * 94}")

    for div in [fleet, snipers]:
        ic = "🟢" if div.pnl_n > 0 else "🔴"
        print(f"  {ic} {div.name:<26} {div.tf:<5} {div.n:>5} {div.wins:>5} {div.wr:>5.1f}% "
              f"${div.pnl_g:>9,.2f} ${div.comm:>8,.2f} ${div.pnl_n:>9,.2f} "
              f"{div.pf:>4.2f} {div.mdd:>4.1f}%")

    print(f"  {'─' * 94}")
    wr = (tot_w / tot_n * 100) if tot_n > 0 else 0
    ic = "🟢" if tot_net > 0 else "🔴"
    print(f"  {ic} {'HEDGE FUND TOTAL':<26} {'':5} {tot_n:>5} {tot_w:>5} {wr:>5.1f}% "
          f"${tot_g:>9,.2f} ${tot_c:>8,.2f} ${tot_net:>9,.2f}")
    print(f"  {'═' * 94}")

    # ── DIVISION 1 BREAKDOWN ──
    print(f"\n  🚢 DIVISION 1 — THE FLEET (1H Day Trading)")
    print(f"  {'─' * 80}")
    sorted_fleet = sorted(fleet.per_inst.items(), key=lambda x: x[1]["pnl_n"], reverse=True)
    for epic, d in sorted_fleet:
        wr_i = d["wins"] / d["n"] * 100 if d["n"] > 0 else 0
        ic = "🟢" if d["pnl_n"] > 0 else "🔴"
        name = REG.get(epic, Inst(epic, epic, 0, 0, 0, "")).name
        print(f"    {ic} {name:<12} ({epic:<8}) | {d['n']:>3} trades | WR {wr_i:>5.1f}% "
              f"| PnL ${d['pnl_n']:>+9,.2f} | Comm ${d['comm']:>7,.2f}")

    # Strategy breakdown Fleet
    strats_f = {}
    for t in fleet.trades:
        if t.strat not in strats_f:
            strats_f[t.strat] = {"n": 0, "w": 0, "pnl": 0, "comm": 0}
        strats_f[t.strat]["n"] += 1
        strats_f[t.strat]["pnl"] += t.pnl_n
        strats_f[t.strat]["comm"] += t.comm
        if t.result == "WIN": strats_f[t.strat]["w"] += 1

    print(f"\n  📊 Fleet Strategy Mix:")
    for s, d in sorted(strats_f.items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr_s = d["w"] / d["n"] * 100 if d["n"] > 0 else 0
        ic = "🟢" if d["pnl"] > 0 else "🔴"
        label = {"BK": "Breakout", "MR": "Mean Reversion", "TF": "Trend Follow"}.get(s, s)
        print(f"    {ic} {label:<18} | {d['n']:>4} trades | WR {wr_s:>5.1f}% | PnL ${d['pnl']:>+10,.2f}")

    # ── DIVISION 2 BREAKDOWN ──
    print(f"\n  🎯 DIVISION 2 — THE SNIPERS (5m Scalping)")
    print(f"  {'─' * 80}")
    for epic, d in sorted(snipers.per_inst.items(), key=lambda x: x[1]["pnl_n"], reverse=True):
        wr_i = d["wins"] / d["n"] * 100 if d["n"] > 0 else 0
        ic = "🟢" if d["pnl_n"] > 0 else "🔴"
        name = REG.get(epic, Inst(epic, epic, 0, 0, 0, "")).name
        print(f"    {ic} {name:<12} ({epic:<8}) | {d['n']:>3} trades | WR {wr_i:>5.1f}% "
              f"| PnL ${d['pnl_n']:>+9,.2f} | Comm ${d['comm']:>7,.2f}")

    # ── FEE ANALYSIS ──
    print(f"\n  💸 COMMISSION ANALYSIS")
    print(f"  {'─' * 60}")
    print(f"  PnL Brut (strategy alpha)  : ${tot_g:>+12,.2f}")
    print(f"  IC Markets Commissions     : ${tot_c:>+12,.2f}")
    print(f"  PnL Net                    : ${tot_net:>+12,.2f}")
    if tot_g > 0:
        print(f"  Comm / PnL Brut            :   {tot_c / tot_g * 100:>8.1f}%")

    # ── ECN vs RETAIL ──
    print(f"\n  ⚡ ECN vs RETAIL")
    print(f"  {'─' * 60}")
    retail_cost = sum(
        1.5 * REG.get(t.inst, REG["EURUSD"]).pip * REG.get(t.inst, REG["EURUSD"]).contract * t.lots
        for t in all_trades
    )
    print(f"  Retail cost (1.5 pip)      : ${retail_cost:>+12,.2f}")
    print(f"  ECN cost ($7/lot + 0.1pip) : ${tot_c:>+12,.2f}")
    print(f"  SAVINGS                    : ${retail_cost - tot_c:>+12,.2f}")
    print(f"  PnL if Retail              : ${tot_g - retail_cost:>+12,.2f}")
    print(f"  PnL on ECN                 : ${tot_net:>+12,.2f}")

    # ── ROC ──
    roc = tot_net / 25000 * 100
    print(f"\n  📈 RETURNS")
    print(f"  {'─' * 60}")
    print(f"  Return on Capital          :   {roc:>+8.2f}%")
    print(f"  Avg PnL / Trade            : ${tot_net / tot_n if tot_n else 0:>+10.2f}")
    print(f"  Avg Commission / Trade     : ${tot_c / tot_n if tot_n else 0:>+10.2f}")

    # Best
    profitable = [(e, d) for div in [fleet, snipers] for e, d in div.per_inst.items() if d["pnl_n"] > 0]
    if profitable:
        best = max(profitable, key=lambda x: x[1]["pnl_n"])
        print(f"\n  🏆 BEST PERFORMER: {best[0]} → ${best[1]['pnl_n']:+,.2f}")

    v_ecn = "✅ HEDGE FUND PROFITABLE" if tot_net > 0 else "⚠️ TUNING REQUIRED"
    v_ret = "❌ RETAIL DESTROYED" if (tot_g - retail_cost) < 0 else "⚠️ RETAIL MARGINAL"
    print(f"\n  🏆 VERDICT ECN    : {v_ecn}")
    print(f"  💀 VERDICT RETAIL : {v_ret}")
    print("═" * W + "\n")


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "═" * 80)
    print("  THE HYBRID APEX ENGINE — Initializing...")
    print("═" * 80)

    fetcher = Fetcher()
    fleet_trades = []
    sniper_trades = []

    # ── DIVISION 1: THE FLEET (1H) ──
    print(f"\n🚢 DIVISION 1 — THE FLEET (1H Day Trading) × {len(FLEET_EPICS)} assets")
    print("─" * 60)
    for epic in FLEET_EPICS:
        inst = REG.get(epic)
        if not inst:
            continue
        print(f"  📊 {inst.name:<12} ({epic})", end=" ", flush=True)
        df = fetcher.fetch(epic, resolution=FLEET_TF, count=1000)
        if df is None or len(df) < 50:
            print(f"❌ ({len(df) if df is not None else 0})")
            continue
        print(f"✅ {len(df)} bars", end="")

        t = run_backtest(df, inst, "FLEET", fleet_signal,
                         FLEET_SL_MULT, FLEET_TP_RR, FLEET_RISK,
                         FLEET_RSI_P, FLEET_ATR_P, FLEET_EMA_F, FLEET_EMA_S,
                         FLEET_BK_LB, FLEET_TF_LABEL)
        fleet_trades.extend(t)
        pnl = sum(tr.pnl_n for tr in t)
        ic = "🟢" if pnl > 0 else "🔴"
        print(f" → {ic} {len(t)} trades | ${pnl:+,.0f}")
        time.sleep(0.3)

    # ── DIVISION 2: THE SNIPERS (5m) ──
    print(f"\n🎯 DIVISION 2 — THE SNIPERS (5m Scalping) × {len(SNIPER_EPICS)} assets")
    print("─" * 60)
    for epic in SNIPER_EPICS:
        inst = REG.get(epic)
        if not inst:
            continue
        print(f"  📊 {inst.name:<12} ({epic})", end=" ", flush=True)
        df = fetcher.fetch(epic, resolution=SNIPER_TF, count=1000)
        if df is None or len(df) < 50:
            print(f"❌ ({len(df) if df is not None else 0})")
            continue
        print(f"✅ {len(df)} bars", end="")

        t = run_backtest(df, inst, "SNIPER", sniper_signal,
                         SNIPER_SL_MULT, SNIPER_TP_RR, SNIPER_RISK,
                         SNIPER_RSI_P, SNIPER_ATR_P, SNIPER_EMA_F, SNIPER_EMA_S,
                         SNIPER_BK_LB, SNIPER_TF_LABEL)
        sniper_trades.extend(t)
        pnl = sum(tr.pnl_n for tr in t)
        ic = "🟢" if pnl > 0 else "🔴"
        print(f" → {ic} {len(t)} trades | ${pnl:+,.0f}")
        time.sleep(0.3)

    # ── TEAR SHEET ──
    fleet_r = build_div_result("🚢 THE FLEET", FLEET_TF_LABEL, fleet_trades)
    sniper_r = build_div_result("🎯 THE SNIPERS", SNIPER_TF_LABEL, sniper_trades)
    print_tear_sheet(fleet_r, sniper_r)


if __name__ == "__main__":
    main()
