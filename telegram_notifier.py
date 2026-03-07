"""
telegram_notifier.py — Envoie des alertes Telegram pour chaque événement du bot.
"""
import os
import asyncio
from telegram import Bot
from telegram.error import TelegramError
from loguru import logger
from dotenv import load_dotenv

load_dotenv()


class TelegramNotifier:
    """Wrapper autour de python-telegram-bot pour envoyer des notifications."""

    def __init__(self):
        token   = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if not token or not self.chat_id:
            logger.warning(
                "⚠️  TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant — "
                "les alertes Telegram seront désactivées."
            )
            self.bot = None
            return

        try:
            self.bot = Bot(token=token)
            logger.info("📱 Telegram notifier initialisé.")
        except Exception as e:
            logger.error(f"❌ Erreur init Telegram : {e}")
            self.bot = None

    async def send_message(self, message: str) -> bool:
        """Envoie un message texte Markdown au chat configuré."""
        if not self.bot:
            return False
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode="Markdown"
            )
            return True
        except TelegramError as e:
            logger.error(f"❌ Erreur Telegram send_message : {e}")
            return False

    def notify(self, message: str):
        """Version synchrone — crée une coroutine et l'exécute."""
        try:
            asyncio.run(self.send_message(message))
        except RuntimeError:
            # Si une boucle asyncio est déjà active (ex: Jupyter)
            loop = asyncio.get_event_loop()
            loop.run_until_complete(self.send_message(message))

    # ─── Messages pré-formatés ───────────────────────────────────────────────

    def notify_start(self, balance: float):
        msg = (
            "🤖 *Bot de Trading démarré*\n"
            f"💰 Solde initial : `{balance:.2f} USDT`\n"
            "📊 Marché : `BTC/USDT` | TF : `15m`\n"
            "🎯 Stratégie : EMA + RSI + MACD"
        )
        self.notify(msg)

    def notify_order(
        self,
        side: str,
        symbol: str,
        amount: float,
        entry: float,
        sl: float,
        tp: float,
        balance: float,
    ):
        emoji = "🟢" if side == "BUY" else "🔴"
        msg = (
            f"{emoji} *ORDRE {side} EXÉCUTÉ*\n"
            f"📌 Paire : `{symbol}`\n"
            f"📦 Quantité : `{amount:.5f} BTC`\n"
            f"💵 Entrée : `{entry:.2f} USDT`\n"
            f"🛑 Stop-Loss : `{sl:.2f} USDT`\n"
            f"🎯 Take-Profit : `{tp:.2f} USDT`\n"
            f"💰 Solde restant : `{balance:.2f} USDT`"
        )
        self.notify(msg)

    def notify_drawdown_alert(self, current_balance: float, drawdown_pct: float):
        msg = (
            "⛔ *ALERTE DRAWDOWN*\n"
            f"📉 Perte journalière : `{drawdown_pct:.1%}`\n"
            f"💰 Solde actuel : `{current_balance:.2f} USDT`\n"
            "🔒 Bot en *PAUSE* jusqu'à demain."
        )
        self.notify(msg)

    def notify_error(self, error: str):
        msg = f"⚠️ *Erreur Bot*\n```\n{error}\n```"
        self.notify(msg)
