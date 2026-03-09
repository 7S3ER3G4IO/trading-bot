"""
market_sentiment.py — Fear & Greed Index (#1)

Récupère le Crypto Fear & Greed Index depuis alternative.me (API gratuite).
Utilisé comme filtre global : évite d'entrer en trade dans les extrêmes.

Logique :
  0-20  → Peur Extrême   → SHORT uniquement (ne pas acheter)
  21-40 → Peur           → Prudent, réduire taille
  41-60 → Neutre         → Trading normal
  61-80 → Avidité        → Prudent, réduire taille
  81-100 → Avidité Extrême → LONG uniquement (ne pas shorter)
"""
import json, time, requests
from loguru import logger

FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1&format=json"
CACHE_TTL      = 900   # 15 minutes


class MarketSentiment:

    def __init__(self):
        self._cache = None
        self._cache_ts = 0

    def get_fear_greed(self) -> dict:
        """
        Retourne le Fear & Greed Index actuel.
        Structure : {"value": int, "label": str, "emoji": str}
        """
        now = time.time()
        if self._cache and (now - self._cache_ts) < CACHE_TTL:
            return self._cache

        try:
            r = requests.get(FEAR_GREED_URL, timeout=8)
            data = r.json()["data"][0]
            value = int(data["value"])
            label = data["value_classification"]

            if value <= 20:
                emoji = "😱"
                category = "EXTREME_FEAR"
            elif value <= 40:
                emoji = "😨"
                category = "FEAR"
            elif value <= 60:
                emoji = "😐"
                category = "NEUTRAL"
            elif value <= 80:
                emoji = "🤑"
                category = "GREED"
            else:
                emoji = "🚨"
                category = "EXTREME_GREED"

            result = {
                "value":    value,
                "label":    label,
                "emoji":    emoji,
                "category": category,
            }
            self._cache    = result
            self._cache_ts = now
            logger.info(f"📊 Fear & Greed : {value}/100 {emoji} ({label})")
            return result

        except Exception as e:
            logger.warning(f"⚠️  Fear & Greed unavailable: {e}")
            return {"value": 50, "label": "Neutral", "emoji": "😐", "category": "NEUTRAL"}

    def should_allow_long(self) -> bool:
        """Bloquer les achats en avidité extrême (>80) ET en peur extrême (<20)."""
        fg = self.get_fear_greed()
        if fg["category"] == "EXTREME_GREED":
            logger.warning(f"🚨 Fear & Greed {fg['value']}/100 — LONGS bloqués (extrême avidité)")
            return False
        if fg["category"] == "EXTREME_FEAR":
            logger.warning(f"😱 Fear & Greed {fg['value']}/100 — LONGS bloqués (extrême peur, marché baissier)")
            return False
        return True

    def should_allow_short(self) -> bool:
        """Autoriser les shorts en peur extrême — bloquer en avidité extrême."""
        fg = self.get_fear_greed()
        if fg["category"] == "EXTREME_GREED":
            logger.warning(f"🚨 Fear & Greed {fg['value']}/100 — SHORTS bloqués (extrême avidité)")
            return False
        # En EXTREME_FEAR, les shorts sont au contraire FAVORISÉS (trend)
        return True

    def extreme_fear_bonus(self, signal: str) -> int:
        """
        Retourne +1 si le signal SELL est confirmé par Extreme Fear (<20).
        Le marché descend → shorter dans le sens du marché = bonus.
        """
        fg = self.get_fear_greed()
        if fg["category"] == "EXTREME_FEAR" and signal == "SELL":
            return +1
        return 0

    def position_scale(self) -> float:
        """
        Facteur multiplicateur de la taille de position selon sentiment.
        Neutre = 1.0, Extrêmes = 0.5
        """
        fg = self.get_fear_greed()
        v  = fg["value"]
        if 40 <= v <= 60:
            return 1.0     # Neutre → taille normale
        elif 25 <= v < 40 or 60 < v <= 75:
            return 0.75    # Légère peur/avidité → réduire
        else:
            return 0.5     # Extrêmes → demi-taille


if __name__ == "__main__":
    ms = MarketSentiment()
    fg = ms.get_fear_greed()
    print(f"\n📊 Fear & Greed Index : {fg['value']}/100 {fg['emoji']}")
    print(f"   Catégorie : {fg['label']}")
    print(f"   Longs autorisés  : {ms.should_allow_long()}")
    print(f"   Shorts autorisés : {ms.should_allow_short()}")
    print(f"   Facteur taille   : {ms.position_scale():.2f}x\n")
