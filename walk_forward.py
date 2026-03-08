"""
walk_forward.py — Walk-Forward Optimization Automatique (#2)

Divise l'historique en fenêtres glissantes :
  - In-sample (IS)  : 60 jours → optimiser les paramètres
  - Out-of-sample (OOS) : 20 jours → valider sur données non vues

Méthode :
  1. Divise 80 jours d'historique en IS+OOS
  2. Hyperopt Optuna sur IS → params optimaux
  3. Backtest sur OOS avec ces params
  4. Décale d'1 fenêtre → répète

Usage :
    python3 walk_forward.py
    python3 walk_forward.py --symbol ETH/USDT --windows 3
"""
import sys, warnings, argparse, json, os
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

import numpy as np
from loguru import logger
from datetime import datetime, timezone

try:
    from backtester import fetch_historical, get_exchange
    from optimizer import precompute, vectorized_backtest, _default_params
    from config import SYMBOLS
except ImportError as e:
    print(f"❌ Import error: {e}")
    sys.exit(1)

IS_DAYS     = 60   # In-sample : 60 jours
OOS_DAYS    = 20   # Out-of-sample : 20 jours
WFO_FILE    = "walk_forward_results.json"


def run_wfo(symbol: str, n_windows: int = 3, n_trials: int = 30) -> dict:
    """
    Lance le Walk-Forward Optimization pour un symbole.
    Retourne les résultats OOS moyens.
    """
    exc        = get_exchange()
    total_days = IS_DAYS + OOS_DAYS + (n_windows - 1) * OOS_DAYS + 5
    print(f"\n  📥 {symbol} — {total_days}j de données ({n_windows} fenêtres)...")

    try:
        df = fetch_historical(exc, symbol, "5m", total_days)
        df = precompute(df)
    except Exception as e:
        logger.error(f"WFO {symbol}: {e}")
        return {}

    cpd     = 288   # candles per day (5m)
    oos_n   = OOS_DAYS * cpd
    is_n    = IS_DAYS  * cpd
    results = []

    for w in range(n_windows):
        offset    = w * oos_n
        end_oos   = len(df) - offset
        start_oos = end_oos - oos_n
        start_is  = start_oos - is_n

        if start_is < 0:
            print(f"    ⚠️  Fenêtre {w+1} : données insuffisantes")
            break

        is_df  = df.iloc[start_is:start_oos].copy()
        oos_df = df.iloc[start_oos:end_oos].copy()

        if len(is_df) < 500 or len(oos_df) < 100:
            break

        print(f"\n  ── Fenêtre {w+1}/{n_windows} ─────────────────────────")
        print(f"     IS  : {len(is_df)} bougies | OOS : {len(oos_df)} bougies")

        # Optimisation Optuna sur IS
        best_params = _default_params()
        try:
            import optuna
            optuna.logging.set_verbosity(optuna.logging.WARNING)

            def objective(trial):
                p = {
                    "ema_fast":      trial.suggest_int("ema_fast",     5,  20),
                    "ema_slow":      trial.suggest_int("ema_slow",    20,  60),
                    "rsi_period":    trial.suggest_int("rsi_period",  10,  21),
                    "rsi_ob":        trial.suggest_int("rsi_ob",      65,  80),
                    "rsi_os":        trial.suggest_int("rsi_os",      20,  35),
                    "atr_sl_mult":   trial.suggest_float("atr_sl_mult", 0.8, 2.5),
                    "tp_multiplier": trial.suggest_float("tp_multiplier", 1.5, 4.0),
                    "min_adx":       trial.suggest_int("min_adx",     15,  35),
                }
                n, wr, pnl, dd, sharpe, _ = vectorized_backtest(is_df, p)
                if n < 3: return -999
                return pnl * (1 + sharpe) - dd * 2

            study = optuna.create_study(direction="maximize")
            study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
            best_params = study.best_params
            print(f"     Best IS score: {study.best_value:.2f}")
        except Exception as e:
            logger.debug(f"WFO optuna w{w}: {e}")

        # Validation OOS
        n, wr, pnl, dd, sharpe, sortino = vectorized_backtest(oos_df, best_params)

        verdict = "✅" if sharpe > 0 and wr > 45 else "⚠️ " if sharpe > -0.1 else "❌"
        print(f"     {verdict} OOS — Trades={n}  WR={wr:.0f}%  PnL={pnl:+.2f}$  Sharpe={sharpe:.3f}")

        results.append({
            "window": w + 1, "trades": n,
            "wr": round(wr, 1), "pnl": round(pnl, 2),
            "max_dd": round(dd, 2), "sharpe": round(sharpe, 3),
        })

    if not results:
        return {}

    avg_sharpe = round(np.mean([r["sharpe"] for r in results]), 3)
    avg_wr     = round(np.mean([r["wr"]     for r in results]), 1)
    avg_pnl    = round(np.mean([r["pnl"]    for r in results]), 2)
    avg_dd     = round(np.mean([r["max_dd"] for r in results]), 2)
    robust     = avg_sharpe > 0.1 and avg_wr > 40

    status = "✅ ROBUSTE" if robust else "⚠️  FRAGILE"
    print(f"\n  {status} — Sharpe OOS={avg_sharpe:.3f}  WR={avg_wr:.0f}%  PnL={avg_pnl:+.2f}$")

    return {
        "symbol": symbol, "n_windows": len(results), "windows": results,
        "avg_sharpe": avg_sharpe, "avg_wr": avg_wr,
        "avg_pnl": avg_pnl, "avg_dd": avg_dd, "robust": robust,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def save_results(results: dict):
    existing = {}
    if os.path.exists(WFO_FILE):
        try:
            with open(WFO_FILE) as f:
                existing = json.load(f)
        except Exception:
            pass
    existing.update(results)
    with open(WFO_FILE, "w") as f:
        json.dump(existing, f, indent=2)
    logger.info(f"💾 WFO sauvegardé → {WFO_FILE}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol",  default=None)
    parser.add_argument("--windows", type=int, default=3)
    parser.add_argument("--trials",  type=int, default=30)
    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else SYMBOLS[:2]

    print(f"\n🔄 Walk-Forward Optimization — AlphaTrader")
    print(f"   IS={IS_DAYS}j | OOS={OOS_DAYS}j | Fenêtres={args.windows} | Trials={args.trials}\n")

    all_results = {}
    for sym in symbols:
        res = run_wfo(sym, args.windows, args.trials)
        if res:
            all_results[sym] = res

    save_results(all_results)

    print(f"\n{'═'*52}")
    print(f"  VERDICT FINAL — Walk-Forward")
    print(f"{'═'*52}")
    for sym, res in all_results.items():
        status = "✅ ROBUSTE" if res["robust"] else "⚠️  FRAGILE"
        print(f"  {sym:<14} {status}  Sharpe={res['avg_sharpe']:.3f}  WR={res['avg_wr']:.0f}%")
    print()
