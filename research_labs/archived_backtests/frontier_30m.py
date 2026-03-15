#!/usr/bin/env python3
"""
frontier_30m.py — 🔬 THE FRONTIER TEST: 30M Equilibrium Backtest

Tests the 30-minute timeframe as the potential sweet spot between:
  - 1M HFT (565 trades, -241% → spread > signal, SUICIDE)
  - 1H    (proven profitable, but lower trade volume)

Hypothesis: 30M should double 1H volume while keeping Profit Factor > 1.0

Strategy: Multi-strategy (TF + MR + BK) with moderate sensitivity
  - Breakout: 24-bar lookback (12h of data in 30M)
  - Mean Reversion: RSI 30/70 (standard, not kamikaze)
  - Trend Following: EMA 9/21 cross + ADX filter
  - SL: 1.2× ATR (tight but survivable)
  - TP: 2.0R (positive expectancy if WR > 40%)
  - Spread Guard: ON (filter trades where spread > 30% of SL)

Assets: 24 most liquid (Forex, Crypto, Gold, Oil, US Indices)
Data: 500 candles × 30M = ~10.4 days per asset

Usage:
    docker compose exec bot python frontier_30m.py
"""

import os
import sys
import time
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

from brokers.capital_client import CapitalClient, PIP_FACTOR

# ═════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — 30M FRONTIER
# ═════════════════════════════════════════════════════════════════════════════

INSTRUMENTS = [
    # Forex Majeurs (8)
    "EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "EURJPY",
    "USDCHF", "AUDJPY", "NZDJPY",
    # Crypto (4)
    "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD",
    # Commodités (4)
    "GOLD", "SILVER", "OIL_CRUDE", "OIL_BRENT",
    # Indices (4)
    "US500", "US100", "US30", "DE40",
    # Forex MR (4)
    "AUDNZD", "EURCHF", "EURGBP", "GBPCHF",
]  # 24 assets

TIMEFRAME        = "30m"
CANDLES_PER_CALL = 300
NUM_PAGES        = 2       # 2 × 300 = 600 candles → ~12.5 days of 30M

INITIAL_BALANCE  = 20_000.0
RISK_PER_TRADE   = 0.01    # 1% per trade (standard for 30M)

# Strategy params — Hybrid Sensitivity
RSI_PERIOD       = 14
RSI_BUY          = 30      # Standard oversold
RSI_SELL         = 70      # Standard overbought
EMA_FAST         = 9       # Fast EMA for trend
EMA_SLOW         = 21      # Slow EMA for trend
ADX_MIN          = 20      # Minimum ADX for trend trades
BK_LOOKBACK      = 24      # 24 × 30M = 12h range
BK_MARGIN        = 0.001   # 0.1% above range for breakout confirmation
ATR_PERIOD       = 14
SL_ATR_MULT      = 1.2     # 1.2× ATR (moderate)
TP_RR            = 2.0     # 2.0R (needs >33% WR to be profitable)

# Spread Guard — reject trades where spread > X% of SL distance
SPREAD_GUARD_MAX = 0.30    # 30% of SL = max spread allowed
SPREAD_GUARD_ON  = True

# Strategy assignment per asset class
STRAT_MAP = {
    # Forex → Trend Following
    "EURUSD": "TF", "GBPUSD": "TF", "USDJPY": "TF", "GBPJPY": "TF",
    "EURJPY": "TF", "USDCHF": "TF", "AUDJPY": "TF", "NZDJPY": "TF",
    # Crypto → Breakout
    "BTCUSD": "BK", "ETHUSD": "BK", "SOLUSD": "BK", "XRPUSD": "BK",
    # Commodities → Trend Following
    "GOLD": "TF", "SILVER": "TF", "OIL_CRUDE": "TF", "OIL_BRENT": "TF",
    # Indices → Mean Reversion
    "US500": "MR", "US100": "MR", "US30": "MR", "DE40": "MR",
    # Forex low-vol → Mean Reversion
    "AUDNZD": "MR", "EURCHF": "MR", "EURGBP": "MR", "GBPCHF": "MR",
}

