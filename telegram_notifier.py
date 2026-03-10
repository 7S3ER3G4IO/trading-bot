"""
telegram_notifier.py — Nemesis v2.0
Design unifié : Station X — propre, professionnel, cohérent.
Tous les messages suivent la même structure :
  Header (titre + emoji)
  ━━━━━━━━━━━━━━━━━━━━━━━
  Corps (données structurées)
  ━━━━━━━━━━━━━━━━━━━━━━━
  Footer (statut / action)
"""
import os
import io
import threading
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

load_dotenv()

SEP = "━━━━━━━━━━━━━━━━━━━━━━━"


class TelegramNotifier:

    def __init__(self):
        self._token  = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.bot     = self._token

        if not self._token or not self.chat_id:
            logger.warning("⚠️  Telegram désactivé — credentials manquants.")
            self.bot = None
            return
        self._api = f"https://api.telegram.org/bot{self._token}"
        logger.info("📱 Telegram notifier initialisé (mode sync REST).")

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _wallet_button(self):
        if InlineKeyboardMarkup is None:
            return None
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Wallet en direct", url=WALLET_CHANNEL_URL)
        ]])

    def _send(self, text: str, markup=None):
        """Envoi synchrone via REST API."""
        if not self.bot:
            return
        try:
            payload = {
                "chat_id":    self.chat_id,
                "text":       text,
                "parse_mode": "HTML",
            }
            if markup:
                import json
                payload["reply_markup"] = json.dumps(markup.to_dict())
            r = requests.post(f"{self._api}/sendMessage", json=payload, timeout=10)
            if not r.ok:
                logger.warning(f"⚠️  Telegram {r.status_code}: {r.text[:80]}")
        except Exception as e:
            logger.error(f"❌ Telegram send : {e}")

    def send_message(self, text: str, markup=None):
        self._send(text, markup)

    def _send_photo(self, image_bytes: bytes, caption: str, markup=None):
        """Envoi photo synchrone via multipart REST."""
        if not self.bot or not image_bytes:
            return
        try:
            files = {"photo": ("chart.png", io.BytesIO(image_bytes), "image/png")}
            data  = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"}
            if markup:
                import json
                data["reply_markup"] = json.dumps(markup.to_dict())
            r = requests.post(f"{self._api}/sendPhoto", data=data, files=files, timeout=30)
            if not r.ok:
                logger.warning(f"⚠️  Telegram sendPhoto {r.status_code}: {r.text[:80]}")
        except Exception as e:
            logger.error(f"❌ Telegram send photo : {e}")

    def _ticker(self, symbol: str) -> str:
        return symbol.replace("/USDT", "").replace(":USDT", "")

    def _utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%H:%M UTC")

    def _date_fr(self) -> str:
        months = ["Jan","Fév","Mar","Avr","Mai","Juin",
                  "Juil","Août","Sep","Oct","Nov","Déc"]
        d = datetime.now(timezone.utc)
        return f"{d.day} {months[d.month - 1]}"

    def _session(self) -> str:
        h = datetime.now(timezone.utc).hour
        if 7 <= h < 11:  return "London 🇬🇧"
        if 13 <= h < 17: return "New York 🗽"
        return "Hors session 🌙"

    def _bar(self, value: float, max_val: float = 100, width: int = 8) -> str:
        """Barre de progression visuelle. Ex: ████░░░░ 65%"""
        filled = int((value / max_val) * width) if max_val > 0 else 0
        filled = min(filled, width)
        return "█" * filled + "░" * (width - filled)

    # ─── MESSAGES ─────────────────────────────────────────────────────────────

    def notify_start(self, balance: float, symbols: list, futures_balance: float = 0.0):
        """Message de démarrage — carte NEMESIS ONLINE."""
        now  = self._utc()
        sess = self._session()
        nb   = len(symbols) if symbols else 8
        mode = "🟡 DÉMO" if os.getenv("CAPITAL_DEMO", "true") == "true" else "🟢 LIVE"

        self._send(
            f"⚡ <b>NEMESIS v2.0 — EN LIGNE</b>\n"
            f"{SEP}\n"
            f"💰 Capital    : <b>{balance:,.2f}€</b>   {mode}\n"
            f"🏦 Broker     : Capital.com\n"
            f"📡 Instruments: <b>{nb} actifs</b>\n"
            f"🕐 Démarrage  : <b>{now}</b>\n"
            f"{SEP}\n"
            f"📅 Session actuelle : {sess}\n"
            f"🇬🇧 London : 09h–11h Paris\n"
            f"🗽 NY Open : 14h30–17h Paris\n"
            f"{SEP}\n"
            f"🟢 Tous systèmes opérationnels — surveillance active ✅",
            markup=self._wallet_button(),
        )

    def notify_trade_open(
        self, side, symbol, entry, tp1, tp2, tp3, sl,
        amount, balance, score, confirmations: list,
        context_line: str = "",
        markup=None,
    ):
        ticker = self._ticker(symbol)
        emoji  = "🟢 LONG" if side == "BUY" else "🔴 SHORT"
        stars  = "⭐" * min(score, 3) + "☆" * max(0, 3 - score)

        def pct(t): return abs((t - entry) / entry * 100)

        self._send(
            f"📈 <b>{ticker} — {emoji}</b>\n"
            f"{SEP}\n"
            f"{stars}  Score {score}/3  |  {self._session()}\n"
            f"📍 Entrée : <code>{entry:,.4f}</code>\n"
            f"──────────────────\n"
            f"🎯 TP1 : <code>{tp1:,.4f}</code>  (+{pct(tp1):.1f}%)\n"
            f"🎯 TP2 : <code>{tp2:,.4f}</code>  (+{pct(tp2):.1f}%)\n"
            f"🎯 TP3 : <code>Ouvert</code>\n"
            f"🛑 SL  : <code>{sl:,.4f}</code>  (-{pct(sl):.1f}%)\n"
            f"{SEP}\n"
            f"💰 Balance : <b>{balance:,.2f}€</b>\n"
            f"🔍 {' | '.join(confirmations[:3]) if confirmations else '—'}",
            markup=markup or self._wallet_button(),
        )

    def notify_tp_hit(
        self, tp_num: int, symbol: str, price: float, entry: float,
        pnl_gross: float, fees: float, balance: float,
        remaining_qty: float, be_activated: bool = False,
        markup=None,
    ):
        ticker  = self._ticker(symbol)
        pnl_net = pnl_gross - fees
        pips    = abs(price - entry)
        emoji   = "🔥" * tp_num

        be_line = (
            "\n🟡 <b>Break-Even activé</b> — risque zéro sur TP suivant !"
            if be_activated else
            f"\n⏳ TP{tp_num+1} toujours actif — let it run"
            if tp_num < 3 else ""
        )

        self._send(
            f"🎯 <b>TP{tp_num} TOUCHÉ {emoji} — {ticker}</b>\n"
            f"{SEP}\n"
            f"📍 Prix d'entrée : <code>{entry:,.4f}</code>\n"
            f"🏁 Prix de sortie : <code>{price:,.4f}</code>\n"
            f"📐 Mouvement : +{pips:.4f} ({abs(pips/entry*100):.2f}%)\n"
            f"{SEP}\n"
            f"💰 PnL net : <b>{pnl_net:+.2f} USDT</b>\n"
            f"💼 Balance : <b>{balance:,.2f} USDT</b>"
            f"{be_line}",
            markup=markup or self._wallet_button(),
        )

    def notify_tp3_closed(self, symbol: str, price: float, entry: float,
                          pnl_gross: float, fees: float, balance: float):
        ticker  = self._ticker(symbol)
        pnl_net = pnl_gross - fees
        pips    = abs(price - entry)
        emoji   = "✅" if pnl_net >= 0 else "❌"

        self._send(
            f"🏆 <b>TRADE COMPLET — {ticker}</b>\n"
            f"{SEP}\n"
            f"📍 Entrée : <code>{entry:,.4f}</code>\n"
            f"🏁 Sortie : <code>{price:,.4f}</code>\n"
            f"📐 +{pips:.4f}  ({abs(pips/entry*100):.2f}%)\n"
            f"{SEP}\n"
            f"{emoji} PnL net : <b>{pnl_net:+.2f} USDT</b>\n"
            f"💼 Balance : <b>{balance:,.2f} USDT</b>\n"
            f"🔥🔥🔥 3/3 TP touchés — trade parfait !",
            markup=self._wallet_button(),
        )

    def notify_sl_hit(
        self, symbol: str, price: float, entry: float,
        is_be: bool, pnl_gross: float, fees: float, balance: float
    ):
        ticker  = self._ticker(symbol)
        pnl_net = pnl_gross - fees

        if is_be:
            self._send(
                f"🟡 <b>BREAK-EVEN — {ticker}</b>\n"
                f"{SEP}\n"
                f"Sortie au prix d'entrée.\n"
                f"<b>Capital 100% protégé</b> 💎\n"
                f"PnL : ±0 USDT\n"
                f"{SEP}\n"
                f"🔍 Prochaine opportunité en analyse..."
            )
        else:
            pips = abs(price - entry)
            nb_trades_to_recover = max(1, int(abs(pnl_net) / (balance * 0.01 * 1.8)))
            self._send(
                f"🛑 <b>STOP LOSS — {ticker}</b>\n"
                f"{SEP}\n"
                f"📍 Entrée : <code>{entry:,.4f}</code>\n"
                f"🔻 Sortie : <code>{price:,.4f}</code>\n"
                f"📐 -{pips:.4f}  (-{abs(pips/entry*100):.2f}%)\n"
                f"{SEP}\n"
                f"❌ PnL net : <b>{pnl_net:+.2f} USDT</b>\n"
                f"💼 Balance : <b>{balance:,.2f} USDT</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 {nb_trades_to_recover} trade(s) gagnant(s) pour compenser\n"
                f"🔍 Prochain setup en surveillance..."
            )

    def notify_trade_closed(
        self, symbol: str, reason: str,
        total_pnl_gross: float, total_fees: float,
        balance: float, initial_balance: float,
        entry: float, exit_price: float, daily_summary: str
    ):
        ticker = self._ticker(symbol)
        net    = total_pnl_gross - total_fees
        emoji  = "✅" if net >= 0 else "❌"
        pct_move = abs(exit_price - entry) / entry * 100 if entry > 0 else 0

        self._send(
            f"{emoji} <b>TRADE CLÔTURÉ — {ticker}</b>\n"
            f"{SEP}\n"
            f"📍 <code>{entry:,.4f}</code> → <code>{exit_price:,.4f}</code>  "
            f"({pct_move:.2f}%)\n"
            f"📌 Raison : {reason}\n"
            f"{SEP}\n"
            f"💰 PnL net : <b>{net:+.2f} USDT</b>\n"
            f"💼 Balance : <b>{balance:,.2f} USDT</b>"
        )

    def notify_trailing_stop_update(self, symbol: str, old_sl: float, new_sl: float):
        ticker = self._ticker(symbol)
        logger.info(f"🔄 Trailing Stop {ticker} : {old_sl:,.4f} → {new_sl:,.4f}")

    def notify_daily_report(self, report_lines: list, date_str: str):
        total = len(report_lines)
        wins  = 0
        total_net = 0.0

        lines = ""
        for row in report_lines:
            if len(row) == 5:
                date, side, ticker, result, pnl = row
                is_win = result.startswith("+")
                if is_win: wins += 1
                if isinstance(pnl, (int, float)): total_net += pnl
                e = "✅" if is_win else ("🔵" if result == "BE" else "❌")
                pnl_str = f"{pnl:+.2f}$" if isinstance(pnl, (int, float)) else result
                lines += f"{e} <b>{ticker:<8}</b> {pnl_str}\n"

        wr  = wins / total * 100 if total > 0 else 0
        bar = self._bar(wr)
        trend = "📈" if total_net >= 0 else "📉"

        self._send(
            f"📊 <b>BILAN — {date_str.upper()}</b>\n"
            f"{SEP}\n"
            f"{lines}"
            f"{SEP}\n"
            f"WR : <b>{wr:.0f}%</b>    {bar} {wins}/{total}\n"
            f"{trend} PnL net : <b>{total_net:+.2f} USDT</b>",
            markup=self._wallet_button(),
        )

    def notify_weekly_report(self, report: str):
        self._send(
            f"📅 <b>RAPPORT HEBDOMADAIRE</b>\n"
            f"{SEP}\n"
            f"{report}"
        )

    def notify_morning_brief(self, brief: str, nb_instruments: int = 8):
        """Rapport matinal structuré."""
        d    = self._date_fr()
        sess = self._session()
        self._send(
            f"☀️ <b>BRIEFING DU {d.upper()}</b>\n"
            f"{SEP}\n"
            f"🔐 Session actuelle : {sess}\n"
            f"🇬🇧 London open : 09h00–11h00\n"
            f"🗽 NY open      : 14h30–17h00\n"
            f"{SEP}\n"
            f"{brief}\n"
            f"{SEP}\n"
            f"🤖 Nemesis surveillera <b>{nb_instruments} instruments</b> aujourd'hui ✅"
        )

    def notify_news_pause(self, event_name: str, minutes: float):
        self._send(
            f"⏸ <b>TRADING SUSPENDU</b>\n"
            f"{SEP}\n"
            f"📰 Événement : <b>{event_name}</b>\n"
            f"⏱ Publication dans : <b>{abs(minutes):.0f} min</b>\n"
            f"{SEP}\n"
            f"🤖 Reprise automatique après publication ✅"
        )

    def notify_drawdown_alert(self, balance: float, pct: float):
        bar = self._bar(pct, max_val=10, width=10)
        self._send(
            f"🚨 <b>ALERTE DRAWDOWN</b>\n"
            f"{SEP}\n"
            f"Perte du jour : <b>{pct:.1%}</b>\n"
            f"Limite : {bar} 10%\n"
            f"{SEP}\n"
            f"🛑 Trading suspendu jusqu'à minuit UTC\n"
            f"💼 Capital protégé : <b>{balance:,.2f} USDT</b>"
        )

    def notify_error(self, error: str, balance: float = 0.0, count: int = 1):
        now      = self._utc()
        severity = "🟠" if count < 3 else "🔴"
        level    = "AVERTISSEMENT" if count < 3 else "CRITIQUE"

        self._send(
            f"{severity} <b>ERREUR BOT #{count} — {level}</b>\n"
            f"{SEP}\n"
            f"⏰ {now}  |  Balance : <b>{balance:,.2f}€</b>\n"
            f"{SEP}\n"
            f"<code>{error[:300]}</code>\n"
            f"{SEP}\n"
            f"🔗 <i>Voir les logs → Railway Dashboard</i>\n"
            f"🤖 Bot en cours de récupération..."
        )

    def notify_crash(self, error: str, consecutive: int):
        now = self._utc()
        self._send(
            f"🚨 <b>ALERTE CRITIQUE — BOT INSTABLE</b>\n"
            f"{SEP}\n"
            f"⏰ {now}\n"
            f"🔄 <b>{consecutive} erreurs consécutives</b>\n"
            f"{SEP}\n"
            f"<code>{error[:200]}</code>\n"
            f"{SEP}\n"
            f"⚠️ Action requise : vérifier Railway → Deploy Logs\n"
            f"🔗 https://railway.com"
        )

    def notify_restart(self, balance: float):
        now = self._utc()
        self._send(
            f"✅ <b>NEMESIS REDÉMARRÉ</b>\n"
            f"{SEP}\n"
            f"⏰ Heure : <b>{now}</b>\n"
            f"💰 Balance : <b>{balance:,.2f} USDT</b>\n"
            f"{SEP}\n"
            f"📡 Surveillance des 8 instruments reprise\n"
            f"🟢 Tous systèmes opérationnels ✅"
        )

    def notify_pre_signal(self, side: str, symbol: str, price: float, score: int):
        ticker    = self._ticker(symbol)
        direction = "📈 LONG" if side == "BUY" else "📉 SHORT"
        stars     = "⭐" * min(score, 3) + "☆" * max(0, 3 - score)
        self._send(
            f"⏳ <b>SETUP EN FORMATION — {ticker}</b>\n"
            f"{direction}   {stars}\n"
            f"📍 Prix : <code>{price:,.4f}</code>\n"
            f"<i>Confirmation en attente...</i>"
        )

    def notify_setup_cancelled(self, symbol: str):
        ticker = self._ticker(symbol)
        self._send(
            f"❌ <b>SETUP ANNULÉ — {ticker}</b>\n"
            f"<i>Signal invalidé — prochaine fenêtre en surveillance</i>"
        )

    def notify_futures_closed(
        self, instrument: str, side: str, pnl: float, entry: float, close_price: float
    ):
        _names = {
            "ETH/USDT:USDT": "Ethereum", "XRP/USDT:USDT": "XRP",
            "ADA/USDT:USDT": "Cardano",  "DOGE/USDT:USDT": "Dogecoin",
        }
        name      = _names.get(instrument, instrument.replace(":USDT", "").replace("/USDT", ""))
        direction = "LONG" if side == "BUY" else "SHORT"
        emoji     = "✅" if pnl >= 0 else "❌"
        pct       = abs(close_price - entry) / entry * 100 if entry > 0 else 0

        self._send(
            f"{emoji} <b>FUTURES {direction} — {name}</b>\n"
            f"{SEP}\n"
            f"📍 Entrée : <code>{entry:.4f}</code>\n"
            f"🏁 Sortie : <code>{close_price:.4f}</code>  ({pct:.1f}%)\n"
            f"{SEP}\n"
            f"💰 PnL : <b>{pnl:+.2f} USDT</b>",
            markup=self._wallet_button(),
        )

    def post_wallet_stats(
        self, balance: float, initial_balance: float,
        open_trades: list, daily_pnl: float, total_pnl: float,
        win_rate: float, nb_trades: int
    ):
        try:
            from config import WALLET_CHAT_ID
        except ImportError:
            return

        pct_day = (daily_pnl / initial_balance * 100) if initial_balance > 0 else 0
        pct_all = ((balance - initial_balance) / initial_balance * 100) if initial_balance > 0 else 0
        trend   = "📈" if daily_pnl >= 0 else "📉"
        bar_wr  = self._bar(win_rate)

        trades_block = ""
        for t in open_trades:
            e = "🟢" if t["pnl"] >= 0 else "🔴"
            trades_block += f"{e} {t['symbol'].replace('/USDT','')} — {t['pnl']:+.2f}$\n"
        if not trades_block:
            trades_block = "— Aucun trade ouvert\n"

        msg = (
            f"⚡ <b>NEMESIS — WALLET LIVE</b>\n"
            f"{SEP}\n"
            f"💰 Capital : <b>{balance:,.2f} USDT</b>\n"
            f"{trend} Jour   : <b>{daily_pnl:+.2f}$</b>  ({pct_day:+.1f}%)\n"
            f"📊 Total  : <b>{total_pnl:+.2f}$</b>  ({pct_all:+.1f}%)\n"
            f"{SEP}\n"
            f"<b>Positions :</b>\n"
            f"{trades_block}"
            f"{SEP}\n"
            f"WR : <b>{win_rate:.0f}%</b>  {bar_wr}  {nb_trades} trades\n"
            f"<i>Mise à jour auto toutes les 30 min</i>"
        )

        if not self.bot:
            return
        try:
            payload = {"chat_id": WALLET_CHAT_ID, "text": msg, "parse_mode": "HTML"}
            requests.post(f"{self._api}/sendMessage", json=payload, timeout=10)
        except Exception as e:
            logger.error(f"❌ Wallet stats: {e}")

    # ─── Legacy aliases ───────────────────────────────────────────────────────

    def notify(self, text: str, markup=None):
        self._send(text, markup)

    def send_photo(self, image_bytes: bytes, caption: str, markup=None):
        self._send_photo(image_bytes, caption, markup)
