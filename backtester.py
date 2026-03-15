"""
backtester.py — Moteur de Backtesting NEMESIS
Rejoue la stratégie BK/MR/TF sur données OHLCV historiques (CSV ou MT5).
Simule TP1 (1.5R, 40%) + TP2 (2.5R, 40%) + SL (100%), compute P&L + Sharpe.

Usage :
    python backtester.py --symbol EURUSD --days 90
    python backtester.py --symbol GOLD --days 60 --strategy BK --csv data.csv
    python backtester.py --all --days 30          # backteste tous les instruments
"""
import os
import sys
import math
import argparse
import statistics
from datetime import datetime, timezone, timedelta
from typing import Optional
from loguru import logger

# ─── Optionnel — pas de dépendance à tout le bot ─────────────────────────────
try:
    import pandas as pd
    _PD_OK = True
except ImportError:
    _PD_OK = False
    print("❌ pandas requis : pip install pandas")
    sys.exit(1)

# ─── Config par défaut ───────────────────────────────────────────────────────

RISK_PER_TRADE  = 0.0035   # 0.35% par trade
INITIAL_BALANCE = 100_000.0
TP1_R           = 1.5      # TP1 = 1.5× le risque
TP2_R           = 2.5      # TP2 = 2.5× le risque
TP1_PORTION     = 0.40     # 40% fermé au TP1
TP2_PORTION     = 0.40     # 40% fermé au TP2
SL_PORTION      = 1.00     # 100% perdu au SL
SPREAD_PIPS     = 2        # spread simulé (pips)
COMMISSION_USD  = 3.5      # commission par aller-retour ($)


# ─── Fonctions helpers ────────────────────────────────────────────────────────

