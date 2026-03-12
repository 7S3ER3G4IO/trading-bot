"""
alt_data.py — Moteur 5 : Alternative Data & Sentiment Analysis.

"Le prix n'est que la conséquence de l'information."

Sources de données alternatives:
  1. RSS/News Feed (via feedparser ou requests + XML)
     → Score bullish/bearish NLP léger sur les mots-clés
  2. CryptoCompare API (gratuit) pour le sentiment crypto
  3. Fear & Greed Index (déjà dans le bot) → enrichissement
  4. Open Interest / Funding Rate (CoinGlass) pour les paires crypto

Score de sentiment: -1.0 (extreme bearish) → +1.0 (extreme bullish)
Filtre: si sen_score très négatif pour un signal BUY → annulation.

Usage:
    alt = AltDataEngine(telegram_router)
    score = alt.get_sentiment(instrument)  # ex: "XBTUSD" ou "EURUSD"
    if score < -0.5 and direction == "BUY":
        return  # Signal annulé par le sentiment
"""
import time
import hashlib
import threading
import os
from typing import Dict, Optional
from datetime import datetime, timezone, timedelta
from loguru import logger

# ─── Paramètres ───────────────────────────────────────────────────────────────
_REFRESH_INTERVAL_S = 300    # Rafraichit le sentiment toutes les 5min
_SENTIMENT_DECAY_H  = 2      # Score expire après 2h
_BLOCK_THRESHOLD    = -0.55  # Score < -0.55 → bloque un BUY
_BOOST_THRESHOLD    = 0.55   # Score > +0.55 → valide un signal BUY

# Mots-clés bearish / bullish pour NLP léger
_BEARISH_WORDS = {
    "crash", "collapse", "ban", "regulation", "hack", "breach",
    "lawsuit", "fraud", "bankruptcy", "crisis", "fear", "plunge",
    "sell-off", "dump", "bear", "decline", "drop", "warning",
    "sanctions", "inflation", "recession", "rate hike",
}
_BULLISH_WORDS = {
    "surge", "rally", "bull", "adoption", "etf", "approval",
    "partnership", "launch", "upgrade", "integration", "halving",
    "institutional", "buy", "breakthrough", "growth", "record",
    "ath", "all-time high", "accumulation", "bullish",
}

# News RSS feeds publics (pas de clé API)
_RSS_FEEDS = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=EUR%3DX&region=US&lang=en-US",
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=BTC-USD&region=US&lang=en-US",
    "https://www.cnbc.com/id/100727362/device/rss/rss.html",
]

# Mapping partiel instrument → keywords de recherche
_ASSET_KEYWORDS: Dict[str, list] = {
    "XBTUSD": ["bitcoin", "btc", "crypto"],
    "ETHUSD": ["ethereum", "eth", "defi"],
    "EURUSD": ["euro", "ecb", "eurozone"],
    "GBPUSD": ["pound", "boe", "uk", "britain"],
    "USDJPY": ["yen", "boj", "japan"],
    "XAUUSD": ["gold", "xau", "inflation"],
    "USOIL":  ["oil", "opec", "crude"],
}


