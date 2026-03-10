"""
backtester.py — Backtest de la stratégie Nemesis sur données historiques Binance.
Utilise les vrais indicateurs (même code que strategy.py) sur jusqu'à 12 mois de données.

Usage :
    python3 backtester.py
    python3 backtester.py --symbol ETH/USDT --days 90
    python3 backtester.py --symbol BTC/USDT --days 180 --risk 0.01
"""
import argparse
import os
import sys
from datetime import datetime, timezone, timedelta

import pandas as pd
import ccxt
from dotenv import load_dotenv
from loguru import logger

load_dotenv()
sys.path.insert(0, ".")

from strategy import Strategy, SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD
from risk_manager import RiskManager

# ─── CONFIG ──────────────────────────────────────────────────────────────────
DEFAULT_SYMBOL    = "BTC/USDT"
DEFAULT_DAYS      = 90
DEFAULT_TF        = "15m"
DEFAULT_RISK      = 0.01        # 1% par trade
INITIAL_BALANCE   = 10_000.0    # USDT de départ
FEE_RATE          = 0.001       # 0.1% Binance

# ─── Connexion Binance (données réelles, pas de compte) ──────────────────────

def get_exchange():
    return ccxt.binance({
        "enableRateLimit": True,
        "options": {"defaultType": "spot"}
    })


