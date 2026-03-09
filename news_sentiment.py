"""
news_sentiment.py — NLP Sentiment News (#7)

Analyse les titres d'actualité crypto et génère un score de sentiment.
Utilise des APIs gratuites (CryptoPanic, Alternative.me).

Méthode simple et rapide (pas besoin de modèle NLP lourd) :
  - Mots haussiers (+1) : bullish, surge, rally, buy, moon, ATH...
  - Mots baissiers (-1) : crash, ban, hack, bear, dump, SEC, lawsuit...
  - Score moyen sur N derniers articles → BULLISH / BEARISH / NEUTRAL

Pas de clé API requise pour CryptoPanic (mode public).
"""
import sys, time, re, requests, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")
from loguru import logger

CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/?auth_token=free&public=true&kind=news"
CACHE_TTL       = 600  # 10 minutes

BULL_WORDS = {
    "bullish", "rally", "surge", "pump", "moon", "ath", "breakout", "buy",
    "long", "adoption", "etf", "approved", "launch", "partnership", "upgrade",
    "growth", "gains", "soar", "record", "high", "bull", "recovery", "bounce",
    "accumulate", "institutional", "positive", "optimism",
}

BEAR_WORDS = {
    "bearish", "crash", "dump", "ban", "hack", "lawsuit", "sec", "warning",
    "sell", "short", "fear", "drop", "fall", "plunge", "scam", "fraud",
    "bankrupt", "closure", "delist", "regulation", "fine", "loss", "bear",
    "panic", "sell-off", "downturn", "bearish", "threat", "exploit",
}


def _score_text(text: str) -> int:
    """Score un texte : +1 par mot haussier, -1 par mot baissier."""
    words = set(re.findall(r"\b\w+\b", text.lower()))
    score = sum(1 for w in words if w in BULL_WORDS)
    score -= sum(1 for w in words if w in BEAR_WORDS)
    return score


class NewsSentiment:

    def __init__(self):
        self._cache     = None
        self._cache_ts  = 0

    def get_news(self) -> list:
        """Fetch les dernières news crypto."""
        now = time.time()
        if self._cache and (now - self._cache_ts) < CACHE_TTL:
            return self._cache
        try:
            r    = requests.get(CRYPTOPANIC_URL, timeout=8)
            data = r.json().get("results", [])
            items = [{"title": item.get("title",""), "url": item.get("url","")}
                     for item in data[:30]]
            self._cache    = items
            self._cache_ts = now
            logger.debug(f"📰 News chargées : {len(items)} articles")
            return items
        except Exception as e:
            logger.debug(f"News fetch error: {e}")
            return []

    def get_sentiment(self) -> dict:
        """
        Calcule le sentiment global sur les 30 dernières news.
        Retourne : {"score": int, "signal": str, "articles": int}
        """
        news  = self.get_news()
        if not news:
            return {"score": 0, "signal": "NEUTRAL", "emoji": "😐", "articles": 0}

        scores = [_score_text(n["title"]) for n in news]
        total  = sum(scores)
        avg    = total / len(scores)
        count  = len(scores)

        if avg >= 0.5:
            signal, emoji = "BULLISH", "📰🟢"
        elif avg <= -0.5:
            signal, emoji = "BEARISH", "📰🔴"
        else:
            signal, emoji = "NEUTRAL", "📰⚪"

        # Exemples de titres importants (score extrême)
        top_bull = [n["title"] for n, s in zip(news, scores) if s >= 2][:2]
        top_bear = [n["title"] for n, s in zip(news, scores) if s <= -2][:2]

        return {
            "score":    round(avg, 2),
            "signal":   signal,
            "emoji":    emoji,
            "articles": count,
            "top_bull": top_bull,
            "top_bear": top_bear,
        }

    def should_allow_trade(self, side: str) -> bool:
        """
        Bloque un trade si le sentiment est fortement opposé.
        BUY bloqué si sentiment < -1.5 (news très baissières)
        SELL bloqué si sentiment > +1.5 (news très haussières)
        """
        s = self.get_sentiment()
        score = s.get("score", 0)
        if side == "BUY" and score <= -1.5:
            logger.warning(f"📰 News très baissières (score={score:.1f}) — BUY filtré")
            return False
        if side == "SELL" and score >= 1.5:
            logger.warning(f"📰 News très haussières (score={score:.1f}) — SELL filtré")
            return False
        return True

    def format_for_morning(self) -> str:
        """Format pour la matinale Telegram."""
        s = self.get_sentiment()
        lines = f"\n📰 <b>Sentiment News ({s['articles']} articles)</b>\n"
        lines += f"  Signal : <b>{s['emoji']} {s['signal']}</b> (score={s['score']:+.2f})\n"
        if s.get("top_bull"):
            lines += f"\n  🟢 {s['top_bull'][0][:80]}\n"
        if s.get("top_bear"):
            lines += f"  🔴 {s['top_bear'][0][:80]}\n"
        return lines


if __name__ == "__main__":
    ns = NewsSentiment()
    s  = ns.get_sentiment()
    print(f"\n📰 News Sentiment — Nemesis\n")
    print(f"  Signal   : {s['emoji']} {s['signal']}")
    print(f"  Score    : {s['score']:+.2f}")
    print(f"  Articles : {s['articles']}")
    if s.get("top_bull"):
        print(f"\n  🟢 {s['top_bull'][0][:90]}")
    if s.get("top_bear"):
        print(f"  🔴 {s['top_bear'][0][:90]}")
    print()
