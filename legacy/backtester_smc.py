"""
backtester_smc.py — Backtest de la stratégie SMC sur données historiques Binance.
Simule les signaux SMC bougie par bougie sur 5m pour valider la rentabilité.

Usage :
    python3 backtester_smc.py --symbol BTC/USDT --days 45
    python3 backtester_smc.py --symbol BTC/USDT --days 90
"""
import argparse, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

import pandas as pd
from loguru import logger
logger.remove()

from backtester import fetch_historical, get_exchange, FEE_RATE
from strategy_smc import StrategySMC, SIGNAL_BUY, SIGNAL_SELL, ATR_SL_MULT, TP1_RATIO, TP2_RATIO

INITIAL_BALANCE = 10_000.0
RISK_PER_TRADE  = 0.01   # 1% par trade


def run_smc_backtest(symbol: str, days: int, timeframe: str = "5m"):
    exchange = get_exchange()
    df_raw   = fetch_historical(exchange, symbol, timeframe, days)
    strategy = StrategySMC()

    print(f"\n🔄 SMC Simulation en cours sur {len(df_raw)} bougies {timeframe}...")

    trades   = []
    balance  = INITIAL_BALANCE
    peak     = INITIAL_BALANCE
    max_dd   = 0.0
    in_trade = False

    window = 80   # Bougies min pour calculer swings + indicateurs

    for i in range(window, len(df_raw)):
        df_window = df_raw.iloc[i - window:i].copy()
        df_window = strategy.compute_indicators(df_window)

        sig, score, confs = strategy.get_signal(df_window)

        if sig == "HOLD" or in_trade:
            continue

        entry = float(df_raw.iloc[i]["close"])
        atr   = strategy.get_atr(df_window)
        if atr <= 0:
            continue

        sl_dist = atr * ATR_SL_MULT
        tp1 = entry + sl_dist * TP1_RATIO if sig == SIGNAL_BUY else entry - sl_dist * TP1_RATIO
        tp2 = entry + sl_dist * TP2_RATIO if sig == SIGNAL_BUY else entry - sl_dist * TP2_RATIO
        sl  = entry - sl_dist if sig == SIGNAL_BUY else entry + sl_dist
        be  = entry   # Break-even au prix d'entrée

        risk_amt = balance * RISK_PER_TRADE
        qty      = risk_amt / sl_dist if sl_dist > 0 else 0
        if qty <= 0:
            continue

        in_trade   = True
        pnl        = 0.0
        fees       = 0.0
        tp1_hit    = False
        be_active  = False
        cur_sl     = sl
        rem        = qty
        result     = "OPEN_CLOSE"

        for j in range(i + 1, min(i + 200, len(df_raw))):
            fwd  = df_raw.iloc[j]
            hi   = float(fwd["high"])
            lo   = float(fwd["low"])

            def up(t): return hi >= t if sig == SIGNAL_BUY else lo <= t
            def dn(t): return lo <= t if sig == SIGNAL_BUY else hi >= t

            # TP1 — sort 50%, active BE
            if not tp1_hit and up(tp1):
                q     = rem * 0.5
                pnl  += abs(tp1 - entry) * q
                fees += entry * q * FEE_RATE * 2
                rem  -= q
                tp1_hit   = True
                be_active = True
                cur_sl    = be   # SL → Break Even

            # TP2 — sort le reste
            if tp1_hit and up(tp2):
                pnl  += abs(tp2 - entry) * rem
                fees += entry * rem * FEE_RATE * 2
                rem   = 0
                result = "TP2"
                break

            # SL / BE
            if dn(cur_sl):
                loss  = -abs(cur_sl - entry) * rem if not be_active else 0.0
                pnl  += loss
                fees += entry * rem * FEE_RATE * 2
                rem   = 0
                result = "BE" if be_active else "SL"
                break

        # Clôture forcée fin de données
        if rem > 0:
            last  = float(df_raw.iloc[min(i + 200, len(df_raw) - 1)]["close"])
            pnl  += (last - entry) * rem * (1 if sig == SIGNAL_BUY else -1)
            fees += entry * rem * FEE_RATE * 2

        net      = pnl - fees
        balance += net
        in_trade = False

        if balance > peak: peak = balance
        dd = (peak - balance) / peak * 100
        if dd > max_dd: max_dd = dd

        trades.append({
            "side": sig, "result": result, "score": score,
            "pnl": round(pnl, 2), "fees": round(fees, 2),
            "net": round(net, 2)
        })

    return trades, balance, max_dd


def print_smc_report(trades, final_balance, max_dd, symbol, days, tf):
    if not trades:
        print("⚠️  Aucun trade SMC déclenché.")
        return

    wins    = [t for t in trades if t["result"] in ("TP2", "BE")]
    losses  = [t for t in trades if t["result"] == "SL"]
    bes     = [t for t in trades if t["result"] == "BE"]
    wr      = len(wins) / len(trades) * 100
    net_tot = sum(t["net"] for t in trades)
    fee_tot = sum(t["fees"] for t in trades)

    best  = max(trades, key=lambda t: t["net"])
    worst = min(trades, key=lambda t: t["net"])

    print(f"""
{'='*58}
  ⚡ Nemesis SMC — Résultats Backtest
{'='*58}
  Symbole     : {symbol}
  Timeframe   : {tf} (scalping)
  Période     : {days} jours
  Stratégie   : SMC (BOS + OB + FVG + Liquidity Sweep)
  Capital ini : {INITIAL_BALANCE:,.2f} USDT
{'='*58}
  Trades      : {len(trades)}  (W:{len(wins)}  L:{len(losses)}  BE:{len(bes)})
  Win Rate    : {wr:.1f}%
  PnL net     : {net_tot:+.2f} USDT
  Frais cumulés: {fee_tot:.2f} USDT
  Capital fin : {final_balance:,.2f} USDT
  Rendement   : {(final_balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100:+.1f}%
  Max Drawdown: {max_dd:.1f}%
{'='*58}
  Meilleur    : {best['result']} {best['side']}  {best['net']:+.2f} USDT
  Pire        : {worst['result']} {worst['side']}  {worst['net']:+.2f} USDT
{'='*58}
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--days",   type=int, default=45)
    parser.add_argument("--tf",     default="5m")
    args = parser.parse_args()

    trades, final_bal, max_dd = run_smc_backtest(args.symbol, args.days, args.tf)
    print_smc_report(trades, final_bal, max_dd, args.symbol, args.days, args.tf)
