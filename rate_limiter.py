"""
rate_limiter.py — Rate-Limit Guardian pour Capital.com API.

Gestion intelligente des requêtes HTTP:
- File de priorité: CRITICAL (SL/TP/close) > HIGH (entry/order) > LOW (scan/price)
- Compteur de poids glissant par fenêtre de 1 seconde
- Backoff automatique sur 429 (Too Many Requests)
- Jitter aléatoire pour éviter les bursts synchronisés avec d'autres instances
"""
import time
import random
import threading
from collections import deque
from enum import IntEnum
from loguru import logger


class Priority(IntEnum):
    CRITICAL = 0   # close_position, modify_stop → jamais throttlé
    HIGH     = 1   # place_market_order, place_limit_order
    LOW      = 2   # scan prix, ohlcv, indicateurs


# Limites Capital.com (conservateur : limite réelle inconnue, on reste à 50%)
_MAX_WEIGHT_PER_SEC = 10      # requêtes/s autorisées
_MAX_WEIGHT_429_BACKOFF = 30  # secondes d'attente si 429 reçu
_WINDOW_MS = 1000             # fenêtre glissante en ms


class RateLimiter:
    """
    Gestionnaire de débit adaptatif.

    Usage:
        rl = RateLimiter()
        with rl.throttle(Priority.HIGH):
            response = requests.get(...)
        if response.status_code == 429:
            rl.on_429()
    """

    def __init__(self,
                 max_per_sec: int = _MAX_WEIGHT_PER_SEC,
                 backoff_sec: int = _MAX_WEIGHT_429_BACKOFF):
        self._max     = max_per_sec
        self._backoff = backoff_sec
        self._lock    = threading.Lock()
        # Timestamps des requêtes dans la fenêtre glissante
        self._window: deque = deque()
        self._banned_until: float = 0.0
        self._total_throttled = 0
        self._total_429 = 0

    # ─── Public API ──────────────────────────────────────────────────────────

    def throttle(self, priority: Priority = Priority.LOW):
        """Context manager — bloque si nécessaire selon la priorité."""
        return _ThrottleCtx(self, priority)

    def acquire(self, priority: Priority = Priority.LOW):
        """
        Attend que la requête puisse s'exécuter selon la priorité.
        CRITICAL : ne bloque JAMAIS plus de 0.1s.
        HIGH     : bloque max 1s.
        LOW      : bloque jusqu'à ce que le débit soit libre.
        """
        if priority == Priority.CRITICAL:
            # Requête critique — on laisse passer quoi qu'il arrive
            self._record()
            return

        # Vérifier si on est en ban 429
        remaining_ban = self._banned_until - time.monotonic()
        if remaining_ban > 0:
            if priority == Priority.HIGH:
                wait = min(remaining_ban, 2.0)
                logger.warning(f"⏳ Rate: HIGH throttle {wait:.1f}s (429 backoff)")
                time.sleep(wait)
            else:
                logger.debug(f"⏳ Rate: LOW throttle {remaining_ban:.1f}s (429 backoff)")
                time.sleep(remaining_ban)

        # Fenêtre glissante
        max_wait = 1.0 if priority == Priority.HIGH else 10.0
        waited = 0.0
        step = 0.05

        while waited < max_wait:
            with self._lock:
                now_ms = time.monotonic() * 1000
                # Purge timestamps hors fenêtre
                while self._window and now_ms - self._window[0] > _WINDOW_MS:
                    self._window.popleft()

                if len(self._window) < self._max:
                    self._window.append(now_ms)
                    return  # GO

            # Sleep avec jitter
            sleep_t = step + random.uniform(0, 0.02)
            time.sleep(sleep_t)
            waited += sleep_t
            self._total_throttled += 1

        # Timeout dépassé → on laisse passer quand même (mieux que deadlock)
        self._record()

    def on_429(self, retry_after: int = _MAX_WEIGHT_429_BACKOFF):
        """Appelé quand Capital.com répond 429. Active le backoff."""
        self._total_429 += 1
        self._banned_until = time.monotonic() + retry_after
        logger.warning(
            f"🚫 Rate-Limit 429 capté — backoff {retry_after}s "
            f"(total 429: {self._total_429})"
        )

    def stats(self) -> dict:
        return {
            "throttled_calls": self._total_throttled,
            "total_429": self._total_429,
            "window_size": len(self._window),
            "banned_for": max(0.0, self._banned_until - time.monotonic()),
        }

    # ─── Internals ───────────────────────────────────────────────────────────

    def _record(self):
        with self._lock:
            self._window.append(time.monotonic() * 1000)
            # Purge
            now_ms = time.monotonic() * 1000
            while self._window and now_ms - self._window[0] > _WINDOW_MS:
                self._window.popleft()


class _ThrottleCtx:
    def __init__(self, rl: RateLimiter, priority: Priority):
        self._rl = rl
        self._priority = priority

    def __enter__(self):
        self._rl.acquire(self._priority)
        return self

    def __exit__(self, *_):
        pass


# ─── Singleton global ─────────────────────────────────────────────────────────
_GLOBAL_RL = RateLimiter()


def get_rate_limiter() -> RateLimiter:
    """Retourne le singleton global."""
    return _GLOBAL_RL
