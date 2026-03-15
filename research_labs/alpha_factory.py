#!/usr/bin/env python3
"""
alpha_factory.py — 🏭 THE ALPHA FACTORY
Brute-force optimizer: tests every asset INDEPENDENTLY with its own 10K€ capital.
For each asset, iterates through 243 combos (3 strat × 3 tf × 3 thresh × 3 rr × 3 ts)
until it finds a profitable configuration (PnL > 0, MaxDD < 15%).

Outputs: optimized_rules.json with the perfect mapping for each asset.

KEY OPTIMIZATION: Pre-compute signals ONCE per (asset, timeframe, strategy),
then replay with different threshold/RR/TS combos (pure arithmetic = instant).
"""

import os, sys, json, time, warnings
from datetime import datetime, timedelta
from typing import Optional, List, Tuple, Dict
from itertools import product

import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loguru import logger
logger.remove()  # Kill all loguru output — we want clean Alpha Factory logs only

from strategy import Strategy, SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD
from brokers.capital_client import ASSET_PROFILES

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

# Yahoo Finance ticker map (broker → yfinance)
YF_MAP = {
    "EURUSD": "EURUSD=X", "USDJPY": "JPY=X", "GBPUSD": "GBPUSD=X",
    "GBPJPY": "GBPJPY=X", "EURJPY": "EURJPY=X", "USDCHF": "CHF=X",
    "AUDNZD": "AUDNZD=X", "AUDJPY": "AUDJPY=X", "NZDJPY": "NZDJPY=X",
    "EURCHF": "EURCHF=X", "CHFJPY": "CHFJPY=X",
    "AUDUSD": "AUDUSD=X", "NZDUSD": "NZDUSD=X", "EURGBP": "EURGBP=X",
    "EURAUD": "EURAUD=X", "GBPAUD": "GBPAUD=X", "AUDCAD": "AUDCAD=X",
    "GBPCAD": "GBPCAD=X", "GBPCHF": "GBPCHF=X", "CADCHF": "CADCHF=X",
    "GOLD": "GC=F", "SILVER": "SI=F", "OIL_CRUDE": "CL=F",
    "OIL_BRENT": "BZ=F", "COPPER": "HG=F", "NATURALGAS": "NG=F",
    "US500": "^GSPC", "US100": "^NDX", "US30": "^DJI",
    "DE40": "^GDAXI", "FR40": "^FCHI", "UK100": "^FTSE",
    "J225": "^N225", "AU200": "^AXJO",
    "BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD", "BNBUSD": "BNB-USD",
    "XRPUSD": "XRP-USD", "SOLUSD": "SOL-USD", "AVAXUSD": "AVAX-USD",
    "AAPL": "AAPL", "TSLA": "TSLA", "NVDA": "NVDA", "MSFT": "MSFT",
    "META": "META", "GOOGL": "GOOGL", "AMZN": "AMZN", "AMD": "AMD",
}

# Search grid
STRATEGIES = ["BK", "MR", "TF"]
TIMEFRAMES = ["1h", "4h", "1d"]  # 4h = resample from 1h
THRESHOLDS = [0.50, 0.60, 0.70]
RR_MINS    = [1.0, 1.5, 2.0]
TIME_STOPS = [24, 48, 72]

# Fitness
INITIAL_CAPITAL = 10_000.0
FEE_PCT         = 0.001
SLIP_PCT        = 0.0005
RISK_PER_TRADE  = 0.005
MAX_DD_LIMIT    = -15.0   # Max DD must be < 15%
MIN_TRADES      = 5       # At least 5 trades to be valid
YEARS           = 2

FRIDAY_KILL_H   = 20
FRIDAY_KILL_M   = 0

# ═══════════════════════════════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════════════════════════════

def download(ticker: str, interval: str = "1h") -> Optional[pd.DataFrame]:
    import yfinance as yf
    end = datetime.now()
    start = end - timedelta(days=365 * YEARS)

    if interval in ("1h", "4h"):
        dl_interval = "1h"
        all_dfs = []
        cur = end
        while cur > start:
            cs = max(start, cur - timedelta(days=720))
            df = yf.download(ticker, start=cs, end=cur, interval=dl_interval,
                           progress=False, auto_adjust=True)
            if df is not None and not df.empty:
                all_dfs.append(df)
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

    # Resample to 4h if needed
    if interval == "4h" and dl_interval == "1h":
        df = df.resample("4h").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"
        }).dropna()

    return df


