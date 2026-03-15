"""
watchdog.py — ⚡ Phase 1.1: Dead-Man Switch + Watchdog

Monitors the bot's heartbeat. If no scan completes for DEAD_MAN_TIMEOUT seconds,
triggers a critical Telegram alert + optional restart.

Also provides WebSocket watchdog: if WS heartbeat misses 3 cycles, falls back to REST.

Usage:
    watchdog = DeadManSwitch(telegram_router)
    watchdog.ping()  # Call at end of each _tick()
    watchdog.start() # Start background monitor thread
"""

import threading
import time
from datetime import datetime, timezone
from loguru import logger


DEAD_MAN_TIMEOUT = 300  # 5 minutes without a tick → alert
WS_MISS_LIMIT = 3       # 3 missed WS heartbeats → fallback
CHECK_INTERVAL = 30      # Check every 30s


class DeadManSwitch:
    """Monitors bot liveness. Alerts if no tick for DEAD_MAN_TIMEOUT seconds."""

    def __init__(self, telegram_router=None, mt5_checker=None):
        self._last_ping: float = time.monotonic()
        self._last_ping_utc: datetime = datetime.now(timezone.utc)
        self._router = telegram_router
        self._mt5    = mt5_checker   # callable ou objet avec .available
        self._alerted = False
        self._thread: threading.Thread | None = None
        self._running = False
        self._tick_count = 0

        # WS watchdog
        self._ws_last_msg: float = time.monotonic()
        self._ws_miss_count = 0
        self._ws_fallback_active = False

    # ─── Heartbeat ────────────────────────────────────────────────────────────

    def ping(self):
        """Call at end of each successful _tick()."""
        self._last_ping = time.monotonic()
        self._last_ping_utc = datetime.now(timezone.utc)
        self._tick_count += 1
        if self._alerted:
            self._alerted = False
            logger.info("💚 Dead-Man Switch: bot revenu à la vie")
            self._send_alert(
                "💚 <b>BOT RECOVERED</b>\n\n"
                f"⏰ Heartbeat restauré à {self._last_ping_utc.strftime('%H:%M:%S')} UTC\n"
                f"📊 Tick #{self._tick_count}"
            )

    def ws_ping(self):
        """Call on each WebSocket message received."""
        self._ws_last_msg = time.monotonic()
        self._ws_miss_count = 0
        if self._ws_fallback_active:
            self._ws_fallback_active = False
            logger.info("📡 WebSocket reconnecté — fallback REST désactivé")

    # ─── Background Monitor ───────────────────────────────────────────────────

    def start(self):
        """Start background watchdog thread."""
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="dead_man_switch"
        )
        self._thread.start()
        logger.info(f"🐕 Dead-Man Switch started (timeout={DEAD_MAN_TIMEOUT}s)")

    def stop(self):
        self._running = False

    def _monitor_loop(self):
        while self._running:
            try:
                elapsed = time.monotonic() - self._last_ping

                # Dead-man check
                if elapsed > DEAD_MAN_TIMEOUT and not self._alerted:
                    self._alerted = True
                    mins = int(elapsed / 60)
                    logger.critical(
                        f"💀 DEAD-MAN SWITCH: aucun tick depuis {mins}min!"
                    )
                    self._send_alert(
                        "🚨 <b>DEAD-MAN SWITCH ACTIVÉ</b>\n\n"
                        f"💀 Aucun scan depuis <b>{mins} minutes</b>\n"
                        f"⏰ Dernier heartbeat : {self._last_ping_utc.strftime('%H:%M:%S')} UTC\n\n"
                        "⚠️ Le bot est peut-être crashé ou bloqué.\n"
                        "🔄 Vérifiez Docker : <code>docker logs nemesis_bot --tail 50</code>"
                    )

                # WS watchdog
                ws_elapsed = time.monotonic() - self._ws_last_msg
                if ws_elapsed > CHECK_INTERVAL * WS_MISS_LIMIT:
                    if not self._ws_fallback_active:
                        self._ws_miss_count += 1
                        if self._ws_miss_count >= WS_MISS_LIMIT:
                            self._ws_fallback_active = True
                            # Ne pas logger si MT5 est actif (WS Capital.com volontairement silencieux)
                            mt5_active = bool(self._mt5 and getattr(self._mt5, 'available', False))
                            if not mt5_active:
                                logger.warning(
                                    "📡 WebSocket silencieux > 90s — fallback REST activé"
                                )

            except Exception as e:
                logger.debug(f"Watchdog error: {e}")

            time.sleep(CHECK_INTERVAL)

    @property
    def ws_fallback(self) -> bool:
        """True if WebSocket is down and REST fallback should be used."""
        return self._ws_fallback_active

    @property
    def seconds_since_last_tick(self) -> float:
        return time.monotonic() - self._last_ping

    # ─── Telegram ─────────────────────────────────────────────────────────────

    def _send_alert(self, text: str):
        if self._router:
            try:
                self._router.send_to("risk", text)
            except Exception as e:
                logger.error(f"Watchdog Telegram: {e}")
