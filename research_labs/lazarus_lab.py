#!/usr/bin/env python3
"""
lazarus_lab.py — 🧬 PROJECT LAZARUS
Last-chance resurrection for the 13 CONFIRMED DEAD assets using:

MOTEUR 52: ML ORACLE — RandomForest trained on engineered features.
           Train on 18 months, test on 6 months (out-of-sample).
MOTEUR 53: PAIRS TRADING — Co-integration spread Z-score arbitrage.
           Short overvalued pair, buy undervalued when spread deviates.

Only 4h and 1d timeframes (1h banned — too noisy for these assets).
"""

import os, sys, json, time, warnings
from datetime import datetime, timedelta
from typing import Optional, Tuple

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
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDCHF": "CHF=X",
    "AUDNZD": "AUDNZD=X", "EURCHF": "EURCHF=X", "COPPER": "HG=F",
    "AUDUSD": "AUDUSD=X", "EURGBP": "EURGBP=X", "EURAUD": "EURAUD=X",
    "AUDCAD": "AUDCAD=X", "GBPCAD": "GBPCAD=X",
    "GBPCHF": "GBPCHF=X", "CADCHF": "CADCHF=X",
}

DEAD_ASSETS = list(YF_MAP.keys())

# Pairs for co-integration testing (logically related instruments)
PAIRS_TO_TEST = [
    ("EURUSD", "GBPUSD"),       # Both vs USD, EUR/GBP correlated
    ("EURUSD", "EURCHF"),       # Both EUR-based
    ("EURUSD", "EURGBP"),       # EUR crosses
    ("GBPUSD", "GBPCHF"),      # Both GBP-based
    ("GBPUSD", "GBPCAD"),      # Both GBP-based
    ("AUDUSD", "AUDNZD"),      # Both AUD-based (Oceania)
    ("AUDUSD", "AUDCAD"),      # Both AUD-based (commodity)
    ("AUDNZD", "AUDCAD"),      # AUD crosses
    ("USDCHF", "EURCHF"),      # Both CHF-based
    ("USDCHF", "GBPCHF"),      # Both CHF-based
    ("USDCHF", "CADCHF"),      # Both CHF-based
    ("EURCHF", "GBPCHF"),      # CHF crosses
    ("EURCHF", "CADCHF"),      # CHF crosses
    ("GBPCHF", "CADCHF"),      # CHF crosses
    ("GBPCAD", "AUDCAD"),      # Both CAD-based
]

TIMEFRAMES = ["4h", "1d"]

# ML grid
RF_ESTIMATORS = [50, 100, 200]
RF_DEPTHS     = [3, 5, 7]
HORIZONS      = [3, 5]           # Predict N bars ahead

# Pairs grid
SPREAD_Z_THRS = [1.5, 2.0, 2.5]
SPREAD_WINDOWS = [20, 50, 100]

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

def download(ticker: str, interval: str = "4h") -> Optional[pd.DataFrame]:
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


# ═══════════════════════════════════════════════════════════════════════════════
# M52: ML ORACLE — RandomForest
# ═══════════════════════════════════════════════════════════════════════════════

