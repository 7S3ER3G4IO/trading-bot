"""
capital_stub.py — Stub silencieux remplaçant CapitalClient quand MT5 est actif.

Implémente la même interface que CapitalClient mais retourne des valeurs neutres
(None, False, 0, []) sans aucun appel réseau. Permet de garder le code existant
intact en remplaçant self.capital par ce stub.
"""
from typing import Optional, List
import pandas as pd
from loguru import logger


class CapitalStub:
    """
    Stub no-op de CapitalClient.
    Utilisé quand MT5 est le broker actif — Capital.com n'est pas connecté.
    Toutes les méthodes retournent des valeurs neutres silencieusement.
    """

    # ─── État ────────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return False

    @property
    def session_token(self) -> Optional[str]:
        return None

    # ─── Données de marché ───────────────────────────────────────────────────

    def fetch_ohlcv(self, epic: str, timeframe: str = "5m",
                    count: int = 300) -> Optional[pd.DataFrame]:
        """MT5 actif — données OHLCV non disponibles via Capital.com."""
        return None

    def get_current_price(self, epic: str) -> Optional[dict]:
        """MT5 actif — prix temps réel non disponibles via Capital.com."""
        return None

    def get_balance(self) -> float:
        return 0.0

    def get_open_positions(self) -> List[dict]:
        return []

    def search_markets(self, term: str, limit: int = 5) -> list:
        return []

    def validate_epics(self):
        pass

    # ─── Ordres ──────────────────────────────────────────────────────────────

    def place_market_order(self, epic: str, direction: str, size: float,
                           sl_price: float = None, tp_price: float = None,
                           **kwargs) -> Optional[str]:
        logger.debug(f"CapitalStub.place_market_order({epic}, {direction}, {size}) → MT5 actif, ignoré")
        return None

    def place_limit_order(self, epic: str, direction: str, size: float,
                          limit_price: float, sl_price: float = None,
                          tp_price: float = None, **kwargs) -> Optional[str]:
        return None

    def confirm_deal(self, deal_ref: str, retries: int = 4) -> Optional[str]:
        return None

    def close_position(self, deal_id: str, **kwargs) -> bool:
        logger.debug(f"CapitalStub.close_position({deal_id}) → MT5 actif, ignoré")
        return False

    def close_partial(self, epic: str, direction: str,
                      partial_size: float) -> bool:
        return False

    def modify_position_stop(self, deal_id: str, new_stop: float) -> bool:
        return False

    def position_size(self, *args, **kwargs) -> float:
        return 0.0

    # ─── Utilitaires ─────────────────────────────────────────────────────────

    def _rate_limit(self):
        pass

    def _authenticate(self) -> bool:
        return False

    def _headers(self) -> dict:
        return {}

    def _request_safe(self, *args, **kwargs):
        return None