# Typical retail spreads (price units)
TYPICAL_SPREAD = {
    "EURUSD": 0.00010, "GBPUSD": 0.00012, "USDJPY": 0.012, "GBPJPY": 0.025,
    "EURJPY": 0.015, "USDCHF": 0.00015, "AUDJPY": 0.018, "NZDJPY": 0.020,
    "BTCUSD": 35.0, "ETHUSD": 2.50, "SOLUSD": 0.15, "XRPUSD": 0.0025,
    "GOLD": 0.30, "SILVER": 0.025, "OIL_CRUDE": 0.03, "OIL_BRENT": 0.03,
    "US500": 0.50, "US100": 1.50, "US30": 2.0, "DE40": 1.50,
    "AUDNZD": 0.00020, "EURCHF": 0.00018, "EURGBP": 0.00015, "GBPCHF": 0.00020,
}

SLIPPAGE_FACTOR = 0.3  # 0.3× spread additional slippage


# ═════════════════════════════════════════════════════════════════════════════
# INDICATORS
# ═════════════════════════════════════════════════════════════════════════════

def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_atr(high: pd.Series, low: pd.Series, close: pd.Series,
             period: int = 14) -> pd.Series:
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_adx(high: pd.Series, low: pd.Series, close: pd.Series,
             period: int = 14) -> pd.Series:
    """Simplified ADX calculation."""
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    # Zero out when opposite DM is larger
    plus_dm[minus_dm > plus_dm] = 0
    minus_dm[plus_dm > minus_dm] = 0

    atr = calc_atr(high, low, close, period)
    atr_safe = atr.replace(0, np.nan)

    plus_di = 100 * (plus_dm.rolling(period).mean() / atr_safe)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr_safe)

    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.rolling(period).mean()
    return adx


# ═════════════════════════════════════════════════════════════════════════════
# DATA FETCHER
# ═════════════════════════════════════════════════════════════════════════════

def fetch_candles(client: CapitalClient, epic: str) -> pd.DataFrame:
    frames = []
    for page in range(NUM_PAGES):
        df = client.fetch_ohlcv(epic, TIMEFRAME, CANDLES_PER_CALL)
        if df is None or df.empty:
            break
        frames.append(df)
        time.sleep(0.3)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames).sort_index()
    combined = combined[~combined.index.duplicated(keep='last')]
    return combined


# ═════════════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE — 30M Multi-Strategy
# ═════════════════════════════════════════════════════════════════════════════

