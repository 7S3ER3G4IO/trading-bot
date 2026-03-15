"""
argus_brain.py — ⚡ PROJECT ARGUS T2+T3: Local FinBERT NLP + Asset Mapping

100% gratuit. Aucune API payante. Aucun OpenAI.
Utilise ProsusAI/finbert (HuggingFace) en local.

Pipeline:
  1. Headline → FinBERT → sentiment (positive/negative/neutral) + confidence
  2. Asset Mapping → quel(s) instrument(s) concerné(s)?
  3. News Impulse Signal si impact > seuil

Usage:
    from argus_brain import ArgusBrain
    brain = ArgusBrain()
    brain.load_model()  # Downloads FinBERT on first run (~400MB)
    result = brain.analyze("Fed raises rates by 50bps, surprising markets")
    # → {"sentiment": "negative", "confidence": 0.94, "assets": ["EURUSD", "GOLD"], ...}
"""

import re
import time
from datetime import datetime, timezone
from collections import defaultdict
from loguru import logger

# ─── Lazy imports (heavy libs) ───────────────────────────────────────────────
_TRANSFORMERS_OK = False
_pipeline = None

try:
    import torch as _torch  # noqa: F401 — verify torch available before transformers
    from transformers import pipeline as hf_pipeline, AutoTokenizer, AutoModelForSequenceClassification
    _TRANSFORMERS_OK = True
except (ImportError, Exception):
    pass


# ─── Asset Keyword Mapping ──────────────────────────────────────────────────
# Maps keywords in headlines → trading instruments
ASSET_KEYWORDS = {
    # Forex
    "EURUSD": ["euro", "eur/usd", "eurusd", "ecb", "european central bank", "eurozone", "lagarde"],
    "GBPUSD": ["pound", "sterling", "gbp/usd", "gbpusd", "boe", "bank of england", "bailey"],
    "USDJPY": ["yen", "usd/jpy", "usdjpy", "boj", "bank of japan", "ueda", "kuroda"],
    "USDCHF": ["swiss franc", "usd/chf", "usdchf", "snb", "swiss national bank"],
    "AUDUSD": ["aussie", "aud/usd", "audusd", "rba", "reserve bank of australia"],
    "NZDUSD": ["kiwi", "nzd/usd", "nzdusd", "rbnz"],

    # USD general (affects all USD pairs)
    "_USD": ["dollar", "usd", "fed", "federal reserve", "powell", "fomc",
             "treasury", "nonfarm", "payrolls", "cpi", "inflation", "gdp",
             "interest rate", "rate hike", "rate cut", "unemployment"],

    # Commodities
    "GOLD":  ["gold", "xau", "bullion", "precious metal", "safe haven"],
    "SILVER": ["silver", "xag"],
    "OIL_BRENT": ["oil", "crude", "brent", "opec", "petroleum", "barrel", "wti"],
    "OIL_WTI":   ["oil", "crude", "wti", "opec", "petroleum"],
    "NATGAS": ["natural gas", "natgas", "lng"],
    "COPPER": ["copper"],

    # Indices
    "US500": ["s&p 500", "s&p500", "sp500", "wall street", "us stocks", "american stocks"],
    "US100": ["nasdaq", "tech stocks", "us100", "big tech"],
    "DE40":  ["dax", "german stocks", "de40"],
    "UK100": ["ftse", "uk stocks", "uk100", "london stock"],
    "JP225": ["nikkei", "japanese stocks", "jp225"],

    # Crypto
    "BTCUSD": ["bitcoin", "btc", "crypto", "cryptocurrency", "satoshi"],
    "ETHUSD": ["ethereum", "eth", "ether", "vitalik"],
    "XRPUSD": ["ripple", "xrp"],
    "SOLUSD": ["solana", "sol"],
    "ADAUSD": ["cardano", "ada"],
    "DOGEUSD": ["dogecoin", "doge", "musk"],
}

# ─── Impact Thresholds ──────────────────────────────────────────────────────
# sentiment_score × confidence → impact
NEWS_IMPULSE_THRESHOLD = 0.75   # High-impact news bypass
CONFIDENCE_MINIMUM     = 0.60   # Ignore low-confidence predictions


