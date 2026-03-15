"""
pre_builder.py — M43: Predictive Transaction Pre-Building

L'Ombre : pré-construit les requêtes HTTP en mémoire cache AVANT le signal.

Architecture :
Si les moteurs prédictifs (M24, M29, M32) évaluent ≥ 90% de probabilité
qu'un signal se déclenche, M43 pré-compile :
  1. Le payload JSON sérialisé en bytes
  2. Les headers HTTP complets
  3. Le path de l'endpoint

Quand le signal (100%) tombe → flush instantané du buffer pré-construit.
Temps de calcul au moment T = 0 (tout est déjà en mémoire L1/L2).
"""

import json
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Tuple
from loguru import logger


# ─── Configuration ────────────────────────────────────────────────────────────
PRE_BUILD_THRESHOLD   = 0.90    # Probabilité minimale pour pré-construire
PRE_BUILD_TTL         = 30.0    # Secondes avant expiration d'un pre-build
MAX_PRE_BUILT         = 10      # Max instruments pré-construits simultanément


@dataclass
class PreBuiltOrder:
    """Ordre pré-construit en mémoire cache."""
    instrument: str
    direction: str
    body_bytes: bytes            # Payload JSON pré-sérialisé
    headers: dict                # Headers HTTP complets
    path: str                    # Endpoint path
    created_at: float            # Timestamp de création
    probability: float           # Probabilité estimée au moment du pre-build
    sl: float = 0.0
    tp: float = 0.0
    size: float = 0.0

    @property
    def age_ms(self) -> float:
        return (time.time() - self.created_at) * 1000

    @property
    def is_expired(self) -> bool:
        return time.time() - self.created_at > PRE_BUILD_TTL


class PreBuilder:
    """
    M43 — Predictive Transaction Pre-Building.

    Pré-compile les requêtes HTTP pour un flush instantané
    quand le signal se confirme.
    """

    def __init__(self, base_path: str = "/api/v1"):
        self._cache: Dict[str, PreBuiltOrder] = {}
        self._base_path = base_path
        self._stats = {
            "pre_built": 0,
            "flushed": 0,
            "expired": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "avg_prebuild_to_flush_ms": 0.0,
            "total_flush_ms": 0.0,
        }
        logger.info(
            f"👻 M43 Pre-Builder initialisé | threshold={PRE_BUILD_THRESHOLD*100:.0f}% "
            f"TTL={PRE_BUILD_TTL}s max_cache={MAX_PRE_BUILT}"
        )

    # ═══════════════════════════════════════════════════════════════════════
    #  1. PRE-BUILD : pré-compiler la requête
    # ═══════════════════════════════════════════════════════════════════════

    def pre_build(
        self,
        instrument: str,
        direction: str,
        size: float,
        sl_price: float,
        tp_price: float,
        probability: float,
        auth_headers: dict,
    ) -> bool:
        """
        Pré-construit un ordre si la probabilité dépasse le threshold.

        Parameters:
            instrument: instrument cible
            direction: "BUY" or "SELL"
            size: taille de position
            sl_price: stop loss
            tp_price: take profit
            probability: probabilité estimée par les moteurs prédictifs
            auth_headers: headers d'authentification Capital.com

        Returns:
            True si pré-construit, False sinon.
        """
        if probability < PRE_BUILD_THRESHOLD:
            return False

        # Cleanup des expirés
        self._cleanup_expired()

        # Vérifier la capacité
        if len(self._cache) >= MAX_PRE_BUILT and instrument not in self._cache:
            # Éjecter le plus ancien
            oldest_key = min(self._cache, key=lambda k: self._cache[k].created_at)
            del self._cache[oldest_key]

        # Sérialiser le payload en bytes
        payload = {
            "epic": instrument,
            "direction": direction,
            "size": str(round(size, 2)),
            "orderType": "MARKET",
            "stopLevel": round(sl_price, 5),
            "profitLevel": round(tp_price, 5),
            "guaranteedStop": False,
            "forceOpen": True,
        }
        body_bytes = json.dumps(payload).encode("utf-8")

        # Headers complets pré-construits
        full_headers = {
            **auth_headers,
            "Content-Type": "application/json",
            "Content-Length": str(len(body_bytes)),
            "Connection": "keep-alive",
        }

        pre_built = PreBuiltOrder(
            instrument=instrument,
            direction=direction,
            body_bytes=body_bytes,
            headers=full_headers,
            path=f"{self._base_path}/positions",
            created_at=time.time(),
            probability=probability,
            sl=sl_price,
            tp=tp_price,
            size=size,
        )

        self._cache[instrument] = pre_built
        self._stats["pre_built"] += 1

        logger.info(
            f"👻 M43 PRE-BUILT {direction} {instrument} | "
            f"prob={probability*100:.0f}% size={size} "
            f"body={len(body_bytes)}B"
        )
        return True

    # ═══════════════════════════════════════════════════════════════════════
    #  2. FLUSH : vider le buffer pré-construit instantanément
    # ═══════════════════════════════════════════════════════════════════════

    def get_pre_built(self, instrument: str) -> Optional[PreBuiltOrder]:
        """
        Récupère un ordre pré-construit pour flush instantané.

        Returns:
            PreBuiltOrder si disponible et non expiré, None sinon.
        """
        pre_built = self._cache.get(instrument)
        if pre_built is None:
            self._stats["cache_misses"] += 1
            return None

        if pre_built.is_expired:
            del self._cache[instrument]
            self._stats["expired"] += 1
            self._stats["cache_misses"] += 1
            logger.debug(f"👻 M43 {instrument}: pre-build expiré ({pre_built.age_ms:.0f}ms)")
            return None

        self._stats["cache_hits"] += 1
        return pre_built

    def consume(self, instrument: str) -> Optional[PreBuiltOrder]:
        """
        Récupère ET supprime un ordre pré-construit.
        Utilisé quand le signal se confirme → flush et cleanup.
        """
        pre_built = self.get_pre_built(instrument)
        if pre_built is not None:
            del self._cache[instrument]
            self._stats["flushed"] += 1

            # Track latency from pre-build to flush
            latency_ms = pre_built.age_ms
            self._stats["total_flush_ms"] += latency_ms
            self._stats["avg_prebuild_to_flush_ms"] = (
                self._stats["total_flush_ms"] / max(self._stats["flushed"], 1)
            )

            logger.info(
                f"⚡ M43 FLUSH {instrument} | "
                f"pre-build → flush = {latency_ms:.0f}ms | "
                f"body ready in L1/L2 cache"
            )
            return pre_built
        return None

    def invalidate(self, instrument: str):
        """Invalide un pre-build (signal annulé ou paramètres changés)."""
        if instrument in self._cache:
            del self._cache[instrument]
            logger.debug(f"👻 M43 {instrument}: pre-build invalidé")

    def has_pre_built(self, instrument: str) -> bool:
        """Vérifie si un instrument a un pré-build valide."""
        pb = self._cache.get(instrument)
        return pb is not None and not pb.is_expired

    # ═══════════════════════════════════════════════════════════════════════
    #  MAINTENANCE
    # ═══════════════════════════════════════════════════════════════════════

    def _cleanup_expired(self):
        """Supprime les pre-builds expirés."""
        expired = [k for k, v in self._cache.items() if v.is_expired]
        for k in expired:
            del self._cache[k]
            self._stats["expired"] += 1

    def stats(self) -> dict:
        self._cleanup_expired()
        return {
            **self._stats,
            "active_pre_builds": len(self._cache),
            "threshold_pct": PRE_BUILD_THRESHOLD * 100,
            "ttl_seconds": PRE_BUILD_TTL,
        }
