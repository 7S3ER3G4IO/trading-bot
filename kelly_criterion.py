"""
kelly_criterion.py — M39: Kelly Criterion Kernel (per-engine)

Le Trésorier Impitoyable : position sizing dynamique par moteur.

Formule Kelly : f* = (b×p - q) / b
- p = probabilité de gain du moteur
- q = 1 - p
- b = ratio R:R moyen du moteur

Si un moteur accumule des pertes, son allocation chute à 0.
Hard cap : max 2% du portefeuille par trade.
"""

import time
from collections import defaultdict
from typing import Dict, Optional, Tuple
from loguru import logger


# ─── Configuration ────────────────────────────────────────────────────────────
KELLY_MIN_TRADES     = 10       # Minimum trades avant Kelly actif
KELLY_WINDOW         = 50       # Rolling window (derniers N trades)
KELLY_FRACTION       = 0.5      # Half-Kelly (conservatif)
KELLY_FLOOR          = 0.001    # 0.1% minimum (engine vivant)
KELLY_CEILING        = 0.020    # 2.0% maximum absolu
KELLY_DEFAULT        = 0.005    # 0.5% par défaut (pas assez de données)
KELLY_DEAD_THRESHOLD = 0.0     # Kelly ≤ 0 → engine mort (alloc = 0)


class EngineRecord:
    """Historique de performance d'un moteur."""

    __slots__ = ("engine_id", "trades", "wins", "losses",
                 "total_rr", "last_kelly", "status", "last_update")

    def __init__(self, engine_id: str):
        self.engine_id = engine_id
        self.trades: list = []      # [{won: bool, rr: float, ts: float}]
        self.wins: int = 0
        self.losses: int = 0
        self.total_rr: float = 0.0
        self.last_kelly: float = KELLY_DEFAULT
        self.status: str = "WARMUP"   # WARMUP | ACTIVE | DEGRADED | DEAD
        self.last_update: float = time.time()


