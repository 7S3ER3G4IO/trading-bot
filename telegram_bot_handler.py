"""
telegram_bot_handler.py — Nemesis v3.0 Multi-Channel Edition
Simplified: handles /start, /health, /pause, /resume commands.
No callback routing needed — Hub uses URL buttons to channels.
"""
import os
import time
import threading
import requests
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

POLL_TIMEOUT = 30


class TelegramBotHandler:

    def __init__(self):
        self.token   = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self._base   = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        self._offset  = 0
        self._running = True
        self._paused  = False

        # ── Callbacks (set by bot_init.register_callbacks) ────────────────────
        self._pause_cb  = None
        self._resume_cb = None
        self._get_hub_data = None

    # ── Register callbacks ────────────────────────────────────────────────────

    def register_callbacks(self, **kwargs):
        """Register callbacks from bot_init."""
        self._pause_cb    = kwargs.get("pause")
        self._resume_cb   = kwargs.get("resume")
        self._get_hub_data = kwargs.get("get_hub_data")

    def is_paused(self) -> bool:
        return self._paused

    def start_polling(self):
        if not self.token:
            return
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        logger.info("📱 Polling Telegram démarré (Multi-Channel mode)")

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
                params={
                    "timeout": POLL_TIMEOUT,
                    "offset": self._offset,
                    "allowed_updates": '["message"]',
                },
                timeout=POLL_TIMEOUT + 5,
            )
            data = r.json()
            return data.get("result", []) if data.get("ok") else []
        except Exception:
            return []

    def _dispatch(self, upd: dict):
        if "message" in upd:
            self._handle_message(upd["message"])

    # ─── Message Handler ──────────────────────────────────────────────────────

    def _handle_message(self, msg: dict):
        text = msg.get("text", "").strip()
        if not text.startswith("/"):
            return

        cmd = text.split()[0].lower()

        if cmd == "/start":
            self._cmd_start()
        elif cmd == "/health":
            self._cmd_health()
        elif cmd == "/pause":
            self._cmd_pause()
        elif cmd == "/resume":
            self._cmd_resume()
        elif cmd == "/status":
            self._cmd_status()

    # ─── Commands ─────────────────────────────────────────────────────────────

    def _cmd_start(self):
        """Send the Hub message."""
        from nemesis_ui.hub import NemesisHub
        hub = NemesisHub(self.token, self.chat_id)
        bal, pnl = 0.0, 0.0
        if self._get_hub_data:
            try:
                data = self._get_hub_data()
                bal = data.get("balance", 0.0)
                pnl = data.get("pnl_today", 0.0)
            except Exception:
                pass
        hub.send_hub(bal, pnl)

    def _cmd_health(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        status = "⏸ PAUSÉ" if self._paused else "🟢 ACTIF"
        self._reply(
            f"🏥 <b>Health Check</b>\n\n"
            f"Status : {status}\n"
            f"⏰ {now}\n"
            f"📱 Polling : ✅\n"
            f"🔗 Multi-Channel : ✅"
        )

    def _cmd_pause(self):
        self._paused = True
        if self._pause_cb:
            self._pause_cb()
        self._reply("⏸ <b>Trading pausé.</b>\nEnvoie /resume pour reprendre.")

    def _cmd_resume(self):
        self._paused = False
        if self._resume_cb:
            self._resume_cb()
        self._reply("▶️ <b>Trading repris.</b>\nNemesis est de retour en action.")

    def _cmd_status(self):
        status = "⏸ PAUSÉ" if self._paused else "🟢 ACTIF"
        self._reply(
            f"📊 <b>Status Nemesis</b>\n"
            f"État : {status}\n"
            f"Mode : Multi-Channel ✅"
        )

    # ─── Reply Helper ─────────────────────────────────────────────────────────

    def _reply(self, text: str):
        payload = {
            "chat_id":    self.chat_id,
            "text":       text,
            "parse_mode": "HTML",
        }
        try:
            requests.post(f"{self._base}/sendMessage", json=payload, timeout=10)
        except Exception as e:
            logger.error(f"❌ Reply error : {e}")
