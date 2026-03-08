"""
optimizer.py — Auto-Optimisation des paramètres de stratégie par symbole.

Pour chaque symbole, teste des combinaisons de paramètres sur 60 jours
et trouve la configuration la plus rentable. Sauvegarde les résultats
dans symbol_params.json que strategy.py charge automatiquement.

Usage :
    python3 optimizer.py                          # Optimise tous les symboles
    python3 optimizer.py --symbol BTC/USDT        # Un seul symbole
    python3 optimizer.py --days 90 --workers 4   # 90j, 4 threads
"""
import argparse
import json
import os
import sys
import concurrent.futures
from copy import deepcopy
from datetime import datetime, timezone, timedelta

from loguru import logger

logger.remove()
logger.add(sys.stdout, level="WARNING")  # Silencieux sauf erreurs

sys.path.insert(0, ".")
from backtester import fetch_historical, run_backtest_with_params, get_exchange

PARAMS_FILE   = "symbol_params.json"
DEFAULT_DAYS  = 60
DEFAULT_TF    = "15m"

# ─── Grille de paramètres à tester ───────────────────────────────────────────
# Chaque combinaison sera testée → ~108 backtests par symbole
PARAM_GRID = {
    "required_score":    [4, 5],
    "slope_threshold":   [0.00005, 0.0001, 0.0002],
    "adx_min":           [20, 25, 30],
    "atr_sl_multiplier": [0.8, 1.0, 1.2],
    "rsi_buy_max":       [60, 65, 70],
}

# ─── Générateur de combinaisons ───────────────────────────────────────────────
def param_combinations(grid: dict) -> list:
    from itertools import product
    keys   = list(grid.keys())
    values = list(grid.values())
    combos = []
    for combo in product(*values):
        combos.append(dict(zip(keys, combo)))
    return combos


# ─── Score de fitness — favorise rentabilité + solidité ──────────────────────
def fitness(n_trades: int, winrate: float, pnl_net: float, max_dd: float) -> float:
    """
    Score composite :
    - PnL net (priorité n°1)
    - Pénalité si < 5 trades (pas assez de données)
    - Pénalité si drawdown > 15%
    - Bonus si win rate > 50%
    """
    if n_trades < 5:
        return -9999.0
    score = pnl_net
    if max_dd > 15.0:
        score -= (max_dd - 15.0) * 50      # Pénalise les drawdowns excessifs
    if winrate > 50.0:
        score += (winrate - 50.0) * 10     # Bonus pour win rate > 50%
    return score


# ─── Optimisation d'un symbole ────────────────────────────────────────────────
def optimize_symbol(symbol: str, days: int, tf: str, df_cache=None) -> dict:
    """
    Teste toutes les combinaisons de paramètres sur un symbole.
    Retourne les meilleurs paramètres + leurs métriques.
    """
    exchange = get_exchange()

    if df_cache is None:
        print(f"  📥 {symbol} — téléchargement {days}j...")
        df_raw = fetch_historical(exchange, symbol, tf, days)
    else:
        df_raw = df_cache

    combos   = param_combinations(PARAM_GRID)
    best     = None
    best_fit = -9999999.0
    results  = []

    print(f"  🔄 {symbol} — {len(combos)} combinaisons à tester...")

    for i, params in enumerate(combos):
        try:
            trades, final_bal = run_backtest_with_params(df_raw, params, risk=0.01)
        except Exception as e:
            continue

        if not trades:
            continue

        n  = len(trades)
        wr = sum(1 for t in trades if t.result != "SL") / n * 100
        net = sum(t.pnl - t.fees for t in trades)

        # Max drawdown
        peak = 10000.0
        bal  = 10000.0
        max_dd = 0.0
        for t in trades:
            bal += t.pnl - t.fees
            if bal > peak:
                peak = bal
            dd = (peak - bal) / peak * 100
            if dd > max_dd:
                max_dd = dd

        fit = fitness(n, wr, net, max_dd)
        results.append({**params, "n_trades": n, "winrate": round(wr,1),
                        "pnl_net": round(net,2), "max_dd": round(max_dd,1),
                        "fitness": round(fit,2)})

        if fit > best_fit:
            best_fit = fit
            best     = {**params, "n_trades": n, "winrate": round(wr,1),
                        "pnl_net": round(net,2), "max_dd": round(max_dd,1)}

    if best is None:
        print(f"  ⚠️  {symbol} — aucun combo rentable trouvé, paramètres défaut")
        return _defaults()

    print(f"  ✅ {symbol} — meilleur: score={best['required_score']}/6 "
          f"ADX={best['adx_min']} SL×{best['atr_sl_multiplier']} "
          f"→ WR={best['winrate']}% PnL={best['pnl_net']:+.0f}$ DD={best['max_dd']}%")
    return best