def fetch_historical(exchange, symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    """Télécharge l'historique OHLCV depuis Binance."""
    since = exchange.parse8601(
        (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    )
    print(f"📥 Téléchargement {symbol} {timeframe} ({days} jours)...")
    all_bars = []
    while True:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not bars:
            break
        all_bars.extend(bars)
        since = bars[-1][0] + 1
        if len(bars) < 1000:
            break

    df = pd.DataFrame(all_bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    print(f"   {len(df)} bougies chargées ({df.index[0].date()} → {df.index[-1].date()})")
    return df


# ─── Simulation trade ─────────────────────────────────────────────────────────

class BacktestTrade:
    def __init__(self, i, side, entry, sl, tp1, tp2, tp3, be, amount):
        self.i       = i       # index barre d'entrée
        self.side    = side
        self.entry   = entry
        self.sl      = sl
        self.tp1, self.tp2, self.tp3 = tp1, tp2, tp3
        self.be      = be
        self.amount  = amount
        self.remaining = amount
        self.tp1_hit = self.tp2_hit = self.be_active = False
        self.pnl     = 0.0
        self.fees    = 0.0
        self.result  = None
        self.current_sl = sl

    def fee(self, qty):
        return round(self.entry * qty * FEE_RATE * 2, 4)


def simulate_trade(t: BacktestTrade, rows: pd.DataFrame, start_i: int):
    """Rejoue le trade bougie par bougie."""
    for idx in range(start_i, len(rows)):
        row   = rows.iloc[idx]
        high  = row["high"]
        low   = row["low"]

        def hit_up(target):
            return high >= target if t.side == "BUY" else low <= target

        def hit_down(target):
            return low <= target if t.side == "BUY" else high >= target

        # TP1
        if not t.tp1_hit and hit_up(t.tp1):
            qty = round(t.amount / 3, 5)
            pnl = abs(t.tp1 - t.entry) * qty
            t.pnl += pnl
            t.fees += t.fee(qty)
            t.remaining -= qty
            t.tp1_hit = True
            t.be_active = True
            t.current_sl = t.be

        # TP2
        if t.tp1_hit and not t.tp2_hit and hit_up(t.tp2):
            qty = round(t.amount / 3, 5)
            pnl = abs(t.tp2 - t.entry) * qty
            t.pnl += pnl
            t.fees += t.fee(qty)
            t.remaining -= qty
            t.tp2_hit = True

        # TP3
        if t.tp1_hit and t.tp2_hit and hit_up(t.tp3):
            qty = t.remaining
            pnl = abs(t.tp3 - t.entry) * qty
            t.pnl += pnl
            t.fees += t.fee(qty)
            t.remaining = 0
            t.result = "TP3"
            return

        # SL / BE
        if hit_down(t.current_sl):
            qty = t.remaining
            pnl = (abs(t.current_sl - t.entry) * qty * (-1 if not t.be_active else 0))
            t.pnl += pnl
            t.fees += t.fee(qty)
            t.remaining = 0
            t.result = "BE" if t.be_active else "SL"
            return

    # Fin des données — clôture au dernier prix
    if t.remaining > 0:
        last = rows.iloc[-1]["close"]
        pnl = (last - t.entry) * t.remaining * (1 if t.side == "BUY" else -1)
        t.pnl += pnl
        t.fees += t.fee(t.remaining)
        t.result = "OPEN_CLOSE"


# ─── Backtest principal ───────────────────────────────────────────────────────

def run_backtest(symbol: str, days: int, risk: float, timeframe: str = DEFAULT_TF, min_score: int = None):
    exchange = get_exchange()
    df_raw   = fetch_historical(exchange, symbol, timeframe, days)
    return run_backtest_with_params(df_raw, {}, risk, min_score=min_score)


def run_backtest_with_params(df_raw, params: dict, risk: float = DEFAULT_RISK, min_score: int = None):
    """
    Backtest sur un DataFrame déjà téléchargé, avec des params custom.
    params peut contenir : required_score, slope_threshold, adx_min,
                           atr_sl_multiplier, rsi_buy_max
    Utilisé par optimizer.py pour le grid search.
    min_score : override du seuil (ex: 4 pour Futures, 5 pour Spot)
    """
    import strategy as strat_module
    import config as cfg

    # Sauvegarde des constantes globales
    orig_score  = strat_module.REQUIRED_SCORE
    orig_slope  = strat_module.SLOPE_THRESHOLD
    orig_adx    = cfg.ADX_MIN
    orig_atr    = cfg.ATR_SL_MULTIPLIER
    orig_rsi    = cfg.RSI_BUY_MAX

    # Surcharge temporaire des paramètres
    if "required_score"    in params: strat_module.REQUIRED_SCORE  = params["required_score"]
    if "slope_threshold"   in params: strat_module.SLOPE_THRESHOLD = params["slope_threshold"]
    if "adx_min"           in params: cfg.ADX_MIN                  = params["adx_min"]
    if "atr_sl_multiplier" in params: cfg.ATR_SL_MULTIPLIER        = params["atr_sl_multiplier"]
    if "rsi_buy_max"       in params: cfg.RSI_BUY_MAX              = params["rsi_buy_max"]

    try:
        strategy = Strategy()
        risk_mgr = RiskManager(initial_balance=INITIAL_BALANCE)
        risk_mgr.RISK_PER_TRADE = risk

        trades   = []
        balance  = INITIAL_BALANCE
        in_trade = False
        window   = 250

        for i in range(window, len(df_raw)):
            df_window = df_raw.iloc[i - window:i].copy()
            df_window = strategy.compute_indicators(df_window)
            sig, score, _ = strategy.get_signal(df_window, min_score_override=min_score)

            if sig == SIGNAL_HOLD or in_trade:
                continue

            entry = float(df_raw.iloc[i]["close"])
            atr   = strategy.get_atr(df_window)
            levels = risk_mgr.calculate_levels(entry, atr, sig)
            amount = risk_mgr.position_size(balance, entry, levels["sl"])
            if amount <= 0:
                continue

            t = BacktestTrade(
                i=i, side=sig, entry=entry,
                sl=levels["sl"], tp1=levels["tp1"],
                tp2=levels["tp2"], tp3=levels["tp3"],
                be=levels["be"], amount=amount
            )
            in_trade = True
            simulate_trade(t, df_raw, i + 1)
            in_trade = False

            balance += t.pnl - t.fees
            trades.append(t)

    finally:
        # Restauration des constantes globales
        strat_module.REQUIRED_SCORE  = orig_score
        strat_module.SLOPE_THRESHOLD = orig_slope
        cfg.ADX_MIN                  = orig_adx
        cfg.ATR_SL_MULTIPLIER        = orig_atr
        cfg.RSI_BUY_MAX              = orig_rsi

    return trades, balance




# ─── Rapport ─────────────────────────────────────────────────────────────────

def print_report(trades, final_balance: float, symbol: str, days: int, risk: float):
    if not trades:
        print("⚠️  Aucun trade déclenché sur cette période.")
        return

    wins        = [t for t in trades if t.result not in ("SL",)]
    losses      = [t for t in trades if t.result == "SL"]
    bes         = [t for t in trades if t.result == "BE"]
    total_net   = sum(t.pnl - t.fees for t in trades)
    total_fees  = sum(t.fees for t in trades)
    win_rate    = len(wins) / len(trades) * 100
    max_dd      = 0.0
    peak        = INITIAL_BALANCE
    bal         = INITIAL_BALANCE
    for t in trades:
        bal += t.pnl - t.fees
        if bal > peak:
            peak = bal
        dd = (peak - bal) / peak * 100
        if dd > max_dd:
            max_dd = dd

    best  = max(trades, key=lambda t: t.pnl - t.fees)
    worst = min(trades, key=lambda t: t.pnl - t.fees)

    print(f"""
{'='*55}
  ⚡ Nemesis — Résultats Backtest
{'='*55}
  Symbole     : {symbol}
  Période     : {days} jours
  Risk/trade  : {risk*100:.1f}%
  Capital ini : {INITIAL_BALANCE:,.2f} USDT
{'='*55}
  Trades      : {len(trades)}  (W:{len(wins)}  L:{len(losses)}  BE:{len(bes)})
  Win Rate    : {win_rate:.1f}%
  PnL net     : {total_net:+.2f} USDT
  Frais cumulés: {total_fees:.2f} USDT
  Capital fin : {final_balance:,.2f} USDT
  Rendement   : {(final_balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100:+.1f}%
  Max Drawdown: {max_dd:.1f}%
{'='*55}
  Meilleur    : {best.result} {best.side}  {best.pnl - best.fees:+.2f} USDT
  Pire        : {worst.result} {worst.side}  {worst.pnl - worst.fees:+.2f} USDT
{'='*55}
""")


def format_telegram_report(trades, final_balance: float, symbol: str, days: int) -> str:
    """Formate les résultats backtest pour envoi Telegram (HTML)."""
    if not trades:
        return f"⚠️ <b>Backtest {symbol}</b>\n<code>Aucun trade déclenché sur {days} jours.</code>"

    wins      = [t for t in trades if t.result not in ("SL",)]
    losses    = [t for t in trades if t.result == "SL"]
    bes       = [t for t in trades if t.result == "BE"]
    total_net = sum(t.pnl - t.fees for t in trades)
    win_rate  = len(wins) / len(trades) * 100
    rendement = (final_balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
    max_dd    = 0.0
    peak, bal = INITIAL_BALANCE, INITIAL_BALANCE
    for t in trades:
        bal += t.pnl - t.fees
        if bal > peak: peak = bal
        dd = (peak - bal) / peak * 100
        if dd > max_dd: max_dd = dd

    sign   = "+" if total_net >= 0 else ""
    r_sign = "+" if rendement >= 0 else ""
    emoji  = "🟢" if total_net >= 0 else "🔴"

    return (
        f"🧪 <b>Backtest {symbol} — {days} jours</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Trades : <b>{len(trades)}</b>  W:{len(wins)} L:{len(losses)} BE:{len(bes)}\n"
        f"🎯 Win Rate : <b>{win_rate:.1f}%</b>\n"
        f"{emoji} PnL net : <b>{sign}{total_net:.2f} USDT</b>\n"
        f"📈 Rendement : <b>{r_sign}{rendement:.1f}%</b>\n"
        f"📉 Max Drawdown : <b>{max_dd:.1f}%</b>\n"
        f"💰 Capital final : <b>{final_balance:,.0f} USDT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Capital initial : {INITIAL_BALANCE:,.0f} USDT | Risk/trade : 1%</i>"
    )


# ─── Entrée principale ────────────────────────────────────────────────────────

DEFAULT_TF = DEFAULT_TF  # fix forward reference

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nemesis Backtester")
    parser.add_argument("--symbol",    default=DEFAULT_SYMBOL)
    parser.add_argument("--days",      type=int,   default=DEFAULT_DAYS)
    parser.add_argument("--risk",      type=float, default=DEFAULT_RISK)
    parser.add_argument("--tf",        default=DEFAULT_TF)
    parser.add_argument("--min_score", type=int,   default=None,
                        help="Score minimum (défaut: config MIN_SCORE=5). Ex: 4 pour Futures")
    args = parser.parse_args()

    logger.remove()  # Silence les logs pendant le backtest

    trades, final_bal = run_backtest(
        symbol=args.symbol,
        days=args.days,
        risk=args.risk,
        timeframe=args.tf,
        min_score=args.min_score,
    )
    print_report(trades, final_bal, args.symbol, args.days, args.risk)
