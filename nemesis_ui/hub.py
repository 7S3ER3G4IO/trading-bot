"""
hub.py — Nemesis Command Center Hub
Manages the single pinned hub message with inline keyboard navigation.
Uses Telegram editMessageText to update in-place (zero spam).
"""
import json
import os
from typing import Optional
from loguru import logger
import requests

try:
    from telegram import InlineKeyboardMarkup
except ImportError:
    InlineKeyboardMarkup = None

from .pages import PageBuilder


class NemesisHub:
    """
    Manages the persistent Hub message in Telegram.
    - send_hub() : sends initial hub message and pins it
    - refresh_hub() : edits the hub message in-place with fresh data
    - navigate_to() : edits the hub message to show a specific page
    - back_to_hub() : returns to the main hub view
    """

    def __init__(self, token: str, chat_id: str):
        self._token = token
        self._chat_id = chat_id
        self._api = f"https://api.telegram.org/bot{token}" if token else ""
        self._hub_message_id: Optional[int] = None
        self._current_page: str = "hub"
        self._load_state()

    # ── Public API ────────────────────────────────────────────────────────────

    def send_hub(self, balance: float = 0.0, pnl_today: float = 0.0) -> Optional[int]:
        """Send the main Hub message and pin it. Returns message_id."""
        text, markup = PageBuilder.build_hub(balance, pnl_today)
        msg_id = self._send_message(text, markup)
        if msg_id:
            self._hub_message_id = msg_id
            self._current_page = "hub"
            self._pin_message(msg_id)
            self._save_state()
            logger.info(f"📌 Hub message envoyé et épinglé (ID: {msg_id})")
        return msg_id

    def refresh_hub(self, balance: float = 0.0, pnl_today: float = 0.0):
        """Edit the hub message in-place with fresh data. Zero spam."""
        if not self._hub_message_id:
            self.send_hub(balance, pnl_today)
            return

        if self._current_page != "hub":
            return  # Don't override a page the user is viewing

        text, markup = PageBuilder.build_hub(balance, pnl_today)
        self._edit_message(self._hub_message_id, text, markup)

    def navigate_to(self, page: str, text: str, markup=None):
        """Navigate to a specific page by editing the hub message."""
        if not self._hub_message_id:
            # Fallback : send as new message
            self._send_message(text, markup)
            return

        self._current_page = page
        self._edit_message(self._hub_message_id, text, markup)

    def back_to_hub(self, balance: float = 0.0, pnl_today: float = 0.0):
        """Return to the main hub view."""
        self._current_page = "hub"
        if self._hub_message_id:
            text, markup = PageBuilder.build_hub(balance, pnl_today)
            self._edit_message(self._hub_message_id, text, markup)

    @property
    def hub_message_id(self) -> Optional[int]:
        return self._hub_message_id

    @property
    def current_page(self) -> str:
        return self._current_page

    # ── Telegram REST API ─────────────────────────────────────────────────────

    def _send_message(self, text: str, markup=None) -> Optional[int]:
        """Send a new message. Returns message_id."""
        if not self._api or not self._chat_id:
            return None
        try:
            payload = {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
            }
            if markup:
                payload["reply_markup"] = self._serialize_markup(markup)
            r = requests.post(f"{self._api}/sendMessage", json=payload, timeout=10)
            if r.ok:
                data = r.json()
                return data.get("result", {}).get("message_id")
            else:
                logger.warning(f"⚠️ Hub send: {r.status_code} {r.text[:80]}")
        except Exception as e:
            logger.error(f"❌ Hub send: {e}")
        return None

    def _edit_message(self, message_id: int, text: str, markup=None):
        """Edit an existing message in-place."""
        if not self._api or not self._chat_id:
            return
        try:
            payload = {
                "chat_id": self._chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
            }
            if markup:
                payload["reply_markup"] = self._serialize_markup(markup)
            r = requests.post(f"{self._api}/editMessageText", json=payload, timeout=10)
            if not r.ok:
                error_desc = r.json().get("description", "")
                # "message is not modified" is normal when nothing changed
                if "not modified" not in error_desc:
                    logger.warning(f"⚠️ Hub edit: {r.status_code} {error_desc[:80]}")
        except Exception as e:
            logger.error(f"❌ Hub edit: {e}")

    def _pin_message(self, message_id: int):
        """Pin a message in the chat."""
        if not self._api or not self._chat_id:
            return
        try:
            requests.post(
                f"{self._api}/pinChatMessage",
                json={
                    "chat_id": self._chat_id,
                    "message_id": message_id,
                    "disable_notification": True,
                },
                timeout=10,
            )
        except Exception as e:
            logger.debug(f"Hub pin: {e}")

    @staticmethod
    def _serialize_markup(markup) -> str:
        """Serialize markup to JSON string for the API."""
        if markup is None:
            return ""
        try:
            return json.dumps(markup.to_dict())
        except AttributeError:
            try:
                return markup.to_json()
            except AttributeError:
                return ""

    # ── State Persistence ─────────────────────────────────────────────────────

    _STATE_FILE = "logs/hub_state.json"

    def _save_state(self):
        try:
            os.makedirs("logs", exist_ok=True)
            with open(self._STATE_FILE, "w") as f:
                json.dump({"hub_message_id": self._hub_message_id}, f)
        except Exception:
            pass

    def _load_state(self):
        try:
            if os.path.exists(self._STATE_FILE):
                with open(self._STATE_FILE) as f:
                    data = json.load(f)
                self._hub_message_id = data.get("hub_message_id")
                if self._hub_message_id:
                    logger.debug(f"📌 Hub state restauré : message_id={self._hub_message_id}")
        except Exception:
            pass
