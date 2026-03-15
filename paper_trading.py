"""
paper_trading.py — Mode Paper Trading (Shadow Mode)
Simule les ordres sans les envoyer au broker réel.
Activé via env var SHADOW_MODE=true

Usage :
    SHADOW_MODE=true docker compose up

Le PaperBroker implémente la même interface que MT5Client/CapitalClient
mais enregistre les trades en mémoire + discord webhook uniquement.
"""
import os
from datetime import datetime, timezone
from typing import Optional, List
from loguru import logger

SHADOW_MODE = os.getenv("SHADOW_MODE", "false").lower() in ("true", "1", "yes")
DISCORD_WEBHOOK = os.getenv("DISCORD_MONITORING_WEBHOOK", "")


class PaperBroker:
    """Broker fictif pour le paper trading. Interface identique à MT5Client."""

    def __init__(self, initial_balance: float = 100_000.0):
        self._balance   = initial_balance
        self._positions = {}  # {epic: {deal_id, entry, sl, tp, direction, size}}
        self._trades    = []  # historique fermé
        self._next_id   = 1
        self.available  = True
        logger.info(f"📝 PaperBroker actif — balance fictive {initial_balance:,.0f}$")

    def get_balance(self) -> float:
        return self._balance

    def get_current_price(self, epic: str) -> Optional[dict]:
        """Retourne None — pas de prix live en paper mode (utilise ohlcv_cache)."""
        return None

    def place_market_order(
        self, epic: str, direction: str, size: float,
        sl_price: float, tp_price: float
    ) -> Optional[str]:
        deal_id = f"PAPER-{self._next_id:04d}"
        self._next_id += 1
        now = datetime.now(timezone.utc)

        self._positions[epic] = {
            "deal_id":   deal_id,
            "direction": direction,
            "size":      size,
            "sl":        sl_price,
            "tp":        tp_price,
            "open_time": now,
            "entry":     0.0,  # sera rempli par le caller
        }

        msg = (
            f"📝 **PAPER TRADE** {direction} {epic} "
            f"size={size} SL={sl_price:.5f} TP={tp_price:.5f}"
        )
        logger.info(msg)
        self._notify_discord(msg)
        return deal_id

    def close_position(self, deal_id: str) -> bool:
        for epic, pos in list(self._positions.items()):
            if pos.get("deal_id") == deal_id:
                pnl = round((pos.get("tp", 0) - pos.get("entry", 0)) * pos.get("size", 1), 2)
                self._trades.append({**pos, "closed_at": datetime.now(timezone.utc), "pnl": pnl})
                self._balance += pnl
                del self._positions[epic]
                msg = f"📝 **PAPER CLOSE** {epic} | PnL fictif = {pnl:+.2f}$ | Bal = {self._balance:,.0f}$"
                logger.info(msg)
                self._notify_discord(msg)
                return True
        return False

    def update_position(self, deal_id: str, stop_level: float = None,
                        limit_level: float = None, epic: str = "") -> bool:
        for pos in self._positions.values():
            if pos.get("deal_id") == deal_id:
                if stop_level is not None:
                    pos["sl"] = stop_level
                if limit_level is not None:
                    pos["tp"] = limit_level
                return True
        return False

    def close_partial(self, epic: str, direction: str, partial_size: float) -> bool:
        if epic in self._positions:
            pos = self._positions[epic]
            pos["size"] = max(0, pos.get("size", 0) - partial_size)
            logger.info(f"📝 PAPER PARTIAL CLOSE {epic} -{partial_size:.2f} lots")
            return True
        return False

    def fetch_ohlcv(self, *a, **kw):
        return None

    def position_size(self, balance, risk_pct, entry, sl, epic, free_margin=0.0) -> float:
        sl_dist = abs(entry - sl)
        if sl_dist <= 0:
            return 0.01
        return round(balance * risk_pct / sl_dist, 2)

    def get_open_positions(self) -> List[dict]:
        return [
            {"position": {"dealId": p["deal_id"], "direction": p["direction"],
                          "level": p["entry"], "size": p["size"]},
             "market": {"epic": epic}}
            for epic, p in self._positions.items()
        ]

    def _notify_discord(self, msg: str):
        if not DISCORD_WEBHOOK:
            return
        try:
            import requests
            requests.post(DISCORD_WEBHOOK, json={"content": msg[:2000]}, timeout=5)
        except Exception:
            pass

    def paper_summary(self) -> str:
        open_count = len(self._positions)
        total_pnl  = sum(t.get("pnl", 0) for t in self._trades)
        return (
            f"📝 **Paper Trading Summary**\n"
            f"Balance fictive : {self._balance:,.2f}$\n"
            f"Positions ouvertes : {open_count}\n"
            f"Trades fermés : {len(self._trades)}\n"
            f"P&L simulé total : {total_pnl:+.2f}$"
        )


def get_broker(real_broker, initial_balance: float = 100_000.0):
    """
    Factory : retourne PaperBroker si SHADOW_MODE=true, sinon le broker réel.
    À appeler dans bot_init.py pour remplacer self.broker.
    """
    if SHADOW_MODE:
        logger.warning("🔵 SHADOW_MODE actif — tous les ordres sont FICTIFS")
        return PaperBroker(initial_balance=initial_balance)
    return real_broker