class AltDataEngine:
    """
    Moteur de données alternatives et sentiment NLP léger.
    Tourne en background, rafraichit le sentiment sans bloquer le trading.
    """

    def __init__(self, telegram_router=None):
        self._tg   = telegram_router
        self._lock = threading.Lock()

        # {instrument: {"score": float, "updated": datetime, "sources": list}}
        self._sentiment_cache: Dict[str, dict] = {}
        self._fetch_count  = 0
        self._article_pool  = []   # Pool d'articles récents (toutes sources)

        self._running = True
        self._thread  = threading.Thread(
            target=self._refresh_loop, daemon=True, name="alt_data"
        )
        self._thread.start()
        logger.info("📡 Alt-Data Engine démarré (refresh 5min)")

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_sentiment(self, instrument: str) -> float:
        """
        Retourne le score de sentiment pour cet instrument.
        Range: -1.0 (extreme bearish) → +1.0 (extreme bullish)
        0.0 = neutre / inconnu.
        """
        with self._lock:
            entry = self._sentiment_cache.get(instrument)
        if not entry:
            return 0.0
        age_h = (datetime.now(timezone.utc) - entry["updated"]).total_seconds() / 3600
        if age_h > _SENTIMENT_DECAY_H:
            return 0.0  # Score expiré
        return entry["score"]

    def should_block_entry(self, instrument: str, direction: str) -> tuple:
        """
        Retourne (block: bool, reason: str).
        Bloque si sentiment fortement contre la direction.
        """
        score = self.get_sentiment(instrument)
        if direction == "BUY" and score < _BLOCK_THRESHOLD:
            return True, f"sentiment bearish ({score:.2f})"
        if direction == "SELL" and score > _BOOST_THRESHOLD:
            return True, f"sentiment bullish ({score:.2f})"
        return False, "ok"

    def get_all_scores(self) -> dict:
        """Retourne tous les scores de sentiment actuels."""
        with self._lock:
            out = {}
            for inst, entry in self._sentiment_cache.items():
                age_h = (datetime.now(timezone.utc) - entry["updated"]).total_seconds() / 3600
                out[inst] = {
                    "score": entry["score"],
                    "age_h": round(age_h, 1),
                    "valid": age_h <= _SENTIMENT_DECAY_H,
                }
        return out

    def format_report(self) -> str:
        scores = self.get_all_scores()
        if not scores:
            return "📡 Alt-Data: aucun score disponible"
        lines = []
        for inst, d in sorted(scores.items(), key=lambda x: abs(x[1]["score"]), reverse=True)[:8]:
            icon = "🟢" if d["score"] > 0.2 else ("🔴" if d["score"] < -0.2 else "⚪")
            lines.append(f"  {icon} {inst}: {d['score']:+.2f} ({'✅' if d['valid'] else '⏳ expiré'})")
        return "📡 <b>Alternative Data Sentiment</b>\n" + "\n".join(lines)

    # ─── Refresh Loop ────────────────────────────────────────────────────────

    def _refresh_loop(self):
        while self._running:
            try:
                self._fetch_news()
                self._compute_all_scores()
                self._fetch_count += 1
            except Exception as e:
                logger.debug(f"AltData refresh: {e}")
            time.sleep(_REFRESH_INTERVAL_S)

    def _fetch_news(self):
        """Récupère les articles RSS sans dépendances externes."""
        try:
            import urllib.request
            from xml.etree import ElementTree as ET
        except ImportError:
            return

        new_articles = []
        for feed_url in _RSS_FEEDS:
            try:
                req = urllib.request.Request(
                    feed_url, headers={"User-Agent": "NemesisBot/2.0"}
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    content = resp.read().decode("utf-8", errors="ignore")

                root = ET.fromstring(content)
                for item in root.iter("item"):
                    title = (item.findtext("title") or "").lower()
                    desc  = (item.findtext("description") or "").lower()
                    text  = f"{title} {desc}"
                    pub   = item.findtext("pubDate") or ""
                    art_id = hashlib.md5(title.encode()).hexdigest()[:8]
                    new_articles.append({"text": text, "pub": pub, "id": art_id})

            except Exception:
                pass

        # Conserver les 200 derniers articles
        with self._lock:
            existing_ids = {a["id"] for a in self._article_pool}
            for a in new_articles:
                if a["id"] not in existing_ids:
                    self._article_pool.append(a)
            self._article_pool = self._article_pool[-200:]

    def _compute_all_scores(self):
        """Calcule un score NLP pour chaque instrument connu."""
        with self._lock:
            articles = list(self._article_pool)

        for instrument, keywords in _ASSET_KEYWORDS.items():
            score = self._nlp_score(articles, keywords)
            with self._lock:
                self._sentiment_cache[instrument] = {
                    "score": round(score, 3),
                    "updated": datetime.now(timezone.utc),
                    "article_count": len([a for a in articles if any(kw in a["text"] for kw in keywords)]),
                }

    def _nlp_score(self, articles: list, keywords: list) -> float:
        """
        Score NLP simple: compte les mots bullish vs bearish
        dans les articles contenant les keywords de l'instrument.
        """
        relevant = [a for a in articles if any(kw in a["text"] for kw in keywords)]
        if not relevant:
            return 0.0

        bull_count = 0
        bear_count = 0
        for a in relevant:
            text = a["text"]
            bull_count += sum(1 for w in _BULLISH_WORDS if w in text)
            bear_count += sum(1 for w in _BEARISH_WORDS if w in text)

        total = bull_count + bear_count
        if total == 0:
            return 0.0

        # Score normalisé
        raw = (bull_count - bear_count) / total
        # Atténuation: on ne permet pas un score > 0.8 sur une seule update
        return max(-0.8, min(0.8, raw))

    def stop(self):
        self._running = False