def run_backtest(df: pd.DataFrame, epic: str, balance: float) -> dict:
    """
    Multi-strategy 30M backtest with Spread Guard.
    PnL in euros via R-multiples.
    """
    if df.empty or len(df) < max(ATR_PERIOD, BK_LOOKBACK, EMA_SLOW) + 5:
        return {"trades": 0, "epic": epic, "balance": balance}

    strat = STRAT_MAP.get(epic, "TF")
    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    # Indicators
    rsi     = calc_rsi(close, RSI_PERIOD)
    atr     = calc_atr(high, low, close, ATR_PERIOD)
    ema_f   = calc_ema(close, EMA_FAST)
    ema_s   = calc_ema(close, EMA_SLOW)
    adx     = calc_adx(high, low, close, ATR_PERIOD)

    spread_price = TYPICAL_SPREAD.get(epic, 0.0001)
    total_spread = spread_price * (1 + SLIPPAGE_FACTOR)

    # Breakout ranges
    range_high = high.rolling(BK_LOOKBACK).max()
    range_low  = low.rolling(BK_LOOKBACK).min()

    # State
    trades = []
    in_position = False
    entry_price = 0.0
    entry_side  = ""
    sl_price    = 0.0
    tp_price    = 0.0
    entry_idx   = 0
    size_units  = 0.0

    spread_filtered = 0  # Count of trades blocked by spread guard

    start_bar = max(ATR_PERIOD, BK_LOOKBACK, EMA_SLOW) + 2

    for i in range(start_bar, len(df)):
        price   = close.iloc[i]
        h       = high.iloc[i]
        l       = low.iloc[i]
        cur_atr = atr.iloc[i]
        cur_rsi = rsi.iloc[i]
        cur_adx = adx.iloc[i]

        if pd.isna(cur_atr) or cur_atr <= 0:
            continue

        if in_position:
            # Check SL / TP
            exit_price = None
            result_tag = None

            if entry_side == "BUY":
                if l <= sl_price:
                    exit_price, result_tag = sl_price, "SL"
                elif h >= tp_price:
                    exit_price, result_tag = tp_price, "TP"
            else:
                if h >= sl_price:
                    exit_price, result_tag = sl_price, "SL"
                elif l <= tp_price:
                    exit_price, result_tag = tp_price, "TP"

            if exit_price is not None:
                if entry_side == "BUY":
                    move = exit_price - entry_price
                else:
                    move = entry_price - exit_price

                pnl_gross = move * size_units
                spread_cost_eur = total_spread * size_units
                pnl_net = pnl_gross - spread_cost_eur

                trades.append({
                    "side": entry_side, "entry": entry_price, "exit": exit_price,
                    "pnl_gross": round(pnl_gross, 2),
                    "pnl_net": round(pnl_net, 2),
                    "spread_cost": round(spread_cost_eur, 2),
                    "result": result_tag, "bars": i - entry_idx,
                })
                balance += pnl_net
                in_position = False
                continue
        else:
            signal = None

            # ─── Strategy Selection ───────────────────────────────────
            if strat == "TF":
                # Trend Following: EMA cross + ADX filter
                ema_f_val = ema_f.iloc[i]
                ema_s_val = ema_s.iloc[i]
                ema_f_prev = ema_f.iloc[i - 1]
                ema_s_prev = ema_s.iloc[i - 1]

                if pd.isna(ema_f_val) or pd.isna(ema_s_val) or pd.isna(cur_adx):
                    continue

                # EMA cross + ADX > threshold
                if ema_f_prev <= ema_s_prev and ema_f_val > ema_s_val and cur_adx > ADX_MIN:
                    signal = "BUY"
                elif ema_f_prev >= ema_s_prev and ema_f_val < ema_s_val and cur_adx > ADX_MIN:
                    signal = "SELL"

            elif strat == "MR":
                # Mean Reversion: RSI oversold/overbought
                if pd.isna(cur_rsi):
                    continue
                if cur_rsi < RSI_BUY:
                    signal = "BUY"
                elif cur_rsi > RSI_SELL:
                    signal = "SELL"

            elif strat == "BK":
                # Breakout: price breaks N-bar range
                rh = range_high.iloc[i - 1]
                rl = range_low.iloc[i - 1]
                if pd.isna(rh) or pd.isna(rl):
                    continue
                margin = price * BK_MARGIN
                if price > rh + margin:
                    signal = "BUY"
                elif price < rl - margin:
                    signal = "SELL"

            if signal and balance > 100:
                sl_dist = cur_atr * SL_ATR_MULT
                if sl_dist <= 0:
                    continue

                # ─── Spread Guard ─────────────────────────────────────
                if SPREAD_GUARD_ON:
                    spread_pct = total_spread / sl_dist
                    if spread_pct > SPREAD_GUARD_MAX:
                        spread_filtered += 1
                        continue

                risk_amount = balance * RISK_PER_TRADE
                size_units  = risk_amount / sl_dist
                entry_price = price
                entry_idx   = i
                entry_side  = signal

                if signal == "BUY":
                    sl_price = price - sl_dist
                    tp_price = price + sl_dist * TP_RR
                else:
                    sl_price = price + sl_dist
                    tp_price = price - sl_dist * TP_RR

                in_position = True

    # ─── Stats ────────────────────────────────────────────────────────────
    if not trades:
        return {
            "trades": 0, "epic": epic, "balance": balance,
            "spread_filtered": spread_filtered, "strat": strat,
        }

    total      = len(trades)
    wins       = sum(1 for t in trades if t["result"] == "TP")
    losses     = sum(1 for t in trades if t["result"] == "SL")
    wr         = wins / total * 100
    total_pnl  = sum(t["pnl_net"] for t in trades)
    tot_spread = sum(t["spread_cost"] for t in trades)
    avg_bars   = np.mean([t["bars"] for t in trades])

    # Profit Factor
    gross_wins  = sum(t["pnl_net"] for t in trades if t["pnl_net"] > 0)
    gross_losses = abs(sum(t["pnl_net"] for t in trades if t["pnl_net"] < 0))
    pf = gross_wins / gross_losses if gross_losses > 0 else float('inf')

    # Max Drawdown
    max_dd  = 0
    peak    = INITIAL_BALANCE
    running = INITIAL_BALANCE
    for t in trades:
        running += t["pnl_net"]
        peak = max(peak, running)
        dd = (peak - running) / peak * 100
        max_dd = max(max_dd, dd)

    return {
        "epic":            epic,
        "strat":           strat,
        "trades":          total,
        "wins":            wins,
        "losses":          losses,
        "wr":              round(wr, 1),
        "pnl_net":         round(total_pnl, 2),
        "spread_cost":     round(tot_spread, 2),
        "balance":         round(balance, 2),
        "avg_hold_bars":   round(avg_bars, 1),
        "max_dd_pct":      round(max_dd, 2),
        "profit_factor":   round(pf, 2),
        "spread_filtered": spread_filtered,
    }


