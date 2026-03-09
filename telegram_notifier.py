"""
telegram_notifier.py — AlphaTrader v2.5
Style : Station X — propre, minimaliste, professionnel.
Emojis forts as visual anchors. Pas d'ASCII art.
"""
import os
import io
import threading
import requests
from datetime import datetime, timezone
from typing import Optional
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from loguru import logger
from dotenv import load_dotenv
from config import WALLET_CHANNEL_URL

load_dotenv()


class TelegramNotifier:

    def __init__(self):
        self._token  = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.bot     = self._token  # Compatibilité — truthy si configuré

        if not self._token or not self.chat_id:
            logger.warning("⚠️  Telegram désactivé — credentials manquants.")
            self.bot = None
            return
        self._api = f"https://api.telegram.org/bot{self._token}"
        logger.info("📱 Telegram notifier initialisé (mode sync REST).")

    # ─── Helpers ──────────────────────────────────────────────────────

    def _wallet_button(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Wallet en direct", url=WALLET_CHANNEL_URL)
        ]])

    def _send(self, text: str, markup: Optional[InlineKeyboardMarkup] = None):
        """Envoi synchrone via REST API — zéro asyncio."""
        if not self.bot:
            return
        try:
            payload: dict = {
                "chat_id":    self.chat_id,
                "text":       text,
                "parse_mode": "HTML",
            }
            if markup:
                import json
                payload["reply_markup"] = json.dumps(markup.to_dict())
            r = requests.post(f"{self._api}/sendMessage",
                              json=payload, timeout=10)
            if not r.ok:
                logger.warning(f"⚠️  Telegram sendMessage {r.status_code}: {r.text[:80]}")
        except Exception as e:
            logger.error(f"❌ Telegram send : {e}")

    def send_message(self, text: str, markup: Optional[InlineKeyboardMarkup] = None):
        self._send(text, markup)

    def _send_photo(self, image_bytes: bytes, caption: str,
                    markup: Optional[InlineKeyboardMarkup] = None):
        """Envoi photo synchrone via multipart REST — zéro asyncio."""
        if not self.bot or not image_bytes:
            return
        try:
            files   = {"photo": ("chart.png", io.BytesIO(image_bytes), "image/png")}
            data: dict = {
                "chat_id":    self.chat_id,
                "caption":    caption,
                "parse_mode": "HTML",
            }
            if markup:
                import json
                data["reply_markup"] = json.dumps(markup.to_dict())
            r = requests.post(f"{self._api}/sendPhoto",
                              data=data, files=files, timeout=30)
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
        return "Asie 🌏"

    # ─── MESSAGES ─────────────────────────────────────────────────────────────

    def notify_start(self, balance: float, symbols: list, futures_balance: float = 0.0):
        pairs = " • ".join([s.replace("/USDT", "") for s in symbols])
        bal_line = (
            f"🟣 {futures_balance:,.0f} USDT · LONG+SHORT\n"
            if futures_balance > 0
            else f"💰 {balance:,.0f} USDT\n"
        )
        self._send(
            f"⚡ <b>ALPHATRADER — EN LIGNE</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 {pairs} · 5m\n"
            f"{bal_line}"
            f"🎯 Risk 0.5% → 2% · Levier ×1\n"
            f"✅ Surveillance 24/7"
        )

    def notify_trade_open(
        self, side, symbol, entry, tp1, tp2, tp3, sl,
        amount, balance, score, confirmations: list,
        context_line: str = "",
        markup: Optional[InlineKeyboardMarkup] = None,
    ):
        ticker = self._ticker(symbol)

        def pct(t): return abs((t - entry) / entry * 100)

        if side == "BUY":
            self._send(
                f"🟢 <b>J'ACHÈTE {ticker} à {entry:,.4f}</b>\n"
                f"\n"
                f"🎯 TP1 : {tp1:,.4f}  (+{pct(tp1):.1f}%)\n"
                f"🎯 TP2 : {tp2:,.4f}  (+{pct(tp2):.1f}%)\n"
                f"🎯 TP3 : Ouvert\n"
                f"\n"
                f"🔒 SL : {sl:,.4f}  (-{pct(sl):.1f}%)\n"
                f"\n"
                f"📊 Confiance : {score}/8 | Session : {self._session()}",
                markup=markup or self._wallet_button(),
            )
        else:
            self._send(
                f"🔴 <b>JE VENDS {ticker} à {entry:,.4f}</b>\n"
                f"\n"
                f"🎯 TP1 : {tp1:,.4f}  (-{pct(tp1):.1f}%)\n"
                f"🎯 TP2 : {tp2:,.4f}  (-{pct(tp2):.1f}%)\n"
                f"🎯 TP3 : Ouvert\n"
                f"\n"
                f"🔒 SL : {sl:,.4f}  (+{pct(sl):.1f}%)\n"
                f"\n"
                f"📊 Confiance : {score}/8 | Session : {self._session()}",
                markup=markup or self._wallet_button(),
            )

    def notify_tp_hit(
        self, tp_num: int, symbol: str, price: float, entry: float,
        pnl_gross: float, fees: float, balance: float,
        remaining_qty: float, be_activated: bool = False,
        markup: Optional[InlineKeyboardMarkup] = None,
    ):
        ticker  = self._ticker(symbol)
        pnl_net = pnl_gross - fees
        pips    = abs(price - entry)

        msg = f"TP{tp_num} TOUCHÉ 🔥 +{pips:.4f} ✅\n\n"
        msg += f"<b>{ticker}</b> | PnL : <b>{pnl_net:+.2f} USDT</b>\n"

        if be_activated:
            msg += f"\nMettez votre SL en Break Even ✅"
        elif tp_num == 2:
            msg += f"\nTP3 toujours ouvert — Trailing actif 🤖"

        self._send(msg, markup=markup or self._wallet_button())

    def notify_tp3_closed(self, symbol: str, price: float, entry: float,
                          pnl_gross: float, fees: float, balance: float):
        ticker  = self._ticker(symbol)
        pnl_net = pnl_gross - fees
        pips    = abs(price - entry)
        self._send(
            f"TP3 TOUCHÉ 🔥🔥🔥 +{pips:.4f} ✅\n"
            f"\n"
            f"<b>{ticker}</b> — Trade complet 3/3 🏆\n"
            f"PnL net : <b>{pnl_net:+.2f} USDT</b>\n"
            f"\n"
            f"Balance : {balance:,.2f} USDT",
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
                f"Break Even touché — {ticker} ✅\n"
                f"\n"
                f"Sortie au prix d'entrée.\n"
                f"Capital protégé, aucune perte 💎"
            )
        else:
            pips = abs(price - entry)
            self._send(
                f"STOP LOSS TOUCHÉ ❌\n"
                f"\n"
                f"<b>{ticker}</b> | -{pips:.4f} pips\n"
                f"PnL : <b>{pnl_net:+.2f} USDT</b>\n"
                f"\n"
                f"Prochaine opportunité en analyse... 🔍"
            )

    def notify_trade_closed(
        self, symbol: str, reason: str,
        total_pnl_gross: float, total_fees: float,
        balance: float, initial_balance: float,
        entry: float, exit_price: float, daily_summary: str
    ):
        ticker  = self._ticker(symbol)
        net     = total_pnl_gross - total_fees
        emoji   = "✅" if net >= 0 else "❌"
        self._send(
            f"{emoji} Trade clôturé — <b>{ticker}</b>\n"
            f"\n"
            f"{entry:,.4f} → {exit_price:,.4f}\n"
            f"PnL net : <b>{net:+.2f} USDT</b>\n"
            f"Raison : {reason}"
        )

    def notify_trailing_stop_update(self, symbol: str, old_sl: float, new_sl: float):
        ticker = self._ticker(symbol)
        logger.info(f"🔄 Trailing Stop {ticker} : {old_sl:,.4f} → {new_sl:,.4f}")

    def notify_daily_report(self, report_lines: list, date_str: str):
        total     = len(report_lines)
        wins      = sum(1 for *_, r, __ in report_lines if r.startswith("+")) if report_lines and len(report_lines[0]) == 5 else 0
        total_net = sum(pnl for *_, pnl in report_lines) if report_lines and len(report_lines[0]) == 5 else 0
        wr        = wins / total * 100 if total > 0 else 0

        lines = ""
        for row in report_lines:
            if len(row) == 5:
                date, side, ticker, result, pnl = row
                if result == "BE":
                    lines += f"🔵 {ticker} — Break Even\n"
                elif result.startswith("+"):
                    lines += f"✅ {ticker} — {result}  ({pnl:+.2f}$)\n"
                else:
                    lines += f"❌ {ticker} — {result}  ({pnl:+.2f}$)\n"

        self._send(
            f"📊 <b>BILAN DU {date_str.upper()}</b>\n"
            f"\n"
            f"{lines}"
            f"\n"
            f"BILAN TRADES : <b>{wins}/{total}</b>\n"
            f"WINRATE : <b>{wr:.0f}%</b>\n"
            f"PnL NET : <b>{total_net:+.2f} USDT</b>",
            markup=self._wallet_button(),
        )

    def notify_weekly_report(self, report: str):
        self._send(report)

    def notify_morning_brief(self, brief: str):
        """Rapport matinal style Station X."""
        d = self._date_fr()
        self._send(
            f"☀️ <b>Matinale du {d}</b>\n"
            f"\n"
            f"{brief}"
        )

    def notify_news_pause(self, event_name: str, minutes: float):
        self._send(
            f"⏸ <b>TRADING SUSPENDU</b>\n"
            f"\n"
            f"📰 Événement macro : <b>{event_name}</b>\n"
            f"⏱ Dans <b>{abs(minutes):.0f} minutes</b>\n"
            f"\n"
            f"Le bot reprend automatiquement après la publication ✅"
        )

    def notify_drawdown_alert(self, balance: float, pct: float):
        self._send(
            f"🛑 <b>LIMITE DE PERTE ATTEINTE</b>\n"
            f"\n"
            f"Perte du jour : <b>{pct:.1%}</b>\n"
            f"Trading suspendu jusqu'à minuit UTC 🛌\n"
            f"\n"
            f"Capital protégé : {balance:,.2f} USDT"
        )

    def notify_error(self, error: str, balance: float = 0.0, count: int = 1):
        """Alerte Telegram à chaque erreur de boucle."""
        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        severity = "🟠" if count < 3 else "🔴"
        self._send(
            f"{severity} <b>ERREUR BOT</b> · #{count}\n"
            f"\n"
            f"⏰ <b>{now}</b>\n"
            f"💰 Balance : <b>{balance:,.2f} USDT</b>\n"
            f"\n"
            f"<code>{error[:300]}</code>\n"
            f"\n"
            f"<i>Bot continu - Railway logs pour détails</i>"
        )

    def notify_crash(self, error: str, consecutive: int):
        """Alerte critique : erreurs consécutives détectées."""
        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        self._send(
            f"🚨 <b>ALERTE CRITIQUE — BOT INSTABLE</b>\n"
            f"\n"
            f"⏰ {now}\n"
            f"🔄 {consecutive} erreurs consécutives\n"
            f"\n"
            f"<code>{error[:200]}</code>\n"
            f"\n"
            f"⚠️ Vérifie Railway → Deploy Logs\n"
            f"🔗 https://trading-bot-production-7b2a.up.railway.app"
        )

    def notify_restart(self, balance: float):
        """Envoyé au redémarrage du bot après un crash."""
        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        self._send(
            f"✅ <b>BOT REDÉMARRÉ</b>\n"
            f"\n"
            f"⏰ {now}\n"
            f"💰 Balance : <b>{balance:,.2f} USDT</b>\n"
            f"\n"
            f"<i>AlphaTrader v2.5 actif — Railway Production</i>"
        )

    def notify_pre_signal(self, side: str, symbol: str, price: float, score: int):
        ticker    = self._ticker(symbol)
        direction = "📈 LONG" if side == "BUY" else "📉 SHORT"
        self._send(
            f"⏳ <b>SETUP EN FORMATION — {ticker} {direction}</b>"
        )

    def notify_setup_cancelled(self, symbol: str):
        ticker = self._ticker(symbol)
        self._send(
            f"❌ <b>SETUP ANNULÉ — {ticker}</b>"
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

        trades_block = ""
        for t in open_trades:
            e = "🟢" if t["pnl"] >= 0 else "🔴"
            trades_block += f"{e} {t['symbol'].replace('/USDT','')} — {t['pnl']:+.2f} USDT\n"
        if not trades_block:
            trades_block = "Aucun trade ouvert\n"

        msg = (
            f"⚡ <b>ALPHATRADER — WALLET LIVE</b>\n"
            f"\n"
            f"💰 Capital : <b>{balance:,.2f} USDT</b>\n"
            f"{trend} PnL du jour : <b>{daily_pnl:+.2f} USDT</b> ({pct_day:+.1f}%)\n"
            f"📊 PnL total : <b>{total_pnl:+.2f} USDT</b> ({pct_all:+.1f}%)\n"
            f"\n"
            f"<b>Positions ouvertes :</b>\n"
            f"{trades_block}"
            f"\n"
            f"WINRATE : <b>{win_rate:.0f}%</b>  —  {nb_trades} trades\n"
            f"\n"
            f"Mise à jour automatique toutes les 30min ✅"
        )

        if not self.bot:
            return
        async def _go():
            await self.bot.send_message(
                chat_id=WALLET_CHAT_ID,
                text=msg,
                parse_mode="HTML",
            )
        self._run(_go())

    # ─── Legacy aliases ───────────────────────────────────────────────────────

    def notify(self, text: str, markup=None):
        self._send(text, markup)

    def send_photo(self, image_bytes: bytes, caption: str, markup=None):
        self._send_photo(image_bytes, caption, markup)
