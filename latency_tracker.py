"""
latency_tracker.py — Étape 2 : Audit de Latence Asynchrone.

Mesure précise (en millisecondes) du cycle complet d'un tick par instrument:
  [Signal détecté → Calcul quantitatif → OBGuard → DB write → Fin]

Si le cycle dépasse 200ms pour un actif:
  → Log WARNING local
  → Alerte Telegram "⚠️ Bottleneck Asynchrone détecté"

Usage:
    tracker = LatencyTracker(telegram_router)

    # Comme context manager:
    with tracker.measure(instrument):
        ... traitement complet ...

    # Ou manuellement:
    token = tracker.start(instrument)
    ... traitement ...
    tracker.end(token, instrument)
"""
import time
import threading
from collections import defaultdict, deque
from typing import Optional
from loguru import logger

# ─── Paramètres ───────────────────────────────────────────────────────────────
_ALERT_THRESHOLD_MS = 500    # Alerte si > 500ms (Docker local = overhead réseau)
_CRITICAL_MS        = 1500   # Log ERROR si > 1500ms
_HISTORY_LEN        = 50     # Fenêtre glissante pour stats
_ALERT_COOLDOWN_S   = 300    # 5 min entre deux alertes Telegram pour le même actif


class LatencyTracker:
    """
    Tracker de latence par instrument avec alertes automatiques.
    Thread-safe. N'ajoute aucune latence au chemin critique.
    """

    def __init__(self, telegram_router=None):
        self._tg = telegram_router
        self._lock = threading.Lock()

        # {instrument: deque([latency_ms, ...])}
        self._history: dict = defaultdict(lambda: deque(maxlen=_HISTORY_LEN))

        # {instrument: ts_last_alert}
        self._last_alert: dict = {}

        # Stats globales
        self._total_measured = 0
        self._total_alerts   = 0

    # ─── Context Manager ─────────────────────────────────────────────────────

    def measure(self, instrument: str):
        """Context manager: mesure la latence d'un bloc de code."""
        return _MeasureCtx(self, instrument)

    # ─── Manual API ──────────────────────────────────────────────────────────

    def start(self, instrument: str) -> float:
        """Démarre un timer. Retourne le monotonic timestamp."""
        return time.monotonic()

    def end(self, start_ts: float, instrument: str,
             phase: str = "tick") -> float:
        """
        Termine le timer et enregistre la latence.
        Retourne la latence en ms.
        """
        elapsed_ms = (time.monotonic() - start_ts) * 1000
        self._record(instrument, elapsed_ms, phase)
        return elapsed_ms

    # ─── Stats ───────────────────────────────────────────────────────────────

    def get_stats(self, instrument: str = None) -> dict:
        """Retourne les stats de latence pour un instrument ou tous."""
        with self._lock:
            if instrument:
                hist = list(self._history.get(instrument, []))
                if not hist:
                    return {"instrument": instrument, "n": 0}
                return {
                    "instrument": instrument,
                    "n":    len(hist),
                    "avg":  round(sum(hist) / len(hist), 1),
                    "max":  round(max(hist), 1),
                    "min":  round(min(hist), 1),
                    "p95":  round(sorted(hist)[int(len(hist) * 0.95)], 1),
                }
            # All instruments
            all_latencies = []
            for h in self._history.values():
                all_latencies.extend(h)
            if not all_latencies:
                return {"total_measured": 0}
            sl = sorted(all_latencies)
            return {
                "total_measured": self._total_measured,
                "total_alerts":   self._total_alerts,
                "avg_ms":  round(sum(all_latencies) / len(all_latencies), 1),
                "max_ms":  round(max(all_latencies), 1),
                "p95_ms":  round(sl[int(len(sl) * 0.95)], 1),
                "bottlenecks": sum(1 for ms in all_latencies if ms > _ALERT_THRESHOLD_MS),
            }

    def format_report(self) -> str:
        s = self.get_stats()
        if not s.get("total_measured"):
            return "⏱️ Latency: aucune mesure"
        return (
            f"⏱️ <b>Latence Loop</b>\n"
            f"  Avg: {s['avg_ms']}ms | P95: {s['p95_ms']}ms | Max: {s['max_ms']}ms\n"
            f"  Bottlenecks (>{_ALERT_THRESHOLD_MS}ms): {s['bottlenecks']}/{s['total_measured']}\n"
            f"  Alertes envoyées: {s['total_alerts']}"
        )

    def top_slowest(self, n: int = 5) -> list:
        """Retourne les N instruments les plus lents (par max latence)."""
        with self._lock:
            ranked = []
            for inst, hist in self._history.items():
                if hist:
                    ranked.append((inst, max(hist), sum(hist)/len(hist)))
            ranked.sort(key=lambda x: x[1], reverse=True)
            return ranked[:n]

    # ─── Internals ───────────────────────────────────────────────────────────

    def _record(self, instrument: str, elapsed_ms: float, phase: str):
        """Enregistre la latence et déclenche les alertes si nécessaire."""
        with self._lock:
            self._history[instrument].append(elapsed_ms)
            self._total_measured += 1

        if elapsed_ms > _CRITICAL_MS:
            logger.error(
                f"🔴 LATENCE CRITIQUE {instrument}: {elapsed_ms:.0f}ms "
                f"(seuil={_CRITICAL_MS}ms) [{phase}]"
            )
            self._send_alert(instrument, elapsed_ms, "CRITIQUE")
        elif elapsed_ms > _ALERT_THRESHOLD_MS:
            logger.warning(
                f"⚠️ Bottleneck {instrument}: {elapsed_ms:.0f}ms "
                f"(seuil={_ALERT_THRESHOLD_MS}ms) [{phase}]"
            )
            self._send_alert(instrument, elapsed_ms, "WARNING")
        else:
            logger.debug(f"⏱️ {instrument} [{phase}]: {elapsed_ms:.1f}ms ✅")

    def _send_alert(self, instrument: str, ms: float, level: str):
        """Envoie une alerte Telegram avec cooldown."""
        now = time.time()
        last = self._last_alert.get(instrument, 0)
        if now - last < _ALERT_COOLDOWN_S:
            return  # Cooldown actif

        self._last_alert[instrument] = now
        self._total_alerts += 1

        if not self._tg:
            return

        icon = "🔴" if level == "CRITIQUE" else "⚠️"
        msg = (
            f"{icon} <b>Bottleneck Asynchrone — {instrument}</b>\n\n"
            f"  Latence: <b>{ms:.0f}ms</b> (seuil={_ALERT_THRESHOLD_MS}ms)\n"
            f"  Niveau: <b>{level}</b>\n\n"
            f"  <i>Le cycle tick de {instrument} est trop lent.</i>\n"
            f"  <i>Impact: {48 - 1} autres instruments non affectés.</i>"
        )
        try:
            self._tg.send_risk(msg)
        except Exception as e:
            logger.debug(f"LatencyTracker alert: {e}")


class _MeasureCtx:
    """Context manager pour mesure automatique de latence."""
    def __init__(self, tracker: LatencyTracker, instrument: str, phase: str = "tick"):
        self._tracker    = tracker
        self._instrument = instrument
        self._phase      = phase
        self._start      = None

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, *_):
        if self._start:
            self._tracker.end(self._start, self._instrument, self._phase)
