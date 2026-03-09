"""
brokers/binance_futures.py — Client Binance Futures USDT-Margined.

Trade les mêmes actifs que le spot en LONG et SHORT :
  ✅ ETH/USDT  — Ethereum perpétuel
  ✅ XRP/USDT  — XRP perpétuel
  ✅ ADA/USDT  — Cardano perpétuel
  ✅ DOGE/USDT — Dogecoin perpétuel
  Levier ×1 uniquement — zéro risque de liquidation

Variables d'environnement :
  BINANCE_FUTURES_API_KEY  — Clé API futures mainnet
  BINANCE_FUTURES_SECRET   — Secret API futures mainnet
  BINANCE_FUTURES_TESTNET  — "true" pour testnet (défaut: false)
"""

import os, ccxt
from loguru import logger
from typing import Optional

# ─── Instruments — mêmes actifs que le bot spot ──────────────────────────────
FUTURES_INSTRUMENTS = [
    "ETH/USDT:USDT",
    "XRP/USDT:USDT",
    "ADA/USDT:USDT",
    "DOGE/USDT:USDT",
]

INSTRUMENT_NAMES = {
    "ETH/USDT:USDT":  "Ethereum",
    "XRP/USDT:USDT":  "XRP",
    "ADA/USDT:USDT":  "Cardano",
    "DOGE/USDT:USDT": "Dogecoin",
}

LEVERAGE = 1  # ×1 uniquement — équivalent spot, aucun risque de liquidation


class BinanceFuturesClient:
    """
    Client USDT-Margined Futures pour Binance.
    Gère les ordres, positions et données OHLCV sur le marché futures.
    """

    def __init__(self):
        api_key = os.getenv("BINANCE_FUTURES_API_KEY", "")
        secret  = os.getenv("BINANCE_FUTURES_SECRET", "")
        demo    = os.getenv("BINANCE_FUTURES_TESTNET", "true").lower() == "true"

        self.available = bool(api_key and secret)
        self._demo     = demo

        if not self.available:
            logger.info("ℹ️  Binance Futures non configuré (BINANCE_FUTURES_API_KEY manquant)")
            return

        # URL de base selon mode demo ou live
        if demo:
            fapi_base = "https://demo-fapi.binance.com"
            logger.info("🧪 Binance Futures Demo Trading (0 vrai argent — 5000 USDT virtuels)")
        else:
            fapi_base = "https://fapi.binance.com"
            logger.info("🔴 Binance Futures LIVE")

        self.exchange = ccxt.binance({
            "apiKey": api_key,
            "secret": secret,
            "options": {
                "defaultType": "future",
                "adjustForTimeDifference": True,
            },
            "enableRateLimit": True,
            "urls": {
                "api": {
                    "fapiPublic":   f"{fapi_base}/fapi/v1",
                    "fapiPrivate":  f"{fapi_base}/fapi/v1",
                    "fapiPublicV2": f"{fapi_base}/fapi/v2",
                    "fapiPrivateV2": f"{fapi_base}/fapi/v2",
                }
            },
        })

    def get_balance(self) -> float:
        """Retourne le solde USDT disponible dans le wallet futures."""
        if not self.available:
            return 0.0
        try:
            bal = self.exchange.fetch_balance()
            return float(bal.get("USDT", {}).get("free", 0))
        except Exception as e:
            logger.error(f"❌ Futures balance: {e}")
            return 0.0

    def fetch_ohlcv(self, instrument: str, timeframe: str = "5m", count: int = 300):
        """Retourne un DataFrame OHLCV depuis Binance Futures."""
        if not self.available:
            return None
        try:
            import pandas as pd
            tf_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h"}
            ohlcv  = self.exchange.fetch_ohlcv(instrument, tf_map.get(timeframe, "5m"), limit=count)
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            return df.drop("timestamp", axis=1)
        except Exception as e:
            logger.error(f"❌ Futures OHLCV {instrument}: {e}")
            return None

    def set_leverage(self, instrument: str, leverage: int = LEVERAGE):
        """Configure le levier pour un instrument (×1 par défaut)."""
        if not self.available:
            return
        try:
            sym = instrument.replace(":USDT", "").replace("/", "")
            self.exchange.fapiPrivate_post_leverage({
                "symbol": sym,
                "leverage": leverage,
            })
            logger.debug(f"⚙️  Levier {instrument} → ×{leverage}")
        except Exception as e:
            logger.warning(f"⚠️  set_leverage {instrument}: {e}")

    def place_market_order(
        self,
        instrument: str,
        side: str,          # "BUY" ou "SELL"
        qty: float,         # Quantité en unités de l'actif
        sl_price: float,
        tp_price: float,
    ) -> Optional[str]:
        """
        Place un ordre market avec SL et TP natifs via Binance Futures.
        Retourne le trade_id si succès.
        """
        if not self.available:
            return None
        try:
            self.set_leverage(instrument, LEVERAGE)

            bs = side.upper()  # "BUY" ou "SELL"
            opp = "SELL" if bs == "BUY" else "BUY"  # Côté pour SL/TP

            # Ordre principal
            order = self.exchange.create_order(
                symbol=instrument,
                type="MARKET",
                side=bs,
                amount=qty,
            )
            trade_id = str(order.get("id", ""))
            logger.info(f"📈 Futures {bs} {instrument} qty={qty:.4f} → ID {trade_id}")

            # Stop Loss (ordre opposé stop-market)
            try:
                sl_side   = "stopMarket"
                self.exchange.create_order(
                    symbol=instrument, type="STOP_MARKET",
                    side=opp, amount=qty,
                    params={"stopPrice": sl_price, "reduceOnly": True},
                )
            except Exception as e:
                logger.warning(f"⚠️  SL futures: {e}")

            # Take Profit (ordre opposé take-profit-market)
            try:
                self.exchange.create_order(
                    symbol=instrument, type="TAKE_PROFIT_MARKET",
                    side=opp, amount=qty,
                    params={"stopPrice": tp_price, "reduceOnly": True},
                )
            except Exception as e:
                logger.warning(f"⚠️  TP futures: {e}")

            return trade_id

        except Exception as e:
            logger.error(f"❌ Futures order {instrument}: {e}")
            return None

    def close_position(self, instrument: str) -> bool:
        """Ferme la position ouverte sur un instrument."""
        if not self.available:
            return False
        try:
            positions = self.exchange.fetch_positions([instrument])
            for pos in positions:
                amt = float(pos.get("contracts", 0))
                if abs(amt) > 0:
                    side = "SELL" if amt > 0 else "BUY"
                    self.exchange.create_order(
                        instrument, "MARKET", side, abs(amt),
                        params={"reduceOnly": True}
                    )
                    logger.info(f"✅ Position {instrument} fermée")
                    return True
        except Exception as e:
            logger.error(f"❌ Close futures {instrument}: {e}")
        return False

    def position_size_qty(
        self,
        balance: float,
        risk_pct: float,
        entry: float,
        sl: float,
        instrument: str,
    ) -> float:
        """
        Calcule la quantité à acheter pour risquer risk_pct% du capital.
        Formule : qty = (balance × risk%) / |entry - sl|
        """
        sl_dist = abs(entry - sl)
        if sl_dist <= 0 or entry <= 0:
            return 0.0
        risk_amt = balance * risk_pct
        qty = risk_amt / sl_dist
        # Arrondi selon la précision de l'instrument
        precision = 3 if "XAU" in instrument else 3
        return round(qty, precision)
