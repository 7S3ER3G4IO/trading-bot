"""
telegram_bot_handler.py — Gestion interactive du bot Telegram.
Ajoute le polling pour recevoir messages + boutons inline.

Commandes disponibles :
  /start   - Message de bienvenue
  /status  - Solde + trades actifs
  /trades  - Liste des trades avec PnL en temps réel
  /pause   - Pause le bot
  /resume  - Reprend le bot
  /close <SYMBOL> - Ferme un trade manuellement

Boutons inline sur chaque trade :
  [🔴 Fermer] [🔒 Forcer BE] [⏸️ Pause]
"""

import os
import threading
import asyncio
from typing import Optional, Callable, Dict
from loguru import logger
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

load_dotenv()


class TelegramBotHandler:
    """Gère le polling Telegram et les commandes/boutons interactifs."""

    def __init__(self):
        token        = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.token   = token

        # Callbacks injectés depuis main.py
        self._get_status:    Optional[Callable] = None
        self._get_trades:    Optional[Callable] = None
        self._close_trade:   Optional[Callable] = None
        self._force_be:      Optional[Callable] = None
        self._pause_bot:     Optional[Callable] = None
        self._resume_bot:    Optional[Callable] = None
        self._bot_paused:    bool = False

        if not token:
            logger.warning("⚠️  TelegramBotHandler désactivé — token manquant")
            self.app = None
            return

        self.app = Application.builder().token(token).build()
        self._register_handlers()
        logger.info("🤖 TelegramBotHandler initialisé — polling actif")

    def register_callbacks(
        self,
        get_status:  Callable,
        get_trades:  Callable,
        close_trade: Callable,
        force_be:    Callable,
        pause:       Callable,
        resume:      Callable,
    ):
        """Enregistre les fonctions de contrôle depuis main.py."""
        self._get_status  = get_status
        self._get_trades  = get_trades
        self._close_trade = close_trade
        self._force_be    = force_be
        self._pause_bot   = pause
        self._resume_bot  = resume

    def is_paused(self) -> bool:
        return self._bot_paused

    def start_polling(self):
        """Lance le polling dans un thread séparé."""
        if not self.app:
            return
        thread = threading.Thread(target=self._run_polling, daemon=True)
        thread.start()
        logger.info("📱 Polling Telegram démarré (thread background)")

    def _run_polling(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.app.run_polling(drop_pending_updates=True))

    # ─── Handlers ─────────────────────────────────────────────────────────────

    def _register_handlers(self):
        self.app.add_handler(CommandHandler("start",  self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("trades", self._cmd_trades))
        self.app.add_handler(CommandHandler("pause",  self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("close",  self._cmd_close))
        self.app.add_handler(CommandHandler("help",   self._cmd_help))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))

    def _is_authorized(self, update: Update) -> bool:
        return str(update.effective_chat.id) == str(self.chat_id)

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        await update.message.reply_text(
            "⚡ *AlphaTrader* — Bienvenue !\n\n"
            "Commandes disponibles :\n"
            "📊 /status — Solde & état du bot\n"
            "📋 /trades — Trades actifs\n"
            "⏸️ /pause — Pauser le bot\n"
            "▶️ /resume — Reprendre\n"
            "🔴 /close BTC — Fermer un trade\n"
            "❓ /help — Aide",
            parse_mode="Markdown"
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        await update.message.reply_text(
            "*⚡ AlphaTrader — Aide*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🤖 Bot de trading algorithmique\n"
            "📊 Marchés : BTC • ETH • SOL • BNB\n"
            "⏱  Timeframe : 15 minutes\n"
            "🎯 Stratégie : 6 filtres EMA+RSI+MACD+ADX+Vol+HTF\n"
            "💰 Risk : 1% du capital par trade\n"
            "🎯 3 TP + Break Even automatique\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "*Commandes admin :*\n"
            "`/status` — Solde + état\n"
            "`/trades` — Positions ouvertes\n"
            "`/pause` — Stopper les nouveaux trades\n"
            "`/resume` — Reprendre\n"
            "`/close BTC` — Clôture forcée",
            parse_mode="Markdown"
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if self._get_status:
            text = self._get_status()
        else:
            text = "⚡ AlphaTrader — Status en cours de chargement..."
        await update.message.reply_text(text, parse_mode="Markdown")

    async def _cmd_trades(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if self._get_trades:
            text, markup = self._get_trades()
        else:
            text, markup = "Aucun trade actif.", None
        await update.message.reply_text(
            text, parse_mode="Markdown",
            reply_markup=markup if markup else None
        )

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        self._bot_paused = True
        if self._pause_bot:
            self._pause_bot()
        await update.message.reply_text(
            "⏸️ *Bot en pause* — Plus de nouveaux trades.\n"
            "Utilise `/resume` pour reprendre.",
            parse_mode="Markdown"
        )

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        self._bot_paused = False
        if self._resume_bot:
            self._resume_bot()
        await update.message.reply_text(
            "▶️ *Bot actif* — Reprise de la surveillance.",
            parse_mode="Markdown"
        )

    async def _cmd_close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        args = ctx.args
        if not args:
            await update.message.reply_text(
                "Usage : `/close BTC` ou `/close ETH`", parse_mode="Markdown"
            )
            return
        symbol_base = args[0].upper()
        symbol = f"{symbol_base}/USDT"
        if self._close_trade:
            result = self._close_trade(symbol)
            await update.message.reply_text(result, parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Fonction de clôture non disponible.")

    async def _handle_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Gère les appuis sur les boutons inline."""
        query = update.callback_query
        await query.answer()
        data = query.data

        if data.startswith("close:"):
            symbol = data.split(":")[1]
            if self._close_trade:
                result = self._close_trade(symbol)
                await query.edit_message_text(result, parse_mode="Markdown")

        elif data.startswith("be:"):
            symbol = data.split(":")[1]
            if self._force_be:
                result = self._force_be(symbol)
                await query.edit_message_text(result, parse_mode="Markdown")

        elif data == "pause":
            self._bot_paused = True
            if self._pause_bot:
                self._pause_bot()
            await query.edit_message_text(
                "⏸️ *Bot en pause* — Plus de nouveaux trades.",
                parse_mode="Markdown"
            )
        elif data == "resume":
            self._bot_paused = False
            if self._resume_bot:
                self._resume_bot()
            await query.edit_message_text(
                "▶️ *Bot actif* — Reprise.",
                parse_mode="Markdown"
            )

    # ─── Création des boutons inline ─────────────────────────────────────────

    @staticmethod
    def trade_keyboard(symbol: str) -> InlineKeyboardMarkup:
        """Boutons sous chaque message de trade ouvert."""
        base = symbol.replace("/USDT", "")
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"🔴 Fermer {base}", callback_data=f"close:{symbol}"),
                InlineKeyboardButton("🔒 Forcer BE",      callback_data=f"be:{symbol}"),
            ],
            [
                InlineKeyboardButton("⏸️ Pause bot",  callback_data="pause"),
                InlineKeyboardButton("▶️ Reprendre",  callback_data="resume"),
            ]
        ])
