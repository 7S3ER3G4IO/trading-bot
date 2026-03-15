#!/usr/bin/env python3
"""
╔════════════════════════════════════════════════════════════════════════════╗
║    THE ECN VINDICATOR — HFT Scalping Backtest Engine                     ║
║    IC Markets Raw Spread ECN × Micro-Timeframe Brutality                 ║
╚════════════════════════════════════════════════════════════════════════════╝

Proves that IC Markets True ECN with $7/lot RT commission and 0.1 pip spread
unlocks profitable scalping on 1m & 5m — impossible with retail spreads.

Usage: python ecn_scalping_backtest.py
"""

import os
import sys
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════
#  IC MARKETS RAW SPREAD FEE STRUCTURE
# ═══════════════════════════════════════════════════════════════════════════

ECN_SPREAD_PIPS     = 0.1       # IC Markets Raw Spread: ~0.1 pip average
ECN_COMMISSION_RT   = 7.00      # $7.00 per Standard Lot (100k units) round-trip
LOT_SIZE_STANDARD   = 100_000   # 1 Standard Lot = 100,000 units

# ═══════════════════════════════════════════════════════════════════════════
#  INSTRUMENT CONFIG — Top liquidity only
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class InstrumentConfig:
    epic: str
    name: str
    pip_size: float           # 1 pip in price units
    contract_size: float      # contract multiplier (100k for forex, 1 for crypto/indices, 100 for gold)
    min_size: float           # minimum position size in lots
    sl_atr_mult: float        # SL = ATR × this (0.5 to 0.8)
    tp_rr: float              # TP = SL × this (Risk:Reward)
    category: str

INSTRUMENTS = {
    "EURUSD":  InstrumentConfig("EURUSD",  "EUR/USD",    0.0001, 100_000, 0.01, 0.6, 1.5, "forex"),
    "USDJPY":  InstrumentConfig("USDJPY",  "USD/JPY",    0.01,   100_000, 0.01, 0.6, 1.5, "forex"),
    "GOLD":    InstrumentConfig("GOLD",    "Gold",       0.01,   100,     0.01, 0.5, 1.5, "commodity"),
    "US100":   InstrumentConfig("US100",   "NASDAQ 100", 0.01,   1,       1.00, 0.8, 1.5, "index"),
    "BTCUSD":  InstrumentConfig("BTCUSD",  "Bitcoin",    0.01,   1,       0.01, 0.7, 1.5, "crypto"),
    "ETHUSD":  InstrumentConfig("ETHUSD",  "Ethereum",   0.01,   1,       0.01, 0.7, 1.5, "crypto"),
}

# ═══════════════════════════════════════════════════════════════════════════
#  CAPITAL.COM DATA FETCHER (for OHLCV data only)
# ═══════════════════════════════════════════════════════════════════════════

