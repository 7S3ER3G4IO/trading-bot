"""
mev_shield.py — Moteur 12 : MEV Awareness & Anti-Frontrun Shield.

Sur les marchés financiers modernes, les algorithmes haute fréquence (HFT)
peuvent détecter nos patterns TWAP et nous "front-runner":
  1. Ils voient nos ordres arriver à intervalles réguliers
  2. Ils extrapolent notre intention (TWAP = gros acheteur)
  3. Ils achètent AVANT nous, puis nous revendent plus cher (+0.1-0.3%)

Solution: RENDRE NOS ORDRES STATISTIQUEMENT INVISIBLES.

Techniques implémentées:
  1. TEMPORAL RANDOMIZATION: intervalles TWAP aléatoires (distribution log-normale)
     au lieu de fixes (12s, 12s, 12s → 8s, 19s, 11s, 6s, 14s)

  2. SIZE JITTER: taille de chaque slice variée ±15%
     (0.20, 0.20, 0.20 → 0.17, 0.22, 0.19, 0.21, 0.18)

  3. DECOY TIMING: parfois attendre 1-3 secondes supplémentaires
     sans raison apparente (entropy injection)

  4. PATTERN DETECTION (self-audit): détecte si nos propres ordres
     sont pré-exécutés par quelqu'un d'autre (front-run detection)
     → si prix bouge AVANT notre ordre → MEV alert

  5. ORDER FRAGMENTATION: split aléatoire (3, 4 ou 5 slices)
     même pour un critère de taille identique

  6. TIMING WINDOWS: préférer des moments d'activité élevée
     (spread naturellement plus liquide, pattern moins visible)

Note: Sur Capital.com (CEX REST), pas de MEV blockchain.
Mais les mêmes techniques s'appliquent contre l'arbitrage inter-feed.

Usage:
    mev = MEVShield(seed=42)
    timing = mev.get_twap_schedule(total_size=1.0, base_interval=12)
    # → [(0.18, 8.3s), (0.22, 19.1s), (0.19, 11.7s), ...]

    if mev.detect_frontrun(pre_price, post_price, direction):
        logger.warning("FRONT-RUN DETECTED — délai injection")
"""
import math
import random
import time
import threading
from typing import List, Tuple, Optional
from collections import deque
from datetime import datetime, timezone
from loguru import logger

# ─── Paramètres d'obfuscation ─────────────────────────────────────────────────
_SIZE_JITTER_PCT   = 0.15   # ±15% variation de taille par slice
_TIME_JITTER_SIGMA = 0.40   # σ log-normale pour intervalles (0.40 = très aléatoire)
_DECOY_PROB        = 0.20   # 20% de chances d'injecter un délai decoy
_DECOY_MAX_S       = 3.0    # Max durée du decoy (secondes)
_FRONTRUN_THRESH   = 0.0015 # 0.15% de mouvement avant ordre = suspect
_FRONTRUN_WINDOW   = 5.0    # Fenêtre de détection (secondes avant ordre)
_MEV_HISTORY       = 100    # Historique des événements MEV

# ─── Fenêtres d'exécution préférées (UTC) ────────────────────────────────────
# Heures avec le plus de volume = front-run plus difficile (noyé dans le bruit)
_PREFERRED_HOURS_UTC = [8, 9, 10, 13, 14, 15, 16]

# ─── Nombre de slices aléatoires selon la taille ─────────────────────────────
_SLICE_OPTIONS = {
    "small":  [2, 3],      # size < 0.5 lot
    "medium": [3, 4, 5],   # 0.5 ≤ size < 2
    "large":  [4, 5, 6, 7] # size ≥ 2
}