# ═══════════════════════════════════════════════════════════════════════════════
# SIGNAL EXTRACTION (one pass per asset+tf+strat)
# ═══════════════════════════════════════════════════════════════════════════════

def extract_signals(symbol: str, df_ind: pd.DataFrame, profile: dict,
                    strat: str) -> list:
    """Extract all signals for a given strategy. Returns list of tuples."""
    # Override strategy in profile
    p = dict(profile)
    p["strat"] = strat

    st = Strategy()
    lookback = min(300, len(df_ind) - 50) if len(df_ind) > 350 else 50
    signals = []

    for i in range(lookback, len(df_ind)):
        window = df_ind.iloc[max(0, i - lookback):i + 1]
        if len(window) < 30:
            continue

        curr = window.iloc[-1]
        ts = window.index[-1]
        o = float(curr["open"])
        h = float(curr["high"])
        l = float(curr["low"])
        c = float(curr["close"])
        atr = float(curr.get("atr", 0))

        sig, score, confs = st.get_signal(window, symbol=symbol, asset_profile=p)

        sr = None
        if sig in (SIGNAL_BUY, SIGNAL_SELL) and strat == "BK":
            sr = st.compute_session_range(window, range_lookback=p.get("range_lb", 4))

        signals.append((ts, o, h, l, c, atr, sig, score, sr))

    return signals


# ═══════════════════════════════════════════════════════════════════════════════
# REPLAY ENGINE (ultra-fast, pure arithmetic)
# ═══════════════════════════════════════════════════════════════════════════════

