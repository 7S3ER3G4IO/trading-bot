"""
router.py — Nemesis Channel Router
Routes notifications to the correct dedicated Telegram channel.
"""
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

    def send_to(self, channel_key: str, text: str, parse_mode: str = "HTML") -> Optional[int]:
        """Send a message to a specific channel. Returns message_id."""
        ch = CHANNELS.get(channel_key)
        if not ch:
            logger.warning(f"⚠️ Canal inconnu : {channel_key}")
            return None
        return self._send(ch["id"], text, parse_mode)

    def send_dashboard(self, text: str) -> Optional[int]:
        return self.send_to("dashboard", text)

    def send_trade(self, text: str) -> Optional[int]:
        return self.send_to("trades", text)

    def send_performance(self, text: str) -> Optional[int]:
        return self.send_to("performance", text)

    def send_briefing(self, text: str) -> Optional[int]:
        return self.send_to("briefing", text)

    def send_risk(self, text: str) -> Optional[int]:
        return self.send_to("risk", text)

    def send_stats(self, text: str) -> Optional[int]:
        return self.send_to("stats", text)

    # ── Internal API ───────────────────────────────────────────────────────

    def _send(self, chat_id: str, text: str, parse_mode: str = "HTML") -> Optional[int]:
        """Send a message via Telegram API. Returns message_id."""
        if not self._api or not _requests:
            return None
        try:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
            r = _requests.post(f"{self._api}/sendMessage", json=payload, timeout=10)
            if r.ok:
                return r.json().get("result", {}).get("message_id")
            else:
                logger.warning(f"⚠️ Router send to {chat_id}: {r.status_code} {r.text[:80]}")
        except Exception as e:
            logger.error(f"❌ Router send: {e}")
        return None
