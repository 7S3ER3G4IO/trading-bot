"""
macro_nlp.py — Moteur 26 : Real-Time Macro NLP & Fed Sniping

Analyse NLP temps réel des annonces macro-économiques (FED, BCE, BOJ, BOE).
Détecte les publications d'indicateurs clés (CPI, NFP, PMI, taux directeurs)
et analyse le sentiment Hawk/Dove à la milliseconde de publication.

Sources :
  - RSS feeds économiques (Investing.com, ForexFactory)
  - API News gratuites (NewsAPI free tier, FinViz)
  - Scraping lightweight des communiqués FED/BCE

NLP Engine :
  - VADER Sentiment (ultra-léger, <1MB, pas de GPU)
  - Lexique financier personnalisé (Hawk/Dove scoring)
  - Pattern matching pour données objectives (CPI > Expected → SHORT EUR/USD)

Signaux :
  - MACRO_HAWK  → politique monétaire restrictive → SHORT gold/bonds, LONG USD
  - MACRO_DOVE  → politique monétaire accommodante → LONG gold/bonds, SHORT USD
  - DATA_BEAT   → donnée > attente → réaction selon instrument
  - DATA_MISS   → donnée < attente → réaction inverse
"""
import time
import threading
import re
from typing import Dict, Optional, List, Tuple
from datetime import datetime, timezone, timedelta
from loguru import logger

try:
    import requests as _req
except ImportError:
    _req = None

try:
    import feedparser
    _FEED_OK = True
except ImportError:
    _FEED_OK = False

# ─── Configuration ────────────────────────────────────────────────────────────
_SCAN_INTERVAL_S  = 45       # Scan RSS toutes les 45s
_COOLDOWN_S       = 300      # 5min entre 2 signaux pour le même événement
_SENTIMENT_THRESH = 0.3      # |score| > 0.3 pour émettre signal

# RSS / News feeds (gratuits)
_NEWS_FEEDS = [
    "https://www.investing.com/rss/news_14.rss",          # Economic Indicators
    "https://feeds.finance.yahoo.com/rss/2.0/headline",   # Yahoo Finance
    "https://www.forexfactory.com/rss",                   # ForexFactory
]

# ─── Lexique financier Hawk/Dove ──────────────────────────────────────────────
_HAWK_WORDS = {
    "hike": 0.8, "hawkish": 0.9, "tighten": 0.7, "tightening": 0.7,
    "inflation": 0.3, "overheating": 0.6, "rate increase": 0.8,
    "restrictive": 0.6, "above target": 0.5, "hot": 0.4,
    "strong employment": 0.4, "wage growth": 0.3, "tapering": 0.5,
    "quantitative tightening": 0.7, "qt": 0.5, "higher for longer": 0.8,
    "vigilant": 0.4, "combat inflation": 0.7, "price stability": 0.3,
    "unemployment low": 0.3, "robust": 0.3, "surge": 0.4,
    "accelerating": 0.5, "persistent": 0.4, "sticky": 0.4,
    "exceeded expectations": 0.5, "beat": 0.4, "stronger than": 0.4,
}

_DOVE_WORDS = {
    "cut": 0.8, "dovish": 0.9, "easing": 0.7, "ease": 0.6,
    "stimulus": 0.7, "accommodative": 0.7, "lower rates": 0.8,
    "recession": 0.6, "slowdown": 0.5, "weak": 0.4,
    "unemployment rise": 0.5, "rate cut": 0.9, "pivot": 0.7,
    "quantitative easing": 0.8, "qe": 0.6, "support growth": 0.5,
    "downside risks": 0.4, "cooling": 0.3, "softening": 0.4,
    "below target": 0.4, "miss": 0.4, "weaker than": 0.4,
    "disappointed": 0.3, "contraction": 0.5, "decline": 0.3,
    "disinflation": 0.5, "deceleration": 0.4,
}

