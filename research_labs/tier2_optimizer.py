#!/usr/bin/env python3
"""
tier2_optimizer.py — 🔬 Tier 2 Hyperparameter Grid Search

KEY OPTIMIZATION: Pre-compute ALL signals once per instrument, then replay
the signal stream with different RR/TS/Threshold combos. This avoids
recomputing indicators 80 times and makes the grid search 50x faster.
"""

import os, sys, time, warnings
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple
from itertools import product

import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="ERROR")

from strategy import Strategy, SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD
from brokers.capital_client import (
    ASSET_PROFILES, get_asset_class,
    FRIDAY_KILLSWITCH_HOUR, FRIDAY_KILLSWITCH_MINUTE,
)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
TIER2 = {
    "GOLD":   {"ticker": "GC=F",      "profile_key": "GOLD"},
    "US500":  {"ticker": "^GSPC",     "profile_key": "US500"},
    "GBPUSD": {"ticker": "GBPUSD=X", "profile_key": "GBPUSD"},
}

THRESHOLDS = [0.45, 0.50, 0.55, 0.60]
RR_MINS    = [0.8, 1.0, 1.2, 1.5, 2.0]
TIME_STOPS = [8, 12, 24, 48]

MAKER_TAKER_FEE = 0.001
SLIPPAGE_PCT    = 0.0005
INITIAL_CAPITAL = 10_000.0
RISK_PER_TRADE  = 0.005
YEARS = 2

# ═══════════════════════════════════════════════════════════════════════════════
# DATA DOWNLOAD
# ═══════════════════════════════════════════════════════════════════════════════

def download_data(ticker: str) -> Optional[pd.DataFrame]:
    import yfinance as yf
    end = datetime.now()
    start = end - timedelta(days=365 * YEARS)
    all_dfs = []
    chunk_days = 720
    cur = end
    while cur > start:
        cs = max(start, cur - timedelta(days=chunk_days))
        df = yf.download(ticker, start=cs, end=cur, interval="1h", progress=False, auto_adjust=True)
        if df is not None and not df.empty:
            all_dfs.append(df)
        cur = cs - timedelta(hours=1)
    if not all_dfs:
        return None
    df = pd.concat(all_dfs)
    df = df[~df.index.duplicated(keep='first')].sort_index()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated()]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# PRE-COMPUTE: extract all signals once
# ═══════════════════════════════════════════════════════════════════════════════

def precompute_signals(symbol: str, df_ind: pd.DataFrame, profile: dict) -> list:
    """
    Walk through the entire dataset and extract (timestamp, OHLC, signal, score,
    session_range) for every bar. This is the expensive part — done ONCE.
    """
    strat = Strategy()
    lookback = 300
    signals = []

    for i in range(lookback, len(df_ind)):
        window = df_ind.iloc[max(0, i - lookback):i + 1]
        if len(window) < 30:
            continue

        curr = window.iloc[-1]
        ts = window.index[-1]
        o, h, l, c = float(curr["open"]), float(curr["high"]), float(curr["low"]), float(curr["close"])
        sig, score, confs = strat.get_signal(window, symbol=symbol, asset_profile=profile)

        sr = None
        if sig in (SIGNAL_BUY, SIGNAL_SELL):
            sr = strat.compute_session_range(window, range_lookback=profile.get("range_lb", 4))

        signals.append((ts, o, h, l, c, sig, score, sr))

    return signals


# ═══════════════════════════════════════════════════════════════════════════════
# FAST REPLAY: replay pre-computed signals with different params
# ═══════════════════════════════════════════════════════════════════════════════

