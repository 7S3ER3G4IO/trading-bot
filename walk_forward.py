#!/usr/bin/env python3
"""
walk_forward.py — Walk-Forward Testing (#5)

Valide la stratégie sur des données que l'optimiseur n'a PAS vues.
Principe : entraîner sur N jours, tester sur les M jours suivants (out-of-sample).

Usage :
    python3 walk_forward.py                    # 12 fenêtres (défaut)
    python3 walk_forward.py --windows 8        # 8 fenêtres
    python3 walk_forward.py --train 14 --test 7
"""
import argparse, sys, json, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")
import pandas as pd
from datetime import datetime, timezone, timedelta
from optimizer import get_exchange, download, precompute, vectorized_backtest, fitness
from backtester import fetch_historical

SESSION = set(range(7, 11)) | set(range(13, 17))
CAPITAL = 10_000.

def walk_forward_test(symbol: str, train_days: int = 14, test_days: int = 7,
                      windows: int = 8, trials: int = 50) -> dict:
    """Teste la stratégie sur des données out-of-sample."""
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    total_days = (train_days + test_days) * windows
    exc = get_exchange()
    print(f"  📥 {symbol} — {total_days}j de données...")
    df_all = fetch_historical(exc, symbol, "5m", total_days)
    df_all = precompute(df_all)
    if df_all is None:
        return {}

    results = []
    candles_per_day = 288  # 5m
    train_len = train_days * candles_per_day
    test_len  = test_days  * candles_per_day

    for w in range(windows):
        start = w * test_len
        train_end = start + train_len
        test_end  = train_end + test_len
        if test_end > len(df_all):
            break

        df_train = df_all.iloc[start:train_end]
        df_test  = df_all.iloc[train_end:test_end]

        # Optimise sur la fenêtre d'entraînement
        def objective(trial):
            p = {
                "required_score":    trial.suggest_int("required_score", 4, 6),
                "slope_threshold":   trial.suggest_float("slope_threshold", 0.00005, 0.0005, log=True),
                "adx_min":           trial.suggest_int("adx_min", 15, 40),
                "atr_sl_multiplier": trial.suggest_float("atr_sl_multiplier", 0.6, 2.0),
                "rsi_buy_max":       trial.suggest_int("rsi_buy_max", 55, 75),
                "tp_multiplier":     trial.suggest_float("tp_multiplier", 1.5, 4.0),
            }
            n, wr, pnl, dd = vectorized_backtest(df_train, p)
            if n < 3: raise optuna.exceptions.TrialPruned()
            score = pnl
            if dd > 15: score -= (dd - 15) * 80
            return score

        study = optuna.create_study(direction="maximize",
                  sampler=optuna.samplers.TPESampler(seed=w))
        study.optimize(objective, n_trials=trials, show_progress_bar=False)
        best_p = study.best_params

        # Teste sur la fenêtre out-of-sample
        n, wr, pnl, dd = vectorized_backtest(df_test, best_p)
        t_start = df_test.index[0].strftime("%Y-%m-%d")
        t_end   = df_test.index[-1].strftime("%Y-%m-%d")
        results.append({"window": w+1, "start": t_start, "end": t_end,
                        "n": n, "wr": wr, "pnl": pnl, "dd": dd})
        e = "🟢" if pnl > 0 else "🔴"
        print(f"    {e} Win {w+1:2d} [{t_start}→{t_end}]  "
              f"{n}t  WR={wr:.0f}%  PnL={pnl:>+.0f}$  DD={dd:.1f}%")

    if not results: return {}
    avg_pnl = sum(r["pnl"] for r in results) / len(results)
    wins    = sum(1 for r in results if r["pnl"] > 0)
    return {"symbol": symbol, "windows": len(results), "wins": wins,
            "avg_pnl_per_window": round(avg_pnl, 2),
            "win_pct": round(wins/len(results)*100, 1)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--train",   type=int, default=14)
    parser.add_argument("--test",    type=int, default=7)
    parser.add_argument("--windows", type=int, default=6)
    parser.add_argument("--trials",  type=int, default=40)
    args = parser.parse_args()

    from config import SYMBOLS
    print(f"\n🔍 Walk-Forward Test | train={args.train}j test={args.test}j | {args.windows} fenêtres | {args.trials} trials\n")

    all_res = {}
    for sym in SYMBOLS:
        print(f"\n  ═══ {sym} ═══")
        res = walk_forward_test(sym, args.train, args.test, args.windows, args.trials)
        if res:
            all_res[sym] = res
            e = "🟢" if res["win_pct"] >= 50 else "🔴"
            print(f"  {e} {sym:<12} → {res['wins']}/{res['windows']} fenêtres positives "
                  f"({res['win_pct']}%) | PnL moy/fenêtre : {res['avg_pnl_per_window']:>+.0f}$")

    print(f"\n{'='*60}")
    print(f"  Walk-Forward Summary")
    print(f"{'='*60}")
    for sym, r in all_res.items():
        e = "🟢" if r["win_pct"] >= 50 else "🔴"
        print(f"  {e} {sym:<12}  {r['win_pct']:>5.1f}% positif  "
              f"avg PnL/sem = {r['avg_pnl_per_window']:>+.0f}$")
    print(f"{'='*60}\n")
