#!/usr/bin/env python3
"""
recursive_analysis.py — Recursive Analysis (#7)

Vérifie que les indicateurs de la stratégie produisent des résultats
stables quelle que soit la quantité de données historiques disponibles.
(Simule la condition "live" où le bot commence avec peu d'historique)

Usage:
    python3 recursive_analysis.py
    python3 recursive_analysis.py --symbol XRP/USDT
"""
import sys, warnings, argparse
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")
import pandas as pd
import numpy as np
from backtester import fetch_historical, get_exchange
from optimizer import precompute

WARMUP_PERIODS = [50, 100, 200, 500, 1000]  # Nombre de bougies de warmup


def recursive_check(symbol: str, timeframe: str = "5m", days: int = 60) -> dict:
    """
    Teste si les indicateurs sont stables avec différentes quantités de données.
    Si la valeur de l'indicateur varie >0.1% selon le warmup → instabilité détectée.
    """
    exc = get_exchange()
    print(f"\n  📥 {symbol} — téléchargement {days}j...")
    df = fetch_historical(exc, symbol, timeframe, days)
    df = precompute(df)

    if df is None or len(df) < max(WARMUP_PERIODS) + 10:
        print(f"  ❌ Pas assez de données ({len(df) if df is not None else 0} bougies)")
        return {}

    indicators = ["ema9", "ema21", "ema200", "rsi", "adx", "atr"]
    available  = [i for i in indicators if i in df.columns]
    results    = {}

    print(f"  🔬 Test de stabilité sur {WARMUP_PERIODS} périodes de warmup...")

    for col in available:
        values = {}
        for warmup in WARMUP_PERIODS:
            if warmup >= len(df) - 5:
                continue
            df_slice = df.iloc[-warmup:].copy()
            df_slice = precompute(df_slice)
            if col in df_slice.columns and len(df_slice) > 0:
                values[warmup] = float(df_slice.iloc[-1][col])

        if len(values) < 2:
            continue

        vals_arr = np.array(list(values.values()))
        # Variation relative max entre les valeurs
        rel_var = (vals_arr.max() - vals_arr.min()) / (abs(vals_arr.mean()) + 1e-10)
        stable = rel_var < 0.001  # < 0.1% de variation = stable

        results[col] = {"stable": stable, "variation_pct": rel_var * 100, "values": values}
        status = "✅" if stable else "⚠️ "
        print(f"  {status}  {col:<14} variation = {rel_var*100:.4f}%")

    unstable = [k for k, v in results.items() if not v["stable"]]
    stable_l = [k for k, v in results.items() if v["stable"]]

    print(f"\n  {'-'*50}")
    if unstable:
        print(f"  ⚠️  Indicateurs instables : {', '.join(unstable)}")
        print(f"     → Augmenter le warmup minimum dans config.py")
    else:
        print(f"  ✅ Tous les indicateurs sont stables ({symbol})")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--days",   type=int, default=60)
    args = parser.parse_args()

    from config import SYMBOLS
    symbols = [args.symbol] if args.symbol else SYMBOLS

    print(f"\n🔁 Recursive Analysis — Nemesis\n")
    for sym in symbols:
        print(f"\n  ═══ {sym} ═══")
        recursive_check(sym, days=args.days)

    print(f"\n{'='*52}")
    print(f"  Recursive Analysis terminée.")
    print(f"{'='*52}\n")
