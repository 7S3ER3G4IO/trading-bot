"""
onchain_data.py — On-Chain Data (#7)

Récupère des données on-chain depuis des APIs publiques gratuites.

Sources utilisées (sans clé API) :
  - Alternative.me : Fear & Greed, global market cap
  - CoinGecko free API : BTC dominance, volume global
  - Blockchain.info : BTC transaction count, exchange flows approximés

Indicateurs générés :
  - BTC Dominance : >60% = altcoins faibles, <40% = altseason
  - Market Cap Change 24h : direction globale du marché
  - BTC Exchange Inflow niveau : pression vendeuse
"""
import time, requests, json
from loguru import logger

COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"
FEAR_GREED_URL   = "https://api.alternative.me/fng/?limit=7&format=json"   # 7 jours
BLOCKCHAIN_INFO  = "https://api.blockchain.info/stats"
CACHE_TTL        = 600  # 10 minutes


class OnChainData:

    def __init__(self):
        self._cache    = {}
        self._cache_ts = {}

    def _cached(self, key: str, fetcher, ttl: int = CACHE_TTL):
        now = time.time()
        if key in self._cache and (now - self._cache_ts.get(key, 0)) < ttl:
            return self._cache[key]
        try:
            result = fetcher()
            self._cache[key]    = result
            self._cache_ts[key] = now
            return result
        except Exception as e:
            logger.debug(f"OnChain {key} error: {e}")
            return self._cache.get(key, {})

    def get_global_market(self) -> dict:
        """BTC dominance, market cap total, volume total."""
        def fetch():
            r    = requests.get(COINGECKO_GLOBAL, timeout=8)
            data = r.json()["data"]
            return {
                "btc_dominance":     round(float(data["market_cap_percentage"]["btc"]), 1),
                "total_market_cap":  float(data["total_market_cap"]["usd"]),
                "total_volume_24h":  float(data["total_volume"]["usd"]),
                "market_cap_change": round(float(data["market_cap_change_percentage_24h_usd"]), 2),
            }
        return self._cached("global", fetch)

    def get_fear_greed_history(self) -> list:
        """7 derniers jours du Fear & Greed Index."""
        def fetch():
            r    = requests.get(FEAR_GREED_URL, timeout=8)
            data = r.json()["data"]
            return [{"date": d["timestamp"], "value": int(d["value"]),
                     "label": d["value_classification"]} for d in data]
        return self._cached("fg_history", fetch)

    def get_btc_network(self) -> dict:
        """BTC transactions/jour — activité réseau global."""
        def fetch():
            r    = requests.get(BLOCKCHAIN_INFO, timeout=8)
            data = r.json()
            return {
                "n_tx_day":        int(data.get("n_tx", 0)),
                "btc_sent_24h":    float(data.get("total_btc_sent", 0)) / 1e8,
                "hash_rate":       float(data.get("hash_rate", 0)),
                "mempool_size":    int(data.get("mempool_size", 0)),
            }
        return self._cached("btc_network", fetch)

    def get_signal(self) -> dict:
        """
        Génère un signal composite on-chain.
        BULLISH / BEARISH / NEUTRAL + détails
        """
        gm  = self.get_global_market()
        btc = self.get_btc_network()
        fg  = self.get_fear_greed_history()

        score = 0
        details = []

        # BTC dominance
        btc_dom = gm.get("btc_dominance", 50)
        if btc_dom > 60:
            score -= 1
            details.append(f"BTC Dom {btc_dom}% → altcoins risqués")
        elif btc_dom < 45:
            score += 1
            details.append(f"BTC Dom {btc_dom}% → altseason possible")
        else:
            details.append(f"BTC Dom {btc_dom}% → neutre")

        # Market cap change
        mc_change = gm.get("market_cap_change", 0)
        if mc_change > 2:
            score += 1
            details.append(f"Market Cap +{mc_change}% → momentum haussier")
        elif mc_change < -2:
            score -= 1
            details.append(f"Market Cap {mc_change}% → momentum baissier")

        # Fear & Greed trend
        if fg and len(fg) >= 3:
            recent_avg = sum(d["value"] for d in fg[:3]) / 3
            if recent_avg > 70:
                score -= 1
                details.append(f"F&G moyen 3j = {recent_avg:.0f} → avidité")
            elif recent_avg < 30:
                score += 1
                details.append(f"F&G moyen 3j = {recent_avg:.0f} → peur → rebond possible")

        # Signal final
        if score >= 2:
            signal = "BULLISH 🟢"
        elif score <= -2:
            signal = "BEARISH 🔴"
        else:
            signal = "NEUTRAL ⚪"

        return {
            "signal":      signal,
            "score":       score,
            "btc_dom":     btc_dom,
            "mc_change":   mc_change,
            "details":     details,
        }


if __name__ == "__main__":
    oc = OnChainData()
    print(f"\n⛓  On-Chain Data — AlphaTrader\n")

    gm = oc.get_global_market()
    print(f"  BTC Dominance   : {gm.get('btc_dominance', '?')}%")
    print(f"  Market Cap 24h  : {gm.get('market_cap_change', 0):+.2f}%")
    print(f"  Volume 24h      : {gm.get('total_volume_24h', 0)/1e9:.1f}B $")

    btc = oc.get_btc_network()
    if btc:
        print(f"\n  BTC Network")
        print(f"  Transactions/j  : {btc.get('n_tx_day', 0):,}")

    sig = oc.get_signal()
    print(f"\n  Signal On-Chain : {sig['signal']} (score={sig['score']})")
    for d in sig["details"]:
        print(f"  • {d}")
    print()
