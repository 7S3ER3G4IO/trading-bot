#!/usr/bin/env python3
"""
black_ops_factory.py — 🕶️ THE BLACK OPS LABORATORY
Last-chance brute-force for the 22 "UNTRADEABLE" assets using
non-directional strategies that THRIVE on noise and ranges.

MOTEUR 50: GRID TRADING — Captures micro-profits from zigzag noise.
MOTEUR 51: STAT EXTREMES — Z-score > 3.0 flash crash sniper.

Tests each asset independently with 10K€ isolated capital.
"""

import os, sys, json, time, warnings
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loguru import logger
logger.remove()

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

YF_MAP = {
    "EURUSD": "EURUSD=X", "USDJPY": "JPY=X", "GBPUSD": "GBPUSD=X",
    "USDCHF": "CHF=X", "AUDNZD": "AUDNZD=X", "AUDJPY": "AUDJPY=X",
    "NZDJPY": "NZDJPY=X", "EURCHF": "EURCHF=X", "CHFJPY": "CHFJPY=X",
    "FR40": "^FCHI", "COPPER": "HG=F",
    "US500": "^GSPC", "US100": "^NDX", "US30": "^DJI",
    "NVDA": "NVDA", "AUDUSD": "AUDUSD=X", "EURGBP": "EURGBP=X",
    "EURAUD": "EURAUD=X", "AUDCAD": "AUDCAD=X",
    "GBPCAD": "GBPCAD=X", "GBPCHF": "GBPCHF=X", "CADCHF": "CADCHF=X",
}

UNTRADEABLE = list(YF_MAP.keys())

# Search grid for each strategy
GRID_SPACINGS = [0.5, 0.75, 1.0]       # ATR multipliers for grid levels
GRID_TPS      = [0.5, 0.75, 1.0]       # ATR multiplier for TP per level
GRID_SLS      = [1.5, 2.0, 2.5]        # ATR multiplier for SL
ZSCORE_THRS   = [2.5, 3.0, 3.5]        # Z-score entry threshold
ZSCORE_WINDOWS = [50, 100, 200]         # Lookback window for Z-score
TIMEFRAMES    = ["1h", "4h"]

INITIAL_CAPITAL = 10_000.0
FEE_PCT         = 0.001
SLIP_PCT        = 0.0005
RISK_PER_TRADE  = 0.003    # Smaller risk for grid (more frequent trades)
MAX_DD_LIMIT    = -15.0
MIN_TRADES      = 5
YEARS           = 2

FRIDAY_KILL_H   = 20

# ═══════════════════════════════════════════════════════════════════════════════
# DATA DOWNLOAD (reuse from alpha_factory)
# ═══════════════════════════════════════════════════════════════════════════════

