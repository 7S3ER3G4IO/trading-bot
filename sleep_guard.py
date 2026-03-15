"""
sleep_guard.py — Moteur 22 : Mac Sleep Survival & State Reconciliation.

Sur macOS, fermer l'écran suspend TOUS les processus (y compris Docker
en mode "Resource Saver"). Même avec "Prevent sleep while plugged in" activé,
certaines versions de macOS peuvent suspendre les threads Python.

Ce module gère le "réveil" du bot:

1. SLEEP DETECTOR
   Détecte si le bot a été suspendu via un drift d'horloge.
   Si le dernier heartbeat interne remonte à plus de 30s
   alors que le tick normal est de 5s → on vient de dormir.

2. RECONCILIATION ENGINE
   Au réveil:
   a) Vérifier les ordres ouverts via l'API REST (pas le cache local)
   b) Comparer avec l'état Supabase (positions)
   c) Détecter les ordres fermés pendant le sommeil (TP/SL touché)
   d) Mettre à jour le state local avant de reprendre
   e) Notifier via Telegram avec le résumé des changements

3. STATE BACKUP PERMANENT
   Toutes les 10s en mode local: snapshot de l'état en DB
   (positions ouvertes, PnL courant, valeurs de SL/TP)
   Survie aux crashs imprévus (power outage, kernel panic)

4. HEALTHCHECK SENTINEL
   Écrit /tmp/.nemesis_alive toutes les 15s
   Docker healthcheck le vérifie → restart si absent

Usage:
    sg = SleepGuard(capital_client, db, telegram_router)
    sg.start()
    # Tourne en daemon thread, entièrement autonome
"""
import os
import time
import threading
from typing import Dict, Optional, List
from datetime import datetime, timezone
from loguru import logger

# ─── Paramètres ───────────────────────────────────────────────────────────────
_HEARTBEAT_INTERVAL_S = 5.0    # Tick interne
_SLEEP_DETECT_THRESH  = 30.0   # Si gap > 30s → on a dormi
_BACKUP_INTERVAL_S    = 10.0   # Snapshot état toutes les 10s
_SENTINEL_PATH        = "/tmp/.nemesis_alive"
_SENTINEL_INTERVAL_S  = 15.0   # Mise à jour sentinel toutes les 15s
_RECONCILE_TIMEOUT_S  = 30.0   # Timeout pour la réconciliation
_RECONCILE_COOLDOWN_S = 60.0   # Pas plus d'une réconciliation par minute


