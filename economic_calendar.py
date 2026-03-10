"""
economic_calendar.py — Récupère les annonces économiques HIGH impact
depuis le flux RSS gratuit de ForexFactory.
Pause trading 30min avant et 30min après chaque annonce critique.
"""

import xml.etree.ElementTree as ET
from typing import Optional, Tuple
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen
from urllib.error import URLError
from loguru import logger

# Flux RSS ForexFactory (gratuit, pas d'API key)
FF_RSS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

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
        try:
            from urllib.request import Request
            req = Request(
                FF_RSS_URL,
                headers={"User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0)"}
            )
            with urlopen(req, timeout=10) as resp:
                raw = resp.read()
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
            logger.info(f"📅 Calendrier économique : {len(events)} events HIGH impact chargés")

        except URLError as e:
            logger.warning(f"⚠️  ForexFactory RSS inaccessible : {e} — trading autorisé.")
        except Exception as e:
            logger.error(f"❌ Erreur parsing calendrier : {e}")

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
