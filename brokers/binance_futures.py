"""
brokers/binance_futures.py — Client Binance Futures USDT-Margined (Demo ou Mainnet).

Utilise requests + HMAC-SHA256 directement vers demo-fapi.binance.com
pour contourner les limites de ccxt avec l'endpoint demo Binance.

Variables d'environnement :
  BINANCE_FUTURES_API_KEY  — Clé API futures (demo ou mainnet)
  BINANCE_FUTURES_SECRET   — Secret API futures
  BINANCE_FUTURES_TESTNET  — "true" pour demo-fapi (défaut: true)
"""

import os, time, hmac, hashlib
import requests as _req
from loguru import logger
from typing import Optional

# ─── Instruments ─────────────────────────────────────────────────────────────
FUTURES_INSTRUMENTS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "BNB/USDT:USDT",
    "XRP/USDT:USDT",
    "ADA/USDT:USDT",
    "LINK/USDT:USDT",
    "AVAX/USDT:USDT",
    "DOGE/USDT:USDT",
]

# Score minimum pour passer un ordre Futures (plus bas que Spot pour + d'opportunités)
FUTURES_MIN_SCORE = 4  # sur 6 confirmations

INSTRUMENT_NAMES = {
    "BTC/USDT:USDT":  "Bitcoin",
    "ETH/USDT:USDT":  "Ethereum",
    "SOL/USDT:USDT":  "Solana",
    "BNB/USDT:USDT":  "BNB",
    "XRP/USDT:USDT":  "XRP",
    "ADA/USDT:USDT":  "Cardano",
    "LINK/USDT:USDT": "Chainlink",
    "AVAX/USDT:USDT": "Avalanche",
    "DOGE/USDT:USDT": "Dogecoin",
}

# Symbol mapping: ccxt format → Binance REST format
_SYM = {
    "ETH/USDT:USDT":  "ETHUSDT",
    "XRP/USDT:USDT":  "XRPUSDT",
    "ADA/USDT:USDT":  "ADAUSDT",
    "DOGE/USDT:USDT": "DOGEUSDT",
}

LEVERAGE = 1  # ×1 — zéro risque de liquidation


def _to_sym(instrument: str) -> str:
    return _SYM.get(instrument, instrument.replace("/USDT:USDT", "USDT").replace("/", ""))


