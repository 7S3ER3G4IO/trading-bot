"""
funding_rate.py — Funding Rate Filter (#2)

Récupère le funding rate des perp futures Binance.
Filtre les trades dans le sens du financement excessif.

Règles :
  Funding Rate > +0.10% → marché suracheté → BLOQUER les LONGS
  Funding Rate < -0.10% → marché survendu  → BLOQUER les SHORTS
  Entre -0.10% et +0.10% → neutre → trading normal

Le funding rate est mis à jour toutes les 8h sur Binance.
"""
import time, requests, ccxt
from loguru import logger

FUNDING_URL   = "https://fapi.binance.com/fapi/v1/premiumIndex"
CACHE_TTL     = 300   # 5 minutes
LONG_BLOCK    = 0.001   # +0.10% → bloquer longs
SHORT_BLOCK   = -0.001  # -0.10% → bloquer shorts


class FundingRateFilter:

    def __init__(self):
        self._cache    = {}
        self._cache_ts = {}

    def get_funding_rate(self, symbol: str) -> float:
        """
        Retourne le funding rate actuel pour un symbole.
        symbol : 'ETH/USDT' → traduit en 'ETHUSDT'
        """
        now     = time.time()
        fsymbol = symbol.replace("/", "").replace(":USDT", "")

        if fsymbol in self._cache and (now - self._cache_ts.get(fsymbol, 0)) < CACHE_TTL:
            return self._cache[fsymbol]

        try:
            url = f"{FUNDING_URL}?symbol={fsymbol}"
            r   = requests.get(url, timeout=6)
            if r.status_code == 200:
                data = r.json()
                rate = float(data.get("lastFundingRate", 0))
                self._cache[fsymbol]    = rate
                self._cache_ts[fsymbol] = now
                direction = "🔼" if rate > 0 else "🔽"
                logger.info(f"💸 Funding {symbol} : {direction} {rate*100:.4f}%")
                return rate
            return 0.0
        except Exception as e:
            logger.debug(f"Funding rate {symbol} unavailable: {e}")
            return 0.0

    def should_allow_long(self, symbol: str) -> bool:
        """Bloquer un long si funding rate trop positif (marché suracheté)."""
        rate = self.get_funding_rate(symbol)
        if rate > LONG_BLOCK:
            logger.warning(
                f"💸 Funding {symbol} = {rate*100:.4f}% > {LONG_BLOCK*100:.4f}% "
                f"— LONG bloqué (suracheté sur futures)"
            )
            return False
        return True

    def should_allow_short(self, symbol: str) -> bool:
        """Bloquer un short si funding rate trop négatif (marché survendu)."""
        rate = self.get_funding_rate(symbol)
        if rate < SHORT_BLOCK:
            logger.warning(
                f"💸 Funding {symbol} = {rate*100:.4f}% < {SHORT_BLOCK*100:.4f}% "
                f"— SHORT bloqué (survendu sur futures)"
            )
            return False
        return True

    def get_all_rates(self, symbols: list) -> dict:
        """Retourne les funding rates de tous les symboles."""
        result = {}
        for sym in symbols:
            result[sym] = self.get_funding_rate(sym)
        return result


if __name__ == "__main__":
    from config import SYMBOLS
    ff = FundingRateFilter()
    print(f"\n💸 Funding Rate Filter — Nemesis\n")
    for sym in SYMBOLS:
        rate  = ff.get_funding_rate(sym)
        long_ok  = ff.should_allow_long(sym)
        short_ok = ff.should_allow_short(sym)
        bar  = "🟢" if abs(rate) < LONG_BLOCK else "🔴"
        print(f"  {bar} {sym:<14} Funding: {rate*100:+.4f}%  |  Long: {'✅' if long_ok else '❌'}  Short: {'✅' if short_ok else '❌'}")
    print()