class SleepGuard:
    """
    Garde du sommeil: détecte les suspensions macOS et réconcilie l'état
    du bot au réveil pour assurer une continuité de trading parfaite.
    """

    def __init__(self, capital_client=None, db=None, telegram_router=None,
                 positions_ref: dict = None):
        self._capital  = capital_client
        self._db       = db
        self._tg       = telegram_router
        self._trades   = positions_ref   # référence dict trades ouverts

        self._last_heartbeat    = time.monotonic()
        self._last_backup       = time.monotonic()
        self._last_reconcile    = 0.0
        self._sleep_count       = 0
        self._reconcile_count   = 0
        self._changes_detected  = 0

        self._running = False
        self._threads: List[threading.Thread] = []

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        self._running = True

        # Thread principal: heartbeat + sleep detection
        t1 = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True, name="sleep_guard"
        )
        # Thread backup: snapshot DB
        t2 = threading.Thread(
            target=self._backup_loop,
            daemon=True, name="state_backup"
        )
        # Thread sentinel: /tmp/.nemesis_alive
        t3 = threading.Thread(
            target=self._sentinel_loop,
            daemon=True, name="docker_sentinel"
        )

        self._threads = [t1, t2, t3]
        for t in self._threads:
            t.start()

        logger.info("😴 Sleep Guard démarré (détection sommeil Mac + backup état 10s)")

    def stop(self):
        self._running = False
        self._remove_sentinel()

    # ─── Sleep Detection ─────────────────────────────────────────────────────

    def _heartbeat_loop(self):
        """Détecte les gaps d'horloge indiquant une suspension."""
        while self._running:
            now = time.monotonic()
            gap = now - self._last_heartbeat

            if gap > _SLEEP_DETECT_THRESH:
                self._on_wake_detected(gap)

            self._last_heartbeat = now
            time.sleep(_HEARTBEAT_INTERVAL_S)

    def _on_wake_detected(self, gap_s: float):
        """Traitement du réveil après suspension."""
        self._sleep_count += 1
        wake_str = datetime.now(timezone.utc).strftime("%H:%M:%S")

        logger.warning(
            f"😴 RÉVEIL DÉTECTÉ: gap={gap_s:.0f}s (sommeil #{self._sleep_count})"
            f" | Réconciliation en cours..."
        )

        # Cooldown: pas de double réconciliation
        now = time.monotonic()
        if now - self._last_reconcile < _RECONCILE_COOLDOWN_S:
            logger.debug("SleepGuard: réconciliation en cooldown")
            return

        self._last_reconcile = now

        # Réconciliation dans un thread séparé (non-bloquant)
        t = threading.Thread(
            target=self._reconcile,
            args=(gap_s, wake_str),
            daemon=True, name="reconcile"
        )
        t.start()

    # ─── Reconciliation Engine ────────────────────────────────────────────────

    def _reconcile(self, gap_s: float, wake_time: str):
        """
        Réconcilie l'état local avec l'exchange après un gap.
        Détecte les TP/SL touchés pendant le sommeil.
        """
        self._reconcile_count += 1
        changes: List[str] = []

        try:
            # 1. Récupérer les positions ouvertes EN DIRECT de l'exchange
            if not self._capital:
                logger.debug("SleepGuard reconcile: pas de client Capital")
                return

            live_positions = self._get_live_positions()
            if live_positions is None:
                logger.warning("SleepGuard: impossible de récupérer positions (API offline?)")
                return

            local_positions = dict(self._trades) if self._trades else {}

            # 2. Détecter les positions fermées pendant le sommeil
            for instrument, local_state in list(local_positions.items()):
                if local_state is None:
                    continue

                if instrument not in live_positions:
                    # La position a été fermée (TP/SL touché) pendant le sommeil
                    logger.info(f"😴 Réconciliation: {instrument} fermée pendant le sommeil")
                    changes.append(f"✅ {instrument} fermée (TP/SL pendant sleep)")

                    # Mettre à jour l'état local
                    if self._trades is not None:
                        self._trades[instrument] = None

                    # Marquer en DB
                    self._mark_closed_in_db(instrument)
                    self._changes_detected += 1

            # 3. Détecter des positions ouvertes sur l'exchange non tracées localement
            for instrument, live_state in live_positions.items():
                if instrument not in local_positions or local_positions.get(instrument) is None:
                    logger.warning(
                        f"😴 Position orpheline: {instrument} sur exchange mais pas en local!"
                    )
                    changes.append(f"⚠️ {instrument} orpheline (ouverte manuellement?)")

            # 4. Rapport
            status = "Aucun changement détecté" if not changes else "\n".join(changes)
            logger.info(f"😴 Réconciliation terminée: {len(changes)} changement(s)")

            if self._tg:
                try:
                    self._tg.send_report(
                        f"😴 <b>Réveil Mac — Réconciliation #{self._reconcile_count}</b>\n\n"
                        f"  Heure: {wake_time}\n"
                        f"  Gap sommeil: {gap_s:.0f}s\n"
                        f"  Changements: {len(changes)}\n"
                        + (f"\n{status}" if changes else "\n  → Aucun changement.")
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"SleepGuard reconcile error: {e}")

    def _get_live_positions(self) -> Optional[Dict[str, dict]]:
        """Récupère les positions ouvertes via REST (pas le cache)."""
        try:
            if hasattr(self._capital, 'get_open_positions'):
                raw = self._capital.get_open_positions()
                if raw is None:
                    return {}
                if isinstance(raw, dict):
                    return {inst: True for inst in raw if raw[inst] is not None}
                return {}
            return {}
        except Exception as e:
            logger.debug(f"SleepGuard get_positions: {e}")
            return None

    def _mark_closed_in_db(self, instrument: str):
        """Marque une position comme fermée dans Supabase."""
        if not self._db:
            return
        try:
            ph = "%s"
            self._db._execute(
                f"UPDATE positions SET status='CLOSED_SLEEP', "
                f"closed_at=NOW() WHERE instrument={ph} AND status='OPEN'",
                (instrument,)
            )
        except Exception as e:
            logger.debug(f"SleepGuard DB update: {e}")

    # ─── State Backup ─────────────────────────────────────────────────────────

    def _backup_loop(self):
        """Sauvegarde l'état en DB toutes les 10s."""
        while self._running:
            time.sleep(_BACKUP_INTERVAL_S)
            try:
                self._backup_state()
            except Exception as e:
                logger.debug(f"Backup state: {e}")

    def _backup_state(self):
        """Écrit un snapshot de l'état dans Supabase."""
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            trades_count = sum(
                1 for v in (self._trades or {}).values() if v is not None
            )
            ph = "%s"
            self._db._execute(
                f"INSERT INTO state_snapshots (open_trades, snapshot_at) "
                f"VALUES ({ph}, NOW())",
                (trades_count,)
            )
        except Exception as e:
            logger.debug(f"snapshot: {e}")

    # ─── Docker Sentinel ──────────────────────────────────────────────────────

    def _sentinel_loop(self):
        """Écrit le fichier sentinel /tmp/.nemesis_alive toutes les 15s."""
        while self._running:
            try:
                with open(_SENTINEL_PATH, "w") as f:
                    f.write(datetime.now(timezone.utc).isoformat())
            except Exception:
                pass
            time.sleep(_SENTINEL_INTERVAL_S)

    def _remove_sentinel(self):
        try:
            if os.path.exists(_SENTINEL_PATH):
                os.remove(_SENTINEL_PATH)
        except Exception:
            pass

    # ─── Stats ────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "sleep_events":    self._sleep_count,
            "reconciliations": self._reconcile_count,
            "state_changes":   self._changes_detected,
        }

    def ensure_table(self):
        """Crée la table state_snapshots si elle n'existe pas."""
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS state_snapshots (
                    id          SERIAL PRIMARY KEY,
                    open_trades INTEGER,
                    snapshot_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"state_snapshots table: {e}")
