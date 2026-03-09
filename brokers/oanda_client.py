"""
brokers/oanda_client.py — Client OANDA Practice pour Forex, Gold et Indices.

Marchés supportés (décorrélés du crypto bear market) :
  XAU_USD    → Or (Gold)       — valeur refuge, trend fort
  EUR_USD    → Euro/Dollar     — le plus liquide au monde
  GBP_USD    → Livre/Dollar    — très volatile, bon ATR
  USD_JPY    → Dollar/Yen      — tendances longues
  SPX500_USD → S&P 500         → indice US
  NAS100_USD → Nasdaq 100      — tech US
  US30_USD   → Dow Jones       — indice US
  DE30_EUR   → DAX / CAC40 ≈  — indice européen
  BCO_USD    → Brent Oil       — matière première

Prérequis :
  - Compte OANDA Practice gratuit : https://www.oanda.com/register/
  - Variables Railway : OANDA_API_KEY + OANDA_ACCOUNT_ID
  - pip install oandapyV20
"""
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import pandas as pd
from loguru import logger

try:
    import oandapyV20
    import oandapyV20.endpoints.instruments as instruments
    import oandapyV20.endpoints.orders as orders
    import oandapyV20.endpoints.accounts as accounts
    import oandapyV20.endpoints.positions as positions
    HAS_OANDA = True
except ImportError:
    HAS_OANDA = False
    logger.warning("⚠️  oandapyV20 non installé — broker OANDA désactivé")

# ─── Configuration ────────────────────────────────────────────────────────────
OANDA_API_KEY    = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_PRACTICE   = os.getenv("OANDA_PRACTICE", "true").lower() == "true"

# Correspondance timeframe → format OANDA
TF_MAP = {
    "1m":  "M1",  "5m":  "M5",  "15m": "M15",
    "30m": "M30", "1h":  "H1",  "4h":  "H4",
    "1d":  "D",
}

# ─── Instruments OANDA (décorrélés du crypto) ────────────────────────────────
OANDA_INSTRUMENTS = [
    "XAU_USD",    # Or          — valeur refuge, trend fort
    "EUR_USD",    # Euro/Dollar — le plus liquide
    "GBP_USD",    # Livre/Dollar
    "USD_JPY",    # Dollar/Yen
    "SPX500_USD", # S&P 500
    "NAS100_USD", # Nasdaq 100
    "US30_USD",   # Dow Jones
    "DE30_EUR",   # DAX (≈ indice européen)
    "BCO_USD",    # Brent Oil
]

# Taille de pip par instrument (pour affichage lisible)
PIP_FACTOR = {
    "XAU_USD": 0.01,   # Centième de dollar
    "EUR_USD": 0.0001, "GBP_USD": 0.0001, "USD_JPY": 0.01,
    "SPX500_USD": 1.0, "NAS100_USD": 1.0, "US30_USD": 1.0,
    "DE30_EUR": 1.0, "BCO_USD": 0.01,
}


