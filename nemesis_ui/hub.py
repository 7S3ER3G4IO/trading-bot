"""
hub.py — Nemesis Command Center Hub (Multi-Channel Edition)
Sends the main Hub message with URL buttons linking to dedicated channels.
No callbacks needed — each button is a direct link to the channel.
"""
import json
import os
from typing import Optional
from loguru import logger

try:
    import requests
except ImportError:
    requests = None

from config import CHANNELS


class NemesisHub:
    """
    Manages the Hub message in the main bot chat.
    Buttons are URL buttons pointing to dedicated channels (no callbacks).
    """

    def __init__(self, token: str, chat_id: str):
        self._token = token
        self._chat_id = chat_id
        self._api = f"https://api.telegram.org/bot{token}" if token else ""
        self._hub_message_id: Optional[int] = None
        self._load_state()

    # ── Public API ────────────────────────────────────────────────────────────

    def send_hub(self, balance: float = 0.0, pnl_today: float = 0.0,
                 open_positions: int = 0, equity_data: list = None,
                 confidence: tuple = None) -> Optional[int]:
        """Send the main Hub message with URL buttons and pin it."""
        text = self._build_hub_text(balance, pnl_today, open_positions, equity_data, confidence)
        markup = self._build_url_keyboard()
        msg_id = self._send_message(text, markup)
        if msg_id:
            self._hub_message_id = msg_id
            self._pin_message(msg_id)
            self._save_state()
            logger.info(f"📌 Hub envoyé et épinglé (ID: {msg_id})")
        return msg_id

    def refresh_hub(self, balance: float = 0.0, pnl_today: float = 0.0,
                    open_positions: int = 0, equity_data: list = None,
                    confidence: tuple = None):
        """Edit the Hub message in-place with fresh data."""
        if not self._hub_message_id:
            self.send_hub(balance, pnl_today, open_positions, equity_data, confidence)
            return
        text = self._build_hub_text(balance, pnl_today, open_positions, equity_data, confidence)
        markup = self._build_url_keyboard()
        self._edit_message(self._hub_message_id, text, markup)

    @property
    def hub_message_id(self) -> Optional[int]:
        return self._hub_message_id

    # ── Hub Content ───────────────────────────────────────────────────────────

    @staticmethod
    def _build_hub_text(balance: float = 0.0, pnl_today: float = 0.0,
                        open_positions: int = 0, equity_data: list = None,
                        confidence: tuple = None) -> str:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        time_str = now.strftime("%H:%M UTC")
        pnl_sign = "+" if pnl_today >= 0 else ""
        pnl_emoji = "📈" if pnl_today >= 0 else "📉"

        # Equity sparkline
        sparkline = ""
        if equity_data and len(equity_data) >= 3:
            chars = "▁▂▃▄▅▆▇█"
            mn, mx = min(equity_data), max(equity_data)
            rng = mx - mn if mx > mn else 1
            sparkline = "".join(chars[min(int((v - mn) / rng * 7), 7)] for v in equity_data[-12:])
            sparkline = f"\n📊 {sparkline}"

        # Next session
        h = now.hour
        if h < 8:
            next_sess = "🇬🇧 London 08h UTC"
        elif h < 13:
            next_sess = "🗽 NY Open 13h UTC"
        elif h < 22:
            next_sess = "🌙 Clôture 22h UTC"
        else:
            next_sess = "🇬🇧 London 08h UTC"

        # Positions
        pos_line = f"📋 {open_positions} position{'s' if open_positions != 1 else ''} ouverte{'s' if open_positions != 1 else ''}" if open_positions > 0 else "📋 Aucune position"

        # Confidence score
        conf_line = ""
        if confidence:
            conf_score, conf_emoji, conf_label = confidence
            conf_line = f"\n🎢 Confiance : {conf_emoji} <b>{conf_score}%</b> ({conf_label})"

        return (
            "┌─────────────────────────────┐\n"
            "│  ⚡ NEMESIS COMMAND CENTER   │\n"
            "└─────────────────────────────┘\n"
            "\n"
            f"🟢 ONLINE  ·  {time_str}\n"
            "\n"
            f"💰 {balance:,.2f}€  ·  {pnl_emoji} {pnl_sign}{pnl_today:,.2f}€\n"
            f"{pos_line}\n"
            f"⏭ {next_sess}"
            f"{conf_line}"
            f"{sparkline}\n"
            "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "👇 Accède à tes canaux dédiés :\n"
        )

    @staticmethod
    def _build_url_keyboard() -> dict:
        """Build InlineKeyboardMarkup with URL buttons to each channel."""
        buttons = []
        # Row 1: Dashboard + Trades
        buttons.append([
            {"text": CHANNELS["dashboard"]["name"], "url": CHANNELS["dashboard"]["url"]},
            {"text": CHANNELS["trades"]["name"],    "url": CHANNELS["trades"]["url"]},
        ])
        # Row 2: Performance + Briefing
        buttons.append([
            {"text": CHANNELS["performance"]["name"], "url": CHANNELS["performance"]["url"]},
            {"text": CHANNELS["briefing"]["name"],     "url": CHANNELS["briefing"]["url"]},
        ])
        # Row 3: Risk + Stats
        buttons.append([
            {"text": CHANNELS["risk"]["name"],  "url": CHANNELS["risk"]["url"]},
            {"text": CHANNELS["stats"]["name"], "url": CHANNELS["stats"]["url"]},
        ])
        return {"inline_keyboard": buttons}

    # ── Telegram REST API ─────────────────────────────────────────────────────

    def _send_message(self, text: str, markup: dict = None) -> Optional[int]:
        if not self._api or not requests:
            return None
        try:
            payload = {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
            }
            if markup:
                payload["reply_markup"] = markup
            r = requests.post(f"{self._api}/sendMessage", json=payload, timeout=10)
            if r.ok:
                return r.json().get("result", {}).get("message_id")
            else:
                logger.warning(f"⚠️ Hub send: {r.status_code} {r.text[:80]}")
        except Exception as e:
            logger.error(f"❌ Hub send: {e}")
        return None

    def _edit_message(self, message_id: int, text: str, markup: dict = None):
        if not self._api or not requests:
            return
        try:
            payload = {
                "chat_id": self._chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
            }
            if markup:
                payload["reply_markup"] = markup
            r = requests.post(f"{self._api}/editMessageText", json=payload, timeout=10)
            if not r.ok:
                desc = r.json().get("description", "")
                if "not modified" not in desc:
                    logger.warning(f"⚠️ Hub edit: {r.status_code} {desc[:80]}")
        except Exception as e:
            logger.error(f"❌ Hub edit: {e}")

    def _pin_message(self, message_id: int):
        if not self._api or not requests:
            return
        try:
            requests.post(
                f"{self._api}/pinChatMessage",
                json={"chat_id": self._chat_id, "message_id": message_id, "disable_notification": True},
                timeout=10,
            )
        except Exception as e:
            logger.debug(f"Hub pin: {e}")

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
