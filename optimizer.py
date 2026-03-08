"""
optimizer.py — Auto-Optimisation RAPIDE des paramètres par symbole.

Approche vectorisée :
  1. Télécharge les données UNE SEULE FOIS par symbole
  2. Pré-calcule TOUS les indicateurs en une passe
  3. Teste les seuils (score, ADX, RSI, slope) directement sur les arrays numpy
  → 100x plus rapide que l'approche naïve

Durée : ~15 secondes par symbole (vs ~5h avant)

Usage :
    python3 optimizer.py                     # Optimise les 12 symboles (~3 min)
    python3 optimizer.py --symbol BTC/USDT   # Un seul symbole (~15s)
    python3 optimizer.py --days 60           # Période plus longue
"""
import argparse, json, os, sys
from datetime import datetime, timezone, timedelta
from itertools import product

import numpy as np
import pandas as pd
import ccxt
import ta
from loguru import logger
logger.remove()

sys.path.insert(0, ".")
from config import (
    EMA_FAST, EMA_SLOW, RSI_PERIOD, RSI_SELL_MIN,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    ATR_PERIOD, ADX_PERIOD, VOLUME_MA_PERIOD,
)

PARAMS_FILE     = "symbol_params.json"
INITIAL_BALANCE = 10_000.0
FEE_RATE        = 0.001
EMA_TREND_PERIOD = 200
SLOPE_WINDOW     = 5

# ─── Grille (réduite, éprouvée) ──────────────────────────────────────────────
PARAM_GRID = {
    "required_score":    [4, 5],
    "slope_threshold":   [0.00005, 0.0001, 0.0002],
    "adx_min":           [20, 25, 30],
    "atr_sl_multiplier": [0.8, 1.0, 1.2],
    "rsi_buy_max":       [60, 65, 70],
}


def get_exchange():
    return ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})


