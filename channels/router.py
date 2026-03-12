"""
router.py — Nemesis Channel Router v2.0
Routes notifications to the correct dedicated Telegram channel.
Supports silent mode, message pinning, and photo sending.
"""
import io
import json
import os
from typing import Optional
from loguru import logger

try:
    import requests as _requests
except ImportError:
    _requests = None

from config import CHANNELS


class ChannelRouter:
    """
    Routes messages to dedicated Nemesis channels.
    Each notification type goes to its assigned channel.
    """

    def __init__(self, token: str):
        self._token = token
        self._api = f"https://api.telegram.org/bot{token}" if token else ""

    # ── Public send methods ────────────────────────────────────────────────

    def send_to(self, channel_key: str, text: str, parse_mode: str = "HTML",
                silent: bool = False, pin: bool = False,
                reply_to: int = None) -> Optional[int]:
        """Send a message to a specific channel. Returns message_id."""
        ch = CHANNELS.get(channel_key)
        if not ch:
            logger.warning(f"⚠️ Canal inconnu : {channel_key}")
            return None
        msg_id = self._send(ch["id"], text, parse_mode, silent=silent, reply_to=reply_to)
        if pin and msg_id:
            self._pin(ch["id"], msg_id)
        return msg_id

    def send_photo_to(self, channel_key: str, image_bytes: bytes,
                      caption: str, silent: bool = False) -> Optional[int]:
        """Send a photo to a specific channel."""
        ch = CHANNELS.get(channel_key)
        if not ch or not self._api or not _requests or not image_bytes:
            return None
        try:
            files = {"photo": ("chart.png", io.BytesIO(image_bytes), "image/png")}
            data = {
                "chat_id": ch["id"],
                "caption": caption,
                "parse_mode": "HTML",
                "disable_notification": str(silent).lower(),
            }
            r = _requests.post(f"{self._api}/sendPhoto", data=data, files=files, timeout=30)
            if r.ok:
                return r.json().get("result", {}).get("message_id")
            else:
                logger.warning(f"⚠️ Router photo to {channel_key}: {r.status_code} {r.text[:80]}")
        except Exception as e:
            logger.error(f"❌ Router send_photo: {e}")
        return None

    # ── Typed send methods (with smart silence) ────────────────────────────

    def send_dashboard(self, text: str, silent: bool = True, pin: bool = False) -> Optional[int]:
        """Dashboard: silent by default (heartbeat, sessions)."""
        return self.send_to("dashboard", text, silent=silent, pin=pin)

    def send_trade(self, text: str, silent: bool = False,
                   reply_to: int = None) -> Optional[int]:
        """Trades: NOT silent (important alerts)."""
        return self.send_to("trades", text, silent=silent, reply_to=reply_to)

    def send_performance(self, text: str, silent: bool = True) -> Optional[int]:
        """Performance: silent by default (reports, recaps)."""
        return self.send_to("performance", text, silent=silent)

    def send_briefing(self, text: str, silent: bool = False) -> Optional[int]:
        """Briefing: NOT silent (morning alert)."""
        return self.send_to("briefing", text, silent=silent)

    def send_risk(self, text: str, silent: bool = False) -> Optional[int]:
        """Risk: NOT silent (critical alerts)."""
        return self.send_to("risk", text, silent=silent)

    def send_stats(self, text: str, silent: bool = True) -> Optional[int]:
        """Stats: silent by default (gamification)."""
        return self.send_to("stats", text, silent=silent)

    # ── Internal API ───────────────────────────────────────────────────────

    _send_count = 0
    _send_count_reset = 0.0
    _muted_channels: set = set()   # channels that returned 401 — warn once then silence

    def _send(self, chat_id: str, text: str, parse_mode: str = "HTML",
              silent: bool = False, reply_to: int = None) -> Optional[int]:
        """Send a message via Telegram API. Returns message_id. Retries on failure."""
        if not self._api or not _requests:
            return None

        # Skip channels that already returned 401 (bot not admin)
        if chat_id in ChannelRouter._muted_channels:
            return None

        import time as _time

        # Rate-limit tracking (warn at >50 msg/min)
        now = _time.time()
        if now - ChannelRouter._send_count_reset > 60:
            ChannelRouter._send_count = 0
            ChannelRouter._send_count_reset = now
        ChannelRouter._send_count += 1
        if ChannelRouter._send_count > 50:
            logger.warning(f"⚠️ Router rate: {ChannelRouter._send_count} msg/min")

        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": silent,
        }
        if reply_to:
            payload["reply_to_message_id"] = reply_to

        # Retry with exponential backoff (3 attempts)
        for attempt in range(3):
            try:
                r = _requests.post(f"{self._api}/sendMessage", json=payload, timeout=10)
                if r.ok:
                    return r.json().get("result", {}).get("message_id")
                elif r.status_code == 401:
                    # Bot not admin of this channel — mute and warn once
                    ChannelRouter._muted_channels.add(chat_id)
                    logger.warning(f"⚠️ Canal {chat_id} → 401 Unauthorized (bot pas admin) — muted")
                    return None
                elif r.status_code == 429:
                    # Rate limited by Telegram — wait and retry
                    retry_after = r.json().get("parameters", {}).get("retry_after", 5)
                    logger.warning(f"⚠️ Telegram 429 — retry in {retry_after}s")
                    _time.sleep(retry_after)
                    continue
                else:
                    logger.warning(f"⚠️ Router send to {chat_id}: {r.status_code} {r.text[:80]}")
                    return None
            except Exception as e:
                if attempt < 2:
                    _time.sleep(1 * (2 ** attempt))  # 1s, 2s
                    continue
                logger.error(f"❌ Router send (attempt {attempt+1}): {e}")
        return None


    def _pin(self, chat_id: str, message_id: int):
        """Pin a message in a channel."""
        if not self._api or not _requests:
            return
        try:
            _requests.post(
                f"{self._api}/pinChatMessage",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "disable_notification": True,
                },
                timeout=10,
            )
        except Exception as e:
            logger.debug(f"Pin: {e}")
