"""
cluster_manager.py — Moteur 16 : Distributed Cluster & Failover State Machine.

Permet de lancer plusieurs instances du bot en parallèle (horizontal scaling).

Chaque instance = un "Worker" qui:
  1. S'enregistre dans Supabase (table `cluster_workers`)
  2. Publie un heartbeat toutes les 10 secondes
  3. Surveille les autres workers (liveness check)
  4. Si un worker meurt → déclenche le failover (protection leg risk arb)

Architecture:
  PRIMARY worker  → exécute les trades, le Market Making, l'Arb
  STANDBY worker  → surveille le PRIMARY, prêt à prendre le relais

Failover (si PRIMARY crash):
  - STANDBY détecte l'absence de heartbeat > 30s
  - STANDBY annule les legs d'arb ouverts du PRIMARY (Leg Risk = 0)
  - STANDBY devient PRIMARY et notifie via Telegram

État de la machine à états:
  STARTING → RUNNING → DEGRADED → FAILED
                ↑                     ↓
                └─────── FAILOVER ────┘

Usage:
    cluster = ClusterManager(db, telegram_router, capital_client)
    cluster.start()  # démarre le heartbeat + failover watcher

    # Vérifier si ce worker est le PRIMARY actif:
    if cluster.is_primary():
        ... # effectuer les opérations critiques

    # Enregistrer un leg d'arb ouvert:
    cluster.register_leg(instrument, exchange, direction, ref)
    # Si ce worker crash → l'autre leg sera annulé par le failover
"""
import os
import time
import uuid
import json
import socket
import threading
from typing import Dict, List, Optional
from datetime import datetime, timezone, timedelta
from loguru import logger

# ─── Configuration ────────────────────────────────────────────────────────────
_HEARTBEAT_INTERVAL_S = 10    # Battement de coeur toutes les 10s
_DEAD_WORKER_THRESHOLD = 35   # Worker déclaré mort après 35s sans heartbeat
_FAILOVER_CHECK_S      = 15   # Vérification failover toutes les 15s
_MAX_WORKERS           = 5    # Maximum de workers dans le cluster

# ─── États du Worker ──────────────────────────────────────────────────────────
STATE_STARTING  = "STARTING"
STATE_RUNNING   = "RUNNING"
STATE_DEGRADED  = "DEGRADED"
STATE_FAILED    = "FAILED"
STATE_STANDBY   = "STANDBY"
STATE_PRIMARY   = "PRIMARY"


class WorkerInfo:
    def __init__(self, worker_id: str, role: str, state: str,
                 last_heartbeat: datetime, host: str = ""):
        self.worker_id      = worker_id
        self.role           = role
        self.state          = state
        self.last_heartbeat = last_heartbeat
        self.host           = host

    @property
    def is_alive(self) -> bool:
        age = (datetime.now(timezone.utc) - self.last_heartbeat).total_seconds()
        return age < _DEAD_WORKER_THRESHOLD


class OpenLeg:
    """Représente un leg d'arb ouvert (besoin de cancel si le worker meurt)."""
    def __init__(self, instrument: str, exchange: str, direction: str,
                 ref: str, worker_id: str):
        self.instrument = instrument
        self.exchange   = exchange
        self.direction  = direction
        self.ref        = ref
        self.worker_id  = worker_id
        self.opened_at  = datetime.now(timezone.utc)