def replay_signals(symbol: str, signals: list, profile: dict,
                   threshold: float, rr_min: float, time_stop_h: float) -> dict:
    """
    Replay pre-computed signals with specific hyperparams. Ultra-fast (no strategy calls).
    """
    capital = INITIAL_CAPITAL
    peak = capital
    max_dd = 0.0
    trade = None  # (direction, entry, sl, tp, size, entry_time)
    wins = 0
    losses = 0
    total_pnl = 0.0
    total_fees = 0.0
    n_trades = 0
    sum_win_pnl = 0.0
    sum_loss_pnl = 0.0
    friday_kills = 0

    for ts, o, h, l, c, sig, score, sr in signals:
        # ── Update existing trade ──
        if trade is not None:
            t_dir, t_entry, t_sl, t_tp, t_size, t_time = trade
            age_h = (ts - t_time).total_seconds() / 3600
            closed = False
            exit_p = 0.0
            reason = ""

            # SL/TP
            if t_dir == "BUY":
                if l <= t_sl:
                    exit_p, reason, closed = t_sl * (1 - SLIPPAGE_PCT), "SL", True
                elif h >= t_tp:
                    exit_p, reason, closed = t_tp * (1 - SLIPPAGE_PCT), "TP", True
            else:
                if h >= t_sl:
                    exit_p, reason, closed = t_sl * (1 + SLIPPAGE_PCT), "SL", True
                elif l <= t_tp:
                    exit_p, reason, closed = t_tp * (1 + SLIPPAGE_PCT), "TP", True

            # Time Stop
            if not closed and age_h > time_stop_h:
                exit_p = c * (1 + SLIPPAGE_PCT) if t_dir == "SELL" else c * (1 - SLIPPAGE_PCT)
                reason, closed = "TIME_STOP", True

            # Friday Kill-Switch
            if not closed and hasattr(ts, 'weekday'):
                if (ts.weekday() == 4 and
                    (ts.hour > FRIDAY_KILLSWITCH_HOUR or
                     (ts.hour == FRIDAY_KILLSWITCH_HOUR and ts.minute >= FRIDAY_KILLSWITCH_MINUTE))):
                    exit_p = c * (1 + SLIPPAGE_PCT) if t_dir == "SELL" else c * (1 - SLIPPAGE_PCT)
                    reason, closed = "FRIDAY_KILL", True
                    friday_kills += 1

            if closed:
                if t_dir == "BUY":
                    pnl = (exit_p - t_entry) * t_size
                else:
                    pnl = (t_entry - exit_p) * t_size
                fees = (t_size * t_entry + t_size * exit_p) * MAKER_TAKER_FEE
                capital += pnl - fees
                total_pnl += pnl
                total_fees += fees
                n_trades += 1
                if pnl > 0:
                    wins += 1
                    sum_win_pnl += pnl
                else:
                    losses += 1
                    sum_loss_pnl += abs(pnl)
                trade = None
                peak = max(peak, capital)
                dd = (capital - peak) / peak * 100 if peak > 0 else 0
                max_dd = min(max_dd, dd)

        # ── Open new trade ──
        if trade is None and score >= threshold and sig in (SIGNAL_BUY, SIGNAL_SELL):
            direction = "BUY" if sig == SIGNAL_BUY else "SELL"
            if sr is None or sr.get("size", 0) <= 0:
                continue

            raw_entry = c
            sl_dist = sr["size"] * profile.get("sl_buffer", 0.12)
            if direction == "BUY":
                sl = raw_entry - sl_dist
                tp = raw_entry + sr["size"] * profile.get("tp1", 1.5)
            else:
                sl = raw_entry + sl_dist
                tp = raw_entry - sr["size"] * profile.get("tp1", 1.5)

            risk = abs(raw_entry - sl)
            if risk <= 0:
                continue
            if abs(tp - raw_entry) / risk < rr_min:
                if direction == "BUY":
                    tp = raw_entry + risk * rr_min
                else:
                    tp = raw_entry - risk * rr_min

            entry = raw_entry * (1 + SLIPPAGE_PCT) if direction == "BUY" else raw_entry * (1 - SLIPPAGE_PCT)
            risk_per_unit = abs(entry - sl)
            if risk_per_unit <= 0:
                continue
            size = (capital * RISK_PER_TRADE) / risk_per_unit
            trade = (direction, entry, sl, tp, size, ts)

    # Close remaining
    if trade is not None:
        t_dir, t_entry, t_sl, t_tp, t_size, t_time = trade
        last_c = signals[-1][4]
        if t_dir == "BUY":
            pnl = (last_c - t_entry) * t_size
        else:
            pnl = (t_entry - last_c) * t_size
        fees = (t_size * t_entry + t_size * last_c) * MAKER_TAKER_FEE
        capital += pnl - fees
        total_pnl += pnl
        total_fees += fees
        n_trades += 1
        if pnl > 0:
            wins += 1
        else:
            losses += 1

    wr = wins / n_trades * 100 if n_trades > 0 else 0
    avg_win = sum_win_pnl / wins if wins > 0 else 0
    avg_loss = sum_loss_pnl / losses if losses > 0 else 1
    avg_rr = avg_win / avg_loss if avg_loss > 0 else 0
    pf = sum_win_pnl / sum_loss_pnl if sum_loss_pnl > 0 else 0
    pnl_net = total_pnl - total_fees

    return {
        "symbol": symbol, "threshold": threshold,
        "rr_min": rr_min, "time_stop_h": time_stop_h,
        "pnl_net": round(pnl_net, 2), "pnl_brut": round(total_pnl, 2),
        "fees": round(total_fees, 2), "trades": n_trades,
        "win_rate": round(wr, 1), "avg_rr": round(avg_rr, 2),
        "profit_factor": round(pf, 2), "max_dd": round(max_dd, 2),
        "final_capital": round(capital, 2), "friday_kills": friday_kills,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    t0 = time.time()
    combos = len(THRESHOLDS) * len(RR_MINS) * len(TIME_STOPS)
    total = combos * len(TIER2)

    print()
    print("═" * 70)
    print("  🔬 TIER 2 HYPERPARAMETER GRID SEARCH (v2 — pré-calcul)")
    print(f"  Actifs : {list(TIER2.keys())}")
    print(f"  Grid : {len(THRESHOLDS)}×{len(RR_MINS)}×{len(TIME_STOPS)} = {combos} combos/actif")
    print(f"  Total : {total} replays (signaux pré-calculés)")
    print("═" * 70)

    # 1. Download + compute indicators + extract signals
    print("\n📥 Phase 1 : Données + Indicateurs + Extraction des signaux")
    all_signals = {}

    for symbol, info in TIER2.items():
        ticker = info["ticker"]
        profile = ASSET_PROFILES.get(info["profile_key"], {})

        print(f"  ⬇️  {symbol} ({ticker})...", end=" ", flush=True)
        df = download_data(ticker)
        if df is None or len(df) < 400:
            print("❌ données insuffisantes")
            continue
        print(f"✅ {len(df)} bougies")

        print(f"  ⚙️  Indicateurs {symbol}...", end=" ", flush=True)
        strat = Strategy()
        df_ind = strat.compute_indicators(df.copy())
        print(f"✅ {len(df_ind)} barres")

        print(f"  🔍 Extraction signaux {symbol}...", end=" ", flush=True)
        sigs = precompute_signals(symbol, df_ind, profile)
        n_valid = sum(1 for s in sigs if s[5] in (SIGNAL_BUY, SIGNAL_SELL))
        print(f"✅ {len(sigs)} barres, {n_valid} signaux BUY/SELL détectés")

        all_signals[symbol] = (sigs, profile)

    if not all_signals:
        print("❌ Aucune donnée — abandon")
        return

    # 2. Grid search (FAST — replay only)
    print(f"\n🚀 Phase 2 : Grid Search ({total} replays)...")
    results = []
    done = 0

    for symbol, (sigs, profile) in all_signals.items():
        for threshold, rr_min, ts_h in product(THRESHOLDS, RR_MINS, TIME_STOPS):
            r = replay_signals(symbol, sigs, profile, threshold, rr_min, ts_h)
            results.append(r)
            done += 1
            if done % 20 == 0:
                print(f"  ⏳ {done}/{total} ({done*100//total}%)...", flush=True)

    elapsed = time.time() - t0
    print(f"  ✅ {done}/{total} terminés en {elapsed:.0f}s")

    # 3. Report
    print("\n")
    print("═" * 70)
    print("  🏆 TIER 2 CALIBRATION REPORT")
    print("═" * 70)

    for symbol in all_signals:
        sr = sorted([r for r in results if r["symbol"] == symbol],
                    key=lambda x: (x["pnl_net"], x["win_rate"]), reverse=True)

        profitable = [r for r in sr if r["pnl_net"] > 0]
        cls = get_asset_class(symbol)

        print(f"\n{'─' * 70}")
        print(f"  📊 {symbol} ({cls}) — {len(sr)} combinaisons testées")
        print(f"{'─' * 70}")
        print(f"  Rentables : {len(profitable)}/{len(sr)} ({len(profitable)*100//len(sr)}%)")

        best = sr[0]
        print(f"\n  🥇 MEILLEURE COMBINAISON :")
        print(f"  ┌──────────────────────────────────────────────┐")
        print(f"  │  Threshold  = {best['threshold']:<6.2f}                       │")
        print(f"  │  R:R min    = {best['rr_min']:<6.1f}                       │")
        print(f"  │  Time Stop  = {best['time_stop_h']:<3}h                          │")
        print(f"  ├──────────────────────────────────────────────┤")
        print(f"  │  PnL Net    = {best['pnl_net']:>+10,.2f}€                 │")
        print(f"  │  PnL Brut   = {best['pnl_brut']:>+10,.2f}€                 │")
        print(f"  │  Frais      = {best['fees']:>10,.2f}€                 │")
        print(f"  │  Trades     = {best['trades']:>4}                          │")
        print(f"  │  Win Rate   = {best['win_rate']:>5.1f}%                       │")
        print(f"  │  Avg R:R    = {best['avg_rr']:>5.2f}x                        │")
        print(f"  │  P. Factor  = {best['profit_factor']:>5.2f}                         │")
        print(f"  │  Max DD     = {best['max_dd']:>7.2f}%                      │")
        icon = "🟢" if best['pnl_net'] > 0 else "🔴"
        verd = "RENTABLE" if best['pnl_net'] > 0 else "NON RENTABLE"
        print(f"  │  {icon} VERDICT  = {verd:<22}        │")
        print(f"  └──────────────────────────────────────────────┘")

        print(f"\n  📋 TOP 5 :")
        print(f"  {'#':<3} {'Th':>5} {'RR':>5} {'TS':>4} {'PnL Net':>10} {'Trades':>7} {'WR%':>6} {'MaxDD':>8} {'PF':>5}")
        print(f"  {'─'*3} {'─'*5} {'─'*5} {'─'*4} {'─'*10} {'─'*7} {'─'*6} {'─'*8} {'─'*5}")
        for i, r in enumerate(sr[:5], 1):
            ic = "🟢" if r["pnl_net"] > 0 else "🔴"
            print(f"  {ic}{i:<2} {r['threshold']:>5.2f} {r['rr_min']:>5.1f} {r['time_stop_h']:>3}h "
                  f"{r['pnl_net']:>+9.0f}€ {r['trades']:>7} {r['win_rate']:>5.1f}% "
                  f"{r['max_dd']:>7.2f}% {r['profit_factor']:>5.2f}")

    # Global summary
    print(f"\n{'═' * 70}")
    print(f"  📊 RÉSUMÉ GLOBAL")
    print(f"  Temps : {elapsed:.0f}s | Backtests : {len(results)}")
    print(f"  Rentables : {sum(1 for r in results if r['pnl_net'] > 0)}/{len(results)}")
    print(f"\n  🎯 RÉGLAGES OPTIMAUX TIER 2 :")
    print(f"  {'Actif':<10} {'Threshold':>10} {'R:R':>6} {'TS(h)':>7} {'PnL Net':>10} {'WR%':>6} {'MaxDD':>8}")
    print(f"  {'─'*10} {'─'*10} {'─'*6} {'─'*7} {'─'*10} {'─'*6} {'─'*8}")
    for sym in all_signals:
        b = sorted([r for r in results if r["symbol"] == sym],
                   key=lambda x: x["pnl_net"], reverse=True)[0]
        ic = "🟢" if b["pnl_net"] > 0 else "🔴"
        print(f"  {ic} {sym:<8} {b['threshold']:>10.2f} {b['rr_min']:>6.1f} {b['time_stop_h']:>7} "
              f"{b['pnl_net']:>+9.0f}€ {b['win_rate']:>5.1f}% {b['max_dd']:>7.2f}%")
    print(f"{'═' * 70}\n")


if __name__ == "__main__":
    main()