def _defaults() -> dict:
    return {
        "required_score": 5, "slope_threshold": 0.0001,
        "adx_min": 25, "atr_sl_multiplier": 1.0, "rsi_buy_max": 65,
        "n_trades": 0, "winrate": 0.0, "pnl_net": 0.0, "max_dd": 0.0
    }


# ─── Chargement / sauvegarde des paramètres ──────────────────────────────────
def load_params() -> dict:
    if os.path.exists(PARAMS_FILE):
        with open(PARAMS_FILE) as f:
            return json.load(f)
    return {}


def save_params(params: dict):
    with open(PARAMS_FILE, "w") as f:
        json.dump(params, f, indent=2)
    print(f"\n💾 Paramètres sauvegardés → {PARAMS_FILE}")


# ─── Rapport global ───────────────────────────────────────────────────────────
def print_global_report(all_params: dict):
    print(f"\n{'='*65}")
    print(f"  ⚡ AlphaTrader — Résultats Optimisation Multi-Symboles")
    print(f"{'='*65}")
    print(f"  {'Symbole':<12} {'Score':<6} {'ADX':<6} {'SL×':<6} {'WR%':<8} {'PnL':<12} {'DD%'}")
    print(f"  {'-'*60}")
    total_pnl = 0.0
    for sym, p in sorted(all_params.items()):
        pnl = p.get('pnl_net', 0)
        total_pnl += pnl
        emoji = "🟢" if pnl > 0 else "🔴"
        print(f"  {emoji} {sym.replace('/USDT',''):<11} "
              f"{p['required_score']}/6   "
              f"{p['adx_min']:<6} "
              f"{p['atr_sl_multiplier']:<6} "
              f"{p.get('winrate',0):<8.1f} "
              f"{pnl:<+12.0f} "
              f"{p.get('max_dd',0):.1f}%")
    print(f"  {'-'*60}")
    print(f"  {'TOTAL PORTEFEUILLE':<40} {total_pnl:+.0f} USDT")
    print(f"{'='*65}\n")


# ─── Point d'entrée ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AlphaTrader — Optimiseur par symbole")
    parser.add_argument("--symbol",  default=None,        help="Symbole unique (ex: BTC/USDT)")
    parser.add_argument("--days",    type=int, default=DEFAULT_DAYS)
    parser.add_argument("--tf",      default=DEFAULT_TF)
    parser.add_argument("--workers", type=int, default=1, help="Threads parallèles")
    args = parser.parse_args()

    from config import SYMBOLS as ALL_SYMBOLS
    symbols = [args.symbol] if args.symbol else ALL_SYMBOLS

    print(f"\n🚀 AlphaTrader Optimizer — {len(symbols)} symboles × {len(param_combinations(PARAM_GRID))} combos")
    print(f"   Période : {args.days} jours | Timeframe : {args.tf}")
    print(f"   Durée estimée : ~{len(symbols) * 1.5:.0f} minutes\n")

    existing = load_params()

    if args.workers > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(optimize_symbol, s, args.days, args.tf): s for s in symbols}
            for fut in concurrent.futures.as_completed(futures):
                sym    = futures[fut]
                result = fut.result()
                existing[sym] = result
    else:
        for sym in symbols:
            result = optimize_symbol(sym, args.days, args.tf)
            existing[sym] = result

    save_params(existing)
    print_global_report(existing)
