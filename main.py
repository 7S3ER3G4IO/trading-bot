"""
main.py — ⚡ AlphaTrader — Multi-Asset | 3 TP + BE | Notifications complètes + Charts.
"""

import time
import signal
from typing import Optional, Dict
from datetime import datetime, timezone, timedelta
from loguru import logger

from logger import setup_logger
from config import SYMBOLS, LOOP_INTERVAL_SECONDS, DAILY_REPORT_HOUR_UTC
from data_fetcher import DataFetcher
from strategy import Strategy, SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD
from risk_manager import RiskManager
from order_executor import OrderExecutor
from telegram_notifier import TelegramNotifier
from daily_reporter import DailyReporter
from economic_calendar import EconomicCalendar
from chart_generator import ChartGenerator

bot_running = True

def shutdown_handler(sig, frame):
    global bot_running
    logger.warning("🛑 Arrêt propre en cours...")
    bot_running = False

signal.signal(signal.SIGINT,  shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)


class TradeState:
    def __init__(self, side, entry, total_amount, sl, tp1, tp2, tp3, be, symbol):
        self.symbol        = symbol
        self.side          = side
        self.entry         = entry
        self.current_sl    = sl
        self.initial_sl    = sl
        self.tp1           = tp1
        self.tp2           = tp2
        self.tp3           = tp3
        self.be            = be
        self.total_amount  = total_amount
        self.remaining     = total_amount
        self.tp1_hit       = False
        self.tp2_hit       = False
        self.be_active     = False
        self.total_pnl     = 0.0

    def fraction_size(self, frac):
        return round(self.total_amount * frac, 5)

    def is_open(self):
        return self.remaining > 0.000001


