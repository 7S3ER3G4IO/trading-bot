"""
main.py — ⚡ AlphaTrader v2.0
Multi-Asset | 3 TP + BE + Trailing Stop | Inline Buttons | DB Persistance
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
from telegram_bot_handler import TelegramBotHandler, InlineKeyboardMarkup
from daily_reporter import DailyReporter
from economic_calendar import EconomicCalendar
from market_context import MarketContext
from database import Database
from chart_generator import ChartGenerator
from brokers.oanda_client import OandaClient, OANDA_INSTRUMENTS
from brokers.binance_futures import BinanceFuturesClient, FUTURES_INSTRUMENTS

BINANCE_FEE_RATE  = 0.001   # 0.1% par ordre
TRAILING_ATR_MULT = 1.5     # Trailing stop à 1.5x ATR après TP2

bot_running = True

def shutdown_handler(sig, frame):
    global bot_running
    logger.warning("🛑 Arrêt propre en cours...")
    bot_running = False

signal.signal(signal.SIGINT,  shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)


# ─── TradeState ───────────────────────────────────────────────────────────────

class TradeState:
    def __init__(self, symbol, side, entry, total_amount, sl, tp1, tp2, tp3, be,
                 db_id: int = 0):
        self.symbol        = symbol
        self.side          = side
        self.entry         = entry
        self.total_amount  = total_amount
        self.current_sl    = sl
        self.initial_sl    = sl
        self.tp1           = tp1
        self.tp2           = tp2
        self.tp3           = tp3
        self.be            = be
        self.remaining     = total_amount
        self.tp1_hit       = False
        self.tp2_hit       = False
        self.be_active     = False
        self.trailing_active = False
        self.total_pnl     = 0.0
        self.total_fees    = 0.0
        self.db_id         = db_id
        # Vrais IDs d'ordres Binance (None = surveillance logicielle)
        self.sl_order_id   = None
        self.tp1_order_id  = None
        self.tp2_order_id  = None

    def is_open(self) -> bool:
        return self.remaining > 0.000001

    def fees_for(self, qty: float) -> float:
        """Frais Binance 0.1% × 2 ordres."""
        return round(self.entry * qty * BINANCE_FEE_RATE * 2, 4)


# ─── TradingBot ───────────────────────────────────────────────────────────────

class TradingBot:
    def __init__(self):
        setup_logger()
        logger.info("=" * 60)
        logger.info("  ⚡  ALPHATRADER v2.0 — Multi-Asset + 3 TP + BE")
        logger.info(f"  📊  {' | '.join(SYMBOLS)}")
        logger.info("=" * 60)

        # Modules core
        self.fetcher  = DataFetcher()
        self.strategy = Strategy()
        self.executor = OrderExecutor()
        self.db       = Database()
        self.telegram = TelegramNotifier()
        self.handler  = TelegramBotHandler()
        self.reporter = DailyReporter()
        self.calendar = EconomicCalendar()
        self.context  = MarketContext()
        self.charter  = ChartGenerator()

        # Broker OANDA (Forex / Gold / Indices)
        self.oanda = OandaClient()
        if self.oanda.available:
            logger.info(f"🏦 OANDA actif — {len(OANDA_INSTRUMENTS)} instruments : {', '.join(OANDA_INSTRUMENTS)}")
        else:
            logger.info("ℹ️  OANDA non configuré — uniquement crypto Binance")

        # Broker Binance Futures Demo (XAU/USDT — or vs crypto hedge)
        self.futures = BinanceFuturesClient()
        if self.futures.available:
            logger.info(f"🥇 Binance Futures Demo actif — {', '.join(FUTURES_INSTRUMENTS)}")
        else:
            logger.info("ℹ️  Binance Futures non configuré")

        bal = self.fetcher.get_balance()["free"]
        self.risk             = RiskManager(bal)
        self.initial_balance  = bal
        self.trades: Dict[str, Optional[TradeState]]       = {s: None for s in SYMBOLS}
        self.oanda_trades: Dict[str, Optional[str]]         = {s: None for s in OANDA_INSTRUMENTS}
        self.futures_trades: Dict[str, Optional[str]]       = {s: None for s in FUTURES_INSTRUMENTS}

        self.last_reset_day       = datetime.now(timezone.utc).date()
        self.last_report_hour     = -1
        self._manual_pause        = False
        self._news_pause_notified = False
        self._last_wallet_post    = datetime.now(timezone.utc)
        # Pre-alert : {symbol: datetime du dernier envoi}
        self._pre_alert_sent: Dict[str, Optional[datetime]] = {s: None for s in SYMBOLS}

        # Enregistre les callbacks pour les boutons inline / commandes
        self.handler.register_callbacks(
            get_status  = self._status_text,
            get_trades  = self._trades_text,
            close_trade = self._force_close,
            force_be    = self._force_be,
            pause       = self._do_pause,
            resume      = self._do_resume,
        )
        self.handler.start_polling()

        # Reprend les trades ouverts depuis la BDD (survie aux redémarrages)
        self._restore_from_db()

        self.calendar.refresh()
        self.telegram.notify_start(bal, SYMBOLS)
        logger.info(f"💰 Solde initial : {bal:.2f} USDT")

    def _restore_from_db(self):
        """Restaure les trades ouverts en cas de redémarrage."""
        open_trades = self.db.load_open_trades()
        for t_dict in open_trades:
            symbol = t_dict["symbol"]
            if symbol not in SYMBOLS:
                continue
            try:
                state = TradeState(
                    symbol=symbol, side=t_dict["side"], entry=t_dict["entry"],
                    total_amount=t_dict["amount"], sl=t_dict["sl"],
                    tp1=t_dict["tp1"], tp2=t_dict["tp2"], tp3=t_dict["tp3"],
                    be=t_dict["be"], db_id=t_dict["id"]
                )
                state.current_sl     = t_dict["current_sl"]
                state.remaining      = t_dict["remaining"]
                state.tp1_hit        = bool(t_dict["tp1_hit"])
                state.tp2_hit        = bool(t_dict["tp2_hit"])
                state.be_active      = bool(t_dict["be_active"])
                state.total_pnl      = t_dict["total_pnl"]
                state.sl_order_id    = t_dict.get("sl_order_id")
                state.tp1_order_id   = t_dict.get("tp1_order_id")
                state.tp2_order_id   = t_dict.get("tp2_order_id")
                self.trades[symbol]  = state
                logger.info(f"🔄 Trade restauré : {symbol} {t_dict['side']} @ {t_dict['entry']}")
            except Exception as e:
                logger.error(f"❌ Restauration trade {symbol} : {e}")

    # ─── Boucle principale ───────────────────────────────────────────────────

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

        # Morning Brief
        if self.context.should_send_brief():
            balance = self.fetcher.get_balance()["free"]
            next_news = None
            _, reason = self.calendar.should_pause_trading()
            if reason:
                next_news = reason
            brief = self.context.build_morning_brief(balance, next_news)
            self.telegram.notify_morning_brief(brief)
            self.context.mark_brief_sent()

        # Bilan journalier
        if now.hour == DAILY_REPORT_HOUR_UTC and self.last_report_hour != now.hour:
            if self.reporter.should_send_report():
                report_lines = self.reporter.build_report_lines()
                date_str = datetime.now(timezone.utc).strftime("%d/%m")
                self.telegram.notify_daily_report(report_lines, date_str)
                self.reporter.mark_report_sent()
            self.last_report_hour = now.hour

        # Bilan hebdomadaire (dimanche 22h CET)
        if self.reporter.should_send_weekly():
            self.telegram.notify_weekly_report(self.reporter.build_weekly_report())
            self.reporter.mark_weekly_sent()

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
            if not self._news_pause_notified:
                self.telegram.notify_news_pause(reason, 30)
                self._news_pause_notified = True
            logger.warning(f"⏸  Pause news — {reason}")
            return
        self._news_pause_notified = False

        # Pause manuelle
        if self._manual_pause or self.handler.is_paused():
            logger.info("⏸  Bot en pause manuelle")
            return

        balance = self.fetcher.get_balance()["free"]
        logger.info(
            f"[{now.strftime('%H:%M:%S')}] Solde={balance:.2f} USDT | "
            f"Trades={sum(1 for t in self.trades.values() if t)}/{len(SYMBOLS)}"
        )

        # Wallet stats auto-post toutes les 30 min dans le groupe dédié
        wallet_interval = timedelta(minutes=30)
        if now - self._last_wallet_post >= wallet_interval:
            self._post_wallet_stats(balance)
            self._last_wallet_post = now

        # Refresh Fear & Greed une fois par tick
        self.context.refresh_fear_greed()

        for symbol in SYMBOLS:
            try:
                self._process_symbol(symbol, balance)
            except Exception as e:
                logger.error(f"❌ {symbol} : {e}")

        # ─── Loop OANDA (Forex / Gold / Indices) ─────────────────────────────
        if self.oanda.available and not self._manual_pause:
            oanda_balance = self.oanda.get_balance()
            for instrument in OANDA_INSTRUMENTS:
                try:
                    self._process_oanda_symbol(instrument, oanda_balance)
                except Exception as e:
                    logger.error(f"❌ OANDA {instrument} : {e}")

        # ─── Loop Binance Futures (XAU + crypto futures) ─────────────────────
        if self.futures.available and not self._manual_pause:
            fut_balance = self.futures.get_balance()
            for instrument in FUTURES_INSTRUMENTS:
                try:
                    self._process_futures_symbol(instrument, fut_balance)
                except Exception as e:
                    logger.error(f"❌ Futures {instrument} : {e}")

    def _process_futures_symbol(self, instrument: str, balance: float):
        """
        Analyse un instrument Binance Futures (XAU/USDT, BTC/USDT:USDT…)
        et passe un ordre futures si le signal est confirmé.
        Endpoint : demo-fapi.binance.com (5000 USDT virtuels).
        """
        # Trade déjà ouvert sur cet instrument
        if self.futures_trades.get(instrument):
            return

        # Vérification session (London + NY open uniquement)
        if not self.strategy.is_session_ok():
            return

        # Données OHLCV depuis Binance Futures Demo
        df = self.futures.fetch_ohlcv(instrument, timeframe="5m", count=300)
        if df is None or len(df) < 100:
            return

        # Indicateurs + signal (même stratégie que crypto + Liquidity Sweep)
        df = self.strategy.compute_indicators(df)
        sig, score, confirmations = self.strategy.get_signal(df, symbol=instrument)

        if sig == "HOLD":
            return

        entry = float(df.iloc[-1]["close"])
        atr   = self.strategy.get_atr(df)
        if atr <= 0:
            return

        # SL = 0.8 × ATR (tight pour scalping), TP1 = 2×, TP2 intégré via SL→BE
        sl_dist = atr * 0.8
        sl  = entry - sl_dist if sig == "BUY" else entry + sl_dist
        tp  = entry + sl_dist * 2.0 if sig == "BUY" else entry - sl_dist * 2.0

        # Taille de position via position_size_qty du futures client
        qty = self.futures.position_size_qty(
            balance=balance, risk_pct=0.01,
            entry=entry, sl=sl, instrument=instrument,
        )
        if qty <= 0:
            return

        trade_id = self.futures.place_market_order(
            instrument=instrument,
            side=sig,
            qty=qty,
            sl_price=sl,
            tp_price=tp,
        )

        if trade_id:
            self.futures_trades[instrument] = trade_id
            name = instrument.replace(":USDT", "").replace("/", "")
            self.telegram.send_message(
                f"🥇 <b>Futures Demo Signal</b> — {name}\n"
                f"{'🟢 LONG' if sig == 'BUY' else '🔴 SHORT'}   Score {score}/7\n"
                f"\n"
                f"📍 Entrée : <code>{entry:.2f}</code>\n"
                f"🛑 SL     : <code>{sl:.2f}</code> ({sl_dist:.2f}$)\n"
                f"🎯 TP     : <code>{tp:.2f}</code> (R:R 1:2)\n"
                f"\n"
                f"💼 Qté : {qty:.4f} | TradeID: {trade_id}\n"
                f"🏦 <i>Binance Futures Demo (0 vrai argent)</i>"
            )

    def _process_oanda_symbol(self, instrument: str, balance: float):
        """
        Analyse un instrument OANDA (Forex/Gold/Indices) et passe un ordre
        si le signal est suffisamment fort. Miroir de _process_symbol() pour Binance.
        """
        from brokers.oanda_client import PIP_FACTOR

        # Trade déjà ouvert sur cet instrument — pas de nouveau signal
        if self.oanda_trades.get(instrument):
            return

        # Données OANDA → même format OHLCV que Binance
        df = self.oanda.fetch_ohlcv(instrument, timeframe="15m", count=250)
        if df is None or len(df) < 100:
            return

        # Indicateurs + signal via la même stratégie
        df_htf = self.oanda.fetch_htf(instrument, timeframe="1h", count=50)
        df     = self.strategy.compute_indicators(df, df_htf=df_htf)
        sig, score, confirmations = self.strategy.get_signal(df, symbol=instrument)

        if sig == "HOLD":
            return

        entry = float(df.iloc[-1]["close"])
        atr   = self.strategy.get_atr(df)
        levels = self.risk.calculate_levels(entry, atr, sig)

        # Taille de position en unités OANDA (pas en BTC)
        units_raw = self.oanda.position_size_units(
            balance=balance, risk_pct=0.01,
            entry=entry, sl=levels["sl"], instrument=instrument
        )
        if units_raw <= 0:
            return

        units = units_raw if sig == "BUY" else -units_raw
        tp_price = levels["tp1"]   # OANDA gère le SL/TP natif

        trade_id = self.oanda.place_market_order(
            instrument=instrument,
            units=units,
            sl_price=levels["sl"],
            tp_price=tp_price,
        )

        if trade_id:
            self.oanda_trades[instrument] = trade_id
            pip = PIP_FACTOR.get(instrument, 0.0001)
            pips_sl = round(abs(entry - levels["sl"]) / pip)
            pips_tp = round(abs(entry - tp_price) / pip)
            self.telegram.send_message(
                f"🏦 <b>OANDA Signal</b> — {instrument}\n"
                f"{'🟢 LONG' if sig == 'BUY' else '🔴 SHORT'}   Score {score}/6\n"
                f"\n"
                f"📍 Entrée : <code>{entry:.5f}</code>\n"
                f"🛑 SL     : <code>{levels['sl']:.5f}</code> ({pips_sl} pips)\n"
                f"🎯 TP     : <code>{tp_price:.5f}</code> ({pips_tp} pips)\n"
                f"\n"
                f"💼 Unités : {abs(units):.0f} | TradeID: {trade_id}"
            )

    def _process_symbol(self, symbol: str, balance: float):
        trade = self.trades.get(symbol)

        if trade and trade.is_open():
            ticker = self.fetcher.get_ticker(symbol)
            self._monitor_trade(trade, ticker["last"], balance)
            return

        if not self.risk.can_open_trade(balance):
            return

        df = self.fetcher.get_ohlcv(symbol=symbol)
        df = self.strategy.compute_indicators(df)

        sig, score, confirmations = self.strategy.get_signal(df)

        # ═ PRE-ALERT : score = 4/6 mais pas encore signal ═
        PRE_ALERT_SCORE   = 4
        PRE_ALERT_COOLDOWN = timedelta(hours=2)
        now_utc = datetime.now(timezone.utc)

        if sig == SIGNAL_HOLD:
            last_pre = self._pre_alert_sent.get(symbol)
            if score >= PRE_ALERT_SCORE:
                # Envoie le pre-alert si cooldown écoulé
                if last_pre is None or (now_utc - last_pre) >= PRE_ALERT_COOLDOWN:
                    ticker_price = self.fetcher.get_ticker(symbol)["last"]
                    # Direction via régime de marché (déjà calculé dans df)
                    regime    = self.strategy.market_regime(df)
                    direction = "BUY" if regime == "BULL" else "SELL"
                    self.telegram.notify_pre_signal(direction, symbol, ticker_price, score)
                    self._pre_alert_sent[symbol] = now_utc
                    logger.info(f"⏳ Pre-alert {symbol} | score={score}/6 | {regime}")

            elif last_pre is not None and score < PRE_ALERT_SCORE:
                # Setup qui était en formation mais qui s'est annulé
                if (now_utc - last_pre) < PRE_ALERT_COOLDOWN:
                    self.telegram.notify_setup_cancelled(symbol)
                    self._pre_alert_sent[symbol] = None
                    logger.info(f"❌ Setup annulé {symbol}")
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
            symbol=symbol, side=sig, entry=price, total_amount=amount,
            sl=levels["sl"], tp1=levels["tp1"], tp2=levels["tp2"],
            tp3=levels["tp3"], be=levels["be"],
        )
        # Sauvegarde en BDD
        t.db_id = self.db.save_trade_open(t)
        self.trades[symbol] = t
        self.risk.on_trade_opened()

        # ══ VRAIS ORDRES BINANCE ══
        frac = round(amount / 3, 5)
        # Stop-Loss sur toute la position restante
        t.sl_order_id = self.executor.place_stop_loss(symbol, sig, amount, levels["sl"])
        # TP1 et TP2 : ordres LIMIT pour 1/3 de la position chacun
        t.tp1_order_id = self.executor.place_take_profit(symbol, sig, frac, levels["tp1"])
        t.tp2_order_id = self.executor.place_take_profit(symbol, sig, frac, levels["tp2"])
        # TP3 : trailing stop logiciel (dynamique, pas d'ordre fixe)

        # Persistance des IDs d'ordres
        self.db.update_trade(t.db_id,
            sl_order_id=t.sl_order_id,
            tp1_order_id=t.tp1_order_id,
            tp2_order_id=t.tp2_order_id,
        )

        # Clavier inline
        from telegram_bot_handler import TelegramBotHandler
        keyboard = TelegramBotHandler.trade_keyboard(symbol)

        # Contexte macro
        ctx_line = self.context.get_context_line()

        # Notification texte avec boutons
        self.telegram.notify_trade_open(
            side=sig, symbol=symbol, entry=price,
            tp1=levels["tp1"], tp2=levels["tp2"],
            tp3=levels["tp3"], sl=levels["sl"],
            amount=amount, balance=balance,
            score=score, confirmations=confirmations,
            context_line=ctx_line,
            markup=keyboard,
        )

        # Chart
        try:
            score_desc = f"ADX+RSI+EMA {score}/6"
            chart = self.charter.generate_trade_chart(
                df=df, symbol=symbol, side=sig,
                entry=price, tp1=levels["tp1"], tp2=levels["tp2"],
                tp3=levels["tp3"], sl=levels["sl"],
                score=score, indicators_desc=score_desc,
            )
            if chart:
                pair   = symbol.replace("/", "")
                action = "ACHAT" if sig == SIGNAL_BUY else "VENTE"
                self.telegram.send_photo(
                    chart,
                    f"📊 *{pair} {action}* — Score `{score}/6`",
                    markup=keyboard,
                )
        except Exception as e:
            logger.warning(f"⚠️  Chart : {e}")

    def _monitor_trade(self, t: TradeState, price: float, balance: float):
        buy = t.side == SIGNAL_BUY
        keyboard = TelegramBotHandler.trade_keyboard(t.symbol)

        def hit_up(target):   return price >= target if buy else price <= target
        def hit_down(target): return price <= target if buy else price >= target

        # Trailing Stop (après TP2)
        if t.trailing_active:
            atr_approx = abs(t.tp1 - t.entry)  # approx
            new_sl     = price - TRAILING_ATR_MULT * atr_approx if buy \
                         else price + TRAILING_ATR_MULT * atr_approx
            if (buy and new_sl > t.current_sl) or (not buy and new_sl < t.current_sl):
                old_sl = t.current_sl
                t.current_sl = new_sl
                self.db.update_trade(t.db_id, current_sl=new_sl)
                self.telegram.notify_trailing_stop_update(t.symbol, old_sl, new_sl)

        # TP1
        if not t.tp1_hit and hit_up(t.tp1):
            qty      = round(t.total_amount / 3, 5)
            fees     = t.fees_for(qty)
            pnl_g    = abs(t.tp1 - t.entry) * qty
            t.total_pnl  += pnl_g
            t.total_fees += fees
            t.remaining  -= qty
            t.tp1_hit     = True
            t.current_sl  = t.be
            t.be_active   = True

            # L'ordre TP1 LIMIT a été exécuté par Binance — pas de close_partial nécessaire
            # si TP1 order_id existe (ordre Binance fillé automatiquement)
            if not t.tp1_order_id:
                self._close_partial(t.symbol, t.side, qty)   # fallback logiciel

            # ═ Remplacer le SL par un vrai ordre Break Even sur Binance ═
            new_sl_id = self.executor.replace_stop_loss(
                t.symbol, t.side, t.sl_order_id, t.remaining, t.be
            )
            t.sl_order_id = new_sl_id

            self.db.update_trade(t.db_id,
                tp1_hit=1, be_active=1, current_sl=t.current_sl,
                remaining=t.remaining, total_pnl=t.total_pnl,
                sl_order_id=t.sl_order_id,
            )
            self.telegram.notify_tp_hit(
                1, t.symbol, price, t.entry, pnl_g, fees,
                balance, t.remaining, be_activated=True, markup=keyboard
            )
            self.reporter.record_trade(
                t.symbol, t.side, "TP1", pnl_g, t.entry, price, qty
            )
            logger.info(f"🎯 {t.symbol} TP1 | SL→BE={t.be:.2f} (ordre Binance placé)")
            return

        # TP2
        if t.tp1_hit and not t.tp2_hit and hit_up(t.tp2):
            qty      = round(t.total_amount / 3, 5)
            fees     = t.fees_for(qty)
            pnl_g    = abs(t.tp2 - t.entry) * qty
            t.total_pnl  += pnl_g
            t.total_fees += fees
            t.remaining  -= qty
            t.tp2_hit     = True
            t.trailing_active = True

            # TP2 LIMIT Binance fillé automatiquement si ordre existait
            if not t.tp2_order_id:
                self._close_partial(t.symbol, t.side, qty)  # fallback logiciel

            self.db.update_trade(t.db_id,
                tp2_hit=1, remaining=t.remaining, total_pnl=t.total_pnl
            )
            self.telegram.notify_tp_hit(
                2, t.symbol, price, t.entry, pnl_g, fees,
                balance, t.remaining, markup=keyboard
            )
            self.reporter.record_trade(
                t.symbol, t.side, "TP2", pnl_g, t.entry, price, qty
            )
            logger.info(f"🎯 {t.symbol} TP2 | Trailing Stop activé")
            return

        # TP3 → ferme tout (Trailing Stop ou prix atteint)
        if t.tp1_hit and t.tp2_hit and hit_up(t.tp3):
            qty   = t.remaining
            fees  = t.fees_for(qty)
            pnl_g = abs(t.tp3 - t.entry) * qty
            t.total_pnl  += pnl_g
            t.total_fees += fees
            self._close_partial(t.symbol, t.side, qty)
            self.telegram.notify_tp3_closed(
                t.symbol, price, t.entry, pnl_g, fees, balance
            )
            self._finalize_trade(t, price, "TP3 MAX PROFIT", balance)
            return

        # SL / BE
        if hit_down(t.current_sl):
            qty   = t.remaining
            fees  = t.fees_for(qty)
            pnl_g = (abs(t.current_sl - t.entry) * qty * (-1 if not t.be_active else 0))
            t.total_pnl  += pnl_g
            t.total_fees += fees

            # Annule tous les ordres Binance restants (TP3 limit s'il existe, etc.)
            self.executor.cancel_all_orders(t.symbol)

            # Si pas de vrai ordre SL (surveillance logicielle), close manuellement
            if not t.sl_order_id:
                self._close_partial(t.symbol, t.side, qty)

            self.telegram.notify_sl_hit(
                t.symbol, price, t.entry, t.be_active, pnl_g, fees, balance
            )
            label = "Break Even" if t.be_active else "Stop-Loss"
            self._finalize_trade(t, price, label, balance)

    def _post_wallet_stats(self, balance: float):
        """Poste les stats wallet toutes les 30 min dans le groupe dédié."""
        try:
            open_trades = []
            for sym, t in self.trades.items():
                if t:
                    ticker = self.fetcher.get_ticker(sym)
                    price  = ticker["last"]
                    pnl    = (price - t.entry) * t.remaining * (1 if t.side == "BUY" else -1)
                    open_trades.append({"symbol": sym, "side": t.side, "pnl": pnl})

            daily_pnl  = sum(tr.pnl_net for tr in self.reporter._trades)
            total_pnl  = balance - self.initial_balance
            nb_trades  = len(self.reporter._trades)
            wins       = sum(1 for tr in self.reporter._trades if tr.result != "SL")
            win_rate   = (wins / nb_trades * 100) if nb_trades > 0 else 0

            self.telegram.post_wallet_stats(
                balance=balance,
                initial_balance=self.initial_balance,
                open_trades=open_trades,
                daily_pnl=daily_pnl,
                total_pnl=total_pnl,
                win_rate=win_rate,
                nb_trades=nb_trades,
            )
        except Exception as e:
            logger.warning(f"⚠️  Wallet stats : {e}")

    def _finalize_trade(self, t: TradeState, exit_price: float, reason: str, balance: float):

        result   = "BE" if "BE" in reason else ("TP3" if "TP3" in reason else "SL")
        net      = t.total_pnl - t.total_fees
        day_summ = f"PnL net du jour estimé : {net:+.2f} USDT"

        self.db.close_trade(t.db_id, exit_price, result, t.total_pnl, t.total_fees)
        self.reporter.record_trade(
            t.symbol, t.side, result, t.total_pnl,
            t.entry, exit_price, t.remaining
        )
        # notify_trade_closed appelé uniquement pour les clôtures inattendues (MANUAL, SL)
        # TP3 et BE ont déjà leur propre notification via notify_tp3_closed / notify_sl_hit
        if result not in ("TP3 MAX PROFIT", "BE"):
            self.telegram.notify_trade_closed(
                t.symbol, reason, t.total_pnl, t.total_fees,
                balance, self.initial_balance,
                t.entry, exit_price, ""
            )
        self._end_trade(t.symbol)

    # ─── Ordres ──────────────────────────────────────────────────────────────

    def _open_order(self, symbol: str, side: str, amount: float):
        if side == SIGNAL_BUY:
            return self.executor.buy_market(symbol, amount)
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

    # ─── Callbacks boutons inline ─────────────────────────────────────────────

    def _force_close(self, symbol: str) -> str:
        t = self.trades.get(symbol)
        if not t:
            return f"❌ Pas de trade actif sur `{symbol}`"
        ticker = self.fetcher.get_ticker(symbol)
        price  = ticker["last"]
        self._close_partial(symbol, t.side, t.remaining)
        pnl_g = abs(price - t.entry) * t.remaining * (1 if (t.side=="BUY") == (price > t.entry) else -1)
        fees  = t.fees_for(t.remaining)
        self.db.close_trade(t.db_id, price, "MANUAL", pnl_g, fees)
        self.reporter.record_trade(symbol, t.side, "MANUAL", pnl_g, t.entry, price, t.remaining)
        self._end_trade(symbol)
        return (
            f"🔴 *Trade `{symbol}` fermé manuellement*\n"
            f"💵 Prix : `{price:,.2f}`\n"
            f"💰 PnL net : `{pnl_g - fees:+.2f} USDT`"
        )

    def _force_be(self, symbol: str) -> str:
        t = self.trades.get(symbol)
        if not t:
            return f"❌ Pas de trade actif sur `{symbol}`"
        t.current_sl = t.be
        t.be_active  = True
        self.db.update_trade(t.db_id, current_sl=t.be, be_active=1)
        return (
            f"🔒 *Break Even forcé — `{symbol}`*\n"
            f"SL déplacé à `{t.be:,.2f}` (entrée)"
        )

    def _do_pause(self):
        self._manual_pause = True
        logger.info("⏸️  Bot mis en pause manuellement")

    def _do_resume(self):
        self._manual_pause = False
        logger.info("▶️  Bot repris manuellement")

    def _status_text(self) -> str:
        balance = self.fetcher.get_balance()["free"]
        self.context.refresh_fear_greed()
        nb_open = sum(1 for t in self.trades.values() if t)
        paused  = "⏸️ PAUSED" if (self._manual_pause or self.handler.is_paused()) else "🟢 ACTIF"

        open_lines = ""
        for sym, t in self.trades.items():
            if t:
                ticker = self.fetcher.get_ticker(sym)
                price  = ticker["last"]
                pnl_est = (price - t.entry) * t.remaining * (1 if t.side=="BUY" else -1)
                open_lines += f"  • `{sym}` {t.side} PnL≈{pnl_est:+.2f}\n"

        ctx = self.context.get_context_line()
        return (
            f"⚡ *AlphaTrader — Statut*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Capital : `{balance:,.2f} USDT`\n"
            f"📊 Trades ouverts : `{nb_open}/{len(SYMBOLS)}`\n"
            f"{open_lines}"
            f"🤖 État : {paused}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{ctx}"
        )

    def _trades_text(self):
        open_trades = {s: t for s, t in self.trades.items() if t}
        if not open_trades:
            return "📋 *Aucun trade actif.*", None

        lines, markup_sym = [], None
        for sym, t in open_trades.items():
            ticker = self.fetcher.get_ticker(sym)
            price  = ticker["last"]
            pnl    = (price - t.entry) * t.remaining * (1 if t.side=="BUY" else -1)
            tp_status = f"TP1{'✅' if t.tp1_hit else '○'} TP2{'✅' if t.tp2_hit else '○'}"
            lines.append(
                f"*{sym}* {t.side}\n"
                f"  💵 Entrée: `{t.entry:,.2f}` | Prix: `{price:,.2f}`\n"
                f"  PnL≈`{pnl:+.2f}` | {tp_status}\n"
                f"  SL: `{t.current_sl:,.2f}` {'🔒BE' if t.be_active else ''}"
            )
            markup_sym = sym  # Boutons du dernier trade

        text = "📋 *Trades actifs :*\n━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n\n".join(lines)
        markup = TelegramBotHandler.trade_keyboard(markup_sym) if markup_sym else None
        return text, markup


if __name__ == "__main__":
    bot = TradingBot()
    bot.run()