# ═════════════════════════════════════════════════════════════════════════════
# TEAR SHEET
# ═════════════════════════════════════════════════════════════════════════════

def print_tear_sheet(results: list):
    active = [r for r in results if r["trades"] > 0]
    total_trades  = sum(r["trades"] for r in results)
    total_wins    = sum(r.get("wins", 0) for r in results)
    total_losses  = sum(r.get("losses", 0) for r in results)
    total_pnl     = sum(r.get("pnl_net", 0) for r in results)
    total_spread  = sum(r.get("spread_cost", 0) for r in results)
    total_filtered = sum(r.get("spread_filtered", 0) for r in results)
    max_dd        = max((r.get("max_dd_pct", 0) for r in results), default=0)
    global_wr     = total_wins / total_trades * 100 if total_trades > 0 else 0
    final_balance = INITIAL_BALANCE + total_pnl
    pnl_pct       = (total_pnl / INITIAL_BALANCE) * 100

    # Global Profit Factor
    all_wins  = sum(r.get("pnl_net", 0) for r in results if r.get("pnl_net", 0) > 0)
    all_losses = abs(sum(r.get("pnl_net", 0) for r in results if r.get("pnl_net", 0) < 0))
    global_pf = all_wins / all_losses if all_losses > 0 else float('inf')

    print()
    print("=" * 76)
    print("  🔬  THE FRONTIER TEST — 30M EQUILIBRIUM TEAR SHEET")
    print("=" * 76)
    print()

    # Config
    print("  ┌────────────────────────────────────────────────────────────────┐")
    print("  │  ⚙️  CONFIGURATION 30M FRONTIER                               │")
    print("  ├────────────────────────────────────────────────────────────────┤")
    print(f"  │  Timeframe      : 30 MINUTES                                 │")
    print(f"  │  Assets         : {len(INSTRUMENTS)} instruments                               │")
    print(f"  │  Strategies     : TF (EMA 9/21) + MR (RSI 30/70) + BK (24b) │")
    print(f"  │  SL / TP        : {SL_ATR_MULT}× ATR / {TP_RR}R                                │")
    print(f"  │  Spread Guard   : {'✅ ON' if SPREAD_GUARD_ON else '❌ OFF'} (max {SPREAD_GUARD_MAX*100:.0f}% of SL)                       │")
    print(f"  │  Risk/Trade     : {RISK_PER_TRADE*100:.0f}%                                          │")
    print("  └────────────────────────────────────────────────────────────────┘")
    print()

    # Per-instrument table
    print("  ┌──────────┬──────┬────────┬───────┬─────────┬──────────┬───────────┬────────┐")
    print("  │  ASSET   │ Strt │ TRADES │ WR %  │ PF      │ Spread € │  PnL Net  │ Filtr. │")
    print("  ├──────────┼──────┼────────┼───────┼─────────┼──────────┼───────────┼────────┤")
    for r in results:
        if r["trades"] == 0:
            f_cnt = r.get('spread_filtered', 0)
            f_str = f"{f_cnt}" if f_cnt > 0 else "—"
            print(f"  │  {r['epic']:<6}  │  {r.get('strat','—'):>2}  │    0   │   —   │    —    │      —   │       —   │  {f_str:>4}  │")
            continue
        pnl_icon = "🟢" if r["pnl_net"] >= 0 else "🔴"
        pf_str   = f"{r.get('profit_factor', 0):.2f}" if r.get("profit_factor", 0) < 100 else "∞"
        f_cnt    = r.get('spread_filtered', 0)
        f_str    = f"{f_cnt}" if f_cnt > 0 else "—"
        print(f"  │  {r['epic']:<6}  │  {r.get('strat','—'):>2}  │ {r['trades']:>5}  │ {r['wr']:>4.1f}% │ {pf_str:>7} │ {r['spread_cost']:>7,.0f}€ │ {pnl_icon}{r['pnl_net']:>+8,.0f}€ │  {f_str:>4}  │")
    print("  └──────────┴──────┴────────┴───────┴─────────┴──────────┴───────────┴────────┘")
    print()

    # Strategy breakdown
    strat_results = {}
    for r in active:
        s = r.get("strat", "?")
        if s not in strat_results:
            strat_results[s] = {"trades": 0, "wins": 0, "pnl": 0, "spread": 0}
        strat_results[s]["trades"] += r["trades"]
        strat_results[s]["wins"]   += r.get("wins", 0)
        strat_results[s]["pnl"]    += r.get("pnl_net", 0)
        strat_results[s]["spread"] += r.get("spread_cost", 0)

    print("  📊 Par stratégie :")
    for s, d in sorted(strat_results.items()):
        wr = d["wins"] / d["trades"] * 100 if d["trades"] > 0 else 0
        icon = "🟢" if d["pnl"] >= 0 else "🔴"
        label = {"TF": "Trend Following", "MR": "Mean Reversion", "BK": "Breakout"}.get(s, s)
        print(f"    {s:>2} ({label:<16}) │ {d['trades']:>4} trades │ WR {wr:>4.1f}% │ {icon} PnL {d['pnl']:>+9,.2f}€ │ Spread {d['spread']:>7,.2f}€")
    print()

    # Global summary
    print("  ╔════════════════════════════════════════════════════════════════╗")
    print("  ║  🔬  VERDICT DE LA FRONTIÈRE 30M                              ║")
    print("  ╠════════════════════════════════════════════════════════════════╣")
    print(f"  ║  Total Trades       : {total_trades:>8,}                                ║")
    print(f"  ║  Wins / Losses      : {total_wins:>5,} / {total_losses:>5,}                            ║")
    print(f"  ║  Win Rate           : {global_wr:>7.1f}%                                ║")
    print(f"  ║  Profit Factor      : {global_pf:>7.2f}                                ║")
    print(f"  ║  Max Drawdown       : {max_dd:>7.1f}%                                ║")
    print(f"  ║  Spread Guard bloqué: {total_filtered:>6} trades                          ║")
    print("  ╠════════════════════════════════════════════════════════════════╣")
    spread_icon = "🩸" if total_spread > abs(total_pnl) * 0.5 else "💰"
    pnl_icon    = "🟢" if total_pnl >= 0 else "💀"
    print(f"  ║  {spread_icon} Coût Spread Total : {total_spread:>12,.2f}€                       ║")
    print(f"  ║  {pnl_icon} PnL NET           : {total_pnl:>+12,.2f}€                       ║")
    print(f"  ║  Balance Finale     : {final_balance:>12,.2f}€                       ║")
    print(f"  ║  Rendement          : {pnl_pct:>+10.2f}%                           ║")
    print("  ╚════════════════════════════════════════════════════════════════╝")
    print()

    # Comparison table
    print("  ┌────────────────────────────────────────────────────────────────┐")
    print("  │  📊  COMPARAISON DES TIMEFRAMES                               │")
    print("  ├──────────┬──────────┬─────────┬───────────┬───────────────────┤")
    print("  │  TF      │  Trades  │  WR %   │  PF       │  Verdict          │")
    print("  ├──────────┼──────────┼─────────┼───────────┼───────────────────┤")
    print("  │  1M HFT  │     565  │  49.2%  │  < 0.10   │  💀 SUICIDE       │")
    print(f"  │  30M     │  {total_trades:>6,}  │  {global_wr:>4.1f}%  │  {global_pf:>6.2f}   │  {'🟢 RENTABLE' if global_pf > 1.0 else '🔴 NON RENTABLE' if global_pf > 0.8 else '💀 PERTE'}   │")
    print("  │  1H      │    ~200  │  ~55%   │  ~1.30    │  🟢 PROUVÉ        │")
    print("  └──────────┴──────────┴─────────┴───────────┴───────────────────┘")
    print()

    if global_pf > 1.0:
        print("  ✅ Le 30M PEUT être le Sweet Spot : plus de volume que le 1H,")
        print("  avec un Profit Factor positif. Le spread est gérable.")
    elif global_pf > 0.8:
        print("  ⚠️ Le 30M est en zone grise : le Profit Factor est proche de 1.0.")
        print("  Des optimisations ciblées pourraient le rendre rentable.")
    else:
        print("  ❌ Le 30M reste trop agressif : le spread mange encore trop de signal.")
        print("  Le 1H reste le timeframe optimal pour ce broker.")
    print()


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print()
    print("=" * 76)
    print("  🔬  THE FRONTIER TEST — 30M EQUILIBRIUM BACKTEST")
    print(f"  24 assets × 500+ candles × 30M | SL {SL_ATR_MULT}×ATR | TP {TP_RR}R | Spread Guard ON")
    print("=" * 76)
    print()

    client = CapitalClient()
    if not client.available:
        print("❌ Capital.com non connecté")
        sys.exit(1)

    print(f"  ✅ Capital.com connecté")
    print(f"  📊 {len(INSTRUMENTS)} assets: {', '.join(INSTRUMENTS[:6])}...")
    print(f"  💰 Capital initial: {INITIAL_BALANCE:,.2f}€")
    print()

    results = []
    balance = INITIAL_BALANCE

    for idx, epic in enumerate(INSTRUMENTS, 1):
        print(f"  [{idx:>2}/{len(INSTRUMENTS)}] {epic:<12}", end="", flush=True)

        df = fetch_candles(client, epic)
        if df.empty:
            print(f"⚠️ no data")
            results.append({
                "trades": 0, "epic": epic, "balance": balance,
                "spread_filtered": 0, "strat": STRAT_MAP.get(epic, "?"),
            })
            continue

        span_h = (df.index[-1] - df.index[0]).total_seconds() / 3600
        result = run_backtest(df, epic, balance)
        results.append(result)

        t = result["trades"]
        if t > 0:
            icon = "🟢" if result["pnl_net"] >= 0 else "🔴"
            print(f" {len(df):>3} candles ({span_h:.0f}h) │ {t:>3} trades WR {result['wr']:.0f}% │ {icon} {result['pnl_net']:>+8,.0f}€ │ PF {result.get('profit_factor',0):.2f}")
        else:
            f = result.get('spread_filtered', 0)
            print(f" {len(df):>3} candles ({span_h:.0f}h) │   0 trades" + (f" (🛡️ {f} filtered)" if f else ""))

    print_tear_sheet(results)
