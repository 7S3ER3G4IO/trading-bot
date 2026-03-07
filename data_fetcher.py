"""
data_fetcher.py — Récupère les données de marché OHLCV depuis Binance Testnet.
"""
import os
import ccxt
import pandas as pd
from loguru import logger
from dotenv import load_dotenv
from config import SYMBOL, TIMEFRAME, LIMIT

load_dotenv()


class DataFetcher:
    """Connexion à Binance Testnet et récupération des données OHLCV."""

    def __init__(self):
        api_key = os.getenv("BINANCE_API_KEY")
        secret  = os.getenv("BINANCE_SECRET")

        if not api_key or not secret:
            raise EnvironmentError(
                "❌ BINANCE_API_KEY et BINANCE_SECRET manquants dans le fichier .env"
            )

        self.exchange = ccxt.binance({
            "apiKey": api_key,
            "secret": secret,
            "options": {
                "defaultType": "spot",
                # Force sandbox testnet endpoints explicitly
                "urls": {
                    "api": {
                        "public": "https://testnet.binance.vision/api/v3",
                        "private": "https://testnet.binance.vision/api/v3",
                        "v1": "https://testnet.binance.vision/api/v1",
                    },
                    "test": {
                        "public": "https://testnet.binance.vision/api/v3",
                        "private": "https://testnet.binance.vision/api/v3",
                    }
                }
            },
            "enableRateLimit": True,
            "headers": {
                "User-Agent": "Mozilla/5.0 (compatible; tradingbot/1.0)"
            }
        })
        # Active le mode Testnet (sandbox)
        self.exchange.set_sandbox_mode(True)
        logger.info(f"✅ Connecté à Binance Testnet — Marché : {SYMBOL} | TF : {TIMEFRAME}")

    def get_ohlcv(self, symbol: str = SYMBOL, timeframe: str = TIMEFRAME, limit: int = LIMIT) -> pd.DataFrame:
        """
        Retourne un DataFrame OHLCV avec colonnes :
        timestamp, open, high, low, close, volume
        """
        try:
            raw = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            logger.debug(f"📊 OHLCV chargé : {len(df)} bougies")
            return df
        except Exception as e:
            logger.error(f"❌ Erreur fetch_ohlcv : {e}")
            raise

    def get_balance(self) -> dict:
        """Retourne le solde USDT disponible sur le compte testnet."""
        try:
            balance = self.exchange.fetch_balance()
            usdt = balance.get("USDT", {})
            result = {
                "free":  usdt.get("free",  0.0),
                "used":  usdt.get("used",  0.0),
                "total": usdt.get("total", 0.0),
            }
            logger.info(f"💰 Solde USDT — Libre: {result['free']:.2f} | Utilisé: {result['used']:.2f}")
            return result
        except Exception as e:
            logger.error(f"❌ Erreur fetch_balance : {e}")
            raise

    def get_ticker(self, symbol: str = SYMBOL) -> dict:
        """Retourne le prix actuel du marché."""
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return {"last": ticker["last"], "bid": ticker["bid"], "ask": ticker["ask"]}
        except Exception as e:
            logger.error(f"❌ Erreur fetch_ticker : {e}")
            raise
