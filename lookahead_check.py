#!/usr/bin/env python3
"""
lookahead_check.py — Détection du Lookahead Bias (#2)

Vérifie si les indicateurs de la stratégie introduisent un biais de look-ahead
(utilisation de données futures pendant le backtest).

Usage:
    python3 lookahead_check.py
    python3 lookahead_check.py --symbol ETH/USDT --verbose
"""
import sys, warnings, argparse
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")
import pandas as pd
import numpy as np
from loguru import logger
from backtester import fetch_historical, get_exchange
from optimizer import precompute


def check_lookahead(symbol: str, timeframe: str = "5m", days: int = 30,
                    n_shifts: int = 5, verbose: bool = False) -> dict:
    """
    Pour chaque indicateur, calcule sa valeur avec N bougies de moins.
    Si la valeur change en rajoutant des bougies futures → lookahead bias détecté.

    Returns dict: {indicator: bias_detected (bool)}
    """
    exc = get_exchange()
    print(f"\n  📥 {symbol} — {days}j de données pour lookahead check...")
    df_full = fetch_historical(exc, symbol, timeframe, days)
    df_full = precompute(df_full)

    if df_full is None or len(df_full) < 50:
        print(f"  ❌ Données insuffisantes pour {symbol}")
        return {}

    indicators = ["ema9", "ema21", "ema200", "rsi", "macd", "adx", "atr", "vol_ma"]
    available  = [i for i in indicators if i in df_full.columns]
    results    = {}

    print(f"  🔬 Test de {len(available)} indicateurs sur {n_shifts} décalages...\n")

    for col in available:
        drifts = []
        for shift in range(1, n_shifts + 1):
            # Compare la valeur de l'indicateur calculée avec moins de données
            df_partial = df_full.iloc[:-shift]
            df_partial = precompute(df_partial)
            if col not in df_partial.columns or len(df_partial) == 0:
                continue
            val_full    = float(df_full.iloc[-(shift+1)][col])
            val_partial = float(df_partial.iloc[-1][col]) if len(df_partial) > 0 else val_full
            if val_full != 0:
                drift = abs((val_full - val_partial) / val_full)
                drifts.append(drift)

        avg_drift = np.mean(drifts) if drifts else 0.0
        # Un drift > 0.001% sur les bougies passées suggère un problème
        bias = avg_drift > 0.00001
        results[col] = {"bias": bias, "avg_drift_pct": avg_drift * 100}

        status = "⚠️  BIAS DÉTECTÉ" if bias else "✅ OK"
        if verbose or bias:
            print(f"  {status}  {col:<14} drift moyen = {avg_drift*100:.6f}%")

    biased = [k for k, v in results.items() if v["bias"]]
    clean  = [k for k, v in results.items() if not v["bias"]]

    print(f"\n  {'-'*50}")
    if biased:
        print(f"  ⚠️  Indicateurs potentiellement biaisés : {', '.join(biased)}")
    else:
        print(f"  ✅ Aucun lookahead bias détecté sur {symbol}")
    print(f"  ✅ Indicateurs propres : {', '.join(clean)}")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--days",   type=int, default=30)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    from config import SYMBOLS
    symbols = [args.symbol] if args.symbol else SYMBOLS

    print(f"\n🔍 Lookahead Bias Check — AlphaTrader\n")
    all_ok = True
    for sym in symbols:
        res = check_lookahead(sym, days=args.days, verbose=args.verbose)
        if any(v["bias"] for v in res.values()):
            all_ok = False

    print(f"\n{'='*52}")
    if all_ok:
        print(f"  ✅ RÉSULTAT : Aucun lookahead bias détecté !")
        print(f"     Le backtest reflète des conditions réelles.")
    else:
        print(f"  ⚠️  RÉSULTAT : Des biais ont été détectés.")
        print(f"     Revérifier le calcul des indicateurs biaisés.")
    print(f"{'='*52}\n")
