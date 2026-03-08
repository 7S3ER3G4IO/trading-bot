"""
order_executor.py — Passe les ordres sur Binance (Testnet ou Live).
SL et TP sont de VRAIS ordres Binance — survivent au crash du bot.
"""
import os
from typing import Optional
import ccxt
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"


class OrderExecutor:
    """Exécution des ordres sur Binance avec SL/TP natifs."""

    def __init__(self):
        api_key = os.getenv("BINANCE_API_KEY")
        secret  = os.getenv("BINANCE_SECRET")

        if USE_TESTNET:
            self.exchange = ccxt.binance({
                "apiKey": api_key,
                "secret": secret,
                "options": {
                    "defaultType": "spot",
                    "urls": {
                        "api": {
                            "public":  "https://testnet.binance.vision/api/v3",
                            "private": "https://testnet.binance.vision/api/v3",
                            "v1":      "https://testnet.binance.vision/api/v1",
                        },
                        "test": {
                            "public":  "https://testnet.binance.vision/api/v3",
                            "private": "https://testnet.binance.vision/api/v3",
                        }
                    }
                },
                "enableRateLimit": True,
                "headers": {"User-Agent": "Mozilla/5.0 (compatible; tradingbot/1.0)"}
            })
            self.exchange.set_sandbox_mode(True)
            logger.info("🔧 OrderExecutor → Binance Testnet")
        else:
            self.exchange = ccxt.binance({
                "apiKey": api_key,
                "secret": secret,
                "options": {"defaultType": "spot"},
                "enableRateLimit": True,
            })
            logger.info("🔧 OrderExecutor → Binance LIVE ⚠️")

    # ─── Ordres Market ────────────────────────────────────────────────────────

    def buy_market(self, symbol: str, amount: float) -> Optional[dict]:
        """Achat au marché."""
        try:
            amount = round(amount, 5)
            if amount <= 0:
                logger.warning("⚠️  Taille d'ordre nulle — annulé.")
                return None
            order = self.exchange.create_market_buy_order(symbol, amount)
            logger.info(f"✅ ACHAT marché | {symbol} | Qté: {amount} | ID: {order['id']}")
            return order
        except ccxt.InsufficientFunds:
            logger.error("❌ Fonds insuffisants.")
            return None
        except Exception as e:
            logger.error(f"❌ buy_market : {e}")
            return None

    def sell_market(self, symbol: str, amount: float) -> Optional[dict]:
        """Vente au marché."""
        try:
            amount = round(amount, 5)
            if amount <= 0:
                logger.warning("⚠️  Taille d'ordre nulle — annulé.")
                return None
            order = self.exchange.create_market_sell_order(symbol, amount)
            logger.info(f"✅ VENTE marché | {symbol} | Qté: {amount} | ID: {order['id']}")
            return order
        except ccxt.InsufficientFunds:
            logger.error("❌ Fonds insuffisants.")
            return None
        except Exception as e:
            logger.error(f"❌ sell_market : {e}")
            return None

    # ─── Vrais ordres SL/TP (survivent au crash du bot) ──────────────────────

    def place_stop_loss(self, symbol: str, side: str, amount: float,
                        sl_price: float) -> Optional[str]:
        """
        Place un vrai Stop-Loss sur Binance.
        side = "BUY" → stop sell | "SELL" → stop buy
        Retourne l'order_id ou None.
        """
        try:
            amount    = round(amount, 5)
            sl_price  = round(sl_price, 2)
            # Limit légèrement en dessous du stop pour garantir l'exécution
            limit_off = sl_price * 0.001   # 0.1% de marge
            limit_px  = round(sl_price - limit_off, 2) if side == "BUY" \
                        else round(sl_price + limit_off, 2)

            order_side = "sell" if side == "BUY" else "buy"
            order = self.exchange.create_order(
                symbol=symbol,
                type="STOP_LOSS_LIMIT",
                side=order_side,
                amount=amount,
                price=limit_px,
                params={"stopPrice": sl_price, "timeInForce": "GTC"}
            )
            logger.info(f"🔒 STOP-LOSS placé | {symbol} | stop={sl_price} | ID: {order['id']}")
            return str(order["id"])
        except Exception as e:
            logger.warning(f"⚠️  Stop-Loss Binance impossible ({e}) — surveillance logicielle activée")
            return None

    def place_take_profit(self, symbol: str, side: str, amount: float,
                          tp_price: float) -> Optional[str]:
        """
        Place un Take-Profit LIMIT sur Binance.
        side = "BUY" → limit sell above entry | "SELL" → limit buy below entry
        Retourne l'order_id ou None.
        """
        try:
            amount   = round(amount, 5)
            tp_price = round(tp_price, 2)
            order_side = "sell" if side == "BUY" else "buy"

            order = self.exchange.create_order(
                symbol=symbol,
                type="LIMIT",
                side=order_side,
                amount=amount,
                price=tp_price,
                params={"timeInForce": "GTC"}
            )
            logger.info(f"🎯 TAKE-PROFIT placé | {symbol} | target={tp_price} | ID: {order['id']}")
            return str(order["id"])
        except Exception as e:
            logger.warning(f"⚠️  Take-Profit Binance impossible ({e}) — surveillance logicielle activée")
            return None

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Annule un ordre ouvert. Retourne True si succès."""
        if not order_id:
            return False
        try:
            self.exchange.cancel_order(str(order_id), symbol)
            logger.info(f"🗑️  Ordre annulé | {symbol} | ID: {order_id}")
            return True
        except ccxt.OrderNotFound:
            logger.warning(f"⚠️  Ordre {order_id} introuvable (déjà exécuté ?)")
            return False
        except Exception as e:
            logger.error(f"❌ cancel_order : {e}")
            return False

    def get_order_status(self, symbol: str, order_id: str) -> Optional[dict]:
        """
        Retourne le statut d'un ordre.
        dict avec 'status': 'open'|'closed'|'canceled', 'filled': float
        """
        if not order_id:
            return None
        try:
            return self.exchange.fetch_order(str(order_id), symbol)
        except ccxt.OrderNotFound:
            return None
        except Exception as e:
            logger.warning(f"⚠️  get_order_status {order_id} : {e}")
            return None

    def replace_stop_loss(self, symbol: str, side: str, old_order_id: str,
                          amount: float, new_sl: float) -> Optional[str]:
        """Annule l'ancien SL et place un nouveau (pour le Break Even)."""
        self.cancel_order(symbol, old_order_id)
        return self.place_stop_loss(symbol, side, amount, new_sl)

    # ─── Utilitaires ──────────────────────────────────────────────────────────

    def get_open_orders(self, symbol: str) -> list:
        try:
            return self.exchange.fetch_open_orders(symbol)
        except Exception as e:
            logger.error(f"❌ fetch_open_orders : {e}")
            return []

    def cancel_all_orders(self, symbol: str):
        for order in self.get_open_orders(symbol):
            self.cancel_order(symbol, str(order["id"]))

    def get_position(self, base_currency: str = "BTC") -> float:
        try:
            balance = self.exchange.fetch_balance()
            return balance.get(base_currency, {}).get("free", 0.0)
        except Exception as e:
            logger.error(f"❌ get_position : {e}")
            return 0.0
