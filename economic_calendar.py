"""
economic_calendar.py — Récupère les annonces économiques HIGH impact
depuis le flux RSS gratuit de ForexFactory.
Pause trading 30min avant et 30min après chaque annonce critique.
"""

import os
import xml.etree.ElementTree as ET
from typing import Optional, Tuple
from datetime import datetime, timezone, timedelta
from loguru import logger

# Flux RSS ForexFactory (via proxy WARP si disponible)
FF_RSS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

# Compteur d'erreurs RSS (log silencieux après 1er échec)
_ff_fail_count: int = 0

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

    def refresh(self):
        """Télécharge et parse le calendrier RSS de la semaine."""
        global _ff_fail_count
        try:
            import requests as _rq
            # Utiliser WARP proxy si disponible (Hetzner bloque faireconomy.media directement)
            _proxy = os.getenv("HTTPS_PROXY", "") or os.getenv("HTTP_PROXY", "")
            _px = {"https": _proxy, "http": _proxy} if _proxy else {}
            _headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                )
            }
            resp = _rq.get(FF_RSS_URL, headers=_headers, proxies=_px, timeout=12)
            resp.raise_for_status()
            raw = resp.content
            root = ET.fromstring(raw)
            events = []
            for item in root.iter("item"):
                title    = item.findtext("title", "")
                country  = item.findtext("country", "")
                impact   = item.findtext("impact", "")
                date_str = item.findtext("date", "")
                time_str = item.findtext("time", "")

                # Uniquement HIGH impact et USD/EUR/GBP/JPY (nos instruments)
                if impact.lower() != "high":
                    continue
                if country not in ("USD", "EUR", "GBP", "JPY", "ALL", ""):
                    continue

                # Parser la date
                try:
                    dt_str = f"{date_str} {time_str}"
                    dt = datetime.strptime(dt_str, "%m-%d-%Y %I:%M%p")
                    dt = dt.replace(tzinfo=timezone.utc)
                    events.append({"title": title, "dt": dt, "impact": impact})
                except Exception:
                    continue

            self._events = events
            self._last_fetch = datetime.now(timezone.utc)
            _ff_fail_count = 0  # Reset compteur d'erreurs
            logger.info(f"📅 Calendrier économique : {len(events)} events HIGH impact chargés")

        except Exception as e:
            _ff_fail_count += 1
            if _ff_fail_count == 1:
                logger.warning(f"⚠️  ForexFactory RSS inaccessible : {e} — trading autorisé.")
            elif _ff_fail_count % 10 == 0:
                logger.debug(f"ForexFactory RSS inaccessible ({_ff_fail_count}x) — trading autorisé: {e}")

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