class MEVShield:
    """
    Obfuscateur d'ordres: rend les patterns d'exécution statistiquement
    invisibles aux algorithmes de front-running et de MEV.
    """

    def __init__(self, seed: int = None):
        if seed is not None:
            random.seed(seed)

        self._frontrun_events = deque(maxlen=_MEV_HISTORY)
        self._price_history: dict = {}   # {instrument: deque of (ts, price)}
        self._lock  = threading.Lock()

        self._total_orders   = 0
        self._frontrun_count = 0
        self._decoy_injected = 0

        logger.info("🥷 MEV Shield initialisé (temporal/size obfuscation actif)")

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_twap_schedule(self, total_size: float,
                           base_interval: float = 12.0) -> List[Tuple[float, float]]:
        """
        Génère un planning TWAP obfusqué: [(slice_size, wait_seconds), ...].
        Les tailles et intervalles sont randomisés pour être statistiquement
        indiscernables d'un flux d'ordres naturel.
        """
        n_slices = self._choose_n_slices(total_size)
        slices   = self._randomize_sizes(total_size, n_slices)
        timings  = self._randomize_intervals(base_interval, n_slices)

        schedule = list(zip(slices, timings))
        self._total_orders += 1

        logger.debug(
            f"🥷 MEV schedule: {n_slices} slices | "
            f"sizes: {[round(s,2) for s in slices]} | "
            f"intervals: {[round(t,1) for t in timings]}s"
        )
        return schedule

    def inject_decoy_delay(self) -> float:
        """
        Injecte aléatoirement un délai "decoy" (faux signal de réflexion).
        Returns: secondes à attendre (0 si pas de decoy).
        """
        if random.random() < _DECOY_PROB:
            delay = random.uniform(0.5, _DECOY_MAX_S)
            self._decoy_injected += 1
            logger.debug(f"🥷 Decoy delay: {delay:.1f}s")
            return delay
        return 0.0

    def record_price(self, instrument: str, price: float):
        """Enregistre un prix pour la détection de front-run."""
        with self._lock:
            if instrument not in self._price_history:
                self._price_history[instrument] = deque(maxlen=20)
            self._price_history[instrument].append((time.monotonic(), price))

    def detect_frontrun(self, instrument: str, direction: str,
                         entry_price: float) -> bool:
        """
        Détecte si un front-runner a bougé le prix avant notre ordre.
        Retourne True si front-run suspecté.
        """
        with self._lock:
            history = list(self._price_history.get(instrument, []))

        if len(history) < 3:
            return False

        # Prix il y a ~5s
        now = time.monotonic()
        older_prices = [p for (ts, p) in history if now - ts > _FRONTRUN_WINDOW and p > 0]
        if not older_prices:
            return False

        ref_price  = older_prices[-1]
        move_pct   = (entry_price - ref_price) / ref_price

        # Si BUY et prix déjà monté de >0.15% → quelqu'un nous a frontrunné
        frontrun_detected = (
            (direction == "BUY"  and move_pct > _FRONTRUN_THRESH) or
            (direction == "SELL" and move_pct < -_FRONTRUN_THRESH)
        )

        if frontrun_detected:
            self._frontrun_count += 1
            event = {
                "instrument": instrument,
                "direction": direction,
                "move_pct": round(move_pct * 100, 3),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            with self._lock:
                self._frontrun_events.append(event)
            logger.warning(
                f"🥷 FRONT-RUN DÉTECTÉ: {instrument} {direction} "
                f"prix pré-bougé {move_pct:+.3%} dans les {_FRONTRUN_WINDOW}s"
            )

        return frontrun_detected

    def is_preferred_execution_window(self) -> bool:
        """Retourne True si nous sommes dans une fenêtre de liquidité haute."""
        hour = datetime.now(timezone.utc).hour
        return hour in _PREFERRED_HOURS_UTC

    def get_execution_quality_score(self, expected_price: float,
                                     executed_price: float,
                                     direction: str) -> float:
        """
        Qualité d'exécution: 1.0 = parfait, <0.75 = mauvaise exécution.
        Indique si nous nous sommes fait front-runner sur ce trade.
        """
        if expected_price <= 0:
            return 1.0
        slippage = (executed_price - expected_price) / expected_price
        if direction == "BUY":
            # BUY: slippage négatif = on a payé moins → bon
            impact = -slippage
        else:
            impact = slippage

        # Score: 1.0 si pas de slippage, 0.0 si 0.5%+ de slippage
        return max(0.0, min(1.0, 1.0 - impact / 0.005))

    def stats(self) -> dict:
        return {
            "total_orders":    self._total_orders,
            "frontrun_events": self._frontrun_count,
            "decoy_delays":    self._decoy_injected,
            "recent_events":   list(self._frontrun_events)[-5:],
        }

    def format_report(self) -> str:
        s = self.stats()
        return (
            f"🥷 <b>MEV Shield</b>\n"
            f"  Ordres obfusqués: {s['total_orders']}\n"
            f"  Front-runs détectés: {s['frontrun_events']}\n"
            f"  Decoy delays: {s['decoy_delays']}"
        )

    # ─── Randomization Internals ─────────────────────────────────────────────

    def _choose_n_slices(self, total_size: float) -> int:
        """Choisit aléatoirement le nombre de slices selon la taille."""
        if total_size < 0.5:
            options = _SLICE_OPTIONS["small"]
        elif total_size < 2.0:
            options = _SLICE_OPTIONS["medium"]
        else:
            options = _SLICE_OPTIONS["large"]
        return random.choice(options)

    def _randomize_sizes(self, total: float, n: int) -> List[float]:
        """
        Génère n tailles qui somment à total, avec jitter ±15%.
        Utilise distribution de Dirichlet approchée.
        """
        if n <= 0:
            return [total]

        base = total / n
        sizes = []
        for i in range(n - 1):
            jitter = random.uniform(1 - _SIZE_JITTER_PCT, 1 + _SIZE_JITTER_PCT)
            sizes.append(round(base * jitter, 3))

        # Dernière slice = reste pour garantir que la somme = total
        remaining = round(total - sum(sizes), 3)
        sizes.append(max(0.001, remaining))

        return sizes

    def _randomize_intervals(self, base_interval: float, n: int) -> List[float]:
        """
        Génère n intervalles avec distribution log-normale.
        Log-normale: plus naturelle que uniforme (ressemble aux ordres humains).
        μ = log(base), σ = 0.40
        """
        mu   = math.log(max(base_interval, 1.0))
        intervals = []
        for _ in range(n):
            # Log-normale: e^(μ + σ*N(0,1))
            z     = random.gauss(0, _TIME_JITTER_SIGMA)
            inter = math.exp(mu + z)
            inter = max(1.5, min(inter, base_interval * 3))  # Clamp [1.5s, 3×base]
            intervals.append(round(inter, 1))
        return intervals