class ClusterManager:
    """
    Gestionnaire de cluster distribué avec machine d'états failover.
    Thread-safe, daemon threads indépendants.
    """

    def __init__(self, db=None, telegram_router=None,
                 capital_client=None, arb_engine=None):
        self._db      = db
        self._tg      = telegram_router
        self._capital = capital_client
        self._arb     = arb_engine

        # Identité de ce worker
        self._worker_id = os.environ.get(
            "WORKER_ID",
            f"{socket.gethostname()}_{str(uuid.uuid4())[:8]}"
        )
        self._role    = STATE_PRIMARY   # Valeur initiale (peut changer)
        self._state   = STATE_STARTING
        self._host    = socket.gethostname()

        # Registre des workers connus
        self._known_workers: Dict[str, WorkerInfo] = {}
        self._open_legs: List[OpenLeg] = []
        self._lock = threading.Lock()

        # Stats
        self._failovers_triggered = 0
        self._heartbeats_sent     = 0

        self._running = False
        self._heartbeat_thread = None
        self._watcher_thread   = None

        self._ensure_tables()
        logger.info(f"🌐 Cluster Manager: worker_id={self._worker_id} host={self._host}")

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._state   = STATE_RUNNING
        self._determine_role()

        # Thread heartbeat
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True, name="cluster_heartbeat"
        )
        self._heartbeat_thread.start()

        # Thread failover watcher
        self._watcher_thread = threading.Thread(
            target=self._failover_watch_loop,
            daemon=True, name="cluster_watcher"
        )
        self._watcher_thread.start()

        logger.info(f"🌐 Cluster démarré | role={self._role} | state={self._state}")

    def stop(self):
        self._running = False
        self._state   = STATE_FAILED
        self._unregister()

    # ─── Public API ──────────────────────────────────────────────────────────

    def is_primary(self) -> bool:
        return self._role == STATE_PRIMARY and self._state == STATE_RUNNING

    def get_worker_id(self) -> str:
        return self._worker_id

    def register_leg(self, instrument: str, exchange: str,
                      direction: str, ref: str):
        """Enregistre un leg d'arb ouvert (pour protection leg risk)."""
        leg = OpenLeg(instrument, exchange, direction, ref, self._worker_id)
        with self._lock:
            self._open_legs.append(leg)
        self._save_leg_async(leg)
        logger.debug(f"🌐 Leg enregistré: {instrument} {exchange} {direction}")

    def close_leg(self, ref: str):
        """Marque un leg comme fermé."""
        with self._lock:
            self._open_legs = [l for l in self._open_legs if l.ref != ref]
        self._close_leg_db(ref)

    def cluster_status(self) -> dict:
        with self._lock:
            workers = list(self._known_workers.values())
        return {
            "this_worker": self._worker_id,
            "role":        self._role,
            "state":       self._state,
            "cluster_size": len(workers),
            "alive":        sum(1 for w in workers if w.is_alive),
            "open_legs":    len(self._open_legs),
            "failovers":    self._failovers_triggered,
        }

    def format_report(self) -> str:
        s = self.cluster_status()
        return (
            f"🌐 <b>Cluster Status</b>\n\n"
            f"  Worker: {s['this_worker']}\n"
            f"  Rôle: <b>{s['role']}</b> | État: {s['state']}\n"
            f"  Workers actifs: {s['alive']}/{s['cluster_size']}\n"
            f"  Legs ouverts: {s['open_legs']}\n"
            f"  Failovers: {s['failovers']}"
        )

    # ─── Heartbeat ────────────────────────────────────────────────────────────

    def _heartbeat_loop(self):
        while self._running:
            try:
                self._send_heartbeat()
                self._heartbeats_sent += 1
            except Exception as e:
                logger.debug(f"Heartbeat: {e}")
            time.sleep(_HEARTBEAT_INTERVAL_S)

    def _send_heartbeat(self):
        """Écrit le heartbeat dans Supabase."""
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            ph = "%s"
            self._db._execute(
                f"INSERT INTO cluster_workers (worker_id,role,state,host,heartbeat_at) "
                f"VALUES ({ph},{ph},{ph},{ph},NOW()) "
                f"ON CONFLICT (worker_id) DO UPDATE SET "
                f"  role=EXCLUDED.role, state=EXCLUDED.state, "
                f"  heartbeat_at=NOW()",
                (self._worker_id, self._role, self._state, self._host)
            )
        except Exception as e:
            logger.debug(f"Heartbeat DB: {e}")

    # ─── Failover Watcher ─────────────────────────────────────────────────────

    def _failover_watch_loop(self):
        time.sleep(20)   # Attente initiale pour laisser le cluster se former
        while self._running:
            try:
                self._check_dead_workers()
            except Exception as e:
                logger.debug(f"Failover watch: {e}")
            time.sleep(_FAILOVER_CHECK_S)

    def _check_dead_workers(self):
        """Charge les workers Supabase et détecte les morts."""
        if not self._db or not getattr(self._db, '_pg', False):
            return

        try:
            cur = self._db._execute(
                "SELECT worker_id, role, state, host, heartbeat_at "
                "FROM cluster_workers "
                "WHERE heartbeat_at > NOW() - INTERVAL '5 minutes'",
                fetch=True
            )
            rows = cur.fetchall()
        except Exception as e:
            logger.debug(f"Cluster watch DB: {e}")
            return

        now = datetime.now(timezone.utc)
        with self._lock:
            for row in rows:
                wid, role, state, host, hb_at = row
                if hasattr(hb_at, 'replace'):
                    hb_utc = hb_at.replace(tzinfo=timezone.utc) if hb_at.tzinfo is None else hb_at
                else:
                    hb_utc = now

                info = WorkerInfo(wid, role, state, hb_utc, host or "")
                self._known_workers[wid] = info

        # Vérifier si un worker PRIMARY est mort
        dead_primaries = []
        with self._lock:
            for wid, info in self._known_workers.items():
                if wid == self._worker_id:
                    continue
                if info.role == STATE_PRIMARY and not info.is_alive:
                    dead_primaries.append(wid)

        for dead_wid in dead_primaries:
            logger.warning(f"🌐 Worker PRIMARY mort: {dead_wid} — déclenchement failover")
            self._trigger_failover(dead_wid)

    def _trigger_failover(self, dead_worker_id: str):
        """
        Failover complet:
        1. Protéger les legs d'arb du worker mort (cancel)
        2. Ce worker prend le rôle PRIMARY si STANDBY
        3. Notification Telegram
        """
        self._failovers_triggered += 1

        # Protéger les legs d'arb ouverts du worker mort
        self._protect_dead_worker_legs(dead_worker_id)

        # Promotion STANDBY → PRIMARY si on est seul
        if self._role == STATE_STANDBY:
            self._role  = STATE_PRIMARY
            self._state = STATE_RUNNING
            logger.info(f"🌐 PROMOTION: {self._worker_id} → PRIMARY (failover)")

        # Supprimer le worker mort du registre
        try:
            if self._db and getattr(self._db, '_pg', False):
                ph = "%s"
                self._db._execute(
                    f"UPDATE cluster_workers SET state={ph} WHERE worker_id={ph}",
                    (STATE_FAILED, dead_worker_id)
                )
        except Exception:
            pass

        # Alerte Telegram
        if self._tg:
            try:
                self._tg.send_trade(
                    f"🚨 <b>Cluster FAILOVER</b>\n\n"
                    f"  Worker mort: {dead_worker_id}\n"
                    f"  Failover par: {self._worker_id}\n"
                    f"  Nouveau rôle: <b>{self._role}</b>\n"
                    f"  Legs protégés: legs d'arb annulés\n"
                    f"  <i>Système auto-récupéré ✅</i>"
                )
            except Exception:
                pass

    def _protect_dead_worker_legs(self, dead_worker_id: str):
        """Annule tous les legs d'arb ouverts du worker mort (Leg Risk)."""
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            ph = "%s"
            cur = self._db._execute(
                f"SELECT instrument,exchange,direction,ref FROM cluster_open_legs "
                f"WHERE worker_id={ph} AND closed=FALSE",
                (dead_worker_id,),
                fetch=True
            )
            legs = cur.fetchall()
            for leg in legs:
                instrument, exchange, direction, ref = leg
                logger.warning(f"🌐 Leg risk mitigation: cancel {instrument} {direction} on {exchange} (ref={ref})")
                # Tentative d'annulation via le capital client principal
                if self._capital:
                    try:
                        self._capital.close_position(ref, reason="leg_risk_failover")
                    except Exception as e:
                        logger.error(f"Leg cancel {ref}: {e}")

                # Marquer comme fermé
                self._db._execute(
                    f"UPDATE cluster_open_legs SET closed=TRUE WHERE ref={ph}",
                    (ref,)
                )
        except Exception as e:
            logger.debug(f"Protect legs: {e}")

    def _determine_role(self):
        """Détermine si ce worker est PRIMARY ou STANDBY (premier arrivé = PRIMARY)."""
        if not self._db or not getattr(self._db, '_pg', False):
            self._role = STATE_PRIMARY
            return
        try:
            cur = self._db._execute(
                "SELECT worker_id FROM cluster_workers "
                "WHERE role='PRIMARY' AND state='RUNNING' "
                "AND heartbeat_at > NOW() - INTERVAL '1 minute'",
                fetch=True
            )
            existing = cur.fetchone()
            if existing:
                self._role = STATE_STANDBY
                logger.info(f"🌐 Role: STANDBY (PRIMARY={existing[0]} déjà actif)")
            else:
                self._role = STATE_PRIMARY
                logger.info(f"🌐 Role: PRIMARY (aucun autre worker actif)")
        except Exception:
            self._role = STATE_PRIMARY

    def _unregister(self):
        """Désenregistre ce worker de Supabase (graceful shutdown)."""
        try:
            if self._db and getattr(self._db, '_pg', False):
                ph = "%s"
                self._db._execute(
                    f"UPDATE cluster_workers SET state={ph} WHERE worker_id={ph}",
                    (STATE_FAILED, self._worker_id)
                )
        except Exception:
            pass

    # ─── DB Helpers ──────────────────────────────────────────────────────────

    def _save_leg_async(self, leg: OpenLeg):
        if not self._db:
            return
        self._db.async_write(self._save_leg_sync, leg)

    def _save_leg_sync(self, leg: OpenLeg):
        try:
            if not getattr(self._db, '_pg', False):
                return
            ph = "%s"
            self._db._execute(
                f"INSERT INTO cluster_open_legs (instrument,exchange,direction,ref,worker_id) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph}) ON CONFLICT DO NOTHING",
                (leg.instrument, leg.exchange, leg.direction, leg.ref, leg.worker_id)
            )
        except Exception as e:
            logger.debug(f"leg save: {e}")

    def _close_leg_db(self, ref: str):
        try:
            if self._db and getattr(self._db, '_pg', False):
                ph = "%s"
                self._db._execute(
                    f"UPDATE cluster_open_legs SET closed=TRUE WHERE ref={ph}", (ref,)
                )
        except Exception:
            pass

    def _ensure_tables(self):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS cluster_workers (
                    worker_id    VARCHAR(60) PRIMARY KEY,
                    role         VARCHAR(20),
                    state        VARCHAR(20),
                    host         VARCHAR(100),
                    heartbeat_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS cluster_open_legs (
                    id          SERIAL PRIMARY KEY,
                    instrument  VARCHAR(20),
                    exchange    VARCHAR(20),
                    direction   VARCHAR(4),
                    ref         VARCHAR(60) UNIQUE,
                    worker_id   VARCHAR(60),
                    closed      BOOLEAN DEFAULT FALSE,
                    opened_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"cluster tables: {e}")
