"""
market_context.py — Contexte de marché en temps réel.
  - Fear & Greed Index (alternative.me, gratuit)
  - Tendance Daily BTC (EMA 200 journalier)
  - Morning Brief automatique (8h CET)
  - Market Regime Detection (risk-on / risk-off)
  - Session Overlap Detection (London/NY)
  - Correlation Tracking
"""
import os
from typing import Optional, Dict, List
from urllib.request import urlopen, Request
from urllib.error import URLError
import json
from datetime import datetime, timezone, timedelta
from loguru import logger


# Session windows (UTC hours)
SESSION_LONDON = (7, 16)   # 07:00 - 16:00 UTC
SESSION_NY     = (12, 21)  # 12:00 - 21:00 UTC
SESSION_OVERLAP = (12, 16) # 12:00 - 16:00 UTC (highest volume)
SESSION_ASIA   = (0, 8)    # 00:00 - 08:00 UTC


class MarketContext:
    """Récupère et formate le contexte macro du marché."""

    def __init__(self):
        self._fg_value:   Optional[int] = None
        self._fg_label:   str = "N/A"
        self._fg_ts:      Optional[datetime] = None
        self._daily_trend:str = "N/A"
        self._morning_brief_sent_today = False
        self._last_fg_fetch: Optional[datetime] = None

        # Market Regime
        self._regime: str = "NEUTRAL"  # RISK_ON, RISK_OFF, NEUTRAL
        self._regime_score: float = 0.0  # -1.0 (extreme fear) to +1.0 (extreme greed)

        # Correlation tracking
        self._price_changes: Dict[str, List[float]] = {}
        self._correlation_cache: Dict[str, float] = {}

    # ─── Fear & Greed ────────────────────────────────────────────────────────

    _fg_fail_count: int = 0   # compteur erreurs (class-level default)

    def refresh_fear_greed(self):
        """Fetch Fear & Greed depuis alternative.me via proxy WARP si disponible."""
        now = datetime.now(timezone.utc)
        # Throttle : 1h après succès, 10min après échec (évite spam à chaque tick)
        min_interval = 600 if MarketContext._fg_fail_count > 0 else 3600
        if self._last_fg_fetch and (now - self._last_fg_fetch).total_seconds() < min_interval:
            return

        proxy = os.getenv("HTTPS_PROXY", "") or os.getenv("HTTP_PROXY", "")
        try:
            if proxy:
                import requests as _req
                r = _req.get(
                    "https://api.alternative.me/fng/?limit=1",
                    proxies={"https": proxy, "http": proxy},
                    timeout=10,
                    headers={"User-Agent": "Nemesis/1.0"},
                )
                data = r.json()["data"][0]
            else:
                req = Request(
                    "https://api.alternative.me/fng/?limit=1",
                    headers={"User-Agent": "Nemesis/1.0"}
                )
                with urlopen(req, timeout=10) as r:
                    data = json.loads(r.read())["data"][0]

            self._fg_value = int(data["value"])
            self._fg_label = data["value_classification"]
            self._last_fg_fetch = now
            MarketContext._fg_fail_count = 0   # reset compteur
            logger.info(f"🧠 Fear & Greed : {self._fg_value}/100 ({self._fg_label})")
            self._update_regime()
        except Exception as e:
            MarketContext._fg_fail_count += 1
            self._last_fg_fetch = now  # throttle même en cas d'erreur
            # Log seulement au 1er échec puis toutes les 20 tentatives (≈ 200min)
            if MarketContext._fg_fail_count == 1 or MarketContext._fg_fail_count % 20 == 0:
                logger.debug(
                    f"Fear & Greed inaccessible ({MarketContext._fg_fail_count}x, non critique): {e}"
                )


    def get_context_line(self) -> str:
        """Ligne de contexte à inclure dans les alertes d'entrée."""
        self.refresh_fear_greed()
        fg = f"🧠 Fear & Greed : <code>{self._fg_value}/100</code> <i>{self._fg_label}</i>" \
             if self._fg_value is not None else ""
        trend = f"📈 Tendance Daily : <code>{self._daily_trend}</code>" \
                if self._daily_trend != "N/A" else ""
        regime = f"🌍 Régime : <code>{self._regime}</code>"
        parts = [p for p in [fg, trend, regime] if p]
        return "\n".join(parts) if parts else ""

    def get_fg_emoji(self) -> str:
        if self._fg_value is None: return "❓"
        if self._fg_value < 25: return "😱"
        if self._fg_value < 45: return "😨"
        if self._fg_value < 55: return "😐"
        if self._fg_value < 75: return "😏"
        return "🤑"

    def update_daily_trend(self, current_price: float, ema_daily: float):
        """Met à jour la tendance Daily selon EMA."""
        if current_price > ema_daily * 1.01:
            self._daily_trend = "🟢 Haussière"
        elif current_price < ema_daily * 0.99:
            self._daily_trend = "🔴 Baissière"
        else:
            self._daily_trend = "⚪ Neutre"

    # ─── Market Regime Detection ─────────────────────────────────────────────

    def _update_regime(self):
        """
        Détecte le régime de marché basé sur Fear & Greed.
        RISK_ON  = FG > 60 → pleine capacité
        RISK_OFF = FG < 30 → mode défensif
        NEUTRAL  = 30-60
        """
        if self._fg_value is None:
            self._regime = "NEUTRAL"
            self._regime_score = 0.0
            return
        self._regime_score = (self._fg_value - 50) / 50
        if self._fg_value >= 60:
            self._regime = "RISK_ON"
        elif self._fg_value <= 30:
            self._regime = "RISK_OFF"
        else:
            self._regime = "NEUTRAL"
        logger.debug(f"🌍 Regime: {self._regime} (score={self._regime_score:+.2f})")

    @property
    def regime(self) -> str:
        return self._regime

    @property
    def regime_score(self) -> float:
        return self._regime_score

    def get_regime_multiplier(self) -> float:
        """
        Position size multiplier based on market regime.
        RISK_ON:  ×1.2 | RISK_OFF: ×0.7 | NEUTRAL: ×1.0
        """
        if self._regime == "RISK_ON": return 1.2
        if self._regime == "RISK_OFF": return 0.7
        return 1.0

    # ─── Session Overlap Detection ───────────────────────────────────────────

    @staticmethod
    def get_active_sessions() -> List[str]:
        """Returns list of currently active trading sessions."""
        h = datetime.now(timezone.utc).hour
        sessions = []
        if SESSION_LONDON[0] <= h < SESSION_LONDON[1]: sessions.append("London")
        if SESSION_NY[0] <= h < SESSION_NY[1]: sessions.append("NY")
        if SESSION_ASIA[0] <= h < SESSION_ASIA[1]: sessions.append("Asia")
        return sessions

    @staticmethod
    def is_overlap() -> bool:
        """True during London/NY overlap (12:00-16:00 UTC)."""
        h = datetime.now(timezone.utc).hour
        return SESSION_OVERLAP[0] <= h < SESSION_OVERLAP[1]

    @staticmethod
    def session_quality() -> str:
        """Returns current session quality."""
        h = datetime.now(timezone.utc).hour
        if SESSION_OVERLAP[0] <= h < SESSION_OVERLAP[1]: return "🔥 OVERLAP (volume max)"
        if SESSION_LONDON[0] <= h < SESSION_LONDON[1]: return "🟢 LONDON"
        if SESSION_NY[0] <= h < SESSION_NY[1]: return "🟢 NEW YORK"
        if SESSION_ASIA[0] <= h < SESSION_ASIA[1]: return "🟡 ASIA"
        return "⚪ HORS SESSION"

    # ─── Correlation Tracking ────────────────────────────────────────────────

    def record_price_change(self, epic: str, pct_change: float):
        """Record a price change for correlation tracking."""
        if epic not in self._price_changes:
            self._price_changes[epic] = []
        self._price_changes[epic].append(pct_change)
        if len(self._price_changes[epic]) > 50:
            self._price_changes[epic] = self._price_changes[epic][-50:]

    def get_correlation(self, epic_a: str, epic_b: str) -> float:
        """Returns Pearson correlation between two instruments."""
        a = self._price_changes.get(epic_a, [])
        b = self._price_changes.get(epic_b, [])
        if len(a) < 10 or len(b) < 10:
            return 0.0
        n = min(len(a), len(b))
        a, b = a[-n:], b[-n:]
        mean_a, mean_b = sum(a)/n, sum(b)/n
        cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n)) / n
        std_a = (sum((x - mean_a)**2 for x in a) / n) ** 0.5
        std_b = (sum((x - mean_b)**2 for x in b) / n) ** 0.5
        if std_a < 1e-10 or std_b < 1e-10:
            return 0.0
        return cov / (std_a * std_b)

    # ─── Morning Brief ───────────────────────────────────────────────────────

    def should_send_brief(self) -> bool:
        cet = datetime.now(timezone(timedelta(hours=1)))
        if cet.hour == 8 and cet.minute < 2 and not self._morning_brief_sent_today:
            return True
        if cet.hour == 0:
            self._morning_brief_sent_today = False
        return False

    def build_morning_brief(self, balance: float, next_news: Optional[str] = None) -> str:
        self.refresh_fear_greed()
        cet = datetime.now(timezone(timedelta(hours=1)))
        date_str = cet.strftime("%A %d/%m")
        fg_line = f"{self.get_fg_emoji()} Fear &amp; Greed : <code>{self._fg_value}/100</code> <i>{self._fg_label}</i>" \
                  if self._fg_value else "🧠 Fear &amp; Greed : N/A"
        news_line = f"📅 Prochaine news HIGH : <code>{next_news}</code>" if next_news else "✅ Pas d'annonce majeure prévue"
        regime_line = f"🌍 Régime : <b>{self._regime}</b> (×{self.get_regime_multiplier():.1f})"
        session_line = f"📡 Session : {self.session_quality()}"
        return (
            f"☀️ <b>Nemesis — Morning Brief</b>\n"
            f"<b>{date_str.capitalize()}</b>\n"
            f"{fg_line}\n"
            f"📊 Tendance Daily : <code>{self._daily_trend}</code>\n"
            f"{regime_line}\n"
            f"{session_line}\n"
            f"💰 Capital : <code>{balance:,.2f} €</code>\n"
            f"{news_line}\n"
            f"🟢 <b>Bot actif — Bonne journée !</b>"
        )

    def mark_brief_sent(self):
        self._morning_brief_sent_today = True

    @property
    def stats(self) -> dict:
        return {
            "regime": self._regime,
            "regime_score": round(self._regime_score, 2),
            "fg_value": self._fg_value,
            "session": self.session_quality(),
            "overlap": self.is_overlap(),
            "tracked_instruments": len(self._price_changes),
        }
