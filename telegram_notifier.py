"""
telegram_notifier.py — Notifications AlphaTrader.
Format minimaliste, fond sombre via code blocks, bouton wallet inline.
"""
import os
import io
import asyncio
from typing import Optional
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import TelegramError
from loguru import logger
from dotenv import load_dotenv
from config import WALLET_CHANNEL_URL

load_dotenv()


class TelegramNotifier:

    def __init__(self):
        token        = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.bot     = None

        if not token or not self.chat_id:
            logger.warning("⚠️  Telegram désactivé — credentials manquants.")
            return
        try:
            self.bot = Bot(token=token)
            logger.info("📱 Telegram notifier initialisé.")
        except Exception as e:
            logger.error(f"❌ Telegram init : {e}")

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _run(self, coro):
        """Exécute une coroutine Telegram de façon sûre (thread-safe)."""
        if not self.bot:
            return
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(coro)
        except Exception as e:
            logger.error(f"❌ Telegram send : {e}")
        finally:
            loop.close()

    def _wallet_button(self) -> InlineKeyboardMarkup:
        """Bouton inline → canal wallet temps réel."""
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Voir le Wallet en direct", url=WALLET_CHANNEL_URL)
        ]])

    def _send(self, text: str, markup: Optional[InlineKeyboardMarkup] = None):
        """Envoie un message texte (Markdown)."""
        if not self.bot:
            return
        async def _go():
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=markup,
            )
        self._run(_go())

    def _send_photo(self, image_bytes: bytes, caption: str,
                    markup: Optional[InlineKeyboardMarkup] = None):
        if not self.bot or not image_bytes:
            return
        async def _go():
            buf = io.BytesIO(image_bytes)
            buf.name = "chart.png"
            await self.bot.send_photo(
                chat_id=self.chat_id, photo=buf,
                caption=caption, parse_mode="Markdown",
                reply_markup=markup,
            )
        self._run(_go())

    def _coin(self, symbol: str) -> str:
        """BTC/USDT → BITCOIN"""
        names = {
            "BTC": "BITCOIN", "ETH": "ETHEREUM",
            "SOL": "SOLANA",  "BNB": "BNB",
        }
        ticker = symbol.replace("/USDT", "")
        return names.get(ticker, ticker)

    def _ticker(self, symbol: str) -> str:
        return symbol.replace("/USDT", "")

    # ─── Messages ─────────────────────────────────────────────────────────────

    def notify_start(self, balance: float, symbols: list):
        pairs = " • ".join([s.replace("/USDT", "") for s in symbols])
        self._send(
            f"⚡ *AlphaTrader* — Démarrage 🟢\n"
            f"```\n"
            f"Marchés   : {pairs}\n"
            f"Timeframe : 15min\n"
            f"Capital   : {balance:,.2f} USDT\n"
            f"Risk/trade: 1%  |  Pause: -5%/j\n"
            f"```\n"
            f"🟢 *Surveillance active 24/7*"
        )

    def notify_trade_open(
        self, side, symbol, entry, tp1, tp2, tp3, sl,
        amount, balance, score, confirmations: list,
        context_line: str = "",
        markup: Optional[InlineKeyboardMarkup] = None,
    ):
        coin   = self._coin(symbol)
        ticker = self._ticker(symbol)
        action = "J'ACHÈTE" if side == "BUY" else "JE VENDS"
        alert  = "🚨 ACHAT" if side == "BUY" else "🚨 VENTE"
        emoji  = "🟢" if side == "BUY" else "🔴"

        # Message 1 — Alerte choc
        self._send(f"{alert} {coin} NOW !")

        # Message 2 — Détails du trade (fond sombre via code block)
        self._send(
            f"{emoji} *{action} {ticker}/USDT à {entry:,.2f}*\n"
            f"```\n"
            f"🎯 TP1 : {tp1:,.2f}\n"
            f"🎯 TP2 : {tp2:,.2f}\n"
            f"🎯 TP3 : Ouvert (Trailing Stop actif)\n"
            f"\n"
            f"🔒 SL  : {sl:,.2f}\n"
            f"```",
            markup=self._wallet_button(),
        )

    def notify_tp_hit(
        self, tp_num: int, symbol: str, price: float, entry: float,
        pnl_gross: float, fees: float, balance: float,
        remaining_qty: float, be_activated: bool = False,
        markup: Optional[InlineKeyboardMarkup] = None,
    ):
        ticker  = self._ticker(symbol)
        pips    = abs(price - entry)
        pnl_net = pnl_gross - fees

        msg = (
            f"✅ *TP{tp_num} TOUCHÉ — {ticker}*\n"
            f"```\n"
            f"Prix   : {price:,.2f}  (+{pips:.0f} pips)\n"
            f"PnL net: {pnl_net:+.2f} USDT\n"
            f"```"
        )

        if tp_num == 1:
            msg += "\n➡️ *Mettez votre SL en Break Even*"
        elif tp_num == 2:
            msg += "\n🔓 TP3 toujours ouvert — Trailing actif"

        if be_activated and tp_num == 1:
            msg += "\n🔒 SL automatiquement passé en Break Even"

        self._send(msg, markup=self._wallet_button())

    def notify_tp3_closed(self, symbol: str, price: float, entry: float,
                          pnl_gross: float, fees: float, balance: float):
        """TP3 fermé par le Trailing Stop (retournement détecté)."""
        ticker  = self._ticker(symbol)
        pips    = abs(price - entry)
        pnl_net = pnl_gross - fees
        self._send(
            f"🔒 *TP3 CLÔTURÉ — {ticker}*\n"
            f"```\n"
            f"Clôture : {price:,.2f}  (+{pips:.0f} pips)\n"
            f"PnL net : {pnl_net:+.2f} USDT\n"
            f"```\n"
            f"🏆 Trade complet — 3/3 TP touchés",
            markup=self._wallet_button(),
        )

    def notify_sl_hit(
        self, symbol: str, price: float, entry: float,
        is_be: bool, pnl_gross: float, fees: float, balance: float
    ):
        ticker  = self._ticker(symbol)
        pnl_net = pnl_gross - fees
        pips    = abs(price - entry)

        if is_be:
            self._send(
                f"🛡️ *Break Even touché — {ticker}*\n"
                f"```\n"
                f"Clôture : {price:,.2f}\n"
                f"Résultat: Aucune perte\n"
                f"```"
            )
        else:
            self._send(
                f"🛑 *Stop Loss touché — {ticker}*\n"
                f"```\n"
                f"Clôture : {price:,.2f}  (-{pips:.0f} pips)\n"
                f"PnL net : {pnl_net:+.2f} USDT\n"
                f"```"
            )

    def notify_trade_closed(
        self, symbol: str, reason: str,
        total_pnl_gross: float, total_fees: float,
        balance: float, initial_balance: float,
        entry: float, exit_price: float, daily_summary: str
    ):
        # Le résumé de clôture est déjà géré par notify_tp3_closed / notify_sl_hit
        # Cette méthode envoie uniquement un résumé si reason est inattendue
        ticker  = self._ticker(symbol)
        net     = total_pnl_gross - total_fees
        pips    = abs(exit_price - entry)
        emoji   = "✅" if net >= 0 else "❌"
        self._send(
            f"{emoji} *Trade clôturé — {ticker}*\n"
            f"```\n"
            f"{entry:,.2f} → {exit_price:,.2f}  ({'+' if pips > 0 else '-'}{pips:.0f} pips)\n"
            f"PnL net : {net:+.2f} USDT\n"
            f"Raison  : {reason}\n"
            f"```"
        )

    def notify_trailing_stop_update(self, symbol: str, old_sl: float, new_sl: float):
        # Silencieux — pas de notif pour éviter le spam de trailing stop
        ticker = self._ticker(symbol)
        logger.info(f"🔄 Trailing Stop {ticker} : {old_sl:,.2f} → {new_sl:,.2f}")

    def notify_daily_report(self, report_lines: list, date_str: str):
        """
        report_lines = liste de tuples : (date, side, ticker, result_str)
        ex: [("06/03", "ACHAT", "BTC", "+680 pips"), ...]
        """
        wins  = sum(1 for _, _, _, r in report_lines if r.startswith("+"))
        total = len(report_lines)

        lines = ""
        for date, side, ticker, result in report_lines:
            if result == "BE":
                lines += f"{date} {side} {ticker} (BE)\n"
            else:
                prefix = "🟢" if result.startswith("+") else "🔴"
                lines += f"{prefix} {date} {side} {ticker} {result}\n"

        self._send(
            f"📊 *BILAN — {date_str}*\n"
            f"```\n"
            f"{lines}\n"
            f"BILAN TRADES : {wins}/{total}\n"
            f"```",
            markup=self._wallet_button(),
        )

    def notify_weekly_report(self, report: str):
        self._send(report)

    def notify_morning_brief(self, brief: str):
        self._send(brief)

    def notify_news_pause(self, event_name: str, minutes: float):
        self._send(
            f"⏸️ *Pause News*\n"
            f"```\n"
            f"Événement : {event_name}\n"
            f"Dans      : {abs(minutes):.0f} min\n"
            f"Trading suspendu ±30min\n"
            f"```"
        )

    def notify_drawdown_alert(self, balance: float, pct: float):
        self._send(
            f"⛔ *DRAWDOWN — Bot en PAUSE*\n"
            f"```\n"
            f"Perte journalière : {pct:.1%}\n"
            f"Capital           : {balance:,.2f} USDT\n"
            f"Reprise           : demain\n"
            f"```"
        )

    def notify_error(self, error: str):
        self._send(f"⚠️ *Erreur bot*\n```{error[:200]}```")

    # Legacy aliases (compatibilité)
    def notify(self, text: str, markup=None):
        self._send(text, markup)

    def send_photo(self, image_bytes: bytes, caption: str, markup=None):
        self._send_photo(image_bytes, caption, markup)