class OandaClient:
    """Client OANDA Practice — fetch data + exécute ordres."""

    def __init__(self):
        if not HAS_OANDA:
            self._api = None
            return
        if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
            logger.debug("ℹ️  OANDA_API_KEY ou OANDA_ACCOUNT_ID manquants — broker OANDA désactivé")
            self._api = None
            return

        env = "practice" if OANDA_PRACTICE else "live"
        self._api        = oandapyV20.API(access_token=OANDA_API_KEY, environment=env)
        self._account_id = OANDA_ACCOUNT_ID
        logger.info(f"🏦 OANDA connecté ({'Practice' if OANDA_PRACTICE else 'LIVE'}) ✅")

    @property
    def available(self) -> bool:
        return self._api is not None

    # ─── Données de marché ────────────────────────────────────────────────────

    def fetch_ohlcv(self, instrument: str, timeframe: str = "15m",
                    count: int = 250) -> Optional[pd.DataFrame]:
        """Télécharge les bougies OANDA et retourne un DataFrame OHLCV."""
        if not self.available:
            return None
        try:
            gran = TF_MAP.get(timeframe, "M15")
            params = {"count": count, "granularity": gran, "price": "M"}  # Mid price
            r      = instruments.InstrumentsCandles(instrument, params=params)
            self._api.request(r)
            candles = r.response.get("candles", [])
            if not candles:
                return None

            records = []
            for c in candles:
                if not c.get("complete", True):
                    continue
                mid = c["mid"]
                records.append({
                    "timestamp": pd.Timestamp(c["time"]).tz_convert("UTC"),
                    "open":   float(mid["o"]),
                    "high":   float(mid["h"]),
                    "low":    float(mid["l"]),
                    "close":  float(mid["c"]),
                    "volume": float(c.get("volume", 0)),
                })

            if not records:
                return None

            df = pd.DataFrame(records)
            df.set_index("timestamp", inplace=True)
            return df

        except Exception as e:
            logger.error(f"❌ OANDA fetch_ohlcv {instrument}: {e}")
            return None

    def fetch_htf(self, instrument: str, timeframe: str = "1h",
                  count: int = 50) -> Optional[pd.DataFrame]:
        """Fetch Higher TimeFrame pour confirmation de tendance."""
        return self.fetch_ohlcv(instrument, timeframe, count)

    def get_balance(self) -> float:
        """Retourne le solde USDT/USD du compte OANDA."""
        if not self.available:
            return 0.0
        try:
            r = accounts.AccountSummary(self._account_id)
            self._api.request(r)
            return float(r.response["account"]["balance"])
        except Exception as e:
            logger.error(f"❌ OANDA get_balance: {e}")
            return 0.0

    # ─── Ordres ──────────────────────────────────────────────────────────────

    def place_market_order(self, instrument: str, units: float,
                           sl_price: float, tp_price: float) -> Optional[str]:
        """
        Passe un ordre marché OANDA avec SL et TP.
        units > 0 = BUY (long), units < 0 = SELL (short)
        Retourne l'ID de trade ou None si erreur.
        """
        if not self.available:
            return None
        try:
            data = {
                "order": {
                    "type":        "MARKET",
                    "instrument":  instrument,
                    "units":       str(int(units)),
                    "timeInForce": "FOK",
                    "stopLossOnFill": {
                        "price": f"{sl_price:.5f}",
                        "timeInForce": "GTC"
                    },
                    "takeProfitOnFill": {
                        "price": f"{tp_price:.5f}",
                        "timeInForce": "GTC"
                    },
                }
            }
            r = orders.OrderCreate(self._account_id, data=data)
            self._api.request(r)
            trade_id = r.response.get("orderFillTransaction", {}).get("tradeOpened", {}).get("tradeID")
            logger.info(f"✅ OANDA {instrument} {'BUY' if units > 0 else 'SELL'} {abs(units):.0f} units | TradeID={trade_id}")
            return str(trade_id) if trade_id else None
        except Exception as e:
            logger.error(f"❌ OANDA place_order {instrument}: {e}")
            return None

    def close_position(self, instrument: str) -> bool:
        """Ferme toutes les positions ouvertes sur un instrument."""
        if not self.available:
            return False
        try:
            data = {"longUnits": "ALL", "shortUnits": "ALL"}
            r = positions.PositionClose(self._account_id, instrument, data=data)
            self._api.request(r)
            logger.info(f"✅ OANDA {instrument} position fermée")
            return True
        except Exception as e:
            logger.error(f"❌ OANDA close_position {instrument}: {e}")
            return False

    def get_open_trades(self) -> List[dict]:
        """Retourne les trades ouverts sur OANDA."""
        if not self.available:
            return []
        try:
            import oandapyV20.endpoints.trades as trades_ep
            r = trades_ep.OpenTrades(self._account_id)
            self._api.request(r)
            return r.response.get("trades", [])
        except Exception as e:
            logger.error(f"❌ OANDA get_open_trades: {e}")
            return []

    # ─── Calcul taille de position en unités ─────────────────────────────────

    def position_size_units(self, balance: float, risk_pct: float,
                            entry: float, sl: float,
                            instrument: str) -> float:
        """
        Calcule la taille en unités OANDA.
        Pour EUR_USD : 1 unité = 1 EUR → SL_dist en USD
        Pour XAU_USD : 1 unité = 1 once → SL_dist en USD/once
        """
        risk_amount = balance * risk_pct
        sl_dist     = abs(entry - sl)
        if sl_dist == 0:
            return 0.0

        # Pour Forex : sl_dist est en quote currency (USD)
        # Pour XAU_USD : sl_dist est directement en USD/once
        # Pour indices : chaque "unité" vaut 1 unité de l'indice
        units = risk_amount / sl_dist

        # Arrondi et minimum
        units = max(1.0, round(units))
        logger.debug(f"  OANDA units {instrument}: {units:.0f} (risque={risk_amount:.2f}$ / SL={sl_dist:.5f})")
        return units
