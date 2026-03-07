"""
telegram_notifier.py — Notifications premium style Station X.
Nom du bot : ⚡ AlphaTrader | Format propre et précis.
"""
import os
import asyncio
from telegram import Bot
from telegram.error import TelegramError
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

BOT_NAME = "⚡ AlphaTrader"


class TelegramNotifier:

    def __init__(self):
        token        = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if not token or not self.chat_id:
            logger.warning("⚠️  Telegram désactivé — credentials manquants.")
            self.bot = None
            return
        try:
            self.bot = Bot(token=token)
            logger.info("📱 Telegram notifier initialisé.")
        except Exception as e:
            logger.error(f"❌ Telegram init : {e}")
            self.bot = None

    async def _send(self, text: str) -> bool:
        if not self.bot:
            return False
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="Markdown"
            )
            return True
        except TelegramError as e:
            logger.error(f"❌ Telegram : {e}")
            return False

    def notify(self, text: str):
        if not self.bot:
            return
        try:
            asyncio.run(self._send(text))
        except RuntimeError:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self._send(text))
            loop.close()

    # ─── Messages premium ─────────────────────────────────────────────────────

    def notify_start(self, balance: float, symbols: list):
        pairs = " | ".join([s.replace("/USDT", "") for s in symbols])
        self.notify(
            f"*{BOT_NAME}* — Démarrage\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Marchés : `{pairs}`\n"
            f"⏱  Timeframe : `15min`\n"
            f"🎯 Stratégie : 6 filtres\n"
            f"💰 Capital : `{balance:,.0f} USDT`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 *Surveillance active*"
        )

    def notify_trade_open(
        self, side, symbol, entry, tp1, tp2, tp3, sl, amount, balance, score
    ):
        emoji  = "🟢" if side == "BUY" else "🔴"
        action = "J'ACHÈTE" if side == "BUY" else "JE VENDS"
        pair   = symbol.replace("/", "")
        sl_pts = abs(entry - sl)

        self.notify(
            f"*{BOT_NAME}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} *{action} {pair}*\n"
            f"💵 Entrée : `{entry:,.2f}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 TP1 : `{tp1:,.2f}`  _(+{sl_pts:.0f} pts)_\n"
            f"🎯 TP2 : `{tp2:,.2f}`  _(+{sl_pts*2:.0f} pts)_\n"
            f"🎯 TP3 : `{tp3:,.2f}`  _(+{sl_pts*3:.0f} pts)_\n"
            f"🔒 SL  : `{sl:,.2f}`   _(-{sl_pts:.0f} pts)_\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 Qté : `{amount:.5f}` | Score : `{score}/6`\n"
            f"💰 Solde : `{balance:,.2f} USDT`"
        )

    def notify_tp_hit(self, tp_num: int, symbol: str, price: float, pnl: float, be_activated: bool = False):
        pair = symbol.replace("/USDT", "")
        msg  = (
            f"*{BOT_NAME}* — TP{tp_num} ✅\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *TP{tp_num} TOUCHÉ* — `{pair}`\n"
            f"💵 Prix : `{price:,.2f}`\n"
            f"💵 PnL partiel : `{pnl:+.2f} USDT`"
        )
        if be_activated:
            msg += (
                f"\n━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔒 *SL déplacé au Break Even*\n"
                f"✅ TP2 & TP3 *SANS RISQUE !*"
            )
        self.notify(msg)

    def notify_sl_hit(self, symbol: str, price: float, entry: float, is_be: bool, pnl: float):
        pair = symbol.replace("/USDT", "")
        if is_be:
            self.notify(
                f"*{BOT_NAME}* — Break Even 🛡️\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🛡️ *SL BE touché* — `{pair}`\n"
                f"Trade fermé au *Break Even*\n"
                f"✅ *Aucune perte !*\n"
                f"💵 PnL : `{pnl:+.2f} USDT`"
            )
        else:
            self.notify(
                f"*{BOT_NAME}* — Stop-Loss 🛑\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🛑 *SL touché* — `{pair}`\n"
                f"💵 Clôture : `{price:,.2f}`\n"
                f"📉 PnL : `{pnl:+.2f} USDT`"
            )

    def notify_trade_closed(self, symbol: str, reason: str, total_pnl: float, balance: float):
        pair = symbol.replace("/USDT", "")
        emoji = "✅" if total_pnl >= 0 else "❌"
        self.notify(
            f"*{BOT_NAME}* — Clôture {emoji}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"`{pair}` fermé — *{reason}*\n"
            f"💵 PnL total : `{total_pnl:+.2f} USDT`\n"
            f"💰 Capital : `{balance:,.2f} USDT`"
        )

    def notify_news_pause(self, event_name: str, minutes: float):
        self.notify(
            f"*{BOT_NAME}* — ⏸️ Pause News\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 *{event_name}*\n"
            f"⏱  Dans `{abs(minutes):.0f} min`\n"
            f"🔇 Trading suspendu ±30min"
        )

    def notify_daily_report(self, report: str):
        self.notify(report)

    def notify_drawdown_alert(self, balance: float, pct: float):
        self.notify(
            f"*{BOT_NAME}* — ⛔ ALERTE\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📉 Drawdown : `{pct:.1%}`\n"
            f"💰 Capital : `{balance:,.2f} USDT`\n"
            f"🔒 *Bot en PAUSE jusqu'à demain*"
        )

    def notify_error(self, error: str):
        self.notify(
            f"*{BOT_NAME}* — ⚠️ Erreur\n"
            f"```{error[:200]}```"
        )
