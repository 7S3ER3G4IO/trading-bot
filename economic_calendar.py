"""
economic_calendar.py — Récupère les annonces économiques HIGH impact
depuis plusieurs flux RSS avec fallback automatique.
Pause trading 30min avant et 30min après chaque annonce critique.
"""

import os
import xml.etree.ElementTree as ET
from typing import Optional, Tuple
from datetime import datetime, timezone, timedelta
from loguru import logger

# Flux RSS — cascade de sources (essaie dans l'ordre)
CALENDAR_SOURCES = [
    {
        "url": "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
        "name": "ForexFactory",
        "date_fmt": "%m-%d-%Y %I:%M%p",
        "impact_tag": "impact",
        "country_tag": "country",
    },
    {
        "url": "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.xml",
        "name": "ForexFactory CDN",
        "date_fmt": "%m-%d-%Y %I:%M%p",
        "impact_tag": "impact",
        "country_tag": "country",
    },
    {
        "url": "https://www.forexfactory.com/recently.rss?type=economic",
        "name": "ForexFactory Recent",
        "date_fmt": "%m-%d-%Y %I:%M%p",
        "impact_tag": "impact",
        "country_tag": "country",
    },
]

# User-Agents pour rotation anti-429
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0",
]
_ua_idx: int = 0

# Fenêtre de pause autour des news (minutes)
PAUSE_BEFORE_MIN = 30
PAUSE_AFTER_MIN  = 30

# Mots-clés des news très importantes BTC/crypto/USD
HIGH_IMPACT_KEYWORDS = [
    "CPI", "NFP", "FOMC", "Fed", "Interest Rate", "Inflation",
    "GDP", "Non-Farm", "Unemployment", "ECB", "BOE", "PPI",
    "Retail Sales", "ISM", "PMI",
]


class EconomicCalendar:
    """Vérifie si le marché est actuellement au repos à cause d'une news."""

    def __init__(self):
        self._events = []
        self._last_fetch = None
        self._fetch_interval_hours = 6  # Rafraîchir toutes les 6h
        self._fail_count: int = 0

    def refresh(self):
        """Télécharge et parse le calendrier RSS depuis plusieurs sources en cascade."""
        global _ua_idx
        import random

        try:
            import requests as _rq
        except ImportError:
            logger.debug("requests non disponible — calendrier économique désactivé")
            return

        _proxy = os.getenv("HTTPS_PROXY", "") or os.getenv("HTTP_PROXY", "")
        _px = {"https": _proxy, "http": _proxy} if _proxy else {}

        for source in CALENDAR_SOURCES:
            _ua_idx = (_ua_idx + 1) % len(_USER_AGENTS)
            _headers = {
                "User-Agent": _USER_AGENTS[_ua_idx],
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
                "Cache-Control": "no-cache",
            }
            try:
                resp = _rq.get(source["url"], headers=_headers, proxies=_px, timeout=15)
                if resp.status_code == 429:
                    logger.debug(f"📅 {source['name']} rate-limited (429) — essai source suivante")
                    continue
                resp.raise_for_status()
                events = self._parse_rss(resp.content, source)
                if events is not None:
                    self._events = events
                    self._last_fetch = datetime.now(timezone.utc)
                    self._fail_count = 0
                    logger.info(f"📅 Calendrier économique : {len(events)} events HIGH impact ({source['name']})")
                    return
            except Exception as src_err:
                logger.debug(f"📅 {source['name']} inaccessible : {src_err}")
                continue

        # Toutes les sources ont échoué
        self._fail_count += 1
        if self._fail_count == 1:
            logger.warning("⚠️  Calendrier économique : toutes les sources inaccessibles — trading autorisé.")
        elif self._fail_count % 5 == 0:
            logger.debug(f"Calendrier économique {self._fail_count} échecs consécutifs — trading autorisé.")

    def _parse_rss(self, content: bytes, source: dict):
        """Parse le contenu XML. Retourne la liste d'events ou None si format invalide."""
        try:
            root = ET.fromstring(content)
            events = []
            date_fmt   = source.get("date_fmt", "%m-%d-%Y %I:%M%p")
            impact_tag = source.get("impact_tag", "impact")
            country_tag = source.get("country_tag", "country")

            for item in root.iter("item"):
                title    = item.findtext("title", "")
                country  = item.findtext(country_tag, "")
                impact   = item.findtext(impact_tag, "")
                date_str = item.findtext("date", "")
                time_str = item.findtext("time", "")

                if impact.lower() != "high":
                    continue
                if country not in ("USD", "EUR", "GBP", "JPY", "ALL", ""):
                    continue

                try:
                    dt_str = f"{date_str} {time_str}"
                    dt = datetime.strptime(dt_str, date_fmt)
                    dt = dt.replace(tzinfo=timezone.utc)
                    events.append({"title": title, "dt": dt, "impact": impact})
                except Exception:
                    continue

            return events
        except ET.ParseError:
            return None

    def start_background_refresh(self):
        """
        BUG FIX #C : Lance le refresh dans un thread daemon.
        À appeler UNE FOIS au démarrage du bot (main.py __init__).
        should_pause_trading() ne fera plus jamais d'I/O.
        """
        import threading
        import time

        def _loop():
            while True:
                self.refresh()
                time.sleep(self._fetch_interval_hours * 3600)

        t = threading.Thread(target=_loop, daemon=True, name="calendar_refresh")
        t.start()
        logger.info(f"📅 Calendrier économique : refresh en arrière-plan toutes les {self._fetch_interval_hours}h")

    def should_pause_trading(self) -> Tuple[bool, str]:
        """
        Retourne (True, raison) si le trading doit être mis en pause,
        (False, "") sinon.
        BUG FIX #C : Aucun appel HTTP ici — lecture seule du cache _events.
        """
        # Rafraîchir si nécessaire
        now = datetime.now(timezone.utc)
        
        for event in self._events:
            dt = event["dt"]
            delta_min = (dt - now).total_seconds() / 60

            # Dans la fenêtre de pause
            if -PAUSE_AFTER_MIN <= delta_min <= PAUSE_BEFORE_MIN:
                reason = f"📅 News HIGH impact : {event['title']} dans {delta_min:.0f} min"
                logger.warning(f"⏸️  Pause trading — {reason}")
                return True, reason

        return False, ""

    def get_next_event(self) -> Optional[str]:
        """Retourne la prochaine news HIGH impact formatée."""
        now = datetime.now(timezone.utc)
        upcoming = [
            e for e in self._events
            if (e["dt"] - now).total_seconds() > 0
        ]
        if not upcoming:
            return None
        next_ev = min(upcoming, key=lambda e: e["dt"])
        delta   = next_ev["dt"] - now
        hours   = int(delta.total_seconds() // 3600)
        mins    = int((delta.total_seconds() % 3600) // 60)
        return f"{next_ev['title']} dans {hours}h{mins:02d}min"