class CapitalDataFetcher:
    """Fetches historical OHLCV from Capital.com API (data only, not for execution)."""

    def __init__(self):
        self.base_url = "https://demo-api-capital.backend-capital.com/api/v1"
        self.api_key  = os.getenv("CAPITAL_API_KEY", "")
        self.email    = os.getenv("CAPITAL_EMAIL", "")
        self.password = os.getenv("CAPITAL_PASSWORD", "")
        self.cst      = ""
        self.token    = ""
        self._authenticate()

    def _authenticate(self):
        try:
            r = requests.post(
                f"{self.base_url}/session",
                headers={
                    "X-CAP-API-KEY": self.api_key,
                    "Content-Type": "application/json",
                },
                json={"identifier": self.email, "password": self.password},
                timeout=10,
            )
            if r.status_code == 200:
                self.cst   = r.headers.get("CST", "")
                self.token = r.headers.get("X-SECURITY-TOKEN", "")
                print("✅ Capital.com data feed connected")
            else:
                print(f"⚠️  Capital.com auth failed: {r.status_code}")
        except Exception as e:
            print(f"❌ Capital.com auth error: {e}")

    def fetch_ohlcv(self, epic: str, resolution: str = "MINUTE", count: int = 1000) -> Optional[pd.DataFrame]:
        """Fetch OHLCV candles from Capital.com."""
        try:
            r = requests.get(
                f"{self.base_url}/prices/{epic}",
                headers={
                    "X-SECURITY-TOKEN": self.token,
                    "CST": self.cst,
                    "Content-Type": "application/json",
                },
                params={"resolution": resolution, "max": min(count, 1000)},
                timeout=15,
            )
            if r.status_code != 200:
                print(f"  ⚠️ {epic} fetch failed: {r.status_code}")
                return None

            data = r.json()
            prices = data.get("prices", [])
            if not prices:
                return None

            records = []
            for p in prices:
                ts = p.get("snapshotTime", "")
                o  = (float(p["openPrice"]["bid"]) + float(p["openPrice"]["ask"])) / 2
                h  = (float(p["highPrice"]["bid"]) + float(p["highPrice"]["ask"])) / 2
                l  = (float(p["lowPrice"]["bid"])  + float(p["lowPrice"]["ask"]))  / 2
                c  = (float(p["closePrice"]["bid"]) + float(p["closePrice"]["ask"])) / 2
                v  = int(p.get("lastTradedVolume", 0))
                records.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": v})

            df = pd.DataFrame(records)
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df.set_index("timestamp", inplace=True)
            df.sort_index(inplace=True)
            return df

        except Exception as e:
            print(f"  ❌ {epic} fetch error: {e}")
            return None