def download(ticker: str, interval: str = "1h") -> Optional[pd.DataFrame]:
    import yfinance as yf
    end = datetime.now()
    start = end - timedelta(days=365 * YEARS)

    dl_interval = "1h" if interval in ("1h", "4h") else interval
    all_dfs = []
    cur = end
    while cur > start:
        cs = max(start, cur - timedelta(days=720))
        try:
            df = yf.download(ticker, start=cs, end=cur, interval=dl_interval,
                           progress=False, auto_adjust=True)
            if df is not None and not df.empty:
                all_dfs.append(df)
        except Exception:
            pass
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

    if interval == "4h":
        df = df.resample("4h").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"
        }).dropna()

    return df


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ═══════════════════════════════════════════════════════════════════════════════
# M50: GRID TRADING ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def backtest_grid(df: pd.DataFrame, spacing_mult: float, tp_mult: float,
                  sl_mult: float, is_tradfi: bool) -> dict:
    """
    Grid Trading: places buy/sell orders at ATR intervals.
    When price drops to -spacing*ATR → BUY with TP at +tp*ATR
    When price rises to +spacing*ATR → SELL with TP at -tp*ATR
    SL at sl_mult*ATR from entry.
    """
    atr_series = compute_atr(df)
    capital = INITIAL_CAPITAL
    peak = capital
    max_dd = 0.0
    trades = []
    wins = losses = 0
    total_pnl = total_fees = 0.0
    sum_win = sum_loss = 0.0
    n_trades = 0

    # Track open grid positions (max 2 at a time)
    open_pos = []  # list of (direction, entry, sl, tp, size, bar_idx)
    last_trade_bar = -10  # cooldown: min 3 bars between new entries

    for i in range(20, len(df)):
        row = df.iloc[i]
        c = float(row["close"])
        h = float(row["high"])
        l = float(row["low"])
        atr = float(atr_series.iloc[i]) if not pd.isna(atr_series.iloc[i]) else 0
        ts = df.index[i]

        if atr <= 0 or capital <= 0:
            continue

        # Friday Kill for TradFi
        if is_tradfi and hasattr(ts, 'weekday') and ts.weekday() == 4 and ts.hour >= FRIDAY_KILL_H:
            for pos in open_pos:
                d, ent, sl, tp, sz, bi = pos
                pnl = (c - ent) * sz if d == "BUY" else (ent - c) * sz
                fees = (sz * ent + sz * c) * FEE_PCT
                capital += pnl - fees
                total_pnl += pnl; total_fees += fees; n_trades += 1
                if pnl > 0: wins += 1; sum_win += pnl
                else: losses += 1; sum_loss += abs(pnl)
            open_pos = []
            continue

        # ── Manage open positions ──
        closed_indices = []
        for j, pos in enumerate(open_pos):
            d, ent, sl, tp, sz, bi = pos
            closed = False
            exit_p = None

            if d == "BUY":
                if l <= sl: exit_p = sl * (1 - SLIP_PCT)
                elif h >= tp: exit_p = tp * (1 - SLIP_PCT)
            else:
                if h >= sl: exit_p = sl * (1 + SLIP_PCT)
                elif l <= tp: exit_p = tp * (1 + SLIP_PCT)

            if exit_p is not None:
                pnl = (exit_p - ent) * sz if d == "BUY" else (ent - exit_p) * sz
                fees = (sz * ent + sz * exit_p) * FEE_PCT
                capital += pnl - fees
                total_pnl += pnl; total_fees += fees; n_trades += 1
                if pnl > 0: wins += 1; sum_win += pnl
                else: losses += 1; sum_loss += abs(pnl)
                peak = max(peak, capital)
                dd = (capital - peak) / peak * 100 if peak > 0 else -100
                max_dd = min(max_dd, dd)
                closed_indices.append(j)

        for j in reversed(closed_indices):
            open_pos.pop(j)

        if capital <= 0:
            break

        # ── Open new grid positions ──
        if len(open_pos) < 2 and (i - last_trade_bar) >= 3:
            prev_c = float(df.iloc[i-1]["close"])
            move = c - prev_c

            # Price dropped → BUY (catching the dip in the range)
            if move < -spacing_mult * atr * 0.5:
                entry = c * (1 + SLIP_PCT)
                sl = entry - sl_mult * atr
                tp = entry + tp_mult * atr
                risk_u = abs(entry - sl)
                if risk_u > 0:
                    sz = (capital * RISK_PER_TRADE) / risk_u
                    open_pos.append(("BUY", entry, sl, tp, sz, i))
                    last_trade_bar = i

            # Price rose → SELL (catching the spike in the range)
            elif move > spacing_mult * atr * 0.5:
                entry = c * (1 - SLIP_PCT)
                sl = entry + sl_mult * atr
                tp = entry - tp_mult * atr
                risk_u = abs(entry - sl)
                if risk_u > 0:
                    sz = (capital * RISK_PER_TRADE) / risk_u
                    open_pos.append(("SELL", entry, sl, tp, sz, i))
                    last_trade_bar = i

    # Close remaining
    if open_pos and len(df) > 0:
        last_c = float(df.iloc[-1]["close"])
        for pos in open_pos:
            d, ent, sl, tp, sz, bi = pos
            pnl = (last_c - ent) * sz if d == "BUY" else (ent - last_c) * sz
            fees = (sz * ent + sz * last_c) * FEE_PCT
            capital += pnl - fees
            total_pnl += pnl; total_fees += fees; n_trades += 1
            if pnl > 0: wins += 1
            else: losses += 1

    pnl_net = total_pnl - total_fees
    wr = wins / n_trades * 100 if n_trades > 0 else 0
    pf = sum_win / sum_loss if sum_loss > 0 else 0

    return {
        "pnl_net": round(pnl_net, 2), "trades": n_trades,
        "win_rate": round(wr, 1), "max_dd": round(max_dd, 2),
        "profit_factor": round(pf, 2), "fees": round(total_fees, 2),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# M51: STATISTICAL EXTREMES ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def backtest_stat_extreme(df: pd.DataFrame, z_threshold: float,
                          z_window: int, is_tradfi: bool) -> dict:
    """
    Statistical Extremes: only trades when Z-score exceeds threshold.
    BUY on extreme negative Z (flash crash), SELL on extreme positive Z.
    TP: mean reversion to Z=0. SL: Z extends further (1.5x threshold).
    """
    closes = df["close"].astype(float)
    atr_series = compute_atr(df)

    capital = INITIAL_CAPITAL
    peak = capital
    max_dd = 0.0
    trade = None
    wins = losses = 0
    total_pnl = total_fees = 0.0
    sum_win = sum_loss = 0.0
    n_trades = 0

    for i in range(z_window + 5, len(df)):
        row = df.iloc[i]
        c = float(row["close"])
        h = float(row["high"])
        l = float(row["low"])
        atr = float(atr_series.iloc[i]) if not pd.isna(atr_series.iloc[i]) else 0
        ts = df.index[i]

        if atr <= 0 or capital <= 0:
            continue

        # Z-score calculation
        window = closes.iloc[i - z_window:i]
        mu = window.mean()
        sigma = window.std()
        if sigma <= 0:
            continue
        zscore = (c - mu) / sigma

        # Friday Kill
        if is_tradfi and trade and hasattr(ts, 'weekday') and ts.weekday() == 4 and ts.hour >= FRIDAY_KILL_H:
            d, ent, sl, tp, sz, bt = trade
            pnl = (c - ent) * sz if d == "BUY" else (ent - c) * sz
            fees = (sz * ent + sz * c) * FEE_PCT
            capital += pnl - fees
            total_pnl += pnl; total_fees += fees; n_trades += 1
            if pnl > 0: wins += 1; sum_win += pnl
            else: losses += 1; sum_loss += abs(pnl)
            trade = None
            continue

        # ── Manage open trade ──
        if trade is not None:
            d, ent, sl, tp, sz, bt = trade
            exit_p = None

            if d == "BUY":
                if l <= sl: exit_p = sl * (1 - SLIP_PCT)
                elif h >= tp: exit_p = tp * (1 - SLIP_PCT)
                elif zscore >= 0:  # Mean reverted!
                    exit_p = c * (1 - SLIP_PCT)
            else:
                if h >= sl: exit_p = sl * (1 + SLIP_PCT)
                elif l <= tp: exit_p = tp * (1 + SLIP_PCT)
                elif zscore <= 0:  # Mean reverted!
                    exit_p = c * (1 + SLIP_PCT)

            # Time stop: 72h max
            if exit_p is None:
                age_h = (ts - bt).total_seconds() / 3600
                if age_h > 72:
                    exit_p = c * (1 + SLIP_PCT) if d == "SELL" else c * (1 - SLIP_PCT)

            if exit_p is not None:
                pnl = (exit_p - ent) * sz if d == "BUY" else (ent - exit_p) * sz
                fees = (sz * ent + sz * exit_p) * FEE_PCT
                capital += pnl - fees
                total_pnl += pnl; total_fees += fees; n_trades += 1
                if pnl > 0: wins += 1; sum_win += pnl
                else: losses += 1; sum_loss += abs(pnl)
                trade = None
                peak = max(peak, capital)
                dd = (capital - peak) / peak * 100 if peak > 0 else -100
                max_dd = min(max_dd, dd)

        # ── Open new position on extreme Z-score ──
        if trade is None and capital > 0:
            if zscore <= -z_threshold:
                # Extreme negative = flash crash → BUY
                entry = c * (1 + SLIP_PCT)
                sl = entry - atr * 3.0   # Wide SL for extremes
                tp = mu                   # TP = mean price
                risk_u = abs(entry - sl)
                if risk_u > 0 and tp > entry:
                    sz = (capital * RISK_PER_TRADE) / risk_u
                    trade = ("BUY", entry, sl, tp, sz, ts)

            elif zscore >= z_threshold:
                # Extreme positive = blow-off top → SELL
                entry = c * (1 - SLIP_PCT)
                sl = entry + atr * 3.0
                tp = mu
                risk_u = abs(entry - sl)
                if risk_u > 0 and tp < entry:
                    sz = (capital * RISK_PER_TRADE) / risk_u
                    trade = ("SELL", entry, sl, tp, sz, ts)

    # Close remaining
    if trade is not None and len(df) > 0:
        d, ent, sl, tp, sz, bt = trade
        last_c = float(df.iloc[-1]["close"])
        pnl = (last_c - ent) * sz if d == "BUY" else (ent - last_c) * sz
        fees = (sz * ent + sz * last_c) * FEE_PCT
        capital += pnl - fees; total_pnl += pnl; total_fees += fees; n_trades += 1
        if pnl > 0: wins += 1
        else: losses += 1

    pnl_net = total_pnl - total_fees
    wr = wins / n_trades * 100 if n_trades > 0 else 0
    pf = sum_win / sum_loss if sum_loss > 0 else 0

    return {
        "pnl_net": round(pnl_net, 2), "trades": n_trades,
        "win_rate": round(wr, 1), "max_dd": round(max_dd, 2),
        "profit_factor": round(pf, 2), "fees": round(total_fees, 2),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN — THE BLACK OPS LABORATORY
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    t0 = time.time()

    # Grid combos: 3 spacings × 3 TPs × 3 SLs × 2 TFs = 54
    # Stat combos: 3 Z-thresholds × 3 windows × 2 TFs = 18
    # Total per asset: 72 combos

    print()
    print("═" * 70)
    print("  🕶️  THE BLACK OPS LABORATORY — Non-Directional Warfare")
    print(f"  Targets : {len(UNTRADEABLE)} 'UNTRADEABLE' assets")
    print(f"  M50 GRID: {len(GRID_SPACINGS)}×{len(GRID_TPS)}×{len(GRID_SLS)}×{len(TIMEFRAMES)} = "
          f"{len(GRID_SPACINGS)*len(GRID_TPS)*len(GRID_SLS)*len(TIMEFRAMES)} combos")
    print(f"  M51 STAT: {len(ZSCORE_THRS)}×{len(ZSCORE_WINDOWS)}×{len(TIMEFRAMES)} = "
          f"{len(ZSCORE_THRS)*len(ZSCORE_WINDOWS)*len(TIMEFRAMES)} combos")
    print(f"  Fitness : PnL > 0€ AND MaxDD > {MAX_DD_LIMIT}% AND trades ≥ {MIN_TRADES}")
    print("═" * 70)

    rescued = {}
    still_dead = []

    for idx, symbol in enumerate(UNTRADEABLE, 1):
        ticker = YF_MAP[symbol]
        is_tradfi = symbol not in ("BTCUSD", "ETHUSD")  # all are TradFi here

        print(f"\n{'─' * 70}")
        print(f"  🕶️  [{idx}/{len(UNTRADEABLE)}] {symbol} ({ticker})")
        print(f"{'─' * 70}")

        # Download data
        data_by_tf = {}
        for tf in TIMEFRAMES:
            df = download(ticker, tf)
            if df is not None and len(df) > 100:
                data_by_tf[tf] = df
                print(f"    📥 {tf}: {len(df)} bougies ✅")
            else:
                print(f"    📥 {tf}: ❌")

        if not data_by_tf:
            print(f"  💀 {symbol}: AUCUNE donnée → DEAD")
            still_dead.append(symbol)
            continue

        found = False
        best_result = None
        best_config = None
        combos_tested = 0

        # ── M50: GRID TRADING ──
        for tf, df in data_by_tf.items():
            if found: break
            for spacing in GRID_SPACINGS:
                if found: break
                for tp_m in GRID_TPS:
                    if found: break
                    for sl_m in GRID_SLS:
                        combos_tested += 1
                        r = backtest_grid(df, spacing, tp_m, sl_m, is_tradfi)

                        if r["pnl_net"] > 0 and r["max_dd"] > MAX_DD_LIMIT and r["trades"] >= MIN_TRADES:
                            found = True
                            best_result = r
                            best_config = {
                                "engine": "M50_GRID", "tf": tf,
                                "grid_spacing": spacing,
                                "tp_mult": tp_m, "sl_mult": sl_m
                            }
                            break

                        if best_result is None or r["pnl_net"] > best_result.get("pnl_net", -99999):
                            best_result = r
                            best_config = {
                                "engine": "M50_GRID", "tf": tf,
                                "grid_spacing": spacing,
                                "tp_mult": tp_m, "sl_mult": sl_m
                            }

        # ── M51: STATISTICAL EXTREMES ──
        if not found:
            for tf, df in data_by_tf.items():
                if found: break
                for z_thr in ZSCORE_THRS:
                    if found: break
                    for z_win in ZSCORE_WINDOWS:
                        combos_tested += 1
                        r = backtest_stat_extreme(df, z_thr, z_win, is_tradfi)

                        if r["pnl_net"] > 0 and r["max_dd"] > MAX_DD_LIMIT and r["trades"] >= MIN_TRADES:
                            found = True
                            best_result = r
                            best_config = {
                                "engine": "M51_STAT", "tf": tf,
                                "z_threshold": z_thr,
                                "z_window": z_win
                            }
                            break

                        if r["pnl_net"] > best_result.get("pnl_net", -99999):
                            best_result = r
                            best_config = {
                                "engine": "M51_STAT", "tf": tf,
                                "z_threshold": z_thr,
                                "z_window": z_win
                            }

        if found:
            c = best_config
            r = best_result
            eng = c["engine"]
            if eng == "M50_GRID":
                detail = f"sp={c['grid_spacing']} tp={c['tp_mult']} sl={c['sl_mult']}"
            else:
                detail = f"Z≥{c['z_threshold']} win={c['z_window']}"
            print(f"  ✅ RESCUED ! {eng}/{c['tf']} {detail} → "
                  f"PnL={r['pnl_net']:+,.0f}€ WR={r['win_rate']:.0f}% "
                  f"DD={r['max_dd']:.1f}% ({r['trades']} trades) "
                  f"[{combos_tested} combos]")
            rescued[symbol] = {
                **best_config, **{k: v for k, v in best_result.items() if k != "exit_reasons"},
            }
        else:
            c = best_config or {}
            r = best_result or {"pnl_net": 0}
            print(f"  💀 CONFIRMED DEAD ({combos_tested} combos) "
                  f"| Best: {c.get('engine','?')}/{c.get('tf','?')} → "
                  f"PnL={r.get('pnl_net',0):+,.0f}€")
            still_dead.append(symbol)

    elapsed = time.time() - t0

    # Export
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "black_ops_rules.json")
    with open(out_path, "w") as f:
        json.dump(rescued, f, indent=2)

    # Report
    print()
    print("═" * 70)
    print("  🕶️  BLACK OPS LABORATORY — FINAL REPORT")
    print("═" * 70)
    print(f"\n  ⏱️  Time: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"  🎯 Targets : {len(UNTRADEABLE)}")
    print(f"  ✅ Rescued  : {len(rescued)}")
    print(f"  💀 Dead     : {len(still_dead)}")

    if rescued:
        total_pnl = sum(v["pnl_net"] for v in rescued.values())
        print(f"  💰 PnL combiné : {total_pnl:+,.0f}€")

        print(f"\n  {'─' * 66}")
        print(f"  {'Asset':<10} {'Engine':>10} {'TF':>4} {'Config':>20} "
              f"{'PnL':>9} {'WR%':>6} {'DD%':>7} {'Trades':>7} {'PF':>5}")
        print(f"  {'─'*10} {'─'*10} {'─'*4} {'─'*20} "
              f"{'─'*9} {'─'*6} {'─'*7} {'─'*7} {'─'*5}")

        for sym in sorted(rescued, key=lambda s: rescued[s]["pnl_net"], reverse=True):
            v = rescued[sym]
            if v["engine"] == "M50_GRID":
                cfg = f"sp={v['grid_spacing']} tp={v['tp_mult']} sl={v['sl_mult']}"
            else:
                cfg = f"Z≥{v['z_threshold']} w={v['z_window']}"
            print(f"  🟢 {sym:<8} {v['engine']:>10} {v['tf']:>4} {cfg:>20} "
                  f"{v['pnl_net']:>+8,.0f}€ {v['win_rate']:>5.1f}% "
                  f"{v['max_dd']:>6.1f}% {v['trades']:>7} {v['profit_factor']:>5.2f}")

    if still_dead:
        print(f"\n  💀 CONFIRMED DEAD ({len(still_dead)}):")
        print(f"  {', '.join(still_dead)}")

    print(f"\n  💾 Exported: {out_path}")
    print(f"{'═' * 70}\n")


if __name__ == "__main__":
    main()
