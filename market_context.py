"""
market_context.py — Contexte de marché en temps réel.
  - Fear & Greed Index (alternative.me, gratuit)
  - Tendance Daily BTC (EMA 200 journalier)
  - Morning Brief automatique (8h CET)
"""
import os
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError
import json
from datetime import datetime, timezone, timedelta
from loguru import logger


class MarketContext:
    """Récupère et formate le contexte macro du marché."""

    def __init__(self):
        self._fg_value:   Optional[int] = None
        self._fg_label:   str = "N/A"
        self._fg_ts:      Optional[datetime] = None
        self._daily_trend:str = "N/A"
        self._morning_brief_sent_today = False
        self._last_fg_fetch: Optional[datetime] = None

    # ─── Fear & Greed ────────────────────────────────────────────────────────

    def refresh_fear_greed(self):
        """Fetch Fear & Greed depuis alternative.me (gratuit, pas de clé)."""
        now = datetime.now(timezone.utc)
        # Rafraîchir toutes les heures max
        if self._last_fg_fetch and (now - self._last_fg_fetch).total_seconds() < 3600:
            return

        try:
            req = Request(
                "https://api.alternative.me/fng/?limit=1",
                headers={"User-Agent": "AlphaTrader/1.0"}
            )
            with urlopen(req, timeout=10) as r:
                data = json.loads(r.read())["data"][0]
            self._fg_value = int(data["value"])
            self._fg_label = data["value_classification"]
            self._last_fg_fetch = now
            logger.info(f"🧠 Fear & Greed : {self._fg_value}/100 ({self._fg_label})")
        except Exception as e:
            logger.warning(f"⚠️  Fear & Greed inaccessible : {e}")

    def get_context_line(self) -> str:
        """Ligne de contexte à inclure dans les alertes d'entrée."""
        self.refresh_fear_greed()
        fg = f"🧠 Fear & Greed : `{self._fg_value}/100` _{self._fg_label}_" \
             if self._fg_value is not None else ""
        trend = f"📈 Tendance Daily : `{self._daily_trend}`" \
                if self._daily_trend != "N/A" else ""
        parts = [p for p in [fg, trend] if p]
        return "\n".join(parts) if parts else ""

    def get_fg_emoji(self) -> str:
        """Emoji selon le level de peur/avidité."""
        if self._fg_value is None:
            return "❓"
        if self._fg_value < 25:
            return "😱"  # Extreme Fear
        if self._fg_value < 45:
            return "😨"  # Fear
        if self._fg_value < 55:
            return "😐"  # Neutral
        if self._fg_value < 75:
            return "😏"  # Greed
        return "🤑"  # Extreme Greed

    def update_daily_trend(self, current_price: float, ema_daily: float):
        """Met à jour la tendance Daily selon EMA."""
        if current_price > ema_daily * 1.01:
            self._daily_trend = "🟢 Haussière"
        elif current_price < ema_daily * 0.99:
            self._daily_trend = "🔴 Baissière"
        else:
            self._daily_trend = "⚪ Neutre"

    # ─── Morning Brief ───────────────────────────────────────────────────────

    def should_send_brief(self) -> bool:
        """Retourne True à 8h CET si pas encore envoyé."""
        cet = datetime.now(timezone(timedelta(hours=1)))
        if cet.hour == 8 and cet.minute < 2 and not self._morning_brief_sent_today:
            return True
        # Reset à minuit
        if cet.hour == 0:
            self._morning_brief_sent_today = False
        return False

    def build_morning_brief(self, balance: float, next_news: Optional[str] = None) -> str:
        """Construit le message de brief matinal."""
        self.refresh_fear_greed()
        cet = datetime.now(timezone(timedelta(hours=1)))
        date_str = cet.strftime("%A %d/%m")

        fg_line = f"{self.get_fg_emoji()} Fear & Greed : `{self._fg_value}/100` _{self._fg_label}_" \
                  if self._fg_value else "🧠 Fear & Greed : N/A"
        news_line = f"📅 Prochaine news HIGH : `{next_news}`" if next_news else "✅ Pas d'annonce majeure prévue"

        return (
            f"☀️ *AlphaTrader — Morning Brief*\n"
            f"*{date_str.capitalize()}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{fg_line}\n"
            f"📊 Tendance Daily : `{self._daily_trend}`\n"
            f"💰 Capital : `{balance:,.2f} USDT`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{news_line}\n"
            f"🟢 *Bot actif — Bonne journée !*"
        )

    def mark_brief_sent(self):
        self._morning_brief_sent_today = True