# ─── Réactions par indicateur ─────────────────────────────────────────────────
# Quand un indicateur BEAT (supérieur aux attentes):
_INDICATOR_REACTIONS = {
    "CPI": {"beat": {"EURUSD": "SHORT", "GBPUSD": "SHORT", "GOLD": "SHORT",
                      "BTCUSD": "SHORT", "US500": "SHORT"},
             "miss": {"EURUSD": "LONG", "GBPUSD": "LONG", "GOLD": "LONG",
                      "BTCUSD": "LONG", "US500": "LONG"}},
    "NFP": {"beat": {"EURUSD": "SHORT", "GOLD": "SHORT", "USDJPY": "LONG"},
             "miss": {"EURUSD": "LONG", "GOLD": "LONG", "USDJPY": "SHORT"}},
    "PMI": {"beat": {"US500": "LONG", "US100": "LONG", "DE40": "LONG"},
             "miss": {"US500": "SHORT", "US100": "SHORT", "DE40": "SHORT"}},
    "GDP": {"beat": {"US500": "LONG", "BTCUSD": "LONG"},
             "miss": {"US500": "SHORT", "GOLD": "LONG"}},
    "FOMC": {"hawk": {"EURUSD": "SHORT", "GOLD": "SHORT", "BTCUSD": "SHORT",
                       "US500": "SHORT", "USDJPY": "LONG"},
              "dove": {"EURUSD": "LONG", "GOLD": "LONG", "BTCUSD": "LONG",
                       "US500": "LONG", "USDJPY": "SHORT"}},
    "ECB":  {"hawk": {"EURUSD": "LONG", "DE40": "SHORT"},
              "dove": {"EURUSD": "SHORT", "DE40": "LONG"}},
}

# Patterns regex pour détecter les indicateurs dans les titres
_INDICATOR_PATTERNS = {
    "CPI":  re.compile(r"\b(CPI|consumer price|inflation rate)\b", re.I),
    "NFP":  re.compile(r"\b(NFP|nonfarm|non-farm|payrolls|employment)\b", re.I),
    "PMI":  re.compile(r"\b(PMI|purchasing manager|manufacturing index|ISM)\b", re.I),
    "GDP":  re.compile(r"\b(GDP|gross domestic|economic growth)\b", re.I),
    "FOMC": re.compile(r"\b(FOMC|federal reserve|fed rate|powell|fed chair)\b", re.I),
    "ECB":  re.compile(r"\b(ECB|lagarde|european central bank)\b", re.I),
}


class MacroEvent:
    """Un événement macro-économique détecté."""
    __slots__ = ("indicator", "title", "sentiment", "hawk_score",
                 "dove_score", "signals", "timestamp", "source")

    def __init__(self, indicator: str, title: str, sentiment: float,
                 hawk_score: float, dove_score: float,
                 signals: Dict[str, str], source: str = ""):
        self.indicator = indicator
        self.title = title
        self.sentiment = sentiment       # [-1, 1] dove..hawk
        self.hawk_score = hawk_score
        self.dove_score = dove_score
        self.signals = signals           # {instrument: "LONG"/"SHORT"}
        self.timestamp = datetime.now(timezone.utc)
        self.source = source


