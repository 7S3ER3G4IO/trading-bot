"""
telegram_notifier.py — Nemesis v3.0 Multi-Channel Edition
Routes notifications to dedicated channels.
Backward-compatible : même interface publique que v2.0.
"""
import os
import io
import json
import requests
from datetime import datetime, timezone
from typing import Optional
try:
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
except ImportError:
    InlineKeyboardMarkup = None
    InlineKeyboardButton = None
from loguru import logger
from dotenv import load_dotenv
from config import WALLET_CHANNEL_URL

# Nemesis UI imports
from nemesis_ui.hub import NemesisHub
from nemesis_ui.renderer import NemesisRenderer as R
from nemesis_ui.notifications import NotificationFormatter as NF
from nemesis_ui.gamification import GamificationTracker
from channels.router import ChannelRouter

load_dotenv()


class TelegramNotifier:

    def __init__(self):
        self._token  = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.bot     = self._token

        if not self._token or not self.chat_id:
            logger.warning("⚠️  Telegram désactivé — credentials manquants.")
            self.bot = None
            self.hub = None
            self.router = None
            self.gamification = GamificationTracker()
            return

        self._api = f"https://api.telegram.org/bot{self._token}"

        # ── Nemesis Hub, Router & Gamification ────────────────────────────────
        self.hub = NemesisHub(self._token, self.chat_id)
        self.router = ChannelRouter(self._token)
        self.gamification = GamificationTracker()

        logger.info("📱 Telegram notifier v3.0 initialisé (Multi-Channel).")

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _wallet_button(self):
        if InlineKeyboardMarkup is None:
            return None
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Wallet en direct", url=WALLET_CHANNEL_URL)
        ]])

    def _send(self, text: str, markup=None):
        """Send to MAIN bot channel (DM)."""
        if not self.bot:
            return
        try:
            payload = {
                "chat_id":    self.chat_id,
                "text":       text,
                "parse_mode": "HTML",
            }
            if markup:
                try:
                    payload["reply_markup"] = markup.to_dict()
                except AttributeError:
                    if isinstance(markup, dict):
                        payload["reply_markup"] = markup
            r = requests.post(f"{self._api}/sendMessage", json=payload, timeout=10)
            if not r.ok:
                logger.warning(f"⚠️  Telegram {r.status_code}: {r.text[:80]}")
        except Exception as e:
            logger.error(f"❌ Telegram send : {e}")

    def send_message(self, text: str, markup=None):
        self._send(text, markup)

    def _send_photo(self, image_bytes: bytes, caption: str, markup=None):
        if not self.bot or not image_bytes:
            return
        try:
            files = {"photo": ("chart.png", io.BytesIO(image_bytes), "image/png")}
            data  = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"}
            if markup:
                try:
                    data["reply_markup"] = json.dumps(markup.to_dict())
                except AttributeError:
                    pass
            r = requests.post(f"{self._api}/sendPhoto", data=data, files=files, timeout=30)
            if not r.ok:
                logger.warning(f"⚠️  Telegram sendPhoto {r.status_code}: {r.text[:80]}")
        except Exception as e:
            logger.error(f"❌ Telegram send photo : {e}")

    def _ticker(self, symbol: str) -> str:
        return symbol.replace("/USDT", "").replace(":USDT", "")

    # ─── MESSAGES PREMIUM (routed to channels) ───────────────────────────────

    def notify_start(self, balance: float, symbols: list, futures_balance: float = 0.0):
        """Démarrage — envoie le Hub + push startup au canal Dashboard."""
        if self.hub:
            self.hub.send_hub(balance=balance, pnl_today=0.0)

        mode = "🟡 DÉMO" if os.getenv("CAPITAL_DEMO", "true") == "true" else "🟢 LIVE"
        nb = len(symbols) if symbols else 8
        header = R.box_header("⚡ NEMESIS v3.0 — EN LIGNE")

        text = (
            f"{header}\n\n"
            f"💰 Capital : <b>{balance:,.2f}€</b>  {mode}\n"
            f"🏦 Broker  : Capital.com\n"
            f"📡 {nb} instruments en surveillance\n\n"
            f"📅 Session : {R.session_name()}\n"
            f"🇬🇧 London : 09h–11h Paris\n"
            f"🗽 NY Open : 14h30–17h Paris\n\n"
            f"🟢 Tous systèmes opérationnels ✅"
        )
        # Send to Dashboard channel
        if self.router:
            self.router.send_dashboard(text)

    def notify_trade_open(
        self, side, symbol, entry, tp1, tp2, tp3, sl,
        amount, balance, score, confirmations: list,
        context_line: str = "",
        markup=None,
    ):
        """Push premium → canal Trades."""
        name = self._ticker(symbol)
        text = NF.format_trade_open(
            name=name, sig=side, entry=entry, sl=sl,
            tp1=tp1, tp2=tp2, tp3=tp3,
            score=score, confirmations=confirmations,
            session=R.session_name(),
            win_streak=self.gamification.win_streak,
        )
        if self.router:
            self.router.send_trade(text)

    def notify_tp_hit(
        self, tp_num: int, symbol: str, price: float, entry: float,
        pnl_gross: float, fees: float, balance: float,
        remaining_qty: float, be_activated: bool = False,
        markup=None,
    ):
        """Push premium → canal Trades."""
        name = self._ticker(symbol)
        pnl_net = pnl_gross - fees

        self.gamification.on_trade_closed(won=True, pnl=pnl_net)
        new_ach = self.gamification.pop_new_achievements()

        wr = (self.gamification.total_wins / self.gamification.total_trades * 100) \
            if self.gamification.total_trades > 0 else 0.0

        text = NF.format_tp_hit(
            tp_num=tp_num, name=name, entry=entry, price=price,
            pnl_net=pnl_net, balance=balance,
            be_activated=be_activated,
            win_streak=self.gamification.win_streak, wr=wr,
        )
        if self.router:
            self.router.send_trade(text)

        # Achievements → canal Stats
        for ach in new_ach:
            if self.router:
                self.router.send_stats(NF.format_achievement_unlocked(ach["name"], ach["desc"]))

    def notify_tp3_closed(self, symbol: str, price: float, entry: float,
                          pnl_gross: float, fees: float, balance: float):
        """Push → canal Trades."""
        name = self._ticker(symbol)
        pnl_net = pnl_gross - fees

        self.gamification.on_trade_closed(won=True, pnl=pnl_net, is_tp3_complete=True)
        new_ach = self.gamification.pop_new_achievements()

        text = NF.format_trade_complete(
            name=name, entry=entry, price=price,
            pnl_net=pnl_net, balance=balance,
        )
        if self.router:
            self.router.send_trade(text)

        for ach in new_ach:
            if self.router:
                self.router.send_stats(NF.format_achievement_unlocked(ach["name"], ach["desc"]))

    def notify_sl_hit(
        self, symbol: str, price: float, entry: float,
        is_be: bool, pnl_gross: float, fees: float, balance: float
    ):
        """Push → canal Trades."""
        name = self._ticker(symbol)
        pnl_net = pnl_gross - fees

        if is_be:
            text = NF.format_be_hit(name=name, balance=balance)
        else:
            self.gamification.on_trade_closed(won=False, pnl=pnl_net)
            new_ach = self.gamification.pop_new_achievements()

            initial = balance - pnl_net
            portfolio_impact = (pnl_net / initial * 100) if initial > 0 else 0
            wr = (self.gamification.total_wins / self.gamification.total_trades * 100) \
                if self.gamification.total_trades > 0 else 0.0

            text = NF.format_sl_hit(
                name=name, entry=entry, price=price,
                pnl_net=pnl_net, balance=balance,
                portfolio_impact_pct=portfolio_impact,
                wr=wr, win_streak=self.gamification.win_streak,
            )

            for ach in new_ach:
                if self.router:
                    self.router.send_stats(NF.format_achievement_unlocked(ach["name"], ach["desc"]))

        if self.router:
            self.router.send_trade(text)

    def notify_trade_closed(
        self, symbol: str, reason: str,
        total_pnl_gross: float, total_fees: float,
        balance: float, initial_balance: float,
        entry: float, exit_price: float, daily_summary: str
    ):
        """Push → canal Trades."""
        name = self._ticker(symbol)
        net = total_pnl_gross - total_fees
        pct_move = abs(exit_price - entry) / entry * 100 if entry > 0 else 0
        emoji = "✅" if net >= 0 else "❌"

        header = R.box_header(f"{emoji} TRADE CLÔTURÉ — {name}")
        text = (
            f"{header}\n\n"
            f"<code>{entry:,.5f}</code> ➜ <code>{exit_price:,.5f}</code>  ({pct_move:.2f}%)\n"
            f"📌 Raison : {reason}\n\n"
            f"💰 {R.format_pnl(net)}  ·  💼 {balance:,.2f}€"
        )
        if self.router:
            self.router.send_trade(text)

    def notify_trailing_stop_update(self, symbol: str, old_sl: float, new_sl: float):
        """Log only — no push."""
        ticker = self._ticker(symbol)
        logger.info(f"🔄 Trailing Stop {ticker} : {old_sl:,.4f} → {new_sl:,.4f}")

    def notify_daily_report(self, report_lines: list, date_str: str):
        """Push premium → canal Performance."""
        total = len(report_lines)
        wins = 0
        total_net = 0.0
        trades = []

        for row in report_lines:
            if len(row) == 5:
                date, side, ticker, result, pnl = row
                is_win = result.startswith("+")
                if is_win:
                    wins += 1
                pnl_val = pnl if isinstance(pnl, (int, float)) else 0
                total_net += pnl_val
                trades.append({
                    "ticker": ticker, "pnl": pnl_val,
                    "result": result, "rr": None,
                })

        wr = wins / total * 100 if total > 0 else 0
        text = NF.format_daily_report(
            trades=trades, balance=0, pnl_total=total_net,
            wr=wr, win_streak=self.gamification.win_streak,
        )
        if self.router:
            self.router.send_performance(text)

    def notify_weekly_report(self, report: str):
        """Push → canal Performance."""
        header = R.box_header("📅 RAPPORT HEBDOMADAIRE")
        text = f"{header}\n\n{report}"
        if self.router:
            self.router.send_performance(text)

    def notify_morning_brief(self, brief: str, nb_instruments: int = 8):
        """Push → canal Briefing."""
        d = R.date_label()
        sess = R.session_name()
        header = R.box_header(f"☀️ BRIEFING DU {d.upper()}")

        text = (
            f"{header}\n"
            f"  {sess}\n\n"
            f"{brief}\n\n"
            f"🤖 Nemesis surveillera <b>{nb_instruments} instruments</b> ✅"
        )
        if self.router:
            self.router.send_briefing(text)

    def notify_news_pause(self, event_name: str, minutes: float):
        """Push → canal Risk."""
        header = R.box_header("⏸ TRADING SUSPENDU")
        text = (
            f"{header}\n\n"
            f"📰 Événement : <b>{event_name}</b>\n"
            f"⏱ Publication dans : <b>{abs(minutes):.0f} min</b>\n\n"
            f"🤖 Reprise automatique après publication ✅"
        )
        if self.router:
            self.router.send_risk(text)

    def notify_drawdown_alert(self, balance: float, pct: float):
        """Push → canal Risk."""
        header = R.box_header("🚨 ALERTE DRAWDOWN")
        bar = R.wr_bar(pct * 100, max_val=10, width=10)
        text = (
            f"{header}\n\n"
            f"Perte du jour : <b>{pct:.1%}</b>\n"
            f"Limite : {bar} 10%\n\n"
            f"🛑 Trading suspendu jusqu'à minuit UTC\n"
            f"💼 Capital protégé : <b>{balance:,.2f} €</b>"
        )
        if self.router:
            self.router.send_risk(text)

    def notify_circuit_breaker(self, reason: str, balance: float, pnl_pct: float):
        """Push → canal Risk."""
        header = R.box_header("⚡ CIRCUIT BREAKER")
        text = (
            f"{header}\n\n"
            f"🔴 Raison : <b>{reason}</b>\n\n"
            f"💼 {balance:,.2f}€  ·  📉 {pnl_pct:+.1f}%\n\n"
            f"🛑 Trading suspendu — equity sous MA20"
        )
        if self.router:
            self.router.send_risk(text)

    def notify_error(self, error: str, balance: float = 0.0, count: int = 1):
        """Errors → canal Risk."""
        text = NF.format_error(error, balance, count)
        if self.router:
            self.router.send_risk(text)

    def notify_crash(self, error: str, consecutive: int):
        """Crash → canal Risk."""
        text = NF.format_crash(error, consecutive)
        if self.router:
            self.router.send_risk(text)

    def notify_restart(self, balance: float):
        """Restart → canal Dashboard."""
        header = R.box_header("✅ NEMESIS REDÉMARRÉ")
        text = (
            f"{header}\n\n"
            f"⏰ {R.utc_time()}\n"
            f"💰 Balance : <b>{balance:,.2f} €</b>\n\n"
            f"📡 Surveillance reprise\n"
            f"🟢 Tous systèmes opérationnels ✅"
        )
        if self.router:
            self.router.send_dashboard(text)

    def notify_pre_signal(self, side: str, symbol: str, price: float, score: int):
        """Pre-signal → canal Trades."""
        ticker = self._ticker(symbol)
        direction = "📈 LONG" if side == "BUY" else "📉 SHORT"
        score_bar = R.score_bar(score, 3)
        text = (
            f"⏳ <b>SETUP EN FORMATION — {ticker}</b>\n"
            f"{direction}   {score_bar}\n"
            f"📍 Prix : <code>{price:,.4f}</code>\n"
            f"<i>Confirmation en attente...</i>"
        )
        if self.router:
            self.router.send_trade(text)

    def notify_setup_cancelled(self, symbol: str):
        ticker = self._ticker(symbol)
        text = (
            f"❌ <b>SETUP ANNULÉ — {ticker}</b>\n"
            f"<i>Signal invalidé — surveillance continue</i>"
        )
        if self.router:
            self.router.send_trade(text)

    def notify_futures_closed(
        self, instrument: str, side: str, pnl: float, entry: float, close_price: float
    ):
        """Futures close → canal Trades."""
        _names = {
            "GOLD": "Or / Gold", "EURUSD": "EUR/USD",
            "GBPUSD": "GBP/USD", "USDJPY": "USD/JPY",
            "US500": "S&P 500", "US100": "NASDAQ 100",
            "DE40": "DAX 40", "OIL_BRENT": "Brent Oil",
        }
        name = _names.get(instrument, instrument)
        direction = "LONG" if side == "BUY" else "SHORT"
        emoji = "✅" if pnl >= 0 else "❌"
        pct = abs(close_price - entry) / entry * 100 if entry > 0 else 0

        header = R.box_header(f"{emoji} FUTURES {direction} — {name}")
        text = (
            f"{header}\n\n"
            f"<code>{entry:,.5f}</code> ➜ <code>{close_price:,.5f}</code>  ({pct:.1f}%)\n\n"
            f"💰 {R.format_pnl(pnl)}"
        )
        if self.router:
            self.router.send_trade(text)

    def post_wallet_stats(
        self, balance: float, initial_balance: float,
        open_trades: list, daily_pnl: float, total_pnl: float,
        win_rate: float, nb_trades: int
    ):
        """Wallet stats — refresh Hub in-place (zero spam)."""
        if self.hub:
            self.hub.refresh_hub(balance=balance, pnl_today=daily_pnl)

    # ─── Legacy aliases ───────────────────────────────────────────────────────

    def notify(self, text: str, markup=None):
        self._send(text, markup)

    def send_photo(self, image_bytes: bytes, caption: str, markup=None):
        self._send_photo(image_bytes, caption, markup)