# ═══════════════════════════════════════════════════════════════════════════
#  ECN SCALPING STRATEGY — Ultra-Sensitive Triggers
# ═══════════════════════════════════════════════════════════════════════════

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute ATR, RSI, EMA, and micro-breakout levels."""
    # ATR (10-period for scalping)
    h = df["high"]
    l = df["low"]
    c = df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(10).mean()

    # RSI (7-period for ultra-sensitivity)
    delta = c.diff()
    gain  = delta.where(delta > 0, 0).rolling(7).mean()
    loss  = (-delta.where(delta < 0, 0)).rolling(7).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # EMA 9 & EMA 21 (fast/slow)
    df["ema9"]  = c.ewm(span=9, adjust=False).mean()
    df["ema21"] = c.ewm(span=21, adjust=False).mean()

    # Micro-Breakout: session range (8 candles PRIOR — shift to avoid lookahead)
    df["range_high"] = h.rolling(8).max().shift(1)
    df["range_low"]  = l.rolling(8).min().shift(1)

    # Volume surge (relative to 20-period average)
    df["vol_ratio"] = df["volume"] / df["volume"].rolling(20).mean().replace(0, 1)

    return df


def generate_signal(df: pd.DataFrame, i: int) -> Tuple[str, str]:
    """
    Generate signal: 'BUY', 'SELL', or 'HOLD' with strategy tag.

    Ultra-sensitive triggers designed for ECN microstructure:
    - Breakout: 8-candle range breakout with volume confirmation
    - Mean Reversion: RSI 45/55 (hyper-sensitive) + EMA crossover
    """
    if i < 25:
        return "HOLD", ""

    row   = df.iloc[i]
    prev  = df.iloc[i - 1]
    close = row["close"]
    atr   = row["atr"]
    rsi   = row["rsi"]

    if pd.isna(atr) or atr <= 0 or pd.isna(rsi):
        return "HOLD", ""

    ema9   = row["ema9"]
    ema21  = row["ema21"]
    r_high = row["range_high"]
    r_low  = row["range_low"]
    vol_r  = row.get("vol_ratio", 1.0)

    # ── STRATEGY 1: Micro-Breakout (BK) ──────────────────────────────────
    # Price breaks above/below 8-candle range with momentum
    if close > r_high and ema9 > ema21 and vol_r > 0.8:
        return "BUY", "BK"

    if close < r_low and ema9 < ema21 and vol_r > 0.8:
        return "SELL", "BK"

    # ── STRATEGY 2: Mean Reversion (MR) ──────────────────────────────────
    # RSI 45/55 ultra-sensitive thresholds (ECN allows this!)
    if rsi < 45 and ema9 > ema21 and close > prev["close"]:
        return "BUY", "MR"

    if rsi > 55 and ema9 < ema21 and close < prev["close"]:
        return "SELL", "MR"

    return "HOLD", ""


# ═══════════════════════════════════════════════════════════════════════════
#  ECN FEE CALCULATOR
# ═══════════════════════════════════════════════════════════════════════════

def calculate_ecn_fees(size_lots: float, instrument: InstrumentConfig, entry_price: float) -> float:
    """
    Calculate IC Markets Raw Spread round-trip fees.

    For Forex: Commission = $7.00 per Standard Lot (100k) round-trip
    For CFDs:  Commission = proportional to notional value
    Spread: 0.1 pip simulated on all instruments
    """
    # Commission: $7.00 per Standard Lot equivalent
    # For forex: 1 lot = 100k units → $7/lot
    # For crypto/indices/gold: scale by notional relative to 100k
    if instrument.category == "forex":
        commission = ECN_COMMISSION_RT * size_lots
    else:
        # Notional = size × entry_price × contract_size
        notional = size_lots * entry_price * instrument.contract_size
        # Commission proportional: $7 per $100k notional
        commission = (notional / LOT_SIZE_STANDARD) * ECN_COMMISSION_RT

    # Spread cost: 0.1 pip (simulated) — built into entry/exit
    spread_cost = ECN_SPREAD_PIPS * instrument.pip_size * instrument.contract_size * size_lots

    return commission + spread_cost


# ═══════════════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    instrument: str
    direction: str
    strategy: str
    entry: float
    sl: float
    tp: float
    size_lots: float
    commission: float
    bar_open: int
    bar_close: int = 0
    exit_price: float = 0.0
    pnl_gross: float = 0.0
    pnl_net: float = 0.0
    result: str = ""


@dataclass
class BacktestResult:
    instrument: str
    timeframe: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    pnl_gross: float = 0.0
    commissions_total: float = 0.0
    pnl_net: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    trades: List[Trade] = field(default_factory=list)


def run_backtest(
    df: pd.DataFrame,
    instrument: InstrumentConfig,
    timeframe: str,
    account_balance: float = 25_000.0,
    risk_pct: float = 0.005,  # 0.5% risk per trade
) -> BacktestResult:
    """Run ECN scalping backtest on a single instrument."""

    result = BacktestResult(instrument=instrument.epic, timeframe=timeframe)
    df = compute_indicators(df)

    equity = account_balance
    peak_equity = equity
    max_dd = 0.0
    position = None
    daily_returns = []

    for i in range(25, len(df)):
        row = df.iloc[i]

        # ── Check open position ──
        if position is not None:
            current_price = row["close"]

            # Check SL/TP hit via High/Low
            if position.direction == "BUY":
                if row["low"] <= position.sl:
                    position.exit_price = position.sl
                    position.pnl_gross = (position.sl - position.entry) * position.size_lots * instrument.contract_size
                    position.pnl_net = position.pnl_gross - position.commission
                    position.result = "LOSS"
                    position.bar_close = i
                elif row["high"] >= position.tp:
                    position.exit_price = position.tp
                    position.pnl_gross = (position.tp - position.entry) * position.size_lots * instrument.contract_size
                    position.pnl_net = position.pnl_gross - position.commission
                    position.result = "WIN"
                    position.bar_close = i
                else:
                    continue

            elif position.direction == "SELL":
                if row["high"] >= position.sl:
                    position.exit_price = position.sl
                    position.pnl_gross = (position.entry - position.sl) * position.size_lots * instrument.contract_size
                    position.pnl_net = position.pnl_gross - position.commission
                    position.result = "LOSS"
                    position.bar_close = i
                elif row["low"] <= position.tp:
                    position.exit_price = position.tp
                    position.pnl_gross = (position.entry - position.tp) * position.size_lots * instrument.contract_size
                    position.pnl_net = position.pnl_gross - position.commission
                    position.result = "WIN"
                    position.bar_close = i
                else:
                    continue

            # Record closed trade
            equity += position.pnl_net
            daily_returns.append(position.pnl_net / account_balance)
            result.trades.append(position)

            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity * 100
            if dd > max_dd:
                max_dd = dd

            position = None
            continue  # don't open new position on same candle

        # ── Generate signal ──
        sig, strat = generate_signal(df, i)
        if sig == "HOLD":
            continue

        atr = row["atr"]
        if pd.isna(atr) or atr <= 0:
            continue

        entry = row["close"]

        # ── SL/TP with ECN-tight levels ──
        sl_mult = instrument.sl_atr_mult
        tp_mult = instrument.tp_rr

        if sig == "BUY":
            sl = entry - atr * sl_mult
            tp = entry + atr * sl_mult * tp_mult
        else:
            sl = entry + atr * sl_mult
            tp = entry - atr * sl_mult * tp_mult

        sl_dist = abs(entry - sl)
        if sl_dist <= 0:
            continue

        # ── Position sizing (risk-based) ──
        risk_amount = equity * risk_pct
        # Size in lots: risk / (SL_distance × contract_size)
        size_lots = risk_amount / (sl_dist * instrument.contract_size)
        size_lots = max(instrument.min_size, round(size_lots, 2))

        # ── Calculate ECN fees ──
        commission = calculate_ecn_fees(size_lots, instrument, entry)

        # ── Open position ──
        position = Trade(
            instrument=instrument.epic,
            direction=sig,
            strategy=strat,
            entry=entry,
            sl=round(sl, 5),
            tp=round(tp, 5),
            size_lots=size_lots,
            commission=commission,
            bar_open=i,
        )

    # ── Force close any remaining position ──
    if position is not None:
        last_row = df.iloc[-1]
        position.exit_price = last_row["close"]
        if position.direction == "BUY":
            position.pnl_gross = (last_row["close"] - position.entry) * position.size_lots * instrument.contract_size
        else:
            position.pnl_gross = (position.entry - last_row["close"]) * position.size_lots * instrument.contract_size
        position.pnl_net = position.pnl_gross - position.commission
        position.result = "WIN" if position.pnl_net > 0 else "LOSS"
        position.bar_close = len(df) - 1
        equity += position.pnl_net
        result.trades.append(position)

    # ── Compute stats ──
    result.total_trades = len(result.trades)
    if result.total_trades > 0:
        result.wins   = sum(1 for t in result.trades if t.result == "WIN")
        result.losses = sum(1 for t in result.trades if t.result == "LOSS")
        result.win_rate = result.wins / result.total_trades * 100

        result.pnl_gross = sum(t.pnl_gross for t in result.trades)
        result.commissions_total = sum(t.commission for t in result.trades)
        result.pnl_net = sum(t.pnl_net for t in result.trades)

        win_pnls  = [t.pnl_net for t in result.trades if t.result == "WIN"]
        loss_pnls = [t.pnl_net for t in result.trades if t.result == "LOSS"]
        result.avg_win  = np.mean(win_pnls) if win_pnls else 0
        result.avg_loss = np.mean(loss_pnls) if loss_pnls else 0

        total_wins  = sum(win_pnls) if win_pnls else 0
        total_losses = abs(sum(loss_pnls)) if loss_pnls else 0.01
        result.profit_factor = total_wins / total_losses if total_losses > 0 else 0

        result.max_drawdown = max_dd

        # Sharpe Ratio (annualized)
        if daily_returns:
            ret_arr = np.array(daily_returns)
            if ret_arr.std() > 0:
                bars_per_year = 252 * 24 * 60 if "1m" in timeframe else 252 * 24 * 12
                result.sharpe_ratio = (ret_arr.mean() / ret_arr.std()) * np.sqrt(bars_per_year)

    return result


# ═══════════════════════════════════════════════════════════════════════════
#  TEAR SHEET — Institutional Grade
# ═══════════════════════════════════════════════════════════════════════════

def print_tear_sheet(results: List[BacktestResult]):
    """Print institutional-grade tear sheet."""

    print("\n" + "═" * 92)
    print("  ██╗   ██╗██╗███╗   ██╗██████╗ ██╗ ██████╗ █████╗ ████████╗ ██████╗ ██████╗ ")
    print("  ██║   ██║██║████╗  ██║██╔══██╗██║██╔════╝██╔══██╗╚══██╔══╝██╔═══██╗██╔══██╗")
    print("  ██║   ██║██║██╔██╗ ██║██║  ██║██║██║     ███████║   ██║   ██║   ██║██████╔╝")
    print("  ╚██╗ ██╔╝██║██║╚██╗██║██║  ██║██║██║     ██╔══██║   ██║   ██║   ██║██╔══██╗")
    print("   ╚████╔╝ ██║██║ ╚████║██████╔╝██║╚██████╗██║  ██║   ██║   ╚██████╔╝██║  ██║")
    print("    ╚═══╝  ╚═╝╚═╝  ╚═══╝╚═════╝ ╚═╝ ╚═════╝╚═╝  ╚═╝   ╚═╝    ╚═════╝ ╚═╝  ╚═╝")
    print("  THE ECN VINDICATOR — HFT Scalping Backtest × IC Markets Raw Spread")
    print("═" * 92)

    print(f"\n  📅 Date      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  🏦 Broker    : IC Markets True ECN (Raw Spread)")
    print(f"  💰 Capital   : $25,000 USD")
    print(f"  📊 Spread    : {ECN_SPREAD_PIPS} pip (ECN)")
    print(f"  💸 Commission: ${ECN_COMMISSION_RT}/lot RT")
    print(f"  ⚡ Risk/Trade: 0.5%")

    # ── Per-instrument results ──
    print("\n" + "─" * 92)
    print(f"  {'INSTRUMENT':<12} {'TF':<5} {'TRADES':>7} {'WINS':>6} {'W/R':>7} "
          f"{'PnL BRUT':>12} {'COMMISSIONS':>13} {'PnL NET':>12} {'PF':>6} {'MaxDD':>7} {'SHARPE':>7}")
    print("─" * 92)

    total_trades = 0
    total_wins   = 0
    total_pnl_g  = 0
    total_comm   = 0
    total_pnl_n  = 0

    for r in results:
        icon = "🟢" if r.pnl_net > 0 else "🔴" if r.pnl_net < 0 else "⚪"
        print(
            f"  {icon} {r.instrument:<10} {r.timeframe:<5} {r.total_trades:>6} "
            f"{r.wins:>6} {r.win_rate:>6.1f}% "
            f"${r.pnl_gross:>10,.2f} ${r.commissions_total:>11,.2f} "
            f"${r.pnl_net:>10,.2f} {r.profit_factor:>5.2f} "
            f"{r.max_drawdown:>5.1f}% {r.sharpe_ratio:>6.2f}"
        )

        total_trades += r.total_trades
        total_wins   += r.wins
        total_pnl_g  += r.pnl_gross
        total_comm   += r.commissions_total
        total_pnl_n  += r.pnl_net

    print("─" * 92)
    total_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
    icon = "🟢" if total_pnl_n > 0 else "🔴"
    print(
        f"  {icon} {'TOTAL':<10} {'':5} {total_trades:>6} "
        f"{total_wins:>6} {total_wr:>6.1f}% "
        f"${total_pnl_g:>10,.2f} ${total_comm:>11,.2f} "
        f"${total_pnl_n:>10,.2f}"
    )
    print("═" * 92)

    # ── Strategy breakdown ──
    all_trades = [t for r in results for t in r.trades]
    bk_trades = [t for t in all_trades if t.strategy == "BK"]
    mr_trades = [t for t in all_trades if t.strategy == "MR"]

    print("\n  📊 STRATÉGIE BREAKDOWN")
    print("  " + "─" * 55)
    for label, trades in [("Breakout (BK)", bk_trades), ("Mean Reversion (MR)", mr_trades)]:
        if not trades:
            continue
        wins = sum(1 for t in trades if t.result == "WIN")
        wr   = wins / len(trades) * 100 if trades else 0
        pnl  = sum(t.pnl_net for t in trades)
        comm = sum(t.commission for t in trades)
        icon = "🟢" if pnl > 0 else "🔴"
        print(f"  {icon} {label:<22} | {len(trades):>4} trades | WR {wr:>5.1f}% | PnL ${pnl:>+10,.2f} | Comm ${comm:>8,.2f}")

    # ── Commission impact analysis ──
    print(f"\n  💸 ANALYSE IMPACT COMMISSIONS")
    print(f"  " + "─" * 55)
    print(f"  PnL Brut (avant fees)  : ${total_pnl_g:>+12,.2f}")
    print(f"  Commissions IC Markets : ${total_comm:>+12,.2f}")
    print(f"  PnL Net (après fees)   : ${total_pnl_n:>+12,.2f}")
    if total_pnl_g != 0:
        comm_pct = total_comm / abs(total_pnl_g) * 100
        print(f"  Comm / PnL Brut        :  {comm_pct:>10.1f}%")

    # Compare with retail spreads
    print(f"\n  ⚡ COMPARAISON ECN vs RETAIL")
    print(f"  " + "─" * 55)
    retail_spread = 1.5  # typical retail spread
    retail_cost = sum(
        retail_spread * INSTRUMENTS.get(t.instrument, INSTRUMENTS["EURUSD"]).pip_size
        * INSTRUMENTS.get(t.instrument, INSTRUMENTS["EURUSD"]).contract_size * t.size_lots
        for t in all_trades
    )
    ecn_total_cost = total_comm
    savings = retail_cost - ecn_total_cost
    print(f"  Coût Retail (1.5 pip spread) : ${retail_cost:>+12,.2f}")
    print(f"  Coût ECN ($7/lot + 0.1 pip)  : ${ecn_total_cost:>+12,.2f}")
    print(f"  ÉCONOMIES IC Markets         : ${savings:>+12,.2f}")
    print(f"  PnL Net si Retail            : ${total_pnl_g - retail_cost:>+12,.2f}")
    print(f"  PnL Net ECN                  : ${total_pnl_n:>+12,.2f}")

    verdict = "✅ ECN PROFITABLE" if total_pnl_n > 0 else "⚠️ OPTIMISATION REQUISE"
    retail_verdict = "❌ RETAIL IMPOSSIBLE" if (total_pnl_g - retail_cost) < 0 else "⚠️ RETAIL MARGINAL"
    print(f"\n  🏆 VERDICT ECN   : {verdict}")
    print(f"  💀 VERDICT RETAIL: {retail_verdict}")
    print("═" * 92 + "\n")


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print("\n🔬 THE ECN VINDICATOR — Initializing...")
    print("─" * 60)

    fetcher = CapitalDataFetcher()
    all_results = []

    timeframes = [
        ("MINUTE", "1m"),
        ("MINUTE_5", "5m"),
    ]

    for resolution, tf_label in timeframes:
        print(f"\n⚡ TIMEFRAME: {tf_label}")
        print("─" * 40)

        for epic, config in INSTRUMENTS.items():
            print(f"  📊 Fetching {config.name} ({epic}) — {tf_label}...", end=" ")
            df = fetcher.fetch_ohlcv(epic, resolution=resolution, count=1000)

            if df is None or len(df) < 50:
                print(f"❌ insuffisant ({len(df) if df is not None else 0} bougies)")
                continue

            print(f"✅ {len(df)} bougies")

            result = run_backtest(df, config, tf_label)
            all_results.append(result)

            icon = "🟢" if result.pnl_net > 0 else "🔴"
            print(
                f"    {icon} {result.total_trades} trades | "
                f"WR {result.win_rate:.1f}% | "
                f"PnL ${result.pnl_net:+,.2f} | "
                f"Comm ${result.commissions_total:,.2f}"
            )

            time.sleep(0.5)  # Rate limit Capital.com

    if all_results:
        print_tear_sheet(all_results)
    else:
        print("\n❌ No data fetched — backtest impossible.")


if __name__ == "__main__":
    main()