class TradingBot:
    def __init__(self):
        setup_logger()
        logger.info("=" * 60)
        logger.info("  ⚡  ALPHATRADER — Multi-Asset + 3 TP + Break Even")
        logger.info(f"  📊  {' | '.join(SYMBOLS)}")
        logger.info("=" * 60)

        self.fetcher  = DataFetcher()
        self.strategy = Strategy()
        self.executor = OrderExecutor()
        self.telegram = TelegramNotifier()
        self.reporter = DailyReporter()
        self.calendar = EconomicCalendar()
        self.charter  = ChartGenerator()

        bal = self.fetcher.get_balance()["free"]
        self.risk             = RiskManager(bal)
        self.initial_balance  = bal
        self.trades: Dict[str, Optional[TradeState]] = {s: None for s in SYMBOLS}
        self.last_reset_day   = datetime.now(timezone.utc).date()
        self.last_report_hour = -1
        self.news_pause_notified = False

        self.calendar.refresh()
        self.telegram.notify_start(bal, SYMBOLS)
        logger.info(f"💰 Solde initial : {bal:.2f} USDT")

    def run(self):
        logger.info(f"⏱  Boucle toutes les {LOOP_INTERVAL_SECONDS}s | CTRL+C pour arrêter\n")
        while bot_running:
            try:
                self._tick()
            except Exception as e:
                logger.error(f"❌ Erreur boucle : {e}")
                self.telegram.notify_error(str(e))
            time.sleep(LOOP_INTERVAL_SECONDS)
        logger.info("✅ Bot arrêté.")

    def _tick(self):
        now = datetime.now(timezone.utc)
        cet = now + timedelta(hours=1)

        # Bilan journalier
        if now.hour == DAILY_REPORT_HOUR_UTC and self.last_report_hour != now.hour:
            if self.reporter.should_send_report():
                self.telegram.notify_daily_report(self.reporter.build_report())
                self.reporter.mark_report_sent()
            self.last_report_hour = now.hour

        # Reset journalier
        if cet.date() != self.last_reset_day:
            bal = self.fetcher.get_balance()["free"]
            self.risk.reset_daily(bal)
            self.reporter.reset_for_new_day()
            self.initial_balance = bal
            self.last_reset_day  = cet.date()

        # Calendrier économique
        pause, reason = self.calendar.should_pause_trading()
        if pause:
            if not self.news_pause_notified:
                self.telegram.notify_news_pause(reason, 30)
                self.news_pause_notified = True
            logger.warning(f"⏸  Pause — {reason}")
            return
        else:
            self.news_pause_notified = False

        balance = self.fetcher.get_balance()["free"]
        logger.info(
            f"[{now.strftime('%H:%M:%S')}] Solde={balance:.2f} USDT | "
            f"Trades={sum(1 for t in self.trades.values() if t)}/{len(SYMBOLS)}"
        )

        for symbol in SYMBOLS:
            try:
                self._process_symbol(symbol, balance)
            except Exception as e:
                logger.error(f"❌ {symbol} : {e}")

    def _process_symbol(self, symbol: str, balance: float):
        trade = self.trades[symbol]

        if trade and trade.is_open():
            ticker = self.fetcher.get_ticker(symbol)
            self._monitor_trade(trade, ticker["last"], balance)
            return

        if not self.risk.can_open_trade(balance):
            return

        df = self.fetcher.get_ohlcv(symbol=symbol)
        df = self.strategy.compute_indicators(df)

        sig, score, confirmations = self.strategy.get_signal(df)

        if sig == SIGNAL_HOLD:
            return

        ticker = self.fetcher.get_ticker(symbol)
        price  = ticker["last"]
        atr    = self.strategy.get_atr(df)
        levels = self.risk.calculate_levels(price, atr, sig)
        amount = self.risk.position_size(balance, price, levels["sl"])

        if amount <= 0:
            return

        order = self._open_order(symbol, sig, amount)
        if not order:
            return

        t = TradeState(
            symbol=symbol, side=sig, entry=price,
            total_amount=amount,
            sl=levels["sl"], tp1=levels["tp1"],
            tp2=levels["tp2"], tp3=levels["tp3"],
            be=levels["be"],
        )
        self.trades[symbol] = t
        self.risk.on_trade_opened()

        # Notification d'entrée avec confirmations
        self.telegram.notify_trade_open(
            side=sig, symbol=symbol, entry=price,
            tp1=levels["tp1"], tp2=levels["tp2"],
            tp3=levels["tp3"], sl=levels["sl"],
            amount=amount, balance=balance,
            score=score, confirmations=confirmations,
        )

        # Génération et envoi du chart
        try:
            conf_desc = f"ADX+RSI+EMA {score}/6"
            chart = self.charter.generate_trade_chart(
                df=df, symbol=symbol, side=sig,
                entry=price, tp1=levels["tp1"],
                tp2=levels["tp2"], tp3=levels["tp3"],
                sl=levels["sl"], score=score,
                indicators_desc=conf_desc,
            )
            if chart:
                pair = symbol.replace("/", "")
                action = "ACHAT" if sig == SIGNAL_BUY else "VENTE"
                self.telegram.send_photo(
                    chart,
                    f"📊 *{pair} {action}* — Graphique 15m | Score `{score}/6`"
                )
        except Exception as e:
            logger.warning(f"⚠️  Chart non envoyé : {e}")

    def _monitor_trade(self, t: TradeState, price: float, balance: float):
        buy = t.side == SIGNAL_BUY

        def hit_up(target):   return price >= target if buy else price <= target
        def hit_down(target): return price <= target if buy else price >= target

        # TP1
        if not t.tp1_hit and hit_up(t.tp1):
            qty  = t.fraction_size(1/3)
            self._close_partial(t.symbol, t.side, qty)
            pnl  = abs(t.tp1 - t.entry) * qty
            t.total_pnl += pnl
            t.remaining -= qty
            t.tp1_hit    = True
            t.current_sl = t.be
            t.be_active  = True
            self.telegram.notify_tp_hit(
                1, t.symbol, price, t.entry, pnl,
                balance, t.remaining, be_activated=True
            )
            self.reporter.record_trade(t.symbol, t.side, "TP1", pnl, t.entry, price)
            logger.info(f"🎯 {t.symbol} TP1 | SL→BE={t.be:.2f}")
            return

        # TP2
        if t.tp1_hit and not t.tp2_hit and hit_up(t.tp2):
            qty  = t.fraction_size(1/3)
            self._close_partial(t.symbol, t.side, qty)
            pnl  = abs(t.tp2 - t.entry) * qty
            t.total_pnl += pnl
            t.remaining -= qty
            t.tp2_hit    = True
            self.telegram.notify_tp_hit(
                2, t.symbol, price, t.entry, pnl,
                balance, t.remaining
            )
            logger.info(f"🎯 {t.symbol} TP2")
            return

        # TP3 → ferme tout
        if t.tp1_hit and t.tp2_hit and hit_up(t.tp3):
            pnl = abs(t.tp3 - t.entry) * t.remaining
            self._close_partial(t.symbol, t.side, t.remaining)
            t.total_pnl += pnl
            self.telegram.notify_tp_hit(
                3, t.symbol, price, t.entry, pnl,
                balance, 0
            )
            trades_summary = self.reporter.build_report()[:80]
            self.telegram.notify_trade_closed(
                t.symbol, "TP3 🎯 MAX PROFIT", t.total_pnl,
                balance, self.initial_balance,
                t.entry, price, trades_summary
            )
            self.reporter.record_trade(
                t.symbol, t.side, "TP3", t.total_pnl, t.entry, price
            )
            self._end_trade(t.symbol)
            return

        # SL / BE
        if hit_down(t.current_sl):
            pnl_rem = abs(t.current_sl - t.entry) * t.remaining * (-1 if not t.be_active else 0)
            total   = t.total_pnl + pnl_rem
            self._close_partial(t.symbol, t.side, t.remaining)

            self.telegram.notify_sl_hit(
                t.symbol, price, t.entry, t.be_active, total, balance
            )
            result = "BE" if t.be_active else "SL"
            label  = "Break Even 🛡️" if t.be_active else "Stop-Loss 🛑"
            trades_summary = self.reporter.build_report()[:80]
            self.telegram.notify_trade_closed(
                t.symbol, label, total,
                balance, self.initial_balance,
                t.entry, price, trades_summary
            )
            self.reporter.record_trade(
                t.symbol, t.side, result, total, t.entry, price
            )
            self._end_trade(t.symbol)

    def _open_order(self, symbol: str, side: str, amount: float):
        if side == SIGNAL_BUY:
            return self.executor.buy_market(symbol, amount)
        else:
            base = symbol.split("/")[0]
            held = self.executor.get_position(base)
            qty  = min(amount, held)
            if qty > 0.00001:
                return self.executor.sell_market(symbol, qty)
            logger.warning(f"⚠️  {symbol} SELL — pas de {base}")
            return None

    def _close_partial(self, symbol: str, side: str, qty: float):
        qty = round(qty, 5)
        if qty <= 0:
            return
        if side == SIGNAL_BUY:
            self.executor.sell_market(symbol, qty)
        else:
            self.executor.buy_market(symbol, qty)

    def _end_trade(self, symbol: str):
        self.trades[symbol] = None
        self.risk.on_trade_closed()


if __name__ == "__main__":
    bot = TradingBot()
    bot.run()
