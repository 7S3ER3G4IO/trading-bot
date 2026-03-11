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
        self._stats_cb     = kwargs.get("stats")
        self._perf_cb      = kwargs.get("performance")
        self._health_cb    = kwargs.get("health")
        self._achievements_cb = kwargs.get("achievements")

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
        elif cmd == "/stats":
            self._cmd_sys_stats()
        elif cmd == "/performance" or cmd == "/perf":
            self._cmd_perf()
        elif cmd == "/achievements":
            self._cmd_achievements()

    # ─── Commands ─────────────────────────────────────────────────────────────

    def _cmd_start(self):
        """Send the Hub message."""
        from nemesis_ui.hub import NemesisHub
        hub = NemesisHub(self.token, self.chat_id)
        bal, pnl = 0.0, 0.0
        if self._get_hub_data:
            try:
                result = self._get_hub_data()
                # _hub_data returns (balance, pnl_today) tuple
                if isinstance(result, (tuple, list)) and len(result) >= 2:
                    bal, pnl = result[0], result[1]
                elif isinstance(result, dict):
                    bal = result.get("balance", 0.0)
                    pnl = result.get("pnl_today", 0.0)
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

    def _cmd_sys_stats(self):
        """Handle /stats command."""
        if self._stats_cb:
            try:
                result = self._stats_cb()
                self._reply(result)
            except Exception as e:
                self._reply(f"❌ Stats error: {e}")
        else:
            self._reply("⚠️ Stats not available")

    def _cmd_perf(self):
        """Handle /performance command."""
        if self._perf_cb:
            try:
                result = self._perf_cb()
                self._reply(result)
            except Exception as e:
                self._reply(f"❌ Performance error: {e}")
        else:
            self._reply("⚠️ Performance not available")

    def _cmd_achievements(self):
        """Handle /achievements command."""
        if self._achievements_cb:
            try:
                result = self._achievements_cb()
                self._reply(result)
            except Exception as e:
                self._reply(f"❌ Achievements error: {e}")
        else:
            self._reply("⚠️ Achievements not available")

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