class ArgusBrain:
    """
    Local FinBERT NLP engine for financial sentiment analysis.
    Zero API cost. Runs entirely on local hardware.
    """

    def __init__(self, model_name: str = "ProsusAI/finbert",
                 telegram_router=None):
        self._model_name = model_name
        self._router = telegram_router
        self._classifier = None
        self._model_loaded = False

        # Stats
        self._analyzed_count = 0
        self._impulse_count = 0
        self._analysis_log: list = []  # Last 50 analyses

        # News Impulse Signal buffer
        self._impulse_signals: list = []  # Active impulse signals

    def load_model(self) -> bool:
        """
        Download and load FinBERT model locally.
        First run downloads ~400MB. Subsequent runs use cache.

        Returns True if model is ready.
        """
        global _TRANSFORMERS_OK

        if not _TRANSFORMERS_OK:
            logger.error(
                "❌ Argus Brain: 'transformers' not installed. "
                "Run: pip install transformers torch"
            )
            return False

        if self._model_loaded:
            return True

        try:
            logger.info(f"🧠 Argus Brain: Loading {self._model_name}...")
            t0 = time.time()

            self._classifier = hf_pipeline(
                "sentiment-analysis",
                model=self._model_name,
                tokenizer=self._model_name,
                top_k=None,  # Return all labels with scores
                device=-1,   # CPU (use 0 for GPU)
            )

            elapsed = time.time() - t0
            self._model_loaded = True
            logger.info(f"✅ Argus Brain: FinBERT loaded in {elapsed:.1f}s (local)")
            return True

        except Exception as e:
            logger.error(f"❌ Argus Brain load failed: {e}")
            return False

    def analyze(self, headline: str, source: str = "") -> dict:
        """
        Analyze a single headline.

        Returns
        -------
        {
            "headline": str,
            "sentiment": "positive" | "negative" | "neutral",
            "confidence": float (0-1),
            "impact_score": float (-1 to +1),
            "assets": [str],  # Matched instruments
            "is_impulse": bool,  # High-impact bypass signal
            "source": str,
        }
        """
        result = {
            "headline": headline,
            "sentiment": "neutral",
            "confidence": 0.0,
            "impact_score": 0.0,
            "assets": [],
            "is_impulse": False,
            "source": source,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # ─── Step 1: FinBERT Sentiment ────────────────────────────────────
        if self._model_loaded and self._classifier:
            try:
                predictions = self._classifier(headline[:512])  # FinBERT max 512 tokens

                # predictions = [[{"label": "positive", "score": 0.94}, ...]]
                if predictions and isinstance(predictions[0], list):
                    preds = predictions[0]
                else:
                    preds = predictions

                # Find best prediction
                best = max(preds, key=lambda x: x["score"])
                result["sentiment"] = best["label"].lower()
                result["confidence"] = round(best["score"], 4)

                # Impact score: positive → +1, negative → -1, scaled by confidence
                if result["sentiment"] == "positive":
                    result["impact_score"] = round(result["confidence"], 4)
                elif result["sentiment"] == "negative":
                    result["impact_score"] = round(-result["confidence"], 4)
                else:
                    result["impact_score"] = 0.0

            except Exception as e:
                logger.error(f"Argus Brain analyze error: {e}")
        else:
            # Fallback: simple keyword sentiment (no model)
            result = self._fallback_sentiment(headline, result)

        # ─── Step 2: Asset Mapping ────────────────────────────────────────
        result["assets"] = self._map_assets(headline)

        # ─── Step 3: News Impulse Signal ──────────────────────────────────
        abs_impact = abs(result["impact_score"])
        if (abs_impact >= NEWS_IMPULSE_THRESHOLD
                and result["confidence"] >= CONFIDENCE_MINIMUM
                and result["assets"]):
            result["is_impulse"] = True
            self._impulse_count += 1

            # Store impulse signal
            impulse = {
                "headline": headline,
                "sentiment": result["sentiment"],
                "impact": result["impact_score"],
                "assets": result["assets"],
                "timestamp": time.time(),
                "ttl": 1800,  # 30 min validity
            }
            self._impulse_signals.append(impulse)
            # Keep last 20 impulses
            self._impulse_signals = self._impulse_signals[-20:]

            logger.warning(
                f"⚡ NEWS IMPULSE: {result['sentiment'].upper()} "
                f"({result['impact_score']:+.2f}) → {result['assets']} "
                f"| \"{headline[:60]}...\""
            )

            self._send_alert(
                f"⚡ <b>NEWS IMPULSE SIGNAL</b>\n\n"
                f"📰 <i>{headline[:100]}...</i>\n"
                f"📊 Source: {source}\n\n"
                f"🎯 Sentiment: <b>{result['sentiment'].upper()}</b>\n"
                f"💯 Confidence: <b>{result['confidence']:.0%}</b>\n"
                f"📈 Impact: <b>{result['impact_score']:+.2f}</b>\n"
                f"🎯 Assets: <b>{', '.join(result['assets'])}</b>"
            )

        # ─── Log ──────────────────────────────────────────────────────────
        self._analyzed_count += 1
        self._analysis_log.append(result)
        if len(self._analysis_log) > 50:
            self._analysis_log = self._analysis_log[-50:]

        return result

    def analyze_batch(self, headlines: list) -> list:
        """Analyze multiple headlines."""
        return [self.analyze(h.get("title", h) if isinstance(h, dict) else h,
                             h.get("source", "") if isinstance(h, dict) else "")
                for h in headlines]

    def get_active_impulses(self, instrument: str = "") -> list:
        """Get active (non-expired) impulse signals for an instrument."""
        now = time.time()
        active = []
        for imp in self._impulse_signals:
            if now - imp["timestamp"] > imp["ttl"]:
                continue  # Expired
            if instrument and instrument not in imp["assets"]:
                continue  # Not for this instrument
            active.append(imp)
        return active

    def get_news_bias(self, instrument: str) -> float:
        """
        Get aggregate news bias for an instrument.
        Returns float in [-1, +1]. 0 = neutral.
        Used by strategy.py to add/subtract from signal score.
        """
        impulses = self.get_active_impulses(instrument)
        if not impulses:
            return 0.0
        return sum(i["impact"] for i in impulses) / len(impulses)

    # ─── Asset Mapping ───────────────────────────────────────────────────

    def _map_assets(self, headline: str) -> list:
        """Map headline text to trading instruments."""
        text_lower = headline.lower()
        matched = set()

        for instrument, keywords in ASSET_KEYWORDS.items():
            for kw in keywords:
                if kw in text_lower:
                    if instrument.startswith("_"):
                        # Meta group (e.g., _USD → affects all USD pairs)
                        usd_pairs = [
                            "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
                            "AUDUSD", "NZDUSD"
                        ]
                        matched.update(usd_pairs)
                    else:
                        matched.add(instrument)
                    break  # One keyword match per instrument is enough

        return sorted(matched)

    # ─── Fallback Sentiment (no model) ───────────────────────────────────

    @staticmethod
    def _fallback_sentiment(headline: str, result: dict) -> dict:
        """Simple keyword-based sentiment when FinBERT is unavailable."""
        text = headline.lower()

        positive_words = [
            "surge", "soar", "rally", "bullish", "record high", "gains",
            "strong", "boost", "recovery", "beat expectations", "upgrade",
            "outperform", "buy", "growth", "expansion", "breakthrough",
        ]
        negative_words = [
            "crash", "plunge", "collapse", "bearish", "sell-off", "selloff",
            "recession", "crisis", "default", "inflation surge", "downgrade",
            "miss expectations", "warning", "risk", "fear", "panic", "war",
            "sanctions", "tariff", "drought", "destruction", "destroys",
        ]

        pos_count = sum(1 for w in positive_words if w in text)
        neg_count = sum(1 for w in negative_words if w in text)

        if pos_count > neg_count:
            result["sentiment"] = "positive"
            result["confidence"] = min(0.6, 0.3 + pos_count * 0.1)
            result["impact_score"] = result["confidence"]
        elif neg_count > pos_count:
            result["sentiment"] = "negative"
            result["confidence"] = min(0.6, 0.3 + neg_count * 0.1)
            result["impact_score"] = -result["confidence"]
        else:
            result["sentiment"] = "neutral"
            result["confidence"] = 0.3

        return result

    # ─── Status ──────────────────────────────────────────────────────────

    def format_status(self) -> str:
        model_status = "✅ FinBERT" if self._model_loaded else "⚠️ Fallback (keywords)"
        return (
            f"📡 <b>Argus Brain</b>\n"
            f"  🧠 Model: {model_status}\n"
            f"  📊 Analyzed: {self._analyzed_count}\n"
            f"  ⚡ Impulses: {self._impulse_count}\n"
            f"  🎯 Active signals: {len(self.get_active_impulses())}"
        )

    @property
    def stats(self) -> dict:
        return {
            "model_loaded": self._model_loaded,
            "model_name": self._model_name,
            "analyzed": self._analyzed_count,
            "impulses": self._impulse_count,
            "active_signals": len(self.get_active_impulses()),
            "buffer_size": len(self._analysis_log),
        }

    def _send_alert(self, text: str):
        if self._router:
            try:
                self._router.send_to("risk", text)
            except Exception:
                pass