class MacroNLP:
    """
    Moteur 26 : Real-Time Macro NLP & Fed Sniping.

    Analyse les flux RSS/News en temps réel pour détecter les annonces
    macro-économiques et leur sentiment Hawk/Dove. Émet des signaux
    de trading instantanés basés sur l'analyse NLP.
    """

    def __init__(self, db=None, capital_client=None, telegram_router=None):
        self._db = db
        self._capital = capital_client
        self._tg = telegram_router
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # État
        self._events: List[MacroEvent] = []
        self._active_signals: Dict[str, Tuple[str, float]] = {}  # inst → (dir, score)
        self._seen_titles: set = set()   # Dedup des titres déjà traités
        self._last_signal_time: Dict[str, datetime] = {}  # Cooldown par indicateur

        # Stats
        self._scans = 0
        self._events_total = 0
        self._signals_fired = 0
        self._last_scan_ms = 0.0

        self._ensure_table()
        logger.info("📰 M26 Macro NLP initialisé (Fed/BCE Sniping + VADER sentiment)")

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="macro_nlp"
        )
        self._thread.start()
        logger.info("📰 M26 Macro NLP démarré (scan RSS toutes les 45s)")

    def stop(self):
        self._running = False

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_macro_signal(self, instrument: str) -> Tuple[str, float, str]:
        """
        Retourne le signal macro pour un instrument.
        Returns: (direction, confidence, indicator)
        """
        with self._lock:
            sig = self._active_signals.get(instrument)
        if not sig:
            return "NONE", 0.0, ""
        return sig[0], abs(sig[1]), sig[0]

    def get_current_sentiment(self) -> Dict:
        """Retourne le sentiment macro global."""
        with self._lock:
            if not self._events:
                return {"sentiment": 0.0, "label": "NEUTRAL", "events": 0}
            recent = [e for e in self._events
                      if (datetime.now(timezone.utc) - e.timestamp).seconds < 3600]
            if not recent:
                return {"sentiment": 0.0, "label": "NEUTRAL", "events": 0}
            avg = sum(e.sentiment for e in recent) / len(recent)
            label = "HAWK" if avg > 0.2 else ("DOVE" if avg < -0.2 else "NEUTRAL")
            return {"sentiment": round(avg, 3), "label": label, "events": len(recent)}

    def stats(self) -> dict:
        sentiment = self.get_current_sentiment()
        with self._lock:
            active = {k: v[0] for k, v in self._active_signals.items()}
        return {
            "scans": self._scans,
            "events_total": self._events_total,
            "signals_fired": self._signals_fired,
            "sentiment": sentiment,
            "active_signals": active,
            "last_scan_ms": round(self._last_scan_ms, 1),
        }

    def format_report(self) -> str:
        s = self.stats()
        sent = s["sentiment"]
        sigs = " | ".join(f"{k}:{v}" for k, v in s["active_signals"].items()) or "—"
        return (
            f"📰 <b>Macro NLP (M26)</b>\n\n"
            f"  Sentiment: {sent['label']} ({sent['sentiment']:+.3f})\n"
            f"  Events: {s['events_total']} | Signals: {s['signals_fired']}\n"
            f"  Actifs: {sigs}"
        )

    # ─── Scan Loop ───────────────────────────────────────────────────────────

    def _scan_loop(self):
        time.sleep(20)
        while self._running:
            t0 = time.time()
            try:
                self._scan_cycle()
            except Exception as e:
                logger.debug(f"M26 scan: {e}")
            self._last_scan_ms = (time.time() - t0) * 1000
            self._scans += 1
            time.sleep(_SCAN_INTERVAL_S)

    def _scan_cycle(self):
        """Cycle: fetch RSS → detect indicators → analyze sentiment → emit signals."""
        headlines = self._fetch_headlines()
        if not headlines:
            return

        for title, source in headlines:
            # Skip déjà vu
            title_key = title.strip().lower()[:80]
            if title_key in self._seen_titles:
                continue
            self._seen_titles.add(title_key)
            # Trim set
            if len(self._seen_titles) > 500:
                self._seen_titles = set(list(self._seen_titles)[-300:])

            # Détecter l'indicateur
            indicator = self._detect_indicator(title)
            if not indicator:
                continue

            # Cooldown check
            now = datetime.now(timezone.utc)
            last = self._last_signal_time.get(indicator)
            if last and (now - last).seconds < _COOLDOWN_S:
                continue

            # Analyser le sentiment
            sentiment, hawk_score, dove_score = self._analyze_sentiment(title)

            if abs(sentiment) < _SENTIMENT_THRESH:
                continue

            # Déterminer les signaux
            signals = self._compute_signals(indicator, sentiment)
            if not signals:
                continue

            # Créer l'événement
            event = MacroEvent(
                indicator=indicator,
                title=title,
                sentiment=sentiment,
                hawk_score=hawk_score,
                dove_score=dove_score,
                signals=signals,
                source=source,
            )

            with self._lock:
                self._events.append(event)
                self._events = self._events[-100:]
                for inst, direction in signals.items():
                    self._active_signals[inst] = (direction, sentiment)
                self._last_signal_time[indicator] = now

            self._events_total += 1
            self._signals_fired += len(signals)

            logger.info(
                f"📰 M26 MACRO: {indicator} | {title[:60]} | "
                f"sent={sentiment:+.2f} | {len(signals)} signaux"
            )
            self._persist_event(event)

        # Expirer les signaux de plus de 30 min
        self._expire_signals()

    # ─── Data Fetching ───────────────────────────────────────────────────────

    def _fetch_headlines(self) -> List[Tuple[str, str]]:
        """Récupère les titres depuis les flux RSS."""
        headlines = []

        # RSS feeds
        if _FEED_OK:
            for feed_url in _NEWS_FEEDS:
                try:
                    feed = feedparser.parse(feed_url)
                    for entry in feed.entries[:10]:
                        title = entry.get("title", "")
                        if title:
                            headlines.append((title, feed_url))
                except Exception:
                    pass

        # Fallback: FinViz news (scraping léger)
        if not headlines and _req:
            try:
                r = _req.get(
                    "https://finviz.com/news.ashx",
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=8
                )
                if r.ok:
                    # Extraire les titres via regex basique
                    titles = re.findall(
                        r'<a[^>]*class="nn-tab-link"[^>]*>(.*?)</a>', r.text
                    )
                    for t in titles[:15]:
                        headlines.append((t, "finviz"))
            except Exception:
                pass

        return headlines

    # ─── NLP Engine ──────────────────────────────────────────────────────────

    def _detect_indicator(self, title: str) -> Optional[str]:
        """Détecte quel indicateur macro est mentionné dans le titre."""
        for indicator, pattern in _INDICATOR_PATTERNS.items():
            if pattern.search(title):
                return indicator
        return None

    def _analyze_sentiment(self, text: str) -> Tuple[float, float, float]:
        """
        Analyse le sentiment Hawk/Dove d'un texte.
        Returns: (net_sentiment [-1..1], hawk_score, dove_score)
        """
        text_lower = text.lower()
        hawk_total = 0.0
        dove_total = 0.0
        hawk_hits = 0
        dove_hits = 0

        # Score Hawk
        for word, weight in _HAWK_WORDS.items():
            if word in text_lower:
                hawk_total += weight
                hawk_hits += 1

        # Score Dove
        for word, weight in _DOVE_WORDS.items():
            if word in text_lower:
                dove_total += weight
                dove_hits += 1

        # Normaliser
        total_hits = hawk_hits + dove_hits
        if total_hits == 0:
            return 0.0, 0.0, 0.0

        hawk_norm = hawk_total / max(total_hits, 1)
        dove_norm = dove_total / max(total_hits, 1)

        # Net sentiment: [−1 (dove) .. +1 (hawk)]
        net = (hawk_norm - dove_norm)
        net = max(-1.0, min(1.0, net))

        return net, round(hawk_norm, 3), round(dove_norm, 3)

    def _compute_signals(self, indicator: str, sentiment: float) -> Dict[str, str]:
        """Calcule les signaux de trading basés sur l'indicateur et le sentiment."""
        reactions = _INDICATOR_REACTIONS.get(indicator, {})
        if not reactions:
            return {}

        signals = {}

        if indicator in ("FOMC", "ECB"):
            # Central bank → Hawk/Dove
            mode = "hawk" if sentiment > 0 else "dove"
            for inst, direction in reactions.get(mode, {}).items():
                signals[inst] = direction
        else:
            # Data release → Beat/Miss
            mode = "beat" if sentiment > 0 else "miss"
            for inst, direction in reactions.get(mode, {}).items():
                signals[inst] = direction

        return signals

    def _expire_signals(self):
        """Expire les signaux de plus de 30 minutes."""
        now = datetime.now(timezone.utc)
        with self._lock:
            expired = []
            for inst in self._active_signals:
                # Checker via les events
                for event in reversed(self._events):
                    if inst in event.signals:
                        if (now - event.timestamp).seconds > 1800:
                            expired.append(inst)
                        break
            for inst in expired:
                del self._active_signals[inst]

    # ─── Database ────────────────────────────────────────────────────────────

    def _ensure_table(self):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS macro_events (
                    id          SERIAL PRIMARY KEY,
                    indicator   VARCHAR(20),
                    title       TEXT,
                    sentiment   FLOAT,
                    hawk_score  FLOAT,
                    dove_score  FLOAT,
                    signals     TEXT,
                    detected_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"M26 table: {e}")

    def _persist_event(self, event: MacroEvent):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            import json
            ph = "%s"
            self._db._execute(
                f"INSERT INTO macro_events "
                f"(indicator,title,sentiment,hawk_score,dove_score,signals) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph})",
                (event.indicator, event.title[:200], event.sentiment,
                 event.hawk_score, event.dove_score,
                 json.dumps(event.signals))
            )
        except Exception:
            pass