class BinanceFuturesClient:
    """
    Client REST direct pour Binance Futures Demo (demo-fapi.binance.com).
    Utilise HMAC-SHA256 pour l'authentification — bypass ccxt.
    """

    def __init__(self):
        self._api_key = os.getenv("BINANCE_FUTURES_API_KEY", "")
        self._secret  = os.getenv("BINANCE_FUTURES_SECRET", "")
        demo          = os.getenv("BINANCE_FUTURES_TESTNET", "true").lower() != "false"

        self.available = bool(self._api_key and self._secret)
        self._demo     = demo

        if not self.available:
            logger.info("ℹ️  Binance Futures non configuré (BINANCE_FUTURES_API_KEY manquant)")
            return

        if demo:
            self._base = "https://demo-fapi.binance.com"
            logger.info("🧪 Binance Futures Demo (demo-fapi.binance.com) — argent fictif")
        else:
            self._base = "https://fapi.binance.com"
            logger.info("🔴 Binance Futures LIVE (fapi.binance.com)")

        self._session = _req.Session()
        self._session.headers.update({"X-MBX-APIKEY": self._api_key})

        # Test de connexion
        try:
            bal = self.get_balance()
            logger.info(f"✅ Futures connecté — solde USDT: {bal:.2f}")
        except Exception as e:
            logger.warning(f"⚠️  Futures connexion: {e}")

    # ─── Signature HMAC ──────────────────────────────────────────────────────

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query = "&".join(f"{k}={v}" for k, v in params.items())
        sig = hmac.new(
            self._secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = sig
        return params

    def _get(self, path: str, params: dict = None, signed: bool = False):
        url = self._base + path
        p   = params or {}
        if signed:
            p = self._sign(p)
        r = self._session.get(url, params=p, timeout=10)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, params: dict = None):
        url = self._base + path
        p   = self._sign(params or {})
        r = self._session.post(url, params=p, timeout=10)
        try:
            data = r.json()
        except Exception:
            r.raise_for_status()
            return {}
        if isinstance(data, dict) and "code" in data and data["code"] != 200:
            raise ValueError(f"Binance error {data.get('code')}: {data.get('msg')}")
        return data

    # ─── API publique ────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Retourne le solde USDT disponible (libre) dans le wallet futures."""
        if not self.available:
            return 0.0
        try:
            data = self._get("/fapi/v2/balance", signed=True)
            for asset in data:
                if asset.get("asset") == "USDT":
                    return float(asset.get("availableBalance", 0))
            return 0.0
        except Exception as e:
            logger.error(f"❌ Futures balance: {e}")
            return 0.0

    def get_total_balance(self) -> float:
        """Retourne le solde USDT total (libre + marge utilisée) du wallet futures."""
        if not self.available:
            return 0.0
        try:
            data = self._get("/fapi/v2/balance", signed=True)
            for asset in data:
                if asset.get("asset") == "USDT":
                    return float(asset.get("balance", 0))
            return 0.0
        except Exception as e:
            logger.error(f"❌ Futures total balance: {e}")
            return 0.0

    def get_position(self, instrument: str) -> dict:
        """
        Retourne la position ouverte sur un instrument.
        positionAmt == 0 → position fermée (SL ou TP touché côté Binance).
        """
        if not self.available:
            return {"positionAmt": 0.0, "unrealizedProfit": 0.0, "entryPrice": 0.0}
        try:
            sym  = _to_sym(instrument)
            data = self._get("/fapi/v2/positionRisk", params={"symbol": sym}, signed=True)
            if data:
                p = data[0]
                return {
                    "positionAmt":      float(p.get("positionAmt",      0)),
                    "unrealizedProfit": float(p.get("unRealizedProfit", 0)),
                    "entryPrice":       float(p.get("entryPrice",       0)),
                }
        except Exception as e:
            logger.warning(f"⚠️  get_position {instrument}: {e}")
        return {"positionAmt": 0.0, "unrealizedProfit": 0.0, "entryPrice": 0.0}

    def get_last_realized_pnl(self, instrument: str, limit: int = 10) -> float:
        """Retourne la somme des PnL réalisés récents pour un instrument."""
        if not self.available:
            return 0.0
        try:
            sym  = _to_sym(instrument)
            data = self._get("/fapi/v1/income", params={
                "symbol": sym, "incomeType": "REALIZED_PNL", "limit": limit,
            }, signed=True)
            if data:
                return sum(float(d.get("income", 0)) for d in data)
        except Exception as e:
            logger.warning(f"⚠️  get_last_realized_pnl {instrument}: {e}")
        return 0.0

    def fetch_ohlcv(self, instrument: str, timeframe: str = "5m", count: int = 300):
        """Retourne un DataFrame OHLCV depuis Binance Futures demo."""
        if not self.available:
            return None
        try:
            import pandas as pd
            sym = _to_sym(instrument)
            data = self._get("/fapi/v1/klines", params={
                "symbol": sym, "interval": timeframe, "limit": count
            })
            df = pd.DataFrame(data, columns=[
                "timestamp", "open", "high", "low", "close", "volume",
                "close_time", "quote_vol", "trades", "taker_buy_base",
                "taker_buy_quote", "ignore"
            ])
            df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
            df[["open", "high", "low", "close", "volume"]] = (
                df[["open", "high", "low", "close", "volume"]].astype(float)
            )
            df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            return df.drop("timestamp", axis=1)
        except Exception as e:
            logger.error(f"❌ Futures OHLCV {instrument}: {e}")
            return None

    def set_leverage(self, instrument: str, leverage: int = LEVERAGE):
        """Configure le levier (×1 par défaut)."""
        if not self.available:
            return
        try:
            sym = _to_sym(instrument)
            self._post("/fapi/v1/leverage", {"symbol": sym, "leverage": leverage})
            logger.debug(f"⚙️  Levier {instrument} → ×{leverage}")
        except Exception as e:
            logger.warning(f"⚠️  set_leverage {instrument}: {e}")

    def place_market_order(
        self,
        instrument: str,
        side: str,        # "BUY" ou "SELL"
        qty: float,
        sl_price: float,
        tp_price: float,
    ) -> Optional[str]:
        """Place un ordre MARKET avec SL et TP. Retourne l'order ID."""
        if not self.available:
            return None
        try:
            self.set_leverage(instrument, LEVERAGE)
            sym = _to_sym(instrument)
            bs  = side.upper()
            opp = "SELL" if bs == "BUY" else "BUY"

            # Ordre principal
            order = self._post("/fapi/v1/order", {
                "symbol": sym, "side": bs, "type": "MARKET",
                "quantity": qty, "positionSide": "BOTH",
            })
            trade_id = str(order.get("orderId", ""))
            logger.info(f"📈 Futures {bs} {instrument} qty={qty:.4f} → ID {trade_id}")

            # Stop Loss — closePosition=true ferme automatiquement toute la position
            try:
                self._post("/fapi/v1/order", {
                    "symbol": sym, "side": opp, "type": "STOP_MARKET",
                    "stopPrice": round(sl_price, 4),
                    "positionSide": "BOTH",
                    "closePosition": "true",
                    "workingType": "MARK_PRICE",
                })
            except Exception as e:
                logger.warning(f"⚠️  SL futures: {e}")

            # Take Profit — closePosition=true ferme automatiquement toute la position
            try:
                self._post("/fapi/v1/order", {
                    "symbol": sym, "side": opp, "type": "TAKE_PROFIT_MARKET",
                    "stopPrice": round(tp_price, 4),
                    "positionSide": "BOTH",
                    "closePosition": "true",
                    "workingType": "MARK_PRICE",
                })
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
            sym = _to_sym(instrument)
            # Récupère la position
            positions = self._get("/fapi/v2/positionRisk", params={"symbol": sym}, signed=True)
            for pos in positions:
                amt = float(pos.get("positionAmt", 0))
                if abs(amt) > 0:
                    side = "SELL" if amt > 0 else "BUY"
                    self._post("/fapi/v1/order", {
                        "symbol": sym, "side": side, "type": "MARKET",
                        "quantity": abs(amt), "reduceOnly": "true",
                        "positionSide": "BOTH",
                    })
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
        Calcule la quantité à trader pour risquer risk_pct% du capital.
        Plafonnée à 90% du capital disponible / prix d'entrée (levier ×1).
        Évite l'erreur -2019 Margin is insufficient.
        """
        sl_dist = abs(entry - sl)
        if sl_dist <= 0 or entry <= 0:
            return 0.0

        risk_amt = balance * risk_pct
        qty_risk = risk_amt / sl_dist

        # Plafond margin : à 1x, qty max = 90% du solde / prix entrée
        qty_max_margin = (balance * 0.90) / entry
        qty = min(qty_risk, qty_max_margin)

        # Précision par instrument (step size Binance Futures)
        precision = {
            "BTC/USDT:USDT":  3,
            "ETH/USDT:USDT":  3,
            "SOL/USDT:USDT":  1,
            "BNB/USDT:USDT":  2,
            "XRP/USDT:USDT":  0,
            "ADA/USDT:USDT":  0,
            "LINK/USDT:USDT": 1,
            "AVAX/USDT:USDT": 1,
            "DOGE/USDT:USDT": 0,
        }.get(instrument, 3)
        return round(qty, precision)
