"""
order_executor.py — Passe les ordres sur Binance Testnet.
Gère les ordres Market, Stop-Loss et Take-Profit.
"""
import os
from typing import Optional
import ccxt
from loguru import logger
from dotenv import load_dotenv
from config import SYMBOL

load_dotenv()


class OrderExecutor:
    """Interface d'exécution des ordres sur Binance Testnet."""

    def __init__(self):
        api_key = os.getenv("BINANCE_API_KEY")
        secret  = os.getenv("BINANCE_SECRET")

        self.exchange = ccxt.binance({
            "apiKey": api_key,
            "secret": secret,
            "options": {
                "defaultType": "spot",
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
        self.exchange.set_sandbox_mode(True)
        logger.info("🔧 OrderExecutor connecté à Binance Testnet")

    def buy_market(self, symbol: str, amount: float) -> Optional[dict]:
        """
        Passe un ordre d'achat au prix du marché.
        
        Args:
            symbol: ex. "BTC/USDT"
            amount: quantité en BTC à acheter
        Returns:
            Détails de l'ordre ou None en cas d'erreur
        """
        try:
            # Arrondir à 5 décimales (précision BTC Binance)
            amount = round(amount, 5)
            if amount <= 0:
                logger.warning("⚠️  Taille d'ordre nulle ou négative — ordre annulé.")
                return None

            order = self.exchange.create_market_buy_order(symbol, amount)
            logger.info(
                f"✅ ACHAT exécuté | {symbol} | "
                f"Qté: {amount} | ID: {order['id']}"
            )
            return order
        except ccxt.InsufficientFunds:
            logger.error("❌ Fonds insuffisants pour cet achat.")
            return None
        except Exception as e:
            logger.error(f"❌ Erreur buy_market : {e}")
            return None

    def sell_market(self, symbol: str, amount: float) -> Optional[dict]:
        """
        Passe un ordre de vente au prix du marché.
        
        Args:
            symbol: ex. "BTC/USDT"
            amount: quantité en BTC à vendre
        Returns:
            Détails de l'ordre ou None en cas d'erreur
        """
        try:
            amount = round(amount, 5)
            if amount <= 0:
                logger.warning("⚠️  Taille d'ordre nulle ou négative — ordre annulé.")
                return None

            order = self.exchange.create_market_sell_order(symbol, amount)
            logger.info(
                f"✅ VENTE exécutée | {symbol} | "
                f"Qté: {amount} | ID: {order['id']}"
            )
            return order
        except ccxt.InsufficientFunds:
            logger.error("❌ Fonds insuffisants pour cette vente.")
            return None
        except Exception as e:
            logger.error(f"❌ Erreur sell_market : {e}")
            return None

    def get_open_orders(self, symbol: str = SYMBOL) -> list:
        """Retourne la liste des ordres ouverts."""
        try:
            return self.exchange.fetch_open_orders(symbol)
        except Exception as e:
            logger.error(f"❌ Erreur fetch_open_orders : {e}")
            return []

    def cancel_all_orders(self, symbol: str = SYMBOL):
        """Annule tous les ordres ouverts pour un symbole."""
        try:
            orders = self.get_open_orders(symbol)
            for order in orders:
                self.exchange.cancel_order(order["id"], symbol)
                logger.info(f"🗑️  Ordre annulé : {order['id']}")
        except Exception as e:
            logger.error(f"❌ Erreur cancel_all_orders : {e}")

    def get_position(self, base_currency: str = "BTC") -> float:
        """Retourne la quantité détenue du crypto de base (ex: BTC)."""
        try:
            balance = self.exchange.fetch_balance()
            return balance.get(base_currency, {}).get("free", 0.0)
        except Exception as e:
            logger.error(f"❌ Erreur get_position : {e}")
            return 0.0
