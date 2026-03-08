"""
orderbook_imbalance.py — Order Book Imbalance (#6)

Analyse le carnet d'ordres Binance pour détecter une pression
acheteur ou vendeur dominante.

Méthode :
  - Fetch les 10 meilleurs niveaux d'achat et de vente
  - Calcule : OBI = (bid_volume - ask_volume) / (bid_volume + ask_volume)
  - OBI > 0.3  → forte pression acheteuse → confirme BUY
  - OBI < -0.3 → forte pression vendeuse  → confirme SELL
  - Entre -0.3 et 0.3 → neutre

Usage dans main.py : filtre additionnel sur les entrées.
"""
import sys, time, requests, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")
from loguru import logger

BINANCE_DEPTH_URL = "https://api.binance.com/api/v3/depth"
CACHE_TTL         = 10   # 10 secondes (orderbook varie vite)
OBI_THRESHOLD     = 0.25  # Seuil pour confirmer la direction


class OrderBookImbalance:

    def __init__(self):
        self._cache    = {}
        self._cache_ts = {}

    def _to_binance_symbol(self, symbol: str) -> str:
        return symbol.replace("/", "").replace(":USDT", "")

    def fetch_obi(self, symbol: str, depth: int = 20) -> float:
        """
        Calcule l'Order Book Imbalance (OBI) pour un symbole.
        OBI ∈ [-1, 1] : positif = pression acheteuse, négatif = vendeuse
        """
        now = time.time()
        key = symbol
        if key in self._cache and (now - self._cache_ts.get(key, 0)) < CACHE_TTL:
            return self._cache[key]

        try:
            bsym = self._to_binance_symbol(symbol)
            r    = requests.get(
                BINANCE_DEPTH_URL,
                params={"symbol": bsym, "limit": depth},
                timeout=5,
            )
            data = r.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])

            bid_vol = sum(float(b[1]) for b in bids[:10])
            ask_vol = sum(float(a[1]) for a in asks[:10])
            total   = bid_vol + ask_vol

            obi = (bid_vol - ask_vol) / total if total > 0 else 0.0

            self._cache[key]    = obi
            self._cache_ts[key] = now

            direction = "🟢 Pression acheteuse" if obi > 0.1 else "🔴 Pression vendeuse" if obi < -0.1 else "⚪ Neutre"
            logger.debug(f"📗 OBI {symbol}: {obi:+.3f} → {direction}")
            return obi

        except Exception as e:
            logger.debug(f"OBI fetch {symbol}: {e}")
            return 0.0

    def confirms_signal(self, symbol: str, signal: str) -> bool:
        """
        Vérifie si l'orderbook confirme le signal.
        Ne bloque pas si OBI est neutre (laisse passer).
        """
        obi = self.fetch_obi(symbol)

        if signal == "BUY":
            if obi < -OBI_THRESHOLD:
                logger.warning(f"📗 OBI {symbol} = {obi:+.3f} → forte pression vendeuse, BUY prudent")
                return False  # Carnet dominé par les vendeurs → rejeter BUY
            return True

        if signal == "SELL":
            if obi > OBI_THRESHOLD:
                logger.warning(f"📗 OBI {symbol} = {obi:+.3f} → forte pression acheteuse, SELL prudent")
                return False  # Carnet dominé par les acheteurs → rejeter SELL
            return True

        return True

    def get_all(self, symbols: list) -> dict:
        return {s: self.fetch_obi(s) for s in symbols}


if __name__ == "__main__":
    from config import SYMBOLS
    obi = OrderBookImbalance()
    print(f"\n📗 Order Book Imbalance — AlphaTrader\n")
    for sym in SYMBOLS:
        score = obi.fetch_obi(sym)
        bar   = "🟢" if score > 0.2 else "🔴" if score < -0.2 else "⚪"
        print(f"  {bar} {sym:<14} OBI : {score:+.4f}")
    print()