def load_strategy():
    """Charge le module strategy.py du bot."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from strategy import Strategy
        return Strategy()
    except Exception as e:
        logger.error(f"Impossible de charger strategy.py : {e}")
        sys.exit(1)


def load_ohlcv_from_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["time"] if "time" in pd.read_csv(path, nrows=0).columns else True)
    df.columns = [c.lower() for c in df.columns]
    if "date" in df.columns and "time" not in df.columns:
        df.rename(columns={"date": "time"}, inplace=True)
    df = df.sort_values("time").reset_index(drop=True)
    return df


def load_ohlcv_from_mt5(symbol: str, days: int) -> Optional[pd.DataFrame]:
    """Charge les données OHLCV depuis MT5 via le client du bot."""
    try:
        from brokers.mt5_client import MT5Client
        from brokers.capital_client import ASSET_PROFILES
        client = MT5Client()
        if not client.available:
            logger.warning("⚠️  MT5 non disponible — utilise --csv pour fournir les données")
            return None
        profile = ASSET_PROFILES.get(symbol, {})
        tf = profile.get("tf", "1h")
        count = int(days * 24 * (60 / {"1h": 60, "4h": 240, "15m": 15, "1d": 1440}.get(tf, 60)))
        df = client.fetch_ohlcv(symbol, timeframe=tf, count=min(count, 5000))
        client.shutdown()
        return df
    except Exception as e:
        logger.warning(f"MT5 OHLCV {symbol}: {e}")
        return None


def pip_size(symbol: str) -> float:
    s = symbol.upper()
    if "JPY" in s:          return 0.01
    if s in ("XAUUSD", "GOLD"):  return 0.1
    if any(x in s for x in ["BTC", "ETH", "XRP"]): return 1.0
    if any(x in s for x in ["US500", "US100", "DE40", "UK100", "J225", "AU200"]): return 1.0
    return 0.0001


# ─── Moteur de simulation ─────────────────────────────────────────────────────

class BacktestEngine:
    """Rejoue la stratégie barre par barre et simule les trades."""

    def __init__(self, symbol: str, initial_balance: float = INITIAL_BALANCE,
                 risk_pct: float = RISK_PER_TRADE, verbose: bool = True):
        self.symbol   = symbol
        self.balance  = initial_balance
        self.init_bal = initial_balance
        self.risk_pct = risk_pct
        self.verbose  = verbose
        self.trades   = []
        self._pip     = pip_size(symbol)
        self._strategy = None

    def _calc_size(self, entry: float, sl: float) -> float:
        risk_usd  = self.balance * self.risk_pct
        sl_dist   = abs(entry - sl)
        if sl_dist == 0:
            return 0.0
        pip_value = 1.0  # simplifié pour backtesting
        return round(risk_usd / (sl_dist / self._pip * pip_value), 2)

    def run(self, df: pd.DataFrame, strategy) -> dict:
        """
        Rejoue toutes les barres OHLCV. Ouvre une position sur signal,
        ferme sur TP1/TP2 touchés sur les barres suivantes.
        """
        self._strategy = strategy
        df = strategy.compute_indicators(df.copy())
        warmup    = 50  # bougies de warmup pour les indicateurs
        in_trade  = False
        state     = {}

        for i in range(warmup, len(df)):
            window    = df.iloc[:i].copy()
            cur_row   = df.iloc[i]
            cur_high  = float(cur_row["high"])
            cur_low   = float(cur_row["low"])
            cur_close = float(cur_row["close"])

            # ── Surveillance position ouverte ─────────────────────────────
            if in_trade:
                direction = state["direction"]
                sl   = state["sl"]
                tp1  = state["tp1"]
                tp2  = state["tp2"]
                entry= state["entry"]
                tp1_hit = state.get("tp1_hit", False)

                # SL touché ?
                sl_hit = (direction == "BUY" and cur_low <= sl) or \
                         (direction == "SELL" and cur_high >= sl)

                # TP1 touché ?
                tp1_touched = not tp1_hit and (
                    (direction == "BUY" and cur_high >= tp1) or
                    (direction == "SELL" and cur_low <= tp1)
                )

                # TP2 touché ?
                tp2_touched = tp1_hit and (
                    (direction == "BUY" and cur_high >= tp2) or
                    (direction == "SELL" and cur_low <= tp2)
                )

                if sl_hit:
                    sl_dist   = abs(entry - sl)
                    risk_usd  = self.balance * self.risk_pct
                    pnl       = -(risk_usd * (SL_PORTION if not tp1_hit else (1 - TP1_PORTION))) - COMMISSION_USD
                    self.balance += pnl
                    result = "SL (après TP1)" if tp1_hit else "SL"
                    self._record(state, pnl, result, i, df)
                    in_trade = False

                elif tp1_touched:
                    risk_usd  = self.balance * self.risk_pct
                    pnl_tp1   = risk_usd * TP1_PORTION * TP1_R
                    self.balance += pnl_tp1
                    state["tp1_hit"]  = True
                    state["pnl_tp1"]  = pnl_tp1
                    # Montée SL au BE
                    state["sl"] = entry

                elif tp2_touched:
                    risk_usd  = self.balance * self.risk_pct
                    pnl_tp2   = risk_usd * TP2_PORTION * TP2_R
                    pnl_total = state.get("pnl_tp1", 0) + pnl_tp2 - COMMISSION_USD
                    self.balance += pnl_tp2 - COMMISSION_USD
                    self._record(state, pnl_total, "WIN", i, df)
                    in_trade = False

                continue

            # ── Génération de signal ──────────────────────────────────────
            if len(window) < warmup:
                continue

            try:
                sig, score, _ = strategy.get_signal(window, symbol=self.symbol)
            except Exception:
                continue

            if sig == "HOLD" or score < 0.45:
                continue

            # Calcul SL/TP (ATR-based simplifié)
            try:
                atr = float(window.iloc[-1].get("atr", 0))
            except Exception:
                atr = 0
            if atr <= 0:
                continue

            entry = cur_close
            if sig == "BUY":
                sl  = entry - atr * 1.2
                tp1 = entry + atr * TP1_R * 1.2
                tp2 = entry + atr * TP2_R * 1.2
            else:
                sl  = entry + atr * 1.2
                tp1 = entry - atr * TP1_R * 1.2
                tp2 = entry - atr * TP2_R * 1.2

            in_trade = True
            state = {
                "symbol":    self.symbol,
                "direction": sig,
                "entry":     entry,
                "sl":        sl,
                "tp1":       tp1,
                "tp2":       tp2,
                "score":     score,
                "open_bar":  i,
                "open_time": str(cur_row.get("time", i)),
                "pnl_tp1":   0.0,
                "tp1_hit":   False,
            }

        return self._summary()

    def _record(self, state: dict, pnl: float, result: str, bar: int, df: pd.DataFrame):
        hold_bars = bar - state["open_bar"]
        self.trades.append({
            "symbol":     state["symbol"],
            "direction":  state["direction"],
            "entry":      state["entry"],
            "sl":         state["sl"],
            "tp1":        state["tp1"],
            "pnl":        round(pnl, 2),
            "result":     result,
            "open_time":  state["open_time"],
            "close_time": str(df.iloc[bar].get("time", bar)),
            "hold_bars":  hold_bars,
            "score":      state["score"],
            "balance":    round(self.balance, 2),
        })
        if self.verbose:
            emoji = "✅" if pnl > 0 else "❌"
            logger.info(
                f"  {emoji} {state['direction']:4} {state['symbol']} "
                f"| {result:20} | PnL={pnl:+.2f}$ | Bal={self.balance:,.0f}$"
            )

    def _summary(self) -> dict:
        n        = len(self.trades)
        wins     = [t for t in self.trades if t["pnl"] > 0]
        losses   = [t for t in self.trades if t["pnl"] <= 0]
        pnls     = [t["pnl"] for t in self.trades]
        total    = sum(pnls)
        wr       = len(wins) / n * 100 if n else 0
        avg_w    = statistics.mean([t["pnl"] for t in wins])   if wins   else 0
        avg_l    = statistics.mean([t["pnl"] for t in losses]) if losses else 0
        rr       = abs(avg_w / avg_l) if avg_l else 0

        # Max Drawdown
        peak = self.init_bal; max_dd = 0.0; cur = self.init_bal
        for pnl in pnls:
            cur += pnl
            if cur > peak: peak = cur
            dd = peak - cur
            if dd > max_dd: max_dd = dd

        # Sharpe
        sharpe = 0.0
        if len(pnls) >= 3:
            try:
                mean   = statistics.mean(pnls)
                std    = statistics.stdev(pnls)
                sharpe = round(mean / std * math.sqrt(252), 2) if std else 0
            except Exception:
                pass

        return {
            "symbol":        self.symbol,
            "trades":        n,
            "wins":          len(wins),
            "losses":        len(losses),
            "win_rate":      round(wr, 1),
            "total_pnl":     round(total, 2),
            "total_pnl_pct": round(total / self.init_bal * 100, 2),
            "avg_win":       round(avg_w, 2),
            "avg_loss":      round(avg_l, 2),
            "risk_reward":   round(rr, 2),
            "max_drawdown":  round(max_dd, 2),
            "max_dd_pct":    round(max_dd / self.init_bal * 100, 2),
            "sharpe":        sharpe,
            "final_balance": round(self.balance, 2),
        }


# ─── Rapport ──────────────────────────────────────────────────────────────────

def print_report(results: list[dict]):
    print("\n" + "═" * 70)
    print("  📊  NEMESIS BACKTEST REPORT")
    print("═" * 70)
    total_pnl = sum(r["total_pnl"] for r in results)
    total_trades = sum(r["trades"] for r in results)

    for r in results:
        icon = "✅" if r["total_pnl"] > 0 else "❌"
        print(f"\n{icon}  {r['symbol']}")
        print(f"     Trades    : {r['trades']}  ({r['wins']}W / {r['losses']}L)")
        print(f"     Win Rate  : {r['win_rate']}%")
        print(f"     R:R       : {r['risk_reward']}")
        print(f"     PnL       : {r['total_pnl']:+.2f}$ ({r['total_pnl_pct']:+.2f}%)")
        print(f"     Max DD    : -{r['max_drawdown']:.2f}$ ({r['max_dd_pct']:.2f}%)")
        print(f"     Sharpe    : {r['sharpe']}")

    print("\n" + "─" * 70)
    print(f"  TOTAL : {total_trades} trades | PnL global : {total_pnl:+.2f}$")
    print("═" * 70 + "\n")


def export_csv(results: list[dict], trades: list[dict], output: str):
    import csv
    path = output or f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(path, "w", newline="") as f:
        if trades:
            writer = csv.DictWriter(f, fieldnames=trades[0].keys())
            writer.writeheader()
            writer.writerows(trades)
    print(f"📁 Export CSV : {path}")
    return path


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEMESIS Backtester")
    parser.add_argument("--symbol",   type=str, default="EURUSD",  help="Symbole (ex: EURUSD, GOLD)")
    parser.add_argument("--days",     type=int, default=60,        help="Jours de données (défaut: 60)")
    parser.add_argument("--csv",      type=str, default="",        help="Fichier CSV OHLCV optionnel")
    parser.add_argument("--all",      action="store_true",         help="Tester tous les instruments du bot")
    parser.add_argument("--risk",     type=float, default=RISK_PER_TRADE, help="Risque par trade (défaut: 0.0035)")
    parser.add_argument("--balance",  type=float, default=INITIAL_BALANCE, help="Capital initial (défaut: 100000)")
    parser.add_argument("--output",   type=str, default="",        help="Fichier CSV de sortie")
    parser.add_argument("--quiet",    action="store_true",         help="Mode silencieux (moins de logs)")
    args = parser.parse_args()

    strategy = load_strategy()

    # Liste des symboles à tester
    if args.all:
        from brokers.capital_client import CAPITAL_INSTRUMENTS
        symbols = list(CAPITAL_INSTRUMENTS)
    else:
        symbols = [args.symbol.upper()]

    all_results = []
    all_trades  = []

    for sym in symbols:
        print(f"\n🔍 Backtesting {sym} ({args.days}j)...")

        # Chargement données
        if args.csv:
            df = load_ohlcv_from_csv(args.csv)
        else:
            df = load_ohlcv_from_mt5(sym, args.days)

        if df is None or len(df) < 100:
            print(f"  ⚠️  {sym} : données insuffisantes ({len(df) if df is not None else 0} barres)")
            continue

        engine  = BacktestEngine(sym, initial_balance=args.balance,
                                 risk_pct=args.risk, verbose=not args.quiet)
        results = engine.run(df, strategy)
        all_results.append(results)
        all_trades.extend(engine.trades)

    if not all_results:
        print("❌ Aucun résultat — vérifier les données")
        sys.exit(1)

    print_report(all_results)

    if args.output:
        export_csv(all_results, all_trades, args.output)
