"""
telegram_notifier.py — Alertes Telegram style "Station X".

Format des messages :
  🟢 J'ACHÈTE BTC/USDT à 67 250
  🎯 TP1 : 67 600
  🎯 TP2 : 68 950
  🎯 TP3 : 69 650
  🔒 SL  : 66 900

  + alertes TP atteint / BE activé / clôture
"""
import os
import asyncio
from telegram import Bot
from telegram.error import TelegramError
from loguru import logger
from dotenv import load_dotenv

load_dotenv()


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
            await self.bot.send_message(chat_id=self.chat_id, text=text, parse_mode="Markdown")
            return True
        except TelegramError as e:
            logger.error(f"❌ Telegram : {e}")
            return False

    def notify(self, text: str):
        """Envoie un message (synchrone)."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._send(text))
            else:
                loop.run_until_complete(self._send(text))
        except RuntimeError:
            asyncio.run(self._send(text))

    # ─── Messages formatés ───────────────────────────────────────────────────

    def notify_start(self, balance: float):
        self.notify(
            f"🤖 *Bot de Trading démarré*\n"
            f"💰 Solde : `{balance:,.0f} USDT`\n"
            f"📊 Marché : `BTC/USDT` | `15m`\n"
            f"🎯 Stratégie : EMA + RSI + MACD\n"
            f"✅ Surveillance active"
        )

    def notify_trade_open(
        self,
        side: str,
        symbol: str,
        entry: float,
        tp1: float,
        tp2: float,
        tp3: float,
        sl: float,
        amount: float,
        balance: float,
    ):
        """Message style Station X — ouverture de trade."""
        emoji_side = "🟢" if side == "BUY" else "🔴"
        action     = "J'ACHÈTE" if side == "BUY" else "JE VENDS"
        pair       = symbol.replace("/", "")

        msg = (
            f"{emoji_side} *{action} {pair} à {entry:,.2f}*\n\n"
            f"🎯 TP1 : `{tp1:,.2f}`\n"
            f"🎯 TP2 : `{tp2:,.2f}`\n"
            f"🎯 TP3 : `{tp3:,.2f}`\n"
            f"🔒 SL  : `{sl:,.2f}`\n\n"
            f"📦 Qté : `{amount:.5f} BTC`\n"
            f"💰 Solde : `{balance:,.2f} USDT`"
        )
        self.notify(msg)
        logger.info(f"📤 Telegram — Trade ouvert envoyé")

    def notify_tp_hit(self, tp_num: int, price: float, pnl: float, be_activated: bool = False):
        """Alerte quand un TP est touché."""
        msg = f"🎯 *TP{tp_num} ATTEINT !*\n💵 Prix : `{price:,.2f}` | PnL : `{pnl:+.2f} USDT`"
        if be_activated:
            msg += f"\n\n✅ *SL déplacé au Break Even* — TP2 & TP3 SANS RISQUE ! 🔒"
        self.notify(msg)

    def notify_be_activated(self, entry: float):
        """Alerte séparée quand le BE est activé."""
        self.notify(
            f"🔒 *Break Even activé !*\n"
            f"✅ SL déplacé au prix d'entrée : `{entry:,.2f}`\n"
            f"TP2 et TP3 sont maintenant *sans risque*"
        )

    def notify_sl_hit(self, price: float, entry: float, is_be: bool, pnl: float):
        """Alerte Stop-Loss touché (normal ou BE)."""
        if is_be:
            msg = (
                f"🛡️ *Stop-Loss BE touché*\n"
                f"Trade fermé au *Break Even* — Aucune perte !\n"
                f"💵 Prix : `{price:,.2f}` | PnL : `{pnl:+.2f} USDT`"
            )
        else:
            msg = (
                f"🛑 *Stop-Loss touché*\n"
                f"💵 Prix : `{price:,.2f}` | Entrée : `{entry:,.2f}`\n"
                f"📉 PnL : `{pnl:+.2f} USDT`"
            )
        self.notify(msg)

    def notify_trade_closed(self, reason: str, total_pnl: float, balance: float):
        """Résumé final quand le trade est complètement fermé."""
        self.notify(
            f"✅ *Trade terminé — {reason}*\n"
            f"💵 PnL total : `{total_pnl:+.2f} USDT`\n"
            f"💰 Solde : `{balance:,.2f} USDT`"
        )

    def notify_drawdown_alert(self, balance: float, pct: float):
        self.notify(
            f"⛔ *ALERTE DRAWDOWN*\n"
            f"📉 Perte journalière : `{pct:.1%}`\n"
            f"💰 Solde : `{balance:,.2f} USDT`\n"
            f"🔒 Bot en *PAUSE* jusqu'à demain."
        )

    def notify_error(self, error: str):
        self.notify(f"⚠️ *Erreur Bot*\n```{error[:200]}```")
