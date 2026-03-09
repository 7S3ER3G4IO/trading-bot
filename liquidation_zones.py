"""
liquidation_zones.py — Liquidity Zone Detector
Identifie les zones de liquidité (grandes murailles d'ordres) via l'order book Binance.
Utilisé pour :
  - Valider que le trade va VERS la liquidité (pas contre)
  - Calculer les zones cibles de TP probables
  - Filtrer les trades dans des zones de "compression"
"""
import os
import ccxt
import numpy as np
from loguru import logger
from typing import Optional

# ─── Configuration ────────────────────────────────────────────────────────────
ORDERBOOK_DEPTH   = 100     # Niveaux à analyser
WALL_THRESHOLD    = 2.0     # Taille >= 2x moyenne = "mur"
CLUSTER_PCT       = 0.002   # 0.2% pour regrouper niveaux proches

# Seuils de proximité (trop proche = bloquer, assez loin = bonus)
BLOCK_DIST   = 0.005   # 0.5% — muraille trop proche → bloquer (était 0.3%)
BONUS_DIST   = 0.006   # 0.6% — liquidité cible assez loin → +1 bonus

class LiquidityZones:
    """
    Détecte les zones de haute liquidité dans le carnet d'ordres.
    
    Logique:
    - "Mur d'achat" = grande concentration de bids → support fort
    - "Mur de vente" = grande concentration d'asks → résistance forte
    - Signal valide si price va VERS le mur opposé (comme une cible)
    - Signal invalide si price doit traverser un mur dans sa direction
    """

    def __init__(self):
        api_key = os.getenv("BINANCE_API_KEY", "")
        secret  = os.getenv("BINANCE_SECRET", "")
        testnet = os.getenv("USE_TESTNET", "true").lower() == "true"

        try:
            self.exchange = ccxt.binance({
                "apiKey": api_key,
                "secret": secret,
                "options": {"defaultType": "spot"},
            })
            if testnet:
                self.exchange.set_sandbox_mode(True)
            self.available = True
            logger.info("💧 LiquidityZones initialisé (order book Binance)")
        except Exception as e:
            logger.warning(f"⚠️  LiquidityZones : {e}")
            self.available = False

    def _fetch_orderbook(self, symbol: str) -> Optional[dict]:
        try:
            ob = self.exchange.fetch_order_book(symbol, limit=ORDERBOOK_DEPTH)
            return ob
        except Exception as e:
            logger.debug(f"LiquidityZones fetch {symbol}: {e}")
            return None

    def _find_walls(self, levels: list, side: str) -> list:
        """Trouve les murs (concentrations de liquidité) dans bids ou asks."""
        if not levels:
            return []
        sizes = np.array([float(lvl[1]) for lvl in levels])
        mean_size = sizes.mean()
        walls = []
        for price, size in levels:
            if float(size) >= mean_size * WALL_THRESHOLD:
                walls.append({"price": float(price), "size": float(size), "side": side})
        return walls

    def _cluster_walls(self, walls: list) -> list:
        """Regroupe les murs proches en une zone unique."""
        if not walls:
            return []
        clustered = []
        walls_sorted = sorted(walls, key=lambda w: w["price"])
        current = walls_sorted[0].copy()
        for wall in walls_sorted[1:]:
            gap = abs(wall["price"] - current["price"]) / current["price"]
            if gap <= CLUSTER_PCT:
                # Fusionner : prendre le prix avec la taille max
                if wall["size"] > current["size"]:
                    current.update(wall)
                current["size"] += wall["size"]
            else:
                clustered.append(current)
                current = wall.copy()
        clustered.append(current)
        return clustered

    def analyze(self, symbol: str, direction: str, current_price: float) -> dict:
        """
        Analyse les zones de liquidité pour un trade potentiel.
        
        Args:
            symbol    : ex. "ETH/USDT"
            direction : "BUY" ou "SELL"
            current_price : prix actuel
            
        Returns:
            dict avec :
            - valid : bool — le trade a de la liquidité cible
            - score_bonus : +1 si vers liquidité forte, -1 si contre
            - nearest_support : prix du support le plus proche
            - nearest_resistance : prix de la résistance la plus proche
            - message : description lisible
        """
        result = {
            "valid": True, "score_bonus": 0,
            "nearest_support": None, "nearest_resistance": None,
            "message": "OK"
        }
        if not self.available:
            return result

        ob = self._fetch_orderbook(symbol)
        if not ob:
            return result

        bid_walls = self._cluster_walls(self._find_walls(ob["bids"], "bid"))
        ask_walls = self._cluster_walls(self._find_walls(ob["asks"], "ask"))

        # Support = mur d'achat en dessous du prix actuel
        supports    = [w for w in bid_walls if w["price"] < current_price]
        resistances = [w for w in ask_walls if w["price"] > current_price]

        nearest_sup = max(supports,    key=lambda w: w["price"]) if supports else None
        nearest_res = min(resistances, key=lambda w: w["price"]) if resistances else None

        result["nearest_support"]    = nearest_sup["price"]    if nearest_sup else None
        result["nearest_resistance"] = nearest_res["price"] if nearest_res else None

        if direction == "BUY":
            # LONG valide si résistance loin et support proche (stop protégé)
            if nearest_res:
                dist_to_res = (nearest_res["price"] - current_price) / current_price
                if dist_to_res < BLOCK_DIST:  # résistance trop proche → bloquer
                    result["valid"]       = False
                    result["score_bonus"] = -1
                    result["message"]     = f"Résistance proche à {nearest_res['price']:.4f}"
                elif dist_to_res > BONUS_DIST:  # résistance loin → bon potentiel
                    result["score_bonus"] = 1
                    result["message"]     = f"Liquidité cible à {nearest_res['price']:.4f}"

        elif direction == "SELL":
            # SHORT valide si support loin et résistance proche
            if nearest_sup:
                dist_to_sup = (current_price - nearest_sup["price"]) / current_price
                if dist_to_sup < BLOCK_DIST:  # support trop proche → bloquer
                    result["valid"]       = False
                    result["score_bonus"] = -1
                    result["message"]     = f"Support proche à {nearest_sup['price']:.4f}"
                elif dist_to_sup > BONUS_DIST:
                    result["score_bonus"] = 1
                    result["message"]     = f"Liquidité cible à {nearest_sup['price']:.4f}"

        return result