def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Engineer features for ML model."""
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)

    feat = pd.DataFrame(index=df.index)

    # Return lags (t-1 to t-5)
    for lag in range(1, 6):
        feat[f"ret_lag{lag}"] = c.pct_change(lag)

    # Rolling volatility (ATR proxy)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    feat["atr_14"] = tr.rolling(14).mean() / c  # Normalized
    feat["atr_50"] = tr.rolling(50).mean() / c

    # Distance to moving averages
    feat["dist_ma50"] = (c - c.rolling(50).mean()) / c
    feat["dist_ma200"] = (c - c.rolling(200).mean()) / c

    # RSI
    delta = c.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    feat["rsi"] = 100 - (100 / (1 + rs))

    # Bollinger position
    bb_mid = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    feat["bb_pos"] = (c - bb_mid) / (bb_std * 2 + 1e-10)

    # Day of week (seasonality)
    feat["dow"] = df.index.dayofweek

    # Hour (for 4h data)
    feat["hour"] = df.index.hour

    return feat.dropna()


def backtest_ml(df: pd.DataFrame, n_est: int, max_depth: int,
                horizon: int, is_tradfi: bool) -> dict:
    """Train RandomForest on 75% data, backtest on 25% out-of-sample."""
    from sklearn.ensemble import RandomForestClassifier

    features = build_features(df)
    c = df["close"].astype(float).reindex(features.index)

    # Target: will price be higher in N bars?
    future_ret = c.shift(-horizon) / c - 1
    target = (future_ret > 0).astype(int)

    # Align and drop NaN
    valid = features.join(target.rename("target")).dropna()
    if len(valid) < 200:
        return {"pnl_net": -999, "trades": 0, "win_rate": 0, "max_dd": -100, "profit_factor": 0, "fees": 0}

    X = valid.drop("target", axis=1).values
    y = valid["target"].values
    dates = valid.index

    # Train/test split: 75% train, 25% test (chronological, no leakage)
    split = int(len(X) * 0.75)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]
    dates_test = dates[split:]

    if len(X_test) < MIN_TRADES or len(X_train) < 100:
        return {"pnl_net": -999, "trades": 0, "win_rate": 0, "max_dd": -100, "profit_factor": 0, "fees": 0}

    # Train
    model = RandomForestClassifier(
        n_estimators=n_est, max_depth=max_depth,
        random_state=42, n_jobs=-1, class_weight="balanced"
    )
    model.fit(X_train, y_train)

    # Predict probabilities on test set
    proba = model.predict_proba(X_test)
    # Column index for class 1 (price up)
    up_idx = list(model.classes_).index(1) if 1 in model.classes_ else 0

    # Backtest on test set
    capital = INITIAL_CAPITAL
    peak = capital
    max_dd = 0.0
    trade = None
    wins = losses = 0
    total_pnl = total_fees = 0.0
    sum_win = sum_loss = 0.0
    n_trades = 0

    test_df = df.reindex(dates_test)

    for i in range(len(dates_test) - 1):
        ts = dates_test[i]
        row = test_df.iloc[i]
        c_price = float(row["close"])
        h_price = float(row["high"])
        l_price = float(row["low"])
        prob_up = proba[i][up_idx]

        if capital <= 0:
            break

        # Friday Kill
        if is_tradfi and trade and hasattr(ts, 'weekday') and ts.weekday() == 4 and ts.hour >= FRIDAY_KILL_H:
            d, ent, sl, tp, sz, bt, bars = trade
            pnl = (c_price - ent) * sz if d == "BUY" else (ent - c_price) * sz
            fees = (sz * ent + sz * c_price) * FEE_PCT
            capital += pnl - fees; total_pnl += pnl; total_fees += fees; n_trades += 1
            if pnl > 0: wins += 1; sum_win += pnl
            else: losses += 1; sum_loss += abs(pnl)
            trade = None
            continue

        # Manage open trade
        if trade is not None:
            d, ent, sl, tp, sz, bt, bars = trade
            bars += 1
            exit_p = None

            if d == "BUY":
                if l_price <= sl: exit_p = sl * (1 - SLIP_PCT)
                elif h_price >= tp: exit_p = tp * (1 - SLIP_PCT)
            else:
                if h_price >= sl: exit_p = sl * (1 + SLIP_PCT)
                elif l_price <= tp: exit_p = tp * (1 + SLIP_PCT)

            # Time stop: horizon * 2 bars
            if exit_p is None and bars >= horizon * 2:
                exit_p = c_price

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
            else:
                trade = (d, ent, sl, tp, sz, bt, bars)

        # Open new trade based on ML probability
        if trade is None and capital > 0:
            # ATR for SL/TP
            idx_in_df = df.index.get_loc(ts)
            if idx_in_df < 14:
                continue
            recent = df.iloc[max(0, idx_in_df-14):idx_in_df+1]
            tr_vals = (recent["high"] - recent["low"]).astype(float)
            atr = float(tr_vals.mean()) if len(tr_vals) > 0 else 0
            if atr <= 0:
                continue

            if prob_up > 0.58:  # Strong buy signal
                entry = c_price * (1 + SLIP_PCT)
                sl = entry - atr * 2.0
                tp = entry + atr * 2.5
                risk_u = abs(entry - sl)
                if risk_u > 0:
                    sz = (capital * RISK_PER_TRADE) / risk_u
                    trade = ("BUY", entry, sl, tp, sz, ts, 0)

            elif prob_up < 0.42:  # Strong sell signal
                entry = c_price * (1 - SLIP_PCT)
                sl = entry + atr * 2.0
                tp = entry - atr * 2.5
                risk_u = abs(entry - sl)
                if risk_u > 0:
                    sz = (capital * RISK_PER_TRADE) / risk_u
                    trade = ("SELL", entry, sl, tp, sz, ts, 0)

    # Close remaining
    if trade is not None and len(test_df) > 0:
        d, ent, sl, tp, sz, bt, bars = trade
        last_c = float(test_df.iloc[-1]["close"])
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
# M53: PAIRS TRADING (Co-Integration Spread Arbitrage)
# ═══════════════════════════════════════════════════════════════════════════════

def backtest_pairs(df_a: pd.DataFrame, df_b: pd.DataFrame,
                   z_thr: float, z_window: int, is_tradfi: bool) -> dict:
    """
    Pairs Trading: when spread Z-score exceeds threshold:
    - If Z > +thr: SELL spread (sell A, buy B) — A is overvalued
    - If Z < -thr: BUY spread (buy A, sell B) — A is undervalued
    Exit when Z returns to 0.
    """
    from statsmodels.tsa.stattools import coint

    # Align on common dates
    common = df_a.index.intersection(df_b.index)
    if len(common) < 200:
        return {"pnl_net": -999, "trades": 0, "win_rate": 0, "max_dd": -100, "profit_factor": 0, "fees": 0}

    ca = df_a.loc[common, "close"].astype(float)
    cb = df_b.loc[common, "close"].astype(float)
    ha = df_a.loc[common, "high"].astype(float)
    la = df_a.loc[common, "low"].astype(float)
    hb = df_b.loc[common, "high"].astype(float)
    lb = df_b.loc[common, "low"].astype(float)

    # Test co-integration
    try:
        _, p_value, _ = coint(ca.values, cb.values)
    except Exception:
        return {"pnl_net": -999, "trades": 0, "win_rate": 0, "max_dd": -100, "profit_factor": 0, "fees": 0}

    if p_value > 0.10:  # Not co-integrated at 10% significance
        return {"pnl_net": -999, "trades": 0, "win_rate": 0, "max_dd": -100, "profit_factor": 0, "fees": 0}

    # Compute spread (log ratio for forex)
    spread = np.log(ca / cb)

    capital = INITIAL_CAPITAL
    peak = capital
    max_dd = 0.0
    trade = None  # ("BUY_SPREAD" or "SELL_SPREAD", entry_z, entry_a, entry_b, sz_a, sz_b, ts)
    wins = losses = 0
    total_pnl = total_fees = 0.0
    sum_win = sum_loss = 0.0
    n_trades = 0

    for i in range(z_window + 5, len(common)):
        ts = common[i]
        c_a = float(ca.iloc[i])
        c_b = float(cb.iloc[i])

        if capital <= 0:
            break

        # Z-score of spread
        window = spread.iloc[i - z_window:i]
        mu = window.mean()
        sigma = window.std()
        if sigma <= 0:
            continue
        z = (spread.iloc[i] - mu) / sigma

        # Friday Kill
        if is_tradfi and trade and hasattr(ts, 'weekday') and ts.weekday() == 4 and ts.hour >= FRIDAY_KILL_H:
            d, ez, ea, eb, sza, szb, bt = trade
            if d == "BUY_SPREAD":
                pnl = (c_a - ea) * sza + (eb - c_b) * szb
            else:
                pnl = (ea - c_a) * sza + (c_b - eb) * szb
            fees = (sza * ea + sza * c_a + szb * eb + szb * c_b) * FEE_PCT
            capital += pnl - fees; total_pnl += pnl; total_fees += fees; n_trades += 1
            if pnl > 0: wins += 1; sum_win += pnl
            else: losses += 1; sum_loss += abs(pnl)
            trade = None
            continue

        # Manage open trade
        if trade is not None:
            d, ez, ea, eb, sza, szb, bt = trade
            exit_trade = False

            # Mean reversion: close when Z crosses zero
            if d == "BUY_SPREAD" and z >= 0:
                exit_trade = True
            elif d == "SELL_SPREAD" and z <= 0:
                exit_trade = True
            # Stop-loss: Z extends to 2x threshold
            elif abs(z) > z_thr * 2:
                exit_trade = True
            # Time stop: 100 bars
            age = (ts - bt).total_seconds() / 3600
            if age > 100 * 4:  # ~100 bars on 4h
                exit_trade = True

            if exit_trade:
                if d == "BUY_SPREAD":
                    pnl = (c_a - ea) * sza + (eb - c_b) * szb
                else:
                    pnl = (ea - c_a) * sza + (c_b - eb) * szb
                fees = (sza * ea + sza * c_a + szb * eb + szb * c_b) * FEE_PCT
                capital += pnl - fees; total_pnl += pnl; total_fees += fees; n_trades += 1
                if pnl > 0: wins += 1; sum_win += pnl
                else: losses += 1; sum_loss += abs(pnl)
                trade = None
                peak = max(peak, capital)
                dd = (capital - peak) / peak * 100 if peak > 0 else -100
                max_dd = min(max_dd, dd)

        # Open new pairs trade
        if trade is None and capital > 0:
            half_risk = capital * RISK_PER_TRADE * 0.5

            if z <= -z_thr:
                # Spread too low → BUY spread (buy A, sell B)
                sza = half_risk / (c_a * 0.02 + 1e-10)  # Size based on 2% move
                szb = half_risk / (c_b * 0.02 + 1e-10)
                trade = ("BUY_SPREAD", z, c_a, c_b, sza, szb, ts)

            elif z >= z_thr:
                # Spread too high → SELL spread (sell A, buy B)
                sza = half_risk / (c_a * 0.02 + 1e-10)
                szb = half_risk / (c_b * 0.02 + 1e-10)
                trade = ("SELL_SPREAD", z, c_a, c_b, sza, szb, ts)

    # Close remaining
    if trade is not None:
        d, ez, ea, eb, sza, szb, bt = trade
        c_a = float(ca.iloc[-1]); c_b = float(cb.iloc[-1])
        if d == "BUY_SPREAD":
            pnl = (c_a - ea) * sza + (eb - c_b) * szb
        else:
            pnl = (ea - c_a) * sza + (c_b - eb) * szb
        fees = (sza * ea + sza * c_a + szb * eb + szb * c_b) * FEE_PCT
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
# MAIN — PROJECT LAZARUS
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    t0 = time.time()

    ml_combos = len(RF_ESTIMATORS) * len(RF_DEPTHS) * len(HORIZONS) * len(TIMEFRAMES)
    pairs_combos = len(SPREAD_Z_THRS) * len(SPREAD_WINDOWS) * len(TIMEFRAMES)

    print()
    print("═" * 70)
    print("  🧬 PROJECT LAZARUS — ML & Co-Integration Resurrection")
    print(f"  Targets  : {len(DEAD_ASSETS)} 'CONFIRMED DEAD' assets")
    print(f"  M52 ML   : {ml_combos} combos/asset (RF × depths × horizons × TFs)")
    print(f"  M53 PAIRS: {len(PAIRS_TO_TEST)} pairs × {pairs_combos} combos each")
    print(f"  Fitness  : PnL > 0€ AND MaxDD > {MAX_DD_LIMIT}% AND trades ≥ {MIN_TRADES}")
    print("═" * 70)

    # Download ALL data first (shared by ML and Pairs)
    print("\n  📥 Downloading data for all 13 assets...")
    all_data = {}
    for sym in DEAD_ASSETS:
        ticker = YF_MAP[sym]
        all_data[sym] = {}
        for tf in TIMEFRAMES:
            df = download(ticker, tf)
            if df is not None and len(df) > 100:
                all_data[sym][tf] = df
                print(f"    {sym}/{tf}: {len(df)} bougies ✅")
            else:
                print(f"    {sym}/{tf}: ❌")

    # ════════════════════════════════════
    # Phase 1: M52 ML — per-asset
    # ════════════════════════════════════
    print(f"\n{'═' * 70}")
    print(f"  🤖 PHASE 1: M52 ML ORACLE (RandomForest)")
    print(f"{'═' * 70}")

    ml_rescued = {}
    ml_dead = []

    for idx, sym in enumerate(DEAD_ASSETS, 1):
        print(f"\n  🧠 [{idx}/{len(DEAD_ASSETS)}] {sym}")

        found = False
        best_r = None
        best_cfg = None
        combos = 0

        for tf in TIMEFRAMES:
            if found: break
            if tf not in all_data[sym]:
                continue
            df = all_data[sym][tf]

            for n_est in RF_ESTIMATORS:
                if found: break
                for depth in RF_DEPTHS:
                    if found: break
                    for hz in HORIZONS:
                        combos += 1
                        r = backtest_ml(df, n_est, depth, hz, True)

                        if r["pnl_net"] > 0 and r["max_dd"] > MAX_DD_LIMIT and r["trades"] >= MIN_TRADES:
                            found = True
                            best_r = r
                            best_cfg = {"engine": "M52_ML", "tf": tf,
                                       "n_est": n_est, "depth": depth, "horizon": hz}
                            break

                        if best_r is None or r["pnl_net"] > best_r.get("pnl_net", -99999):
                            best_r = r
                            best_cfg = {"engine": "M52_ML", "tf": tf,
                                       "n_est": n_est, "depth": depth, "horizon": hz}

        if found:
            c = best_cfg; r = best_r
            print(f"  ✅ RESURRECTED ! ML/{c['tf']} n={c['n_est']} d={c['depth']} h={c['horizon']} → "
                  f"PnL={r['pnl_net']:+,.0f}€ WR={r['win_rate']:.0f}% "
                  f"DD={r['max_dd']:.1f}% ({r['trades']} trades) [{combos} combos]")
            ml_rescued[sym] = {**best_cfg, **best_r}
        else:
            r = best_r or {"pnl_net": 0}
            print(f"  💀 STILL DEAD ({combos} combos) | Best ML: PnL={r.get('pnl_net',0):+,.0f}€")
            ml_dead.append(sym)

    # ════════════════════════════════════
    # Phase 2: M53 PAIRS TRADING
    # ════════════════════════════════════
    print(f"\n{'═' * 70}")
    print(f"  🔗 PHASE 2: M53 PAIRS TRADING (Co-Integration)")
    print(f"{'═' * 70}")

    pairs_rescued = {}

    for idx, (sym_a, sym_b) in enumerate(PAIRS_TO_TEST, 1):
        pair_name = f"{sym_a}/{sym_b}"
        print(f"\n  🔗 [{idx}/{len(PAIRS_TO_TEST)}] {pair_name}")

        found = False
        best_r = None
        best_cfg = None
        combos = 0

        for tf in TIMEFRAMES:
            if found: break
            if tf not in all_data.get(sym_a, {}) or tf not in all_data.get(sym_b, {}):
                continue
            df_a = all_data[sym_a][tf]
            df_b = all_data[sym_b][tf]

            for z_thr in SPREAD_Z_THRS:
                if found: break
                for z_win in SPREAD_WINDOWS:
                    combos += 1
                    r = backtest_pairs(df_a, df_b, z_thr, z_win, True)

                    if r["pnl_net"] > 0 and r["max_dd"] > MAX_DD_LIMIT and r["trades"] >= MIN_TRADES:
                        found = True
                        best_r = r
                        best_cfg = {"engine": "M53_PAIRS", "tf": tf,
                                   "pair": pair_name,
                                   "z_threshold": z_thr, "z_window": z_win,
                                   "sym_a": sym_a, "sym_b": sym_b}
                        break

                    if best_r is None or r["pnl_net"] > best_r.get("pnl_net", -99999):
                        best_r = r
                        best_cfg = {"engine": "M53_PAIRS", "tf": tf,
                                   "pair": pair_name,
                                   "z_threshold": z_thr, "z_window": z_win}

        if found:
            c = best_cfg; r = best_r
            print(f"  ✅ PAIR RENTABLE ! {c['tf']} Z≥{c['z_threshold']} w={c['z_window']} → "
                  f"PnL={r['pnl_net']:+,.0f}€ WR={r['win_rate']:.0f}% "
                  f"DD={r['max_dd']:.1f}% ({r['trades']} trades) [{combos} combos]")
            pairs_rescued[pair_name] = {**best_cfg, **best_r}
        else:
            r = best_r or {"pnl_net": 0}
            status = "not cointegrated" if r.get("pnl_net", 0) == -999 else f"PnL={r.get('pnl_net',0):+,.0f}€"
            print(f"  💀 PAIR DEAD ({combos} combos) | {status}")

    elapsed = time.time() - t0

    # ═══════════════════════════════════════════════════════════════════════════
    # EXPORT & FINAL REPORT
    # ═══════════════════════════════════════════════════════════════════════════
    all_rescued = {}
    all_rescued.update(ml_rescued)
    for pair_name, v in pairs_rescued.items():
        all_rescued[f"PAIR_{pair_name.replace('/', '_')}"] = v

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lazarus_rules.json")
    with open(out_path, "w") as f:
        json.dump(all_rescued, f, indent=2)

    print()
    print("═" * 70)
    print("  🧬 PROJECT LAZARUS — FINAL REPORT")
    print("═" * 70)
    print(f"\n  ⏱️  Time: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"  🧠 M52 ML Rescued   : {len(ml_rescued)}/{len(DEAD_ASSETS)}")
    print(f"  🔗 M53 Pairs Found  : {len(pairs_rescued)}/{len(PAIRS_TO_TEST)}")

    if ml_rescued:
        ml_pnl = sum(v["pnl_net"] for v in ml_rescued.values())
        print(f"\n  🧠 ML RESURRECTED ({len(ml_rescued)} assets, PnL: {ml_pnl:+,.0f}€):")
        for sym in sorted(ml_rescued, key=lambda s: ml_rescued[s]["pnl_net"], reverse=True):
            v = ml_rescued[sym]
            print(f"    🟢 {sym:<10} ML/{v['tf']} n={v['n_est']} d={v['depth']} h={v['horizon']} → "
                  f"PnL={v['pnl_net']:>+7,.0f}€  WR={v['win_rate']:>5.1f}%  "
                  f"DD={v['max_dd']:>6.1f}%  trades={v['trades']}  PF={v['profit_factor']:.2f}")

    if pairs_rescued:
        pairs_pnl = sum(v["pnl_net"] for v in pairs_rescued.values())
        print(f"\n  🔗 PAIRS FOUND ({len(pairs_rescued)} pairs, PnL: {pairs_pnl:+,.0f}€):")
        for pn in sorted(pairs_rescued, key=lambda s: pairs_rescued[s]["pnl_net"], reverse=True):
            v = pairs_rescued[pn]
            print(f"    🟢 {pn:<15} {v['tf']} Z≥{v['z_threshold']} w={v['z_window']} → "
                  f"PnL={v['pnl_net']:>+7,.0f}€  WR={v['win_rate']:>5.1f}%  "
                  f"DD={v['max_dd']:>6.1f}%  trades={v['trades']}  PF={v['profit_factor']:.2f}")

    still_dead = [s for s in DEAD_ASSETS if s not in ml_rescued]
    if still_dead:
        print(f"\n  💀 ETERNALLY DEAD ({len(still_dead)}):")
        print(f"    {', '.join(still_dead)}")

    print(f"\n  💾 Exported: {out_path}")
    print(f"{'═' * 70}\n")


if __name__ == "__main__":
    main()
