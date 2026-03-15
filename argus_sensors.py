"""
argus_sensors.py — ⚡ PROJECT ARGUS T1: RSS News Aspirator

100% gratuit. Aucune API payante.
Scrape les flux RSS publics de Yahoo Finance, ForexLive, Investing.com, etc.
Tourne en background thread et alimente un buffer de headlines.

Usage:
    from argus_sensors import ArgusSensors
    sensors = ArgusSensors()
    sensors.start()  # Background thread
    headlines = sensors.get_recent(limit=20)
"""

import time
import threading
from datetime import datetime, timezone
from loguru import logger

try:
    import feedparser
    _FEEDPARSER_OK = True
except ImportError:
    _FEEDPARSER_OK = False
    feedparser = None

try:
    from bs4 import BeautifulSoup
    _BS4_OK = True
except ImportError:
    _BS4_OK = False


# ─── Free Financial RSS Feeds ────────────────────────────────────────────────
RSS_FEEDS = {
    # Forex & Macro
    "forexlive": "https://www.forexlive.com/feed/news",
    "dailyfx":   "https://www.dailyfx.com/feeds/market-news",
    "fxstreet":  "https://www.fxstreet.com/rss/news",

    # General Finance
    "yahoo_finance": "https://finance.yahoo.com/news/rssindex",
    "cnbc_world":    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362",
    "reuters_biz":   "https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best",
    "marketwatch":   "https://feeds.marketwatch.com/marketwatch/topstories/",

    # Commodities & Crypto
    "kitco_gold":    "https://www.kitco.com/rss/gold.xml",
    "coindesk":      "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",

    # Central Banks & Macro
    "ecb_press":     "https://www.ecb.europa.eu/rss/press.html",
}

# Scan interval (seconds)
SCAN_INTERVAL = 300  # Every 5 minutes
MAX_BUFFER_SIZE = 200  # Max headlines in memory


class ArgusSensors:
    """
    Background RSS scraper for financial news.
    Feeds headlines to ArgusNLP for sentiment analysis.
    """

    def __init__(self):
        self._buffer: list = []  # [(timestamp, source, title, link)]
        self._seen_titles: set = set()  # Dedup
        self._lock = threading.Lock()
        self._thread = None
        self._running = False
        self._scan_count = 0
        self._error_count = 0

    def start(self):
        """Start background RSS scanning thread."""
        if not _FEEDPARSER_OK:
            logger.warning("⚠️ Argus: feedparser not installed — sensors disabled")
            return
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="argus-sensors"
        )
        self._thread.start()
        logger.info("📡 Argus Sensors: background RSS scanning started")

    def stop(self):
        self._running = False

    def get_recent(self, limit: int = 20) -> list:
        """Get most recent headlines (newest first)."""
        with self._lock:
            return list(reversed(self._buffer[-limit:]))

    def get_unprocessed(self, since_ts: float = 0) -> list:
        """Get headlines newer than timestamp."""
        with self._lock:
            return [h for h in self._buffer if h[0] > since_ts]

    def inject_headline(self, title: str, source: str = "manual"):
        """Manually inject a headline (for testing / webhooks)."""
        now = datetime.now(timezone.utc).timestamp()
        with self._lock:
            self._buffer.append((now, source, title, ""))
            self._trim_buffer()

    # ─── Internal ─────────────────────────────────────────────────────────

    def _scan_loop(self):
        """Background loop: scan all RSS feeds periodically."""
        # First scan immediately
        self._scan_all_feeds()

        while self._running:
            time.sleep(SCAN_INTERVAL)
            if self._running:
                self._scan_all_feeds()

    def _scan_all_feeds(self):
        """Scan all configured RSS feeds."""
        if not _FEEDPARSER_OK:
            return

        self._scan_count += 1
        new_count = 0

        for source, url in RSS_FEEDS.items():
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:10]:  # Max 10 per feed
                    title = entry.get("title", "").strip()
                    link = entry.get("link", "")

                    if not title or title in self._seen_titles:
                        continue

                    # Parse published time
                    ts = datetime.now(timezone.utc).timestamp()
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        try:
                            import calendar
                            ts = calendar.timegm(entry.published_parsed)
                        except Exception:
                            pass

                    with self._lock:
                        self._buffer.append((ts, source, title, link))
                        self._seen_titles.add(title)
                        new_count += 1
                        self._trim_buffer()

            except Exception as e:
                self._error_count += 1
                logger.debug(f"Argus RSS {source}: {e}")

        if new_count > 0:
            logger.info(
                f"📡 Argus: {new_count} new headlines from {len(RSS_FEEDS)} feeds "
                f"(scan #{self._scan_count})"
            )

    def _trim_buffer(self):
        """Keep buffer within size limit."""
        if len(self._buffer) > MAX_BUFFER_SIZE:
            overflow = len(self._buffer) - MAX_BUFFER_SIZE
            removed = self._buffer[:overflow]
            self._buffer = self._buffer[overflow:]
            for _, _, title, _ in removed:
                self._seen_titles.discard(title)

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "buffer_size": len(self._buffer),
                "scans": self._scan_count,
                "errors": self._error_count,
                "feeds": len(RSS_FEEDS),
                "running": self._running,
            }
