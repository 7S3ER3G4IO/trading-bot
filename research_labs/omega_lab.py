#!/usr/bin/env python3
"""
omega_lab.py — ⚡ PROTOCOL OMEGA
Final macro-economic assault on the last 8 dead assets.
Price-based analysis has FAILED. We switch to macro regime exploitation.

MOTEUR 54: CARRY TRADE — Weekly SMA200 trend + buy & hold with wide SL.
           Captures long-term interest rate differential via trend proxy.
MOTEUR 55: MACRO EVENT SNIPING — Time-based OCO breakout at fixed news hours.
           Blind to indicators. Only reacts to scheduled volatility spikes.

Only these 8 remain: GBPUSD, USDCHF, AUDUSD, EURGBP, EURAUD, AUDCAD, GBPCAD, CADCHF
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
    "GBPUSD": "GBPUSD=X", "USDCHF": "CHF=X", "AUDUSD": "AUDUSD=X",
    "EURGBP": "EURGBP=X", "EURAUD": "EURAUD=X", "AUDCAD": "AUDCAD=X",
    "GBPCAD": "GBPCAD=X", "CADCHF": "CADCHF=X",
}

DEAD_8 = list(YF_MAP.keys())

# Carry Trade grid (Weekly)
CARRY_SMA       = [100, 150, 200]      # SMA period on weekly
CARRY_SL_ATR    = [3.0, 5.0, 7.0]      # Wide SL in ATR multiples
CARRY_REENTRY   = [4, 8, 12]           # Bars to wait before re-entry after SL

# News Sniping grid (1h data, time-based)
NEWS_HOURS      = [8, 13, 14]           # London open, US pre-news, US news
NEWS_RANGE_BARS = [2, 4, 6]             # How many bars to calc range
NEWS_TP_MULT    = [0.5, 1.0, 1.5]       # TP as multiple of range
NEWS_SL_MULT    = [0.5, 0.75]           # SL as multiple of range
NEWS_TIMEOUT    = [2, 4, 8]             # Hours to hold before time stop

INITIAL_CAPITAL = 10_000.0
FEE_PCT         = 0.001
SLIP_PCT        = 0.0005
RISK_PER_TRADE  = 0.003
MAX_DD_LIMIT    = -15.0
MIN_TRADES      = 5
YEARS           = 2
FRIDAY_KILL_H   = 20

# ═══════════════════════════════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════════════════════════════

def download(ticker: str, interval: str) -> Optional[pd.DataFrame]:
    import yfinance as yf
    end = datetime.now()
    start = end - timedelta(days=365 * YEARS)

    if interval in ("1h", "4h"):
        dl_interval = "1h"
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
    else:
        df = yf.download(ticker, start=start, end=end, interval=interval,
                        progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None

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


def compute_atr(df, period=14):
    h, l, c = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ═══════════════════════════════════════════════════════════════════════════════
# M54: CARRY TRADE — Weekly trend following with wide stops
# ═══════════════════════════════════════════════════════════════════════════════

def backtest_carry(df_weekly: pd.DataFrame, sma_period: int,
                   sl_atr_mult: float, reentry_bars: int) -> dict:
    """
    Carry Trade: ride the weekly trend (SMA proxy for rate differential).
    BUY when price > SMA (positive carry assumed), SELL when price < SMA.
    Very wide SL (3-7x ATR). Hold for weeks/months.
    """
    c = df_weekly["close"].astype(float)
    sma = c.rolling(sma_period).mean()
    atr = compute_atr(df_weekly, 14)

    capital = INITIAL_CAPITAL
    peak = capital
    max_dd = 0.0
    trade = None
    wins = losses = 0
    total_pnl = total_fees = 0.0
    sum_win = sum_loss = 0.0
    n_trades = 0
    cooldown = 0

    start_idx = sma_period + 5
    if start_idx >= len(df_weekly):
        return {"pnl_net": -999, "trades": 0, "win_rate": 0, "max_dd": -100, "profit_factor": 0, "fees": 0}

    for i in range(start_idx, len(df_weekly)):
        row = df_weekly.iloc[i]
        price = float(row["close"])
        h_price = float(row["high"])
        l_price = float(row["low"])
        curr_atr = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 0
        curr_sma = float(sma.iloc[i]) if not pd.isna(sma.iloc[i]) else 0

        if curr_atr <= 0 or capital <= 0:
            continue

        if cooldown > 0:
            cooldown -= 1

        # Manage open trade
        if trade is not None:
            d, ent, sl, sz, bt = trade
            exit_p = None

            # SL hit
            if d == "BUY" and l_price <= sl:
                exit_p = sl * (1 - SLIP_PCT)
            elif d == "SELL" and h_price >= sl:
                exit_p = sl * (1 + SLIP_PCT)

            # Trend reversal: close when SMA crosses
            if d == "BUY" and price < curr_sma * 0.995:
                exit_p = price * (1 - SLIP_PCT)
            elif d == "SELL" and price > curr_sma * 1.005:
                exit_p = price * (1 + SLIP_PCT)

            if exit_p is not None:
                pnl = (exit_p - ent) * sz if d == "BUY" else (ent - exit_p) * sz
                fees = (sz * ent + sz * exit_p) * FEE_PCT
                capital += pnl - fees
                total_pnl += pnl; total_fees += fees; n_trades += 1
                if pnl > 0: wins += 1; sum_win += pnl
                else: losses += 1; sum_loss += abs(pnl)
                trade = None
                cooldown = reentry_bars
                peak = max(peak, capital)
                dd = (capital - peak) / peak * 100 if peak > 0 else -100
                max_dd = min(max_dd, dd)

        # Open new trade (follow the trend)
        if trade is None and cooldown <= 0 and capital > 0:
            if price > curr_sma * 1.005:
                # Above SMA → BUY (positive carry/trend)
                entry = price * (1 + SLIP_PCT)
                sl = entry - sl_atr_mult * curr_atr
                risk_u = abs(entry - sl)
                if risk_u > 0:
                    sz = (capital * RISK_PER_TRADE) / risk_u
                    trade = ("BUY", entry, sl, sz, df_weekly.index[i])

            elif price < curr_sma * 0.995:
                # Below SMA → SELL (negative carry/trend)
                entry = price * (1 - SLIP_PCT)
                sl = entry + sl_atr_mult * curr_atr
                risk_u = abs(entry - sl)
                if risk_u > 0:
                    sz = (capital * RISK_PER_TRADE) / risk_u
                    trade = ("SELL", entry, sl, sz, df_weekly.index[i])

    # Close remaining
    if trade is not None and len(df_weekly) > 0:
        d, ent, sl, sz, bt = trade
        last_c = float(df_weekly.iloc[-1]["close"])
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
# M55: MACRO EVENT SNIPING — Time-Based OCO Breakout
# ═══════════════════════════════════════════════════════════════════════════════

def backtest_news_snipe(df_1h: pd.DataFrame, trigger_hour: int,
                        range_bars: int, tp_mult: float, sl_mult: float,
                        timeout_h: int) -> dict:
    """
    News Sniping: at trigger_hour, calculate range of last N bars.
    Place virtual Buy Stop above high + buffer, Sell Stop below low - buffer.
    First breakout wins, hold for timeout_h hours max.
    """
    capital = INITIAL_CAPITAL
    peak = capital
    max_dd = 0.0
    trade = None
    wins = losses = 0
    total_pnl = total_fees = 0.0
    sum_win = sum_loss = 0.0
    n_trades = 0

    for i in range(range_bars + 2, len(df_1h)):
        row = df_1h.iloc[i]
        ts = df_1h.index[i]
        c = float(row["close"])
        h = float(row["high"])
        l = float(row["low"])

        if capital <= 0:
            break

        # Friday Kill
        if trade and hasattr(ts, 'weekday') and ts.weekday() == 4 and ts.hour >= FRIDAY_KILL_H:
            d, ent, sl, tp, sz, bt = trade
            pnl = (c - ent) * sz if d == "BUY" else (ent - c) * sz
            fees = (sz * ent + sz * c) * FEE_PCT
            capital += pnl - fees; total_pnl += pnl; total_fees += fees; n_trades += 1
            if pnl > 0: wins += 1; sum_win += pnl
            else: losses += 1; sum_loss += abs(pnl)
            trade = None
            continue

        # Manage open trade
        if trade is not None:
            d, ent, sl, tp, sz, bt = trade
            age_h = (ts - bt).total_seconds() / 3600
            exit_p = None

            if d == "BUY":
                if l <= sl: exit_p = sl * (1 - SLIP_PCT)
                elif h >= tp: exit_p = tp * (1 - SLIP_PCT)
            else:
                if h >= sl: exit_p = sl * (1 + SLIP_PCT)
                elif l <= tp: exit_p = tp * (1 + SLIP_PCT)

            # Time stop
            if exit_p is None and age_h >= timeout_h:
                exit_p = c

            if exit_p is not None:
                pnl = (exit_p - ent) * sz if d == "BUY" else (ent - exit_p) * sz
                fees = (sz * ent + sz * exit_p) * FEE_PCT
                capital += pnl - fees; total_pnl += pnl; total_fees += fees; n_trades += 1
                if pnl > 0: wins += 1; sum_win += pnl
                else: losses += 1; sum_loss += abs(pnl)
                trade = None
                peak = max(peak, capital)
                dd = (capital - peak) / peak * 100 if peak > 0 else -100
                max_dd = min(max_dd, dd)

        # Trigger: is this the snipe hour? (weekdays only)
        if trade is None and hasattr(ts, 'weekday') and ts.weekday() < 5:
            if ts.hour == trigger_hour:
                # Calculate range of last N bars
                window = df_1h.iloc[max(0, i - range_bars):i]
                if len(window) < range_bars:
                    continue

                range_high = float(window["high"].max())
                range_low = float(window["low"].min())
                range_size = range_high - range_low

                if range_size <= 0:
                    continue

                # The current bar's price action determines direction
                # If price broke above range → BUY, below → SELL
                if h > range_high:
                    # Breakout UP confirmed
                    entry = range_high * (1 + SLIP_PCT)
                    sl_price = entry - range_size * sl_mult
                    tp_price = entry + range_size * tp_mult
                    risk_u = abs(entry - sl_price)
                    if risk_u > 0 and capital > 0:
                        sz = (capital * RISK_PER_TRADE) / risk_u
                        trade = ("BUY", entry, sl_price, tp_price, sz, ts)

                elif l < range_low:
                    # Breakout DOWN confirmed
                    entry = range_low * (1 - SLIP_PCT)
                    sl_price = entry + range_size * sl_mult
                    tp_price = entry - range_size * tp_mult
                    risk_u = abs(entry - sl_price)
                    if risk_u > 0 and capital > 0:
                        sz = (capital * RISK_PER_TRADE) / risk_u
                        trade = ("SELL", entry, sl_price, tp_price, sz, ts)

    # Close remaining
    if trade is not None and len(df_1h) > 0:
        d, ent, sl, tp, sz, bt = trade
        last_c = float(df_1h.iloc[-1]["close"])
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
# MAIN — PROTOCOL OMEGA
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    t0 = time.time()

    carry_combos = len(CARRY_SMA) * len(CARRY_SL_ATR) * len(CARRY_REENTRY)
    news_combos = len(NEWS_HOURS) * len(NEWS_RANGE_BARS) * len(NEWS_TP_MULT) * len(NEWS_SL_MULT) * len(NEWS_TIMEOUT)

    print()
    print("═" * 70)
    print("  ⚡ PROTOCOL OMEGA — Global Macro Final Assault")
    print(f"  Targets : {len(DEAD_8)} 'ETERNALLY DEAD' assets")
    print(f"  M54 CARRY  : {carry_combos} combos (SMA × SL × Reentry on Weekly)")
    print(f"  M55 SNIPE  : {news_combos} combos (Hour × Range × TP × SL × Timeout)")
    print(f"  Fitness    : PnL > 0€ AND MaxDD > {MAX_DD_LIMIT}% AND trades ≥ {MIN_TRADES}")
    print("═" * 70)

    # Download all data
    print("\n  📥 Downloading data...")
    data_1h = {}
    data_wk = {}
    for sym in DEAD_8:
        ticker = YF_MAP[sym]
        df = download(ticker, "1h")
        if df is not None and len(df) > 100:
            data_1h[sym] = df
            print(f"    {sym}/1h: {len(df)} bougies ✅")

        df_w = download(ticker, "1wk")
        if df_w is not None and len(df_w) > 50:
            data_wk[sym] = df_w
            print(f"    {sym}/1wk: {len(df_w)} bougies ✅")
        else:
            print(f"    {sym}/1wk: ❌")

    rescued = {}
    still_dead = []

    for idx, sym in enumerate(DEAD_8, 1):
        print(f"\n{'─' * 70}")
        print(f"  ⚡ [{idx}/{len(DEAD_8)}] {sym}")
        print(f"{'─' * 70}")

        found = False
        best_r = None
        best_cfg = None
        combos = 0

        # ── M54: Carry Trade (Weekly) ──
        if sym in data_wk:
            df_w = data_wk[sym]
            for sma_p in CARRY_SMA:
                if found: break
                for sl_m in CARRY_SL_ATR:
                    if found: break
                    for re_b in CARRY_REENTRY:
                        combos += 1
                        r = backtest_carry(df_w, sma_p, sl_m, re_b)

                        if r["pnl_net"] > 0 and r["max_dd"] > MAX_DD_LIMIT and r["trades"] >= MIN_TRADES:
                            found = True
                            best_r = r
                            best_cfg = {"engine": "M54_CARRY", "tf": "1wk",
                                       "sma": sma_p, "sl_atr": sl_m, "reentry": re_b}
                            break

                        if best_r is None or r["pnl_net"] > best_r.get("pnl_net", -99999):
                            best_r = r
                            best_cfg = {"engine": "M54_CARRY", "tf": "1wk",
                                       "sma": sma_p, "sl_atr": sl_m, "reentry": re_b}

        # ── M55: News Sniping (1h) ──
        if not found and sym in data_1h:
            df_h = data_1h[sym]
            for hour in NEWS_HOURS:
                if found: break
                for rng_b in NEWS_RANGE_BARS:
                    if found: break
                    for tp_m in NEWS_TP_MULT:
                        if found: break
                        for sl_m in NEWS_SL_MULT:
                            if found: break
                            for tout in NEWS_TIMEOUT:
                                combos += 1
                                r = backtest_news_snipe(df_h, hour, rng_b,
                                                        tp_m, sl_m, tout)

                                if r["pnl_net"] > 0 and r["max_dd"] > MAX_DD_LIMIT and r["trades"] >= MIN_TRADES:
                                    found = True
                                    best_r = r
                                    best_cfg = {"engine": "M55_SNIPE", "tf": "1h",
                                               "hour": hour, "range_bars": rng_b,
                                               "tp_mult": tp_m, "sl_mult": sl_m,
                                               "timeout_h": tout}
                                    break

                                if best_r is None or r["pnl_net"] > best_r.get("pnl_net", -99999):
                                    best_r = r
                                    best_cfg = {"engine": "M55_SNIPE", "tf": "1h",
                                               "hour": hour, "range_bars": rng_b,
                                               "tp_mult": tp_m, "sl_mult": sl_m,
                                               "timeout_h": tout}

        if found:
            c = best_cfg; r = best_r
            if c["engine"] == "M54_CARRY":
                detail = f"SMA={c['sma']} SL={c['sl_atr']}ATR re={c['reentry']}"
            else:
                detail = f"H={c['hour']} rng={c['range_bars']} tp={c['tp_mult']} sl={c['sl_mult']} tout={c['timeout_h']}h"
            print(f"  ✅ OMEGA CAPTURED ! {c['engine']}/{c['tf']} {detail} → "
                  f"PnL={r['pnl_net']:+,.0f}€ WR={r['win_rate']:.0f}% "
                  f"DD={r['max_dd']:.1f}% ({r['trades']} trades) [{combos} combos]")
            rescued[sym] = {**best_cfg, **{k: v for k, v in best_r.items()}}
        else:
            c = best_cfg or {}
            r = best_r or {"pnl_net": 0}
            print(f"  💀 ETERNALLY DEAD ({combos} combos) "
                  f"| Best: {c.get('engine','?')} → PnL={r.get('pnl_net',0):+,.0f}€")
            still_dead.append(sym)

    elapsed = time.time() - t0

    # Export
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "omega_rules.json")
    with open(out_path, "w") as f:
        json.dump(rescued, f, indent=2)

    # Report
    print()
    print("═" * 70)
    print("  ⚡ PROTOCOL OMEGA — FINAL REPORT")
    print("═" * 70)
    print(f"\n  ⏱️  Time: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"  🎯 Targets  : {len(DEAD_8)}")
    print(f"  ✅ Captured  : {len(rescued)}")
    print(f"  💀 Dead      : {len(still_dead)}")

    if rescued:
        total_pnl = sum(v["pnl_net"] for v in rescued.values())
        print(f"  💰 PnL combiné : {total_pnl:+,.0f}€")
        for sym in sorted(rescued, key=lambda s: rescued[s]["pnl_net"], reverse=True):
            v = rescued[sym]
            if v["engine"] == "M54_CARRY":
                cfg = f"SMA={v['sma']} SL={v['sl_atr']}ATR"
            else:
                cfg = f"H={v['hour']} rng={v['range_bars']}"
            print(f"    🟢 {sym:<10} {v['engine']:>10} {v['tf']:>4} {cfg:>20} → "
                  f"PnL={v['pnl_net']:>+7,.0f}€  WR={v['win_rate']:>5.1f}%  "
                  f"DD={v['max_dd']:>6.1f}%  trades={v['trades']}  PF={v['profit_factor']:.2f}")

    if still_dead:
        print(f"\n  💀 TRULY UNTRADEABLE ({len(still_dead)}):")
        print(f"    {', '.join(still_dead)}")

    # Grand total
    print(f"\n  📊 GRAND TOTAL ACROSS ALL 4 PHASES:")
    prev_pnl = 5177 + 371 + 195  # Alpha + Black Ops + Lazarus
    omega_pnl = sum(v["pnl_net"] for v in rescued.values()) if rescued else 0
    print(f"    Alpha Factory  : +5,177€ (26 assets)")
    print(f"    Black Ops      : +371€   (9 assets)")
    print(f"    Lazarus        : +195€   (5+2 assets)")
    print(f"    Omega          : {omega_pnl:+,.0f}€   ({len(rescued)} assets)")
    print(f"    ─────────────────────────────────")
    print(f"    TOTAL          : {prev_pnl + omega_pnl:+,.0f}€ ({26+9+5+len(rescued)}/48 assets)")

    print(f"\n  💾 Exported: {out_path}")
    print(f"{'═' * 70}\n")


if __name__ == "__main__":
    main()