class KellyCriterionKernel:
    """
    M39 — Kelly Criterion per-engine.

    Chaque moteur (M26 NLP, M24 Algo, etc.) a son propre historique.
    Le Kelly fraction détermine la taille de position autorisée.
    """

    def __init__(self):
        self._engines: Dict[str, EngineRecord] = {}
        self._stats = {
            "total_records": 0,
            "engines_active": 0,
            "engines_dead": 0,
            "engines_degraded": 0,
        }
        logger.info(
            f"💰 M39 Kelly Criterion Kernel initialisé | "
            f"half_kelly={KELLY_FRACTION} cap={KELLY_CEILING*100:.1f}% "
            f"window={KELLY_WINDOW}"
        )

    def _get_or_create(self, engine_id: str) -> EngineRecord:
        if engine_id not in self._engines:
            self._engines[engine_id] = EngineRecord(engine_id)
        return self._engines[engine_id]

    # ═══════════════════════════════════════════════════════════════════════
    #  1. ENREGISTREMENT DES RÉSULTATS
    # ═══════════════════════════════════════════════════════════════════════

    def record_engine_result(self, engine_id: str, won: bool,
                             rr_achieved: float = 1.0):
        """
        Enregistre le résultat d'un trade associé à un moteur.

        Parameters:
            engine_id: Identifiant du moteur (ex: "M26_NLP", "M24_ALGO")
            won: True si le trade est gagnant
            rr_achieved: R:R réellement obtenu (ex: 2.5)
        """
        rec = self._get_or_create(engine_id)
        rec.trades.append({
            "won": won,
            "rr": rr_achieved,
            "ts": time.time(),
        })

        if won:
            rec.wins += 1
        else:
            rec.losses += 1
        rec.total_rr += rr_achieved if won else 0

        # Rolling window
        if len(rec.trades) > KELLY_WINDOW:
            oldest = rec.trades.pop(0)
            if oldest["won"]:
                rec.wins -= 1
                rec.total_rr -= oldest["rr"]
            else:
                rec.losses -= 1

        # Recalcul Kelly
        rec.last_kelly = self._compute_kelly(rec)
        rec.last_update = time.time()

        # Mise à jour du statut
        n = len(rec.trades)
        if n < KELLY_MIN_TRADES:
            rec.status = "WARMUP"
        elif rec.last_kelly <= KELLY_DEAD_THRESHOLD:
            rec.status = "DEAD"
        elif rec.last_kelly < KELLY_DEFAULT * 0.5:
            rec.status = "DEGRADED"
        else:
            rec.status = "ACTIVE"

        self._stats["total_records"] += 1
        self._update_stats()

        logger.debug(
            f"💰 M39 {engine_id}: {'WIN' if won else 'LOSS'} RR={rr_achieved:.2f} | "
            f"kelly={rec.last_kelly:.4f} ({rec.last_kelly*100:.2f}%) "
            f"status={rec.status} [{rec.wins}W/{rec.losses}L]"
        )

    # ═══════════════════════════════════════════════════════════════════════
    #  2. CALCUL KELLY
    # ═══════════════════════════════════════════════════════════════════════

    def _compute_kelly(self, rec: EngineRecord) -> float:
        """
        Calcule le Kelly fraction pour un moteur.

        f* = (b×p - q) / b
        Retourne half-Kelly clamped [KELLY_FLOOR, KELLY_CEILING].
        """
        n = len(rec.trades)
        if n < KELLY_MIN_TRADES:
            return KELLY_DEFAULT

        recent = rec.trades[-KELLY_WINDOW:]
        wins = [t for t in recent if t["won"]]
        losses = [t for t in recent if not t["won"]]

        if not wins or not losses:
            if wins and not losses:
                return KELLY_CEILING  # 100% WR → max allocation
            return 0.0  # 0% WR → dead engine

        p = len(wins) / len(recent)     # Probabilité de gain
        q = 1 - p                        # Probabilité de perte
        b = sum(t["rr"] for t in wins) / len(wins)  # R:R moyen des gains

        if b <= 0:
            return 0.0

        # Formule Kelly : f* = (b×p - q) / b
        kelly_full = (b * p - q) / b

        if kelly_full <= KELLY_DEAD_THRESHOLD:
            return 0.0

        # Half-Kelly (conservatif) + clamp
        kelly_half = kelly_full * KELLY_FRACTION
        return max(KELLY_FLOOR, min(KELLY_CEILING, kelly_half))

    # ═══════════════════════════════════════════════════════════════════════
    #  3. POSITION SIZING
    # ═══════════════════════════════════════════════════════════════════════

    def get_engine_fraction(self, engine_id: str) -> float:
        """
        Retourne le Kelly fraction pour un moteur.
        Si DEAD, retourne 0.0 (aucune allocation).
        """
        rec = self._engines.get(engine_id)
        if rec is None:
            return KELLY_DEFAULT

        if rec.status == "DEAD":
            return 0.0

        return rec.last_kelly

    def compute_position_risk(self, engine_id: str,
                               base_risk: float = 0.005) -> float:
        """
        Calcule le risk% ajusté par Kelly pour un moteur.

        Parameters:
            engine_id: identifiant du moteur
            base_risk: risk% de base (ex: 0.005 = 0.5%)

        Returns:
            risk% ajusté, clamped [0, 2%]
        """
        kelly_f = self.get_engine_fraction(engine_id)

        if kelly_f <= 0:
            logger.warning(f"⛔ M39 {engine_id}: DEAD (kelly=0) — allocation ZÉRO")
            return 0.0

        # Ajuster le risk de base par le ratio kelly/default
        kelly_multiplier = kelly_f / KELLY_DEFAULT if KELLY_DEFAULT > 0 else 1.0
        adjusted_risk = base_risk * kelly_multiplier

        # Hard cap 2%
        adjusted_risk = min(adjusted_risk, KELLY_CEILING)

        return adjusted_risk

    def is_engine_dead(self, engine_id: str) -> bool:
        """Vérifie si un moteur est mort (kelly ≤ 0)."""
        rec = self._engines.get(engine_id)
        if rec is None:
            return False
        return rec.status == "DEAD"

    # ═══════════════════════════════════════════════════════════════════════
    #  4. REPORTING
    # ═══════════════════════════════════════════════════════════════════════

    def get_engine_health(self) -> Dict[str, dict]:
        """Retourne la santé de tous les moteurs trackés."""
        result = {}
        for eid, rec in self._engines.items():
            n = len(rec.trades)
            wr = rec.wins / n * 100 if n > 0 else 0
            avg_rr = rec.total_rr / rec.wins if rec.wins > 0 else 0
            result[eid] = {
                "trades": n,
                "win_rate": round(wr, 1),
                "avg_rr": round(avg_rr, 2),
                "kelly_f": round(rec.last_kelly, 4),
                "kelly_pct": f"{rec.last_kelly*100:.2f}%",
                "status": rec.status,
            }
        return result

    def _update_stats(self):
        active = sum(1 for r in self._engines.values() if r.status == "ACTIVE")
        dead = sum(1 for r in self._engines.values() if r.status == "DEAD")
        degraded = sum(1 for r in self._engines.values() if r.status == "DEGRADED")
        self._stats["engines_active"] = active
        self._stats["engines_dead"] = dead
        self._stats["engines_degraded"] = degraded

    def stats(self) -> dict:
        self._update_stats()
        return {
            **self._stats,
            "total_engines": len(self._engines),
            "kelly_fraction": KELLY_FRACTION,
            "kelly_ceiling": KELLY_CEILING,
            "kelly_window": KELLY_WINDOW,
        }