def replay(signals: list, profile: dict, strat: str,
           threshold: float, rr_min: float, ts_hours: float,
           is_tradfi: bool) -> dict:
    """Replay pre-computed signals with specific params. Returns metrics."""
    capital = INITIAL_CAPITAL
    peak = capital
    max_dd = 0.0
    trade = None
    wins = losses = 0
    total_pnl = total_fees = 0.0
    sum_win = sum_loss = 0.0
    n_trades = 0
    exit_reasons = {}

    for ts, o, h, l, c, atr, sig, score, sr in signals:
        # ── Manage open trade ──
        if trade is not None:
            t_dir, t_entry, t_sl, t_tp, t_size, t_time = trade
            age_h = (ts - t_time).total_seconds() / 3600
            closed = False
            exit_p = reason = None

            # SL/TP
            if t_dir == "BUY":
                if l <= t_sl:
                    exit_p, reason = t_sl * (1 - SLIP_PCT), "SL"
                elif h >= t_tp:
                    exit_p, reason = t_tp * (1 - SLIP_PCT), "TP"
            else:
                if h >= t_sl:
                    exit_p, reason = t_sl * (1 + SLIP_PCT), "SL"
                elif l <= t_tp:
                    exit_p, reason = t_tp * (1 + SLIP_PCT), "TP"

            # Time Stop
            if exit_p is None and age_h > ts_hours:
                exit_p = c * (1 + SLIP_PCT) if t_dir == "SELL" else c * (1 - SLIP_PCT)
                reason = "TIME_STOP"

            # Friday Kill (TradFi only)
            if exit_p is None and is_tradfi and hasattr(ts, 'weekday'):
                if ts.weekday() == 4 and ts.hour >= FRIDAY_KILL_H:
                    exit_p = c * (1 + SLIP_PCT) if t_dir == "SELL" else c * (1 - SLIP_PCT)
                    reason = "FRIDAY_KILL"

            if exit_p is not None:
                pnl = (exit_p - t_entry) * t_size if t_dir == "BUY" else (t_entry - exit_p) * t_size
                fees = (t_size * t_entry + t_size * exit_p) * FEE_PCT
                capital += pnl - fees
                total_pnl += pnl
                total_fees += fees
                n_trades += 1
                exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
                if pnl > 0:
                    wins += 1; sum_win += pnl
                else:
                    losses += 1; sum_loss += abs(pnl)
                trade = None
                peak = max(peak, capital)
                dd = (capital - peak) / peak * 100 if peak > 0 else -100
                max_dd = min(max_dd, dd)
                if capital <= 0:
                    break

        # ── Open new trade ──
        if trade is None and score >= threshold and sig in (SIGNAL_BUY, SIGNAL_SELL) and capital > 0:
            direction = "BUY" if sig == SIGNAL_BUY else "SELL"

            if atr <= 0:
                continue

            # SL/TP calculation depends on strategy
            if strat == "BK" and sr and sr.get("size", 0) > 0:
                sl_dist = sr["size"] * profile.get("sl_buffer", 0.12)
                tp_dist = sr["size"] * profile.get("tp1", 1.5)
            else:
                # MR/TF: use ATR-based SL/TP
                sl_dist = atr * 1.5
                tp_dist = atr * 1.5 * rr_min

            if direction == "BUY":
                entry = c * (1 + SLIP_PCT)
                sl = entry - sl_dist
                tp = entry + tp_dist
            else:
                entry = c * (1 - SLIP_PCT)
                sl = entry + sl_dist
                tp = entry - tp_dist

            # Enforce RR
            risk = abs(entry - sl)
            if risk <= 0:
                continue
            reward = abs(tp - entry)
            if reward / risk < rr_min:
                if direction == "BUY":
                    tp = entry + risk * rr_min
                else:
                    tp = entry - risk * rr_min

            # Size
            risk_per_unit = abs(entry - sl)
            if risk_per_unit <= 0:
                continue
            size = (capital * RISK_PER_TRADE) / risk_per_unit

            trade = (direction, entry, sl, tp, size, ts)

    # Close remaining
    if trade is not None and signals:
        t_dir, t_entry, t_sl, t_tp, t_size, t_time = trade
        last_c = signals[-1][4]
        pnl = (last_c - t_entry) * t_size if t_dir == "BUY" else (t_entry - last_c) * t_size
        fees = (t_size * t_entry + t_size * last_c) * FEE_PCT
        capital += pnl - fees
        total_pnl += pnl
        total_fees += fees
        n_trades += 1
        if pnl > 0: wins += 1
        else: losses += 1

    pnl_net = total_pnl - total_fees
    wr = wins / n_trades * 100 if n_trades > 0 else 0
    pf = sum_win / sum_loss if sum_loss > 0 else 0

    return {
        "pnl_net": round(pnl_net, 2),
        "trades": n_trades,
        "win_rate": round(wr, 1),
        "max_dd": round(max_dd, 2),
        "profit_factor": round(pf, 2),
        "fees": round(total_fees, 2),
        "exit_reasons": exit_reasons,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN — THE ALPHA FACTORY
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    t0 = time.time()
    combos_per_asset = len(STRATEGIES) * len(TIMEFRAMES) * len(THRESHOLDS) * len(RR_MINS) * len(TIME_STOPS)

    # Filter to assets that have a yfinance ticker
    assets = [sym for sym in ASSET_PROFILES if sym in YF_MAP]

    print()
    print("═" * 70)
    print("  🏭 THE ALPHA FACTORY — Brute-Force Asset Optimizer")
    print(f"  Assets : {len(assets)}")
    print(f"  Grid   : {len(STRATEGIES)}S × {len(TIMEFRAMES)}TF × {len(THRESHOLDS)}Th × {len(RR_MINS)}RR × {len(TIME_STOPS)}TS = {combos_per_asset}/asset")
    print(f"  Max    : {len(assets) * combos_per_asset} backtests (early-exit on success)")
    print(f"  Fitness: PnL > 0€ AND MaxDD > {MAX_DD_LIMIT}% AND trades ≥ {MIN_TRADES}")
    print("═" * 70)

    optimal_rules = {}
    untradeable = []
    summary_rows = []

    # ── Resume mode: skip already completed assets ──
    ALREADY_DONE = {
        "EURUSD", "USDJPY", "GBPUSD", "GBPJPY", "EURJPY", "USDCHF",
        "AUDNZD", "AUDJPY", "NZDJPY", "EURCHF", "CHFJPY",
        "GOLD", "SILVER",
        "DE40", "FR40", "UK100", "J225", "AU200",
        "BTCUSD", "ETHUSD", "BNBUSD", "XRPUSD", "SOLUSD", "AVAXUSD",
        "AAPL", "TSLA",
    }
    resume = "--resume" in sys.argv
    if resume:
        # Load previous results
        prev_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "optimized_rules.json")
        if os.path.exists(prev_path):
            with open(prev_path) as f:
                prev = json.load(f)
            for k, v in prev.items():
                if not k.startswith("_"):
                    optimal_rules[k] = v
            prev_untrade = prev.get("_UNTRADEABLE", [])
            untradeable.extend(prev_untrade)
            print(f"\n  📦 RESUME: {len(optimal_rules)} rentables + {len(untradeable)} untradeable chargés")
            print(f"  ⏭️  Skip: {len(ALREADY_DONE)} actifs déjà scannés\n")

    for asset_idx, symbol in enumerate(assets, 1):
        # Skip already done in resume mode
        if resume and symbol in ALREADY_DONE:
            continue

        ticker = YF_MAP[symbol]
        profile = ASSET_PROFILES.get(symbol, {})
        cat = profile.get("cat", "forex")
        is_tradfi = cat not in ("crypto",)

        print(f"\n{'─' * 70}")
        print(f"  🔬 [{asset_idx}/{len(assets)}] {symbol} ({ticker}) | cat={cat}")
        print(f"{'─' * 70}")

        # Download data for all timeframes
        data_by_tf = {}
        for tf in TIMEFRAMES:
            df = download(ticker, tf)
            if df is not None and len(df) > 100:
                data_by_tf[tf] = df
                print(f"    📥 {tf}: {len(df)} bougies ✅")
            else:
                print(f"    📥 {tf}: ❌ insuffisant")

        if not data_by_tf:
            print(f"  ❌ {symbol}: AUCUNE donnée → UNTRADEABLE")
            untradeable.append(symbol)
            continue

        # Pre-compute indicators for each timeframe
        strat_engine = Strategy()
        indicators_by_tf = {}
        for tf, df in data_by_tf.items():
            try:
                df_ind = strat_engine.compute_indicators(df.copy())
                if len(df_ind) > 100:
                    indicators_by_tf[tf] = df_ind
                    print(f"    ⚙️  {tf}: {len(df_ind)} barres avec indicateurs ✅")
            except Exception as e:
                print(f"    ⚙️  {tf}: erreur indicateurs ({e})")

        if not indicators_by_tf:
            print(f"  ❌ {symbol}: indicateurs échoués → UNTRADEABLE")
            untradeable.append(symbol)
            continue

        # Pre-compute signals for each (tf, strat) combo
        signals_cache = {}
        for tf, df_ind in indicators_by_tf.items():
            for strat in STRATEGIES:
                key = (tf, strat)
                try:
                    sigs = extract_signals(symbol, df_ind, profile, strat)
                    n_valid = sum(1 for s in sigs if s[6] in (SIGNAL_BUY, SIGNAL_SELL))
                    signals_cache[key] = sigs
                    if n_valid > 0:
                        print(f"    🔍 {tf}/{strat}: {n_valid} signaux")
                except Exception:
                    pass

        # Brute-force search (with early exit)
        found = False
        best_result = None
        best_params = None
        combos_tested = 0

        # Priority order: most likely to succeed first
        # Sort: higher threshold first (fewer trades = less fees)
        for threshold in sorted(THRESHOLDS, reverse=True):
            if found:
                break
            for rr_min in RR_MINS:
                if found:
                    break
                for ts_h in TIME_STOPS:
                    if found:
                        break
                    for tf in TIMEFRAMES:
                        if found:
                            break
                        for strat in STRATEGIES:
                            key = (tf, strat)
                            if key not in signals_cache:
                                continue

                            combos_tested += 1
                            r = replay(
                                signals_cache[key], profile, strat,
                                threshold, rr_min, ts_h, is_tradfi
                            )

                            # Fitness check
                            if (r["pnl_net"] > 0 and
                                r["max_dd"] > MAX_DD_LIMIT and
                                r["trades"] >= MIN_TRADES):

                                found = True
                                best_result = r
                                best_params = {
                                    "strat": strat, "tf": tf,
                                    "threshold": threshold,
                                    "rr": rr_min, "time_stop": ts_h
                                }
                                break

                            # Track best even if not profitable
                            if best_result is None or r["pnl_net"] > best_result["pnl_net"]:
                                best_result = r
                                best_params = {
                                    "strat": strat, "tf": tf,
                                    "threshold": threshold,
                                    "rr": rr_min, "time_stop": ts_h
                                }

        if found:
            p = best_params
            r = best_result
            print(f"  ✅ RENTABLE ! {p['strat']}/{p['tf']} Th={p['threshold']} "
                  f"RR={p['rr']} TS={p['time_stop']}h → "
                  f"PnL={r['pnl_net']:+,.0f}€ WR={r['win_rate']:.0f}% "
                  f"DD={r['max_dd']:.1f}% ({r['trades']} trades) "
                  f"[{combos_tested} combos testées]")
            optimal_rules[symbol] = {
                **best_params,
                "pnl_net": r["pnl_net"],
                "win_rate": r["win_rate"],
                "max_dd": r["max_dd"],
                "trades": r["trades"],
                "profit_factor": r["profit_factor"],
            }
            summary_rows.append((symbol, "✅ RENTABLE", best_params, best_result, combos_tested))
        else:
            p = best_params or {}
            r = best_result or {"pnl_net": 0, "trades": 0, "win_rate": 0, "max_dd": 0}
            print(f"  ❌ UNTRADEABLE ({combos_tested} combos épuisées) "
                  f"| Best: {p.get('strat','?')}/{p.get('tf','?')} → "
                  f"PnL={r['pnl_net']:+,.0f}€ DD={r['max_dd']:.1f}%")
            untradeable.append(symbol)
            summary_rows.append((symbol, "❌ UNTRADEABLE", best_params, best_result, combos_tested))

    elapsed = time.time() - t0

    # ═══════════════════════════════════════════════════════════════════════════
    # EXPORT optimized_rules.json
    # ═══════════════════════════════════════════════════════════════════════════
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "optimized_rules.json")
    with open(out_path, "w") as f:
        json.dump(optimal_rules, f, indent=2)
    print(f"\n💾 Fichier exporté: {out_path}")

    # ═══════════════════════════════════════════════════════════════════════════
    # FINAL REPORT
    # ═══════════════════════════════════════════════════════════════════════════
    print()
    print("═" * 70)
    print("  🏆 ALPHA FACTORY — RAPPORT FINAL")
    print("═" * 70)

    print(f"\n  ⏱️  Temps total : {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"  📊 Actifs testés : {len(assets)}")
    print(f"  ✅ Rentables     : {len(optimal_rules)}")
    print(f"  ❌ Untradeable   : {len(untradeable)}")

    if optimal_rules:
        total_pnl = sum(v["pnl_net"] for v in optimal_rules.values())
        total_trades = sum(v["trades"] for v in optimal_rules.values())
        print(f"  💰 PnL combiné   : {total_pnl:+,.0f}€ (si isolé par actif)")
        print(f"  📈 Trades total  : {total_trades}")

        print(f"\n  {'─' * 66}")
        print(f"  {'Actif':<10} {'Strat':>5} {'TF':>4} {'Th':>5} {'RR':>4} {'TS':>4} "
              f"{'PnL Net':>10} {'WR%':>6} {'DD%':>7} {'Trades':>7} {'PF':>5}")
        print(f"  {'─'*10} {'─'*5} {'─'*4} {'─'*5} {'─'*4} {'─'*4} "
              f"{'─'*10} {'─'*6} {'─'*7} {'─'*7} {'─'*5}")

        for sym in sorted(optimal_rules, key=lambda s: optimal_rules[s]["pnl_net"], reverse=True):
            v = optimal_rules[sym]
            print(f"  🟢 {sym:<8} {v['strat']:>5} {v['tf']:>4} {v['threshold']:>5.2f} "
                  f"{v['rr']:>4.1f} {v['time_stop']:>3}h "
                  f"{v['pnl_net']:>+9,.0f}€ {v['win_rate']:>5.1f}% "
                  f"{v['max_dd']:>6.1f}% {v['trades']:>7} {v['profit_factor']:>5.2f}")

    if untradeable:
        print(f"\n  ❌ UNTRADEABLE ({len(untradeable)} actifs):")
        print(f"  {', '.join(untradeable)}")

    # Strategy distribution
    if optimal_rules:
        strat_dist = {}
        tf_dist = {}
        for v in optimal_rules.values():
            strat_dist[v["strat"]] = strat_dist.get(v["strat"], 0) + 1
            tf_dist[v["tf"]] = tf_dist.get(v["tf"], 0) + 1
        print(f"\n  📊 Distribution des stratégies : {strat_dist}")
        print(f"  📊 Distribution des timeframes : {tf_dist}")

    print(f"\n{'═' * 70}")
    print(f"  💾 Règles exportées dans: optimized_rules.json")
    print(f"{'═' * 70}\n")


if __name__ == "__main__":
    main()