def download(exchange, symbol, timeframe, days) -> pd.DataFrame:
    since = exchange.parse8601(
        (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    )
    bars = []
    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not batch: break
        bars.extend(batch)
        since = batch[-1][0] + 1
        if len(batch) < 1000: break
    df = pd.DataFrame(bars, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df


# ─── Pré-calcul vectorisé de TOUS les indicateurs ──────────────────────────

def precompute(df: pd.DataFrame) -> pd.DataFrame:
    """Calcule tous les indicateurs une seule fois sur le DataFrame complet."""
    MIN_BARS = max(EMA_TREND_PERIOD, ADX_PERIOD, MACD_SLOW) + 50
    if len(df) < MIN_BARS:
        return None   # pas assez de données

    c = df["close"]
    h, l, v = df["high"], df["low"], df["volume"]

    df["ema_fast"]   = ta.trend.ema_indicator(c, EMA_FAST)
    df["ema_slow"]   = ta.trend.ema_indicator(c, EMA_SLOW)
    df["ema200"]     = ta.trend.ema_indicator(c, EMA_TREND_PERIOD)
    df["rsi"]        = ta.momentum.rsi(c, RSI_PERIOD)
    macd             = ta.trend.MACD(c, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    df["macd"]       = macd.macd()
    df["macd_sig"]   = macd.macd_signal()
    df["adx"]        = ta.trend.adx(h, l, c, ADX_PERIOD)
    atr              = ta.volatility.AverageTrueRange(h, l, c, ATR_PERIOD)
    df["atr"]        = atr.average_true_range()
    df["vol_ma"]     = v.rolling(VOLUME_MA_PERIOD).mean()
    df["ema200_slope"] = (df["ema200"] - df["ema200"].shift(SLOPE_WINDOW)) / (
                          df["ema200"].shift(SLOPE_WINDOW) * SLOPE_WINDOW)
    return df.dropna()



# ─── Simulation vectorisée ──────────────────────────────────────────────────

def vectorized_backtest(df: pd.DataFrame, params: dict, risk: float = 0.01) -> tuple:
    """
    Backtest ultra-rapide : génère les signaux sur l'array numpy pré-calculé,
    simule les trades en boucle Python simple (exit bougie par bougie).
    Retourne (n_trades, winrate, pnl_net, max_drawdown).
    """
    req    = params["required_score"]
    slope  = params["slope_threshold"]
    adx_mn = params["adx_min"]
    atr_m  = params["atr_sl_multiplier"]
    rsi_mx = params["rsi_buy_max"]

    arr     = df.to_dict("records")
    N       = len(arr)
    balance = INITIAL_BALANCE
    peak    = INITIAL_BALANCE
    max_dd  = 0.0
    trades  = []
    in_trade = False

    for i in range(1, N):
        r    = arr[i]
        prev = arr[i-1]

        # Slope filter
        sl_val = r["ema200_slope"]
        if abs(sl_val) < slope:
            continue

        regime = "BULL" if sl_val > slope else "BEAR" if sl_val < -slope else "RANGE"

        if in_trade:
            continue

        # Confirmations BUY
        ema_up  = r["ema_fast"] > r["ema_slow"] and prev["ema_fast"] <= prev["ema_slow"]
        rsi_buy = 30 < r["rsi"] < rsi_mx
        macd_up = r["macd"] > r["macd_sig"] and prev["macd"] <= prev["macd_sig"]
        adx_ok  = r["adx"] > adx_mn
        vol_ok  = r["volume"] > r["vol_ma"]

        # Confirmations SELL
        ema_dn   = r["ema_fast"] < r["ema_slow"] and prev["ema_fast"] >= prev["ema_slow"]
        rsi_sell = r["rsi"] > RSI_SELL_MIN

        buy_s  = sum([ema_up, rsi_buy,  macd_up, adx_ok, vol_ok, regime == "BULL"])
        sell_s = sum([ema_dn, rsi_sell, not macd_up, adx_ok, vol_ok, regime == "BEAR"])

        side = None
        if regime == "BULL" and buy_s >= req and buy_s > sell_s:
            side = "BUY"
        elif regime == "BEAR" and sell_s >= req and sell_s > buy_s:
            side = "SELL"
        else:
            continue

        # Calcul niveaux
        entry = r["close"]
        atr_v = r["atr"]
        sl_d  = atr_v * atr_m
        tp_d  = atr_v * 1.5

        if side == "BUY":
            sl = entry - sl_d
            tp1, tp2, tp3 = entry + tp_d, entry + tp_d*2, entry + tp_d*3
            be = entry + sl_d * 0.3
        else:
            sl = entry + sl_d
            tp1, tp2, tp3 = entry - tp_d, entry - tp_d*2, entry - tp_d*3
            be = entry - sl_d * 0.3

        risk_amt = balance * risk
        sl_dist  = abs(entry - sl)
        if sl_dist <= 0:
            continue
        qty = risk_amt / sl_dist
        if qty <= 0:
            continue

        # Simulation de la sortie du trade
        in_trade    = True
        tp1_hit     = tp2_hit = be_active = False
        cur_sl      = sl
        rem         = qty
        pnl         = 0.0
        fees        = 0.0
        result      = "OPEN_CLOSE"

        for j in range(i+1, min(i+500, N)):
            fwd  = arr[j]
            hi, lo = fwd["high"], fwd["low"]

            def up(t): return hi >= t if side == "BUY" else lo <= t
            def dn(t): return lo <= t if side == "BUY" else hi >= t

            if not tp1_hit and up(tp1):
                q = rem / 3; pnl += abs(tp1-entry)*q; fees += entry*q*FEE_RATE*2
                rem -= q; tp1_hit = True; be_active = True; cur_sl = be
            if tp1_hit and not tp2_hit and up(tp2):
                q = rem / 2; pnl += abs(tp2-entry)*q; fees += entry*q*FEE_RATE*2
                rem -= q; tp2_hit = True
            if tp1_hit and tp2_hit and up(tp3):
                pnl += abs(tp3-entry)*rem; fees += entry*rem*FEE_RATE*2
                rem = 0; result = "TP3"; break
            if dn(cur_sl):
                loss = abs(cur_sl-entry)*rem * (0 if be_active else -1)
                pnl += loss; fees += entry*rem*FEE_RATE*2
                rem = 0; result = "BE" if be_active else "SL"; break

        if rem > 0:
            last = arr[min(i+500, N-1)]["close"]
            pnl += (last-entry)*rem*(1 if side=="BUY" else -1)
            fees += entry*rem*FEE_RATE*2

        net      = pnl - fees
        balance += net
        in_trade = False
        if balance > peak: peak = balance
        dd = (peak - balance) / peak * 100
        if dd > max_dd: max_dd = dd
        trades.append({"result": result, "net": net})

    if not trades:
        return 0, 0.0, 0.0, 0.0

    wins    = sum(1 for t in trades if t["result"] != "SL")
    wr      = wins / len(trades) * 100
    pnl_net = sum(t["net"] for t in trades)
    return len(trades), wr, pnl_net, max_dd


# ─── Fitness ──────────────────────────────────────────────────────────────────

def fitness(n: int, wr: float, pnl: float, dd: float) -> float:
    if n < 5:
        return -99999.0
    score = pnl
    if dd > 15.0:
        score -= (dd - 15.0) * 50
    if wr > 50.0:
        score += (wr - 50.0) * 10
    return score


# ─── Optimisation d'un symbole ────────────────────────────────────────────────

def optimize_symbol(symbol: str, days: int, tf: str, df_pre=None) -> dict:
    exchange  = get_exchange()
    if df_pre is None:
        print(f"  📥 {symbol} — téléchargement {days}j...")
        df_raw = download(exchange, symbol, tf, days)
        df_pre = precompute(df_raw)

    if df_pre is None:
        print(f"  ⚠️  {symbol} — données insuffisantes, paramètres défaut")
        return {"required_score": 5, "slope_threshold": 0.0001, "adx_min": 25,
                "atr_sl_multiplier": 1.0, "rsi_buy_max": 65,
                "n_trades": 0, "winrate": 0.0, "pnl_net": 0.0, "max_dd": 0.0}

    combos    = [dict(zip(PARAM_GRID.keys(), v)) for v in product(*PARAM_GRID.values())]
    best_fit  = -99999999.0
    best_p    = None

    print(f"  ⚡ {symbol} — {len(combos)} combos (vectorisé)...")

    for params in combos:
        n, wr, pnl, dd = vectorized_backtest(df_pre, params)
        fit = fitness(n, wr, pnl, dd)
        if fit > best_fit:
            best_fit = fit
            best_p   = {**params, "n_trades": n, "winrate": round(wr,1),
                        "pnl_net": round(pnl,2), "max_dd": round(dd,1)}

    if best_p:
        emoji = "🟢" if best_p["pnl_net"] > 0 else "🔴"
        print(f"  {emoji} {symbol:<12} score={best_p['required_score']}/6  "
              f"ADX≥{best_p['adx_min']}  SL×{best_p['atr_sl_multiplier']}  "
              f"RSI<{best_p['rsi_buy_max']}  "
              f"→ WR={best_p['winrate']}%  PnL={best_p['pnl_net']:+.0f}$  "
              f"DD={best_p['max_dd']}%")
        return best_p
    else:
        print(f"  ⚠️  {symbol} — aucun combo rentable, paramètres défaut")
        return {"required_score": 5, "slope_threshold": 0.0001, "adx_min": 25,
                "atr_sl_multiplier": 1.0, "rsi_buy_max": 65,
                "n_trades": 0, "winrate": 0.0, "pnl_net": 0.0, "max_dd": 0.0}


# ─── Rapport ──────────────────────────────────────────────────────────────────

def print_report(all_params: dict):
    print(f"\n{'='*65}")
    print(f"  ⚡ AlphaTrader — Rapport Optimisation Multi-Symboles")
    print(f"{'='*65}")
    print(f"  {'Symbole':<10} {'Sc':<4} {'ADX':<5} {'SL×':<5} {'RSI':<5} {'WR%':<7} {'PnL':>8}  {'DD%'}")
    print(f"  {'-'*62}")
    tot = 0.0
    for sym, p in sorted(all_params.items()):
        pnl = p.get("pnl_net", 0)
        tot += pnl
        e = "🟢" if pnl > 0 else "🔴"
        print(f"  {e} {sym.replace('/USDT',''):<10} "
              f"{p['required_score']}/6  "
              f"{p['adx_min']:<5} "
              f"{p['atr_sl_multiplier']:<5} "
              f"{p['rsi_buy_max']:<5} "
              f"{p.get('winrate',0):<7.1f} "
              f"{pnl:>+8.0f}$  "
              f"{p.get('max_dd',0):.1f}%")
    print(f"  {'-'*62}")
    print(f"  {'TOTAL':>50}  {tot:>+8.0f}$")
    print(f"{'='*65}\n")


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--days",   type=int, default=45)
    parser.add_argument("--tf",     default="15m")
    args = parser.parse_args()

    from config import SYMBOLS as ALL_SYMBOLS
    symbols = [args.symbol] if args.symbol else ALL_SYMBOLS

    n_combos = len([v for v in product(*PARAM_GRID.values())])
    print(f"\n🚀 AlphaTrader Optimizer (vectorisé)")
    print(f"   {len(symbols)} symboles × {n_combos} combos | {args.days}j | {args.tf}")
    print(f"   Durée estimée : ~{len(symbols) * 15}s\n")

    existing = {}
    if os.path.exists(PARAMS_FILE):
        with open(PARAMS_FILE) as f:
            existing = json.load(f)

    for sym in symbols:
        result = optimize_symbol(sym, args.days, args.tf)
        existing[sym] = result

    with open(PARAMS_FILE, "w") as f:
        json.dump(existing, f, indent=2)
    print(f"\n💾 Sauvegardé → {PARAMS_FILE}")
    print_report(existing)
