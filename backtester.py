"""
backtester.py — Backtesting ⚡ AlphaTrader sur données historiques Binance.

Télécharge 1 an de données OHLCV (endpoint public, pas de clé API),
simule la stratégie et génère un rapport complet.

Usage : python backtester.py
        python backtester.py --symbol ETH/USDT --days 365
"""

import argparse
import sys
from datetime import datetime, timezone, timedelta
from typing import List, Optional
import pandas as pd
import numpy as np
import ccxt
from loguru import logger

from strategy import Strategy, SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD
from risk_manager import RiskManager
from config import ATR_PERIOD

BINANCE_FEE = 0.001   # 0.1% par ordre


# ─── Fetch historique (Binance public, pas de clé) ─────────────────────────

def fetch_history(symbol: str, timeframe: str = "15m", days: int = 365) -> pd.DataFrame:
    logger.info(f"📥 Téléchargement {days}j de données {symbol} {timeframe} depuis Binance…")
    exchange = ccxt.binance({"enableRateLimit": True})  # endpoint public
    since_ms  = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    all_ohlcv = []
    limit     = 1000

    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit)
        if not batch:
            break
        all_ohlcv.extend(batch)
        since_ms = batch[-1][0] + 1
        if len(batch) < limit:
            break

    df = pd.DataFrame(all_ohlcv, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    logger.info(f"✅ {len(df)} bougies chargées ({df.index[0].date()} → {df.index[-1].date()})")
    return df


# ─── Moteur de backtest ────────────────────────────────────────────────────

class BacktestEngine:
    def __init__(self, initial_capital: float = 10_000.0):
        self.capital       = initial_capital
        self.initial_cap   = initial_capital
        self.strategy      = Strategy()
        self.risk_mgr      = RiskManager(initial_capital)
        self.trades:  List[dict] = []
        self.equity:  List[float] = [initial_capital]

    def run(self, df: pd.DataFrame, symbol: str) -> dict:
        df = self.strategy.compute_indicators(df)
        warmup     = 50   # Bougies de chauffe pour les indicateurs
        open_trade: Optional[dict] = None

        for i in range(warmup, len(df)):
            window  = df.iloc[:i+1]
            candle  = df.iloc[i]
            price   = float(candle["close"])
            atr     = float(candle["atr"]) if "atr" in candle and not pd.isna(candle["atr"]) else 0

            # ── Gestion du trade ouvert ────────────────────────────────────
            if open_trade:
                side = open_trade["side"]
                en   = open_trade["entry"]
                buy  = side == SIGNAL_BUY

                def hit_up(t):   return price >= t if buy else price <= t
                def hit_dn(t):   return price <= t if buy else price >= t

                # TP1
                if not open_trade["tp1_hit"] and hit_up(open_trade["tp1"]):
                    qty   = open_trade["amount"] / 3
                    pnl_g = abs(open_trade["tp1"] - en) * qty
                    fees  = en * qty * BINANCE_FEE * 2
                    open_trade["pnl"] += pnl_g - fees
                    open_trade["remaining"] -= qty
                    open_trade["tp1_hit"] = True
                    open_trade["sl"]      = en   # Break Even
                    self.capital += pnl_g - fees

                # TP2
                elif open_trade["tp1_hit"] and not open_trade["tp2_hit"] and hit_up(open_trade["tp2"]):
                    qty   = open_trade["amount"] / 3
                    pnl_g = abs(open_trade["tp2"] - en) * qty
                    fees  = en * qty * BINANCE_FEE * 2
                    open_trade["pnl"] += pnl_g - fees
                    open_trade["remaining"] -= qty
                    open_trade["tp2_hit"] = True
                    self.capital += pnl_g - fees

                # TP3
                elif open_trade["tp1_hit"] and open_trade["tp2_hit"] and hit_up(open_trade["tp3"]):
                    qty   = open_trade["remaining"]
                    pnl_g = abs(open_trade["tp3"] - en) * qty
                    fees  = en * qty * BINANCE_FEE * 2
                    open_trade["pnl"] += pnl_g - fees
                    self.capital += pnl_g - fees
                    open_trade["result"] = "TP3"
                    open_trade["exit"]   = price
                    self._close(open_trade)
                    open_trade = None

                # SL
                elif hit_dn(open_trade["sl"]):
                    qty   = open_trade["remaining"]
                    is_be = open_trade["tp1_hit"]
                    pnl_g = 0 if is_be else -abs(open_trade["sl"] - en) * qty
                    fees  = en * qty * BINANCE_FEE * 2
                    open_trade["pnl"] += pnl_g - fees
                    self.capital += pnl_g - fees
                    open_trade["result"] = "BE" if is_be else "SL"
                    open_trade["exit"]   = price
                    self._close(open_trade)
                    open_trade = None

                self.equity.append(self.capital)
                continue

            # ── Cherche un nouveau signal ──────────────────────────────────
            if self.capital < self.initial_cap * 0.5:   # Drawdown max 50% → stop
                break

            sig, score, _ = self.strategy.get_signal(window)
            if sig == SIGNAL_HOLD or score < 4:
                self.equity.append(self.capital)
                continue

            if atr == 0:
                self.equity.append(self.capital)
                continue

            levels = self.risk_mgr.calculate_levels(price, atr, sig)
            amount = self.risk_mgr.position_size(self.capital, price, levels["sl"])
            if amount <= 0:
                self.equity.append(self.capital)
                continue

            # Frais d'entrée
            fees_in = price * amount * BINANCE_FEE
            self.capital -= fees_in

            open_trade = {
                "date_open": candle.name,
                "symbol":    symbol,
                "side":      sig,
                "entry":     price,
                "amount":    amount,
                "remaining": amount,
                "sl":        levels["sl"],
                "tp1":       levels["tp1"],
                "tp2":       levels["tp2"],
                "tp3":       levels["tp3"],
                "tp1_hit":   False,
                "tp2_hit":   False,
                "pnl":       -fees_in,
                "result":    "OPEN",
                "exit":      None,
                "score":     score,
            }
            self.equity.append(self.capital)

        # Force fermeture si encore ouvert à la fin
        if open_trade:
            candle = df.iloc[-1]
            last_p = float(candle["close"])
            qty    = open_trade["remaining"]
            pnl_g  = (last_p - open_trade["entry"]) * qty * (1 if open_trade["side"]==SIGNAL_BUY else -1)
            fees   = open_trade["entry"] * qty * BINANCE_FEE * 2
            open_trade["pnl"] += pnl_g - fees
            open_trade["result"] = "END"
            open_trade["exit"]   = last_p
            self._close(open_trade)

        return self._compute_stats(symbol)

    def _close(self, trade: dict):
        self.trades.append(trade)

    def _compute_stats(self, symbol: str) -> dict:
        if not self.trades:
            return {"symbol": symbol, "error": "Aucun trade exécuté"}

        pnls    = [t["pnl"] for t in self.trades]
        wins    = [p for p in pnls if p > 0]
        losses  = [p for p in pnls if p <= 0]
        equity  = pd.Series(self.equity)
        peak    = equity.cummax()
        dd      = ((equity - peak) / peak * 100)
        max_dd  = float(dd.min())

        # Sharpe Ratio (annualisé sur données 15m : 4 bougies/h × 24h × 365j)
        returns = equity.pct_change().dropna()
        sharpe  = (returns.mean() / returns.std() * np.sqrt(35040)) if returns.std() > 0 else 0

        # Profit Factor
        gross_win  = sum(wins)   if wins   else 0
        gross_loss = abs(sum(losses)) if losses else 0.0001
        pf = gross_win / gross_loss

        total_pnl  = self.capital - self.initial_cap
        total_pct  = total_pnl / self.initial_cap * 100

        stats = {
            "symbol":          symbol,
            "capital_initial": round(self.initial_cap, 2),
            "capital_final":   round(self.capital, 2),
            "total_pnl":       round(total_pnl, 2),
            "total_pct":       round(total_pct, 2),
            "nb_trades":       len(self.trades),
            "nb_wins":         len(wins),
            "nb_losses":       len(losses),
            "win_rate":        round(len(wins) / len(self.trades) * 100, 1),
            "avg_win":         round(np.mean(wins), 2)   if wins   else 0,
            "avg_loss":        round(np.mean(losses), 2) if losses else 0,
            "best_trade":      round(max(pnls), 2),
            "worst_trade":     round(min(pnls), 2),
            "max_drawdown":    round(max_dd, 2),
            "sharpe_ratio":    round(float(sharpe), 2),
            "profit_factor":   round(pf, 2),
            "trades":          self.trades,
            "equity":          self.equity,
        }
        return stats


# ─── Rapport texte ─────────────────────────────────────────────────────────

def print_report(stats: dict):
    if "error" in stats:
        print(f"❌ {stats['error']}")
        return

    s = stats
    emoji = "🟢" if s["total_pnl"] > 0 else "🔴"

    print(f"""
╔══════════════════════════════════════════════════╗
║  ⚡ AlphaTrader — Rapport Backtesting            ║
║  {s['symbol']:<46}║
╚══════════════════════════════════════════════════╝

💰 Capital initial  : {s['capital_initial']:>10,.2f} USDT
{emoji} Capital final    : {s['capital_final']:>10,.2f} USDT
📈 PnL total        : {s['total_pnl']:>+10,.2f} USDT ({s['total_pct']:+.1f}%)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 STATISTIQUES TRADES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Nombre de trades  : {s['nb_trades']}
Trades gagnants   : {s['nb_wins']} ({s['win_rate']}%)
Trades perdants   : {s['nb_losses']}
Meilleur trade    : +{s['best_trade']:,.2f} USDT
Pire trade        : {s['worst_trade']:,.2f} USDT
Gain moyen        : +{s['avg_win']:,.2f} USDT
Perte moyenne     : {s['avg_loss']:,.2f} USDT

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📐 MÉTRIQUES DE RISQUE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Max Drawdown      : {s['max_drawdown']:.2f}%
Sharpe Ratio      : {s['sharpe_ratio']:.2f}  {"✅ Excellent" if s['sharpe_ratio']>1.5 else "⚠️ Acceptable" if s['sharpe_ratio']>0.5 else "❌ Faible"}
Profit Factor     : {s['profit_factor']:.2f}  {"✅ Bon" if s['profit_factor']>1.5 else "⚠️ Passable" if s['profit_factor']>1 else "❌ Mauvais"}
""")

    # Avertissement
    print("⚠️  Les performances passées ne garantissent pas les résultats futurs.")
    print("📋  Ce rapport est fourni à titre informatif uniquement.")


# ─── CLI ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AlphaTrader Backtester")
    parser.add_argument("--symbol",    default="BTC/USDT", help="Symbol ex: BTC/USDT")
    parser.add_argument("--timeframe", default="15m",      help="Timeframe ex: 15m")
    parser.add_argument("--days",      default=365, type=int, help="Nombre de jours")
    parser.add_argument("--capital",   default=10000.0, type=float, help="Capital initial")
    parser.add_argument("--all",       action="store_true", help="Backtest BTC+ETH+SOL+BNB")
    args = parser.parse_args()

    symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"] if args.all else [args.symbol]

    print("\n⚡ AlphaTrader — Backtesting Engine v1.0")
    print("=" * 52)

    all_stats = []
    for sym in symbols:
        try:
            df    = fetch_history(sym, args.timeframe, args.days)
            bot   = BacktestEngine(initial_capital=args.capital)
            stats = bot.run(df, sym)
            print_report(stats)
            all_stats.append(stats)
        except Exception as e:
            logger.error(f"❌ Erreur {sym} : {e}")

    if len(all_stats) > 1 and all("error" not in s for s in all_stats):
        total_pnl = sum(s["total_pnl"] for s in all_stats)
        avg_wr    = sum(s["win_rate"]  for s in all_stats) / len(all_stats)
        print(f"\n{'='*52}")
        print(f"📊 RÉSUMÉ MULTI-ACTIFS ({len(all_stats)} symbols)")
        print(f"   PnL total   : {total_pnl:+,.2f} USDT")
        print(f"   Win rate moy: {avg_wr:.1f}%")
        print(f"{'='*52}\n")


if __name__ == "__main__":
    main()
