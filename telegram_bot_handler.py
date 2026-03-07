"""
telegram_bot_handler.py — Polling Telegram via requests (compatible thead/Python 3.9).
Écoute les commandes /status /trades /pause /resume /close
et les boutons inline (close, BE, pause, resume).
"""

import os
import time
import threading
from typing import Optional, Callable, Tuple
from loguru import logger
from dotenv import load_dotenv
import requests

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

load_dotenv()

POLL_TIMEOUT = 30   # long-polling seconds


class TelegramBotHandler:
    """Polling Telegram sans asyncio — compatible thread background Python 3.9."""

    def __init__(self):
        self.token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._base   = f"https://api.telegram.org/bot{self.token}"
        self._offset  = 0
        self._paused  = False
        self._running = True

        # Callbacks depuis main.py
        self._get_status:  Optional[Callable[[], str]] = None
        self._get_trades:  Optional[Callable[[], Tuple[str, Optional[InlineKeyboardMarkup]]]] = None
        self._close_trade: Optional[Callable[[str], str]] = None
        self._force_be:    Optional[Callable[[str], str]] = None
        self._pause_cb:    Optional[Callable[[], None]]   = None
        self._resume_cb:   Optional[Callable[[], None]]   = None

        if not self.token or not self.chat_id:
            logger.warning("⚠️  TelegramBotHandler désactivé")
            return
        logger.info("🤖 TelegramBotHandler initialisé — polling actif")

    def register_callbacks(self, get_status, get_trades, close_trade,
                           force_be, pause, resume):
        self._get_status  = get_status
        self._get_trades  = get_trades
        self._close_trade = close_trade
        self._force_be    = force_be
        self._pause_cb    = pause
        self._resume_cb   = resume

    def is_paused(self) -> bool:
        return self._paused

    def start_polling(self):
        if not self.token:
            return
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        logger.info("📱 Polling Telegram démarré")

    def stop(self):
        self._running = False

    # ─── Core polling ─────────────────────────────────────────────────────────

    def _poll_loop(self):
        while self._running:
            try:
                updates = self._get_updates()
                for upd in updates:
                    self._offset = upd["update_id"] + 1
                    self._dispatch(upd)
            except Exception as e:
                logger.error(f"❌ Polling error : {e}")
                time.sleep(5)

    def _get_updates(self) -> list:
        try:
            r = requests.get(
                f"{self._base}/getUpdates",
                params={"timeout": POLL_TIMEOUT, "offset": self._offset},
                timeout=POLL_TIMEOUT + 5,
            )
            data = r.json()
            return data.get("result", []) if data.get("ok") else []
        except Exception:
            return []

    def _dispatch(self, upd: dict):
        """Route message ou callback vers le bon handler."""
        if "message" in upd:
            self._handle_message(upd["message"])
        elif "callback_query" in upd:
            self._handle_callback(upd["callback_query"])

    def _handle_message(self, msg: dict):
        chat = str(msg.get("chat", {}).get("id", ""))
        if chat != str(self.chat_id):
            return
        text = msg.get("text", "").strip()
        parts = text.split()
        cmd   = parts[0].lower().split("@")[0] if parts else ""

        if cmd == "/start":
            self._reply(
                "⚡ *AlphaTrader v2.0*\n\n"
                "📊 /status — Solde & état\n"
                "📋 /trades — Positions actives\n"
                "⏸️ /pause — Pauser\n"
                "▶️ /resume — Reprendre\n"
                "🔴 /close BTC — Fermer un trade\n"
                "❓ /help — Aide"
            )
        elif cmd == "/help":
            self._reply(
                "*⚡ AlphaTrader — Aide*\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🤖 Bot de trading algorithmique\n"
                "📊 BTC • ETH • SOL • BNB | 15m\n"
                "🎯 6 filtres | 3 TP + BE + Trailing\n"
                "💰 Risk : 1%/trade\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n"
                "`/status` `/trades` `/pause` `/resume` `/close BTC`"
            )
        elif cmd == "/status":
            text_out = self._get_status() if self._get_status else "Chargement..."
            self._reply(text_out)
        elif cmd == "/trades":
            if self._get_trades:
                text_out, markup = self._get_trades()
                self._reply(text_out, markup)
            else:
                self._reply("Aucun trade actif.")
        elif cmd == "/pause":
            self._paused = True
            if self._pause_cb:
                self._pause_cb()
            self._reply("⏸️ *Bot en pause* — Utilise `/resume` pour reprendre.")
        elif cmd == "/resume":
            self._paused = False
            if self._resume_cb:
                self._resume_cb()
            self._reply("▶️ *Bot actif* — Surveillance reprise.")
        elif cmd == "/close":
            if len(parts) < 2:
                self._reply("Usage : `/close BTC`")
                return
            symbol = f"{parts[1].upper()}/USDT"
            result = self._close_trade(symbol) if self._close_trade else "Erreur"
            self._reply(result)

    def _handle_callback(self, cq: dict):
        cq_id  = cq["id"]
        data   = cq.get("data", "")
        self._answer_callback(cq_id)

        if data.startswith("close:"):
            symbol = data.split(":")[1]
            result = self._close_trade(symbol) if self._close_trade else "Erreur"
            self._reply(result)
        elif data.startswith("be:"):
            symbol = data.split(":")[1]
            result = self._force_be(symbol) if self._force_be else "Erreur"
            self._reply(result)
        elif data == "pause":
            self._paused = True
            if self._pause_cb:
                self._pause_cb()
            self._reply("⏸️ *Bot en pause*")
        elif data == "resume":
            self._paused = False
            if self._resume_cb:
                self._resume_cb()
            self._reply("▶️ *Bot actif*")

    # ─── Helpers HTTP ─────────────────────────────────────────────────────────

    def _reply(self, text: str, markup: Optional[InlineKeyboardMarkup] = None):
        payload = {
            "chat_id":    self.chat_id,
            "text":       text,
            "parse_mode": "Markdown",
        }
        if markup:
            import json
            payload["reply_markup"] = markup.to_json()
        try:
            requests.post(f"{self._base}/sendMessage", json=payload, timeout=10)
        except Exception as e:
            logger.error(f"❌ Reply error : {e}")

    def _answer_callback(self, cq_id: str):
        try:
            requests.post(
                f"{self._base}/answerCallbackQuery",
                json={"callback_query_id": cq_id}, timeout=5
            )
        except Exception:
            pass

    # ─── Keyboards ────────────────────────────────────────────────────────────

    @staticmethod
    def trade_keyboard(symbol: str) -> InlineKeyboardMarkup:
        base = symbol.replace("/USDT", "")
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"🔴 Fermer {base}", callback_data=f"close:{symbol}"),
                InlineKeyboardButton("🔒 Forcer BE",      callback_data=f"be:{symbol}"),
            ],
            [
                InlineKeyboardButton("⏸️ Pause bot", callback_data="pause"),
                InlineKeyboardButton("▶️ Reprendre", callback_data="resume"),
            ]
        ])
