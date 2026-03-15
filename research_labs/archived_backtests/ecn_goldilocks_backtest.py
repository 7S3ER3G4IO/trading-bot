#!/usr/bin/env python3
"""
╔════════════════════════════════════════════════════════════════════════════════╗
║  THE GOLDILOCKS PROTOCOL — Mid-Frequency ECN Sweet Spot Backtest             ║
║  IC Markets Raw Spread × 5m/15m × 10 Assets × Institutional Precision       ║
╚════════════════════════════════════════════════════════════════════════════════╝

Proves that mid-frequency (5m/15m) on IC Markets ECN is the absolute key to
automated profitability — enough ATR to crush commissions, few enough trades
to avoid fee bleed.

Target: ~500 trades total across all instruments.
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
#  IC MARKETS RAW SPREAD — FEE STRUCTURE
# ═══════════════════════════════════════════════════════════════════════════

ECN_SPREAD_PIPS   = 0.1
ECN_COMMISSION_RT = 7.00       # $7/Standard Lot (100k) round-trip
LOT_STANDARD      = 100_000

# ═══════════════════════════════════════════════════════════════════════════
#  GOLDILOCKS STRATEGY PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════

RSI_OVERSOLD  = 35             # MR long trigger  (was 45 in Vindicator)
RSI_OVERBOUGHT = 65            # MR short trigger  (was 55)
BK_LOOKBACK   = 15             # Breakout range lookback (was 8)
TP_RR         = 2.0            # Risk:Reward for TP (was 1.5)
RSI_PERIOD    = 9              # RSI lookback
ATR_PERIOD    = 14             # ATR lookback (slower = smoother)
EMA_FAST      = 9
EMA_SLOW      = 26             # 26 for mid-frequency (MACD-style)

# ═══════════════════════════════════════════════════════════════════════════
#  INSTRUMENT CONFIG — Top 10 Liquidity
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Instrument:
    epic: str
    name: str
    pip: float
    contract: float    # contract multiplier (100k forex, 100 gold, 1 crypto/index)
    min_lot: float
    sl_mult: float     # SL = ATR × this
    cat: str

INSTRUMENTS = {
    # ── Forex Majors ──
    "EURUSD": Instrument("EURUSD", "EUR/USD",  0.0001, 100_000, 0.01, 0.7, "forex"),
    "GBPUSD": Instrument("GBPUSD", "GBP/USD",  0.0001, 100_000, 0.01, 0.7, "forex"),
    "USDJPY": Instrument("USDJPY", "USD/JPY",  0.01,   100_000, 0.01, 0.7, "forex"),
    "GBPJPY": Instrument("GBPJPY", "GBP/JPY",  0.01,   100_000, 0.01, 0.8, "forex"),
    "EURJPY": Instrument("EURJPY", "EUR/JPY",  0.01,   100_000, 0.01, 0.7, "forex"),
    # ── Commodity ──
    "GOLD":   Instrument("GOLD",   "Gold",     0.01,   100,     0.01, 0.6, "commodity"),
    # ── Index ──
    "US100":  Instrument("US100",  "NASDAQ",   0.01,   1,       1.00, 0.8, "index"),
    # ── Crypto ──
    "BTCUSD": Instrument("BTCUSD", "Bitcoin",  0.01,   1,       0.01, 0.7, "crypto"),
    "ETHUSD": Instrument("ETHUSD", "Ethereum", 0.01,   1,       0.01, 0.7, "crypto"),
    "XRPUSD": Instrument("XRPUSD", "XRP",     0.0001, 1,       1.00, 0.7, "crypto"),
}

# ═══════════════════════════════════════════════════════════════════════════
#  DATA FETCHER (Capital.com — data only)
# ═══════════════════════════════════════════════════════════════════════════

class DataFetcher:
    def __init__(self):
        self.url = "https://demo-api-capital.backend-capital.com/api/v1"
        self.cst = self.tok = ""
        self._auth()

    def _auth(self):
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
            else:
                print(f"  ⚠️ Auth failed: {r.status_code}")
        except Exception as e:
            print(f"  ❌ Auth error: {e}")

    def fetch(self, epic: str, resolution: str = "MINUTE_5", count: int = 1500):
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
                rows.append({"ts": p["snapshotTime"], "open":o, "high":h, "low":l, "close":c, "volume":v})
            df = pd.DataFrame(rows)
            df["ts"] = pd.to_datetime(df["ts"])
            df.set_index("ts", inplace=True)
            df.sort_index(inplace=True)
            return df
        except Exception as e:
            print(f"    ❌ {epic}: {e}")
            return None

# ═══════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ═══════════════════════════════════════════════════════════════════════════

def indicators(df):
    h, l, c, v = df["high"], df["low"], df["close"], df["volume"]
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(ATR_PERIOD).mean()

    delta = c.diff()
    gain = delta.where(delta>0, 0).rolling(RSI_PERIOD).mean()
    loss = (-delta.where(delta<0, 0)).rolling(RSI_PERIOD).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    df["ema_f"] = c.ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_s"] = c.ewm(span=EMA_SLOW, adjust=False).mean()

    # MACD histogram for momentum confirmation
    df["macd"] = df["ema_f"] - df["ema_s"]
    df["macd_sig"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_sig"]

    # Breakout range (15 candles prior — shifted to avoid lookahead)
    df["bk_high"] = h.rolling(BK_LOOKBACK).max().shift(1)
    df["bk_low"]  = l.rolling(BK_LOOKBACK).min().shift(1)

    # Volume filter
    df["vol_ma"] = v.rolling(20).mean()
    df["vol_r"]  = v / df["vol_ma"].replace(0, 1)

    # Bollinger Bands for MR confirmation
    df["bb_mid"]  = c.rolling(20).mean()
    df["bb_std"]  = c.rolling(20).std()
    df["bb_up"]   = df["bb_mid"] + 2 * df["bb_std"]
    df["bb_dn"]   = df["bb_mid"] - 2 * df["bb_std"]

    return df

# ═══════════════════════════════════════════════════════════════════════════
#  SIGNAL GENERATION — Goldilocks Precision
# ═══════════════════════════════════════════════════════════════════════════

def signal(df, i):
    if i < 30:
        return "HOLD", ""
    row  = df.iloc[i]
    prev = df.iloc[i-1]
    c  = row["close"]
    rsi = row["rsi"]
    atr = row["atr"]

    if pd.isna(atr) or atr <= 0 or pd.isna(rsi):
        return "HOLD", ""

    ema_f = row["ema_f"]
    ema_s = row["ema_s"]
    macd_h = row["macd_hist"]
    bk_h  = row["bk_high"]
    bk_l  = row["bk_low"]
    vol_r = row.get("vol_r", 1.0)
    bb_dn = row.get("bb_dn", c)
    bb_up = row.get("bb_up", c)

    # ── BREAKOUT (BK) ── 15-candle range break + MACD momentum + volume ──
    if c > bk_h and ema_f > ema_s and macd_h > 0 and vol_r > 0.9:
        return "BUY", "BK"
    if c < bk_l and ema_f < ema_s and macd_h < 0 and vol_r > 0.9:
        return "SELL", "BK"

    # ── MEAN REVERSION (MR) ── RSI 35/65 + Bollinger + EMA direction ──
    if rsi < RSI_OVERSOLD and c <= bb_dn and ema_f > ema_s * 0.999 and c > prev["close"]:
        return "BUY", "MR"
    if rsi > RSI_OVERBOUGHT and c >= bb_up and ema_f < ema_s * 1.001 and c < prev["close"]:
        return "SELL", "MR"

    # ── TREND CONTINUATION (TC) ── Strong MACD + EMA alignment + volume ──
    if ema_f > ema_s and macd_h > 0 and prev["macd_hist"] <= 0 and vol_r > 1.2:
        return "BUY", "TC"
    if ema_f < ema_s and macd_h < 0 and prev["macd_hist"] >= 0 and vol_r > 1.2:
        return "SELL", "TC"

    return "HOLD", ""

# ═══════════════════════════════════════════════════════════════════════════
#  FEE CALCULATOR
# ═══════════════════════════════════════════════════════════════════════════

def ecn_fees(lots, inst, price):
    if inst.cat == "forex":
        comm = ECN_COMMISSION_RT * lots
    else:
        notional = lots * price * inst.contract
        comm = (notional / LOT_STANDARD) * ECN_COMMISSION_RT
    spread = ECN_SPREAD_PIPS * inst.pip * inst.contract * lots
    return comm + spread

# ═══════════════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    inst: str; d: str; strat: str; entry: float; sl: float; tp: float
    lots: float; comm: float; i_open: int
    i_close: int = 0; exit_px: float = 0; pnl_g: float = 0
    pnl_n: float = 0; result: str = ""

@dataclass
class Result:
    inst: str; tf: str
    n: int = 0; wins: int = 0; wr: float = 0
    pnl_g: float = 0; comm: float = 0; pnl_n: float = 0
    pf: float = 0; mdd: float = 0; sharpe: float = 0
    trades: List[Trade] = field(default_factory=list)


def backtest(df, inst: Instrument, tf: str, capital=25000.0, risk=0.005):
    res = Result(inst=inst.epic, tf=tf)
    df = indicators(df)
    eq = capital; peak = eq; dd = 0; pos = None; rets = []

    for i in range(30, len(df)):
        row = df.iloc[i]

        # ── Manage open position ──
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

            eq += pos.pnl_n; rets.append(pos.pnl_n / capital)
            res.trades.append(pos)
            if eq > peak: peak = eq
            d = (peak - eq) / peak * 100
            if d > dd: dd = d
            pos = None; continue

        # ── New signal ──
        sig, strat = signal(df, i)
        if sig == "HOLD":
            continue

        atr = row["atr"]
        if pd.isna(atr) or atr <= 0:
            continue

        entry = row["close"]
        sl_d = atr * inst.sl_mult

        if sig == "BUY":
            sl = entry - sl_d
            tp = entry + sl_d * TP_RR
        else:
            sl = entry + sl_d
            tp = entry - sl_d * TP_RR

        if sl_d <= 0:
            continue

        # Position sizing
        lots = max(inst.min_lot, round((eq * risk) / (sl_d * inst.contract), 2))
        comm = ecn_fees(lots, inst, entry)

        # Minimum R:R check after fees
        gross_reward = sl_d * TP_RR * lots * inst.contract
        if gross_reward < comm * 1.5:
            continue  # Skip if fees eat > 67% of reward

        pos = Trade(inst=inst.epic, d=sig, strat=strat, entry=entry,
                    sl=round(sl,5), tp=round(tp,5), lots=lots, comm=comm, i_open=i)

    # Force close
    if pos is not None:
        last = df.iloc[-1]["close"]
        pos.exit_px = last
        pos.pnl_g = ((last - pos.entry) if pos.d == "BUY" else (pos.entry - last)) * pos.lots * inst.contract
        pos.pnl_n = pos.pnl_g - pos.comm
        pos.result = "WIN" if pos.pnl_n > 0 else "LOSS"
        pos.i_close = len(df)-1; eq += pos.pnl_n; res.trades.append(pos)

    # Stats
    res.n = len(res.trades)
    if res.n > 0:
        res.wins = sum(1 for t in res.trades if t.result == "WIN")
        res.wr   = res.wins / res.n * 100
        res.pnl_g = sum(t.pnl_g for t in res.trades)
        res.comm  = sum(t.comm for t in res.trades)
        res.pnl_n = sum(t.pnl_n for t in res.trades)
        w = [t.pnl_n for t in res.trades if t.result == "WIN"]
        l = [abs(t.pnl_n) for t in res.trades if t.result == "LOSS"]
        res.pf = sum(w) / max(sum(l), 0.01)
        res.mdd = dd
        if rets:
            arr = np.array(rets)
            if arr.std() > 0:
                bpy = 252 * 24 * 12 if "5m" in tf else 252 * 24 * 4
                res.sharpe = (arr.mean() / arr.std()) * np.sqrt(bpy)
    return res

# ═══════════════════════════════════════════════════════════════════════════
#  TEAR SHEET
# ═══════════════════════════════════════════════════════════════════════════

def tear_sheet(results):
    W = 96
    print("\n" + "═"*W)
    print("   ██████╗  ██████╗ ██╗     ██████╗ ██╗██╗      ██████╗  ██████╗██╗  ██╗███████╗")
    print("  ██╔════╝ ██╔═══██╗██║     ██╔══██╗██║██║     ██╔═══██╗██╔════╝██║ ██╔╝██╔════╝")
    print("  ██║  ███╗██║   ██║██║     ██║  ██║██║██║     ██║   ██║██║     █████╔╝ ███████╗")
    print("  ██║   ██║██║   ██║██║     ██║  ██║██║██║     ██║   ██║██║     ██╔═██╗ ╚════██║")
    print("  ╚██████╔╝╚██████╔╝███████╗██████╔╝██║███████╗╚██████╔╝╚██████╗██║  ██╗███████║")
    print("   ╚═════╝  ╚═════╝ ╚══════╝╚═════╝ ╚═╝╚══════╝ ╚═════╝  ╚═════╝╚═╝  ╚═╝╚══════╝")
    print("   THE GOLDILOCKS PROTOCOL — Mid-Frequency ECN Sweet Spot × IC Markets Raw Spread")
    print("═"*W)

    print(f"\n  📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  🏦 IC Markets True ECN | 💰 $25,000 | 📊 {ECN_SPREAD_PIPS} pip | 💸 ${ECN_COMMISSION_RT}/lot RT")
    print(f"  ⚡ Risk 0.5% | RSI {RSI_OVERSOLD}/{RSI_OVERBOUGHT} | BK {BK_LOOKBACK}-candle | TP {TP_RR}R")

    hdr = f"  {'INSTRUMENT':<12} {'TF':<5} {'#':>4} {'W':>4} {'W/R':>6} {'PnL BRUT':>11} {'COMM':>10} {'PnL NET':>11} {'PF':>5} {'MDD':>6} {'SR':>6}"
    print("\n" + "─"*W)
    print(hdr)
    print("─"*W)

    tot_n = tot_w = 0; tot_g = tot_c = tot_net = 0.0

    for r in sorted(results, key=lambda x: x.pnl_n, reverse=True):
        ic = "🟢" if r.pnl_n > 0 else ("🔴" if r.pnl_n < 0 else "⚪")
        print(f"  {ic} {r.inst:<10} {r.tf:<5} {r.n:>3} {r.wins:>4} {r.wr:>5.1f}% "
              f"${r.pnl_g:>9,.2f} ${r.comm:>8,.2f} ${r.pnl_n:>9,.2f} "
              f"{r.pf:>4.2f} {r.mdd:>4.1f}% {r.sharpe:>5.1f}")
        tot_n += r.n; tot_w += r.wins; tot_g += r.pnl_g; tot_c += r.comm; tot_net += r.pnl_n

    print("─"*W)
    wr = (tot_w/tot_n*100) if tot_n > 0 else 0
    ic = "🟢" if tot_net > 0 else "🔴"
    print(f"  {ic} {'TOTAL':<10} {'':5} {tot_n:>3} {tot_w:>4} {wr:>5.1f}% "
          f"${tot_g:>9,.2f} ${tot_c:>8,.2f} ${tot_net:>9,.2f}")
    print("═"*W)

    # Strategy breakdown
    all_t = [t for r in results for t in r.trades]
    print(f"\n  📊 STRATÉGIE BREAKDOWN")
    print("  " + "─"*60)
    for lbl, code in [("Breakout",   "BK"), ("Mean Reversion", "MR"), ("Trend Continuation", "TC")]:
        ts = [t for t in all_t if t.strat == code]
        if not ts: continue
        w = sum(1 for t in ts if t.result == "WIN")
        ic = "🟢" if sum(t.pnl_n for t in ts) > 0 else "🔴"
        pnl = sum(t.pnl_n for t in ts)
        cm  = sum(t.comm for t in ts)
        print(f"  {ic} {lbl:<22} | {len(ts):>4} trades | WR {w/len(ts)*100:>5.1f}% "
              f"| PnL ${pnl:>+10,.2f} | Comm ${cm:>8,.2f}")

    # Fee analysis
    print(f"\n  💸 COMMISSION ANALYSIS")
    print("  " + "─"*60)
    print(f"  PnL Brut (before fees)  : ${tot_g:>+12,.2f}")
    print(f"  IC Markets Commissions  : ${tot_c:>+12,.2f}")
    print(f"  PnL Net (after fees)    : ${tot_net:>+12,.2f}")
    if tot_g > 0:
        print(f"  Comm / PnL Brut         :   {tot_c/tot_g*100:>8.1f}%")

    # ECN vs Retail
    print(f"\n  ⚡ ECN vs RETAIL SPREAD")
    print("  " + "─"*60)
    retail_cost = sum(
        1.5 * INSTRUMENTS.get(t.inst, INSTRUMENTS["EURUSD"]).pip
        * INSTRUMENTS.get(t.inst, INSTRUMENTS["EURUSD"]).contract * t.lots
        for t in all_t
    )
    print(f"  Retail (1.5 pip spread)  : ${retail_cost:>+12,.2f}")
    print(f"  ECN ($7/lot + 0.1 pip)   : ${tot_c:>+12,.2f}")
    print(f"  SAVINGS                  : ${retail_cost - tot_c:>+12,.2f}")
    print(f"  PnL Retail               : ${tot_g - retail_cost:>+12,.2f}")
    print(f"  PnL ECN                  : ${tot_net:>+12,.2f}")

    # Return on Capital
    roc = tot_net / 25000 * 100
    roc_annual = roc * 12  # rough annualization (1000-1500 candles ≈ 1 month 5m/15m)

    print(f"\n  📈 PERFORMANCE")
    print("  " + "─"*60)
    print(f"  Return on Capital (period) :   {roc:>+8.2f}%")
    print(f"  Annualized (est.)          :   {roc_annual:>+8.1f}%")
    print(f"  Avg PnL per Trade          : ${tot_net/tot_n if tot_n else 0:>+10.2f}")
    print(f"  Avg Commission per Trade   : ${tot_c/tot_n if tot_n else 0:>+10.2f}")

    # Best/worst instruments
    profitable = [r for r in results if r.pnl_n > 0]
    if profitable:
        best = max(profitable, key=lambda x: x.pnl_n)
        print(f"\n  🏆 BEST: {best.inst} {best.tf} → ${best.pnl_n:+,.2f} (PF {best.pf:.2f})")

    verdict = "✅ GOLDILOCKS FOUND" if tot_net > 0 else "⚠️ TUNING REQUIRED"
    retail_v = "❌ RETAIL DESTROYED" if (tot_g - retail_cost) < 0 else "⚠️ RETAIL MARGINAL"
    print(f"\n  🏆 VERDICT ECN    : {verdict}")
    print(f"  💀 VERDICT RETAIL : {retail_v}")
    print("═"*W + "\n")


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print("\n🔬 THE GOLDILOCKS PROTOCOL — Mid-Frequency ECN Sweet Spot")
    print("─" * 60)

    fetcher = DataFetcher()
    results = []

    timeframes = [
        ("MINUTE_5",  "5m"),
        ("MINUTE_15", "15m"),
    ]

    for resolution, tf in timeframes:
        print(f"\n⚡ TIMEFRAME: {tf}")
        print("─" * 40)

        for epic, inst in INSTRUMENTS.items():
            print(f"  📊 {inst.name:<12} ({epic}) {tf}...", end=" ", flush=True)
            df = fetcher.fetch(epic, resolution=resolution, count=1000)

            if df is None or len(df) < 50:
                print(f"❌ ({len(df) if df is not None else 0} bars)")
                continue
            print(f"✅ {len(df)} bars", end="")

            r = backtest(df, inst, tf)
            results.append(r)

            ic = "🟢" if r.pnl_n > 0 else "🔴"
            print(f" → {ic} {r.n} trades | WR {r.wr:.0f}% | ${r.pnl_n:+,.0f}")
            time.sleep(0.5)

    if results:
        tear_sheet(results)
    else:
        print("\n❌ No data — abort.")

if __name__ == "__main__":
    main()
