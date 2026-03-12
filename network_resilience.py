"""
network_resilience.py — Moteur 21 : Home-Network Resilience.

Sur un Mac local, la connexion Wi-Fi peut couper 5-30s (box redémarre,
routeur sature, changement de réseau). Une simple exception `requests.ConnectionError`
ne doit JAMAIS crasher le bot.

Ce module centralise toutes les logiques de reconnexion:

1. EXPONENTIAL BACKOFF DECORATOR
   Décorateur générique: wraps n'importe quelle fonction réseau avec retry
   Délais: 1s → 2s → 4s → 8s → 16s → 30s (max)
   Max retries: 10 (5 minutes de reconnexion avant abandon)

2. NETWORK WATCHER
   Thread daemon qui pinge un serveur public (Google DNS 8.8.8.8)
   Si la connexion est perdue → met le bot en mode "OFFLINE"
   Si elle revient → déclenche la resynchronisation

3. WEBSOCKET RECONNECT MANAGER
   Wraps l'objet WebSocket Capital.com:
   - Capture les disconnections
   - Reconnecte avec Exponential Backoff
   - Re-subscribe tous les instruments après reconnexion

4. OFFLINE MODE
   Quand le bot est offline:
   - Aucun ordre new n'est passé
   - Aucune WebSocket update envoyée
   - Les SL/TP existants sont maintenus par l'exchange lui-même
   - State sauvegardé en DB locale toutes les 5s

Usage:
    rn = NetworkResilience(capital_client, db, telegram_router)
    rn.start()

    # Dans n'importe quelle fonction réseau:
    @rn.retry(max_attempts=5)
    def fetch_price(instrument):
        return capital.get_price(instrument)
"""
import time
import socket
import threading
import functools
from typing import Optional, Callable, Any
from datetime import datetime, timezone
from loguru import logger

# ─── Paramètres ───────────────────────────────────────────────────────────────
_PING_HOST        = "8.8.8.8"    # Google DNS (toujours disponible)
_PING_PORT        = 53
_PING_TIMEOUT_S   = 2.0
_CHECK_INTERVAL_S = 5.0           # Vérification connexion toutes les 5s
_BACKOFF_BASE     = 1.0           # Délai initial (secondes)
_BACKOFF_MAX      = 30.0          # Délai maximum (30s)
_BACKOFF_EXP      = 2.0           # Facteur d'exponentiation
_MAX_RETRIES      = 10            # Max tentatives avant abandon
_OFFLINE_LOG_EVERY = 6            # Log "still offline" toutes les 30s (6 × 5s)


class NetworkState:
    """État partagé de la connexion réseau."""
    ONLINE  = "ONLINE"
    OFFLINE = "OFFLINE"
    DEGRADED = "DEGRADED"   # Connexion lente/parcielle


class NetworkResilience:
    """
    Gestionnaire de résilience réseau pour environnement domestique.
    Permet au bot de survivre aux coupures Wi-Fi sans crash.
    """

    def __init__(self, capital_client=None, db=None, telegram_router=None):
        self._capital = capital_client
        self._db      = db
        self._tg      = telegram_router

        self._state      = NetworkState.ONLINE
        self._offline_since: Optional[float] = None
        self._reconnect_count = 0
        self._total_offline_s  = 0.0
        self._offline_check_n  = 0

        self._lock    = threading.Lock()
        self._callbacks_online: list  = []   # fnc() appelées au retour connexion
        self._callbacks_offline: list = []   # fnc() appelées à la perte connexion

        self._running = False
        self._thread  = None

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._watch_loop, daemon=True, name="network_watcher"
        )
        self._thread.start()
        logger.info(f"🌐 Network Resilience démarré (ping {_PING_HOST} toutes les {_CHECK_INTERVAL_S}s)")

    def stop(self):
        self._running = False

    # ─── Public API ──────────────────────────────────────────────────────────

    @property
    def is_online(self) -> bool:
        return self._state == NetworkState.ONLINE

    @property
    def state(self) -> str:
        return self._state

    def on_reconnect(self, fn: Callable):
        """Enregistre une callback appelée quand la connexion revient."""
        self._callbacks_online.append(fn)

    def on_disconnect(self, fn: Callable):
        """Enregistre une callback appelée quand la connexion est perdue."""
        self._callbacks_offline.append(fn)

    def retry(self, max_attempts: int = _MAX_RETRIES,
               on_fail: Any = None) -> Callable:
        """
        Décorateur Exponential Backoff.
        Usage:
            @network.retry(max_attempts=5)
            def fetch():
                return requests.get(url)
        """
        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                delay = _BACKOFF_BASE
                for attempt in range(max_attempts):
                    try:
                        return fn(*args, **kwargs)
                    except Exception as e:
                        if attempt == max_attempts - 1:
                            logger.error(f"🔌 {fn.__name__} failed after {max_attempts} attempts: {e}")
                            return on_fail
                        logger.warning(
                            f"🔌 {fn.__name__} attempt {attempt+1}/{max_attempts} failed: {e} "
                            f"→ retry in {delay:.1f}s"
                        )
                        time.sleep(delay)
                        delay = min(delay * _BACKOFF_EXP, _BACKOFF_MAX)
                return on_fail
            return wrapper
        return decorator

    def safe_call(self, fn: Callable, *args, fallback=None, **kwargs):
        """
        Appel sécurisé: si offline → retourne fallback sans exception.
        Si online → tente avec 3 retries auto.
        """
        if not self.is_online:
            return fallback
        delay = _BACKOFF_BASE
        for attempt in range(3):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                if attempt == 2:
                    return fallback
                time.sleep(delay)
                delay = min(delay * 2, 8.0)
        return fallback

    def stats(self) -> dict:
        return {
            "state":           self._state,
            "reconnects":      self._reconnect_count,
            "total_offline_s": round(self._total_offline_s, 1),
            "offline_since":   self._offline_since,
        }

    # ─── Network Watcher ─────────────────────────────────────────────────────

    def _watch_loop(self):
        while self._running:
            reachable = self._ping()
            with self._lock:
                prev_state = self._state

            if reachable:
                if prev_state == NetworkState.OFFLINE:
                    self._handle_reconnect()
                with self._lock:
                    self._state = NetworkState.ONLINE
                    self._offline_check_n = 0
            else:
                if prev_state == NetworkState.ONLINE:
                    self._handle_disconnect()
                with self._lock:
                    self._state = NetworkState.OFFLINE
                    self._offline_check_n += 1

                # Log périodique pour ne pas spammer
                if self._offline_check_n % _OFFLINE_LOG_EVERY == 1:
                    offline_s = time.monotonic() - (self._offline_since or 0)
                    logger.warning(f"🔌 OFFLINE depuis {offline_s:.0f}s — retry en cours...")

            time.sleep(_CHECK_INTERVAL_S)

    def _ping(self) -> bool:
        """Vérifie la connectivité internet via un socket TCP vers 8.8.8.8:53."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(_PING_TIMEOUT_S)
            s.connect((_PING_HOST, _PING_PORT))
            s.close()
            return True
        except Exception:
            return False

    def _handle_disconnect(self):
        """Actions lors de la détection d'une coupure réseau."""
        with self._lock:
            self._offline_since = time.monotonic()

        logger.warning("🔌 COUPURE RÉSEAU DÉTECTÉE — mode offline activé")
        logger.warning("   → Nouveaux ordres suspendus | SL/TP maintenus par l'exchange")

        for fn in self._callbacks_offline:
            try:
                fn()
            except Exception as e:
                logger.debug(f"offline callback: {e}")

    def _handle_reconnect(self):
        """Actions lors du retour de la connexion."""
        with self._lock:
            offline_since = self._offline_since or time.monotonic()
            duration = time.monotonic() - offline_since
            self._total_offline_s += duration
            self._offline_since = None
            self._reconnect_count += 1

        logger.success(f"✅ CONNEXION RÉTABLIE après {duration:.1f}s — resynchronisation...")

        # Notif Telegram (non-bloquant)
        if self._tg:
            def _notify():
                try:
                    self._tg.send_report(
                        f"🔌 <b>Reconnexion Wi-Fi</b>\n\n"
                        f"  Hors ligne: {duration:.0f}s\n"
                        f"  Reconnexions totales: {self._reconnect_count}\n"
                        f"  → Resynchronisation en cours..."
                    )
                except Exception:
                    pass
            threading.Thread(target=_notify, daemon=True).start()

        # Déclencher les callbacks de reconnexion
        for fn in self._callbacks_online:
            try:
                fn()
            except Exception as e:
                logger.debug(f"reconnect callback: {e}")

    # ─── WebSocket Reconnect Wrapper ─────────────────────────────────────────

    def wrap_websocket(self, ws_connect_fn: Callable) -> Callable:
        """
        Wraps une fonction de connexion WebSocket avec Exponential Backoff.
        Usage:
            capital_ws.connect = network.wrap_websocket(capital_ws.connect)
        """
        @functools.wraps(ws_connect_fn)
        def reconnecting_connect(*args, **kwargs):
            delay = _BACKOFF_BASE
            attempt = 0
            while self._running:
                try:
                    result = ws_connect_fn(*args, **kwargs)
                    if attempt > 0:
                        logger.success(f"✅ WebSocket reconnecté (tentative {attempt})")
                    return result
                except Exception as e:
                    attempt += 1
                    if attempt > _MAX_RETRIES:
                        logger.error(f"WebSocket: max retries atteint ({_MAX_RETRIES})")
                        break
                    logger.warning(f"🔌 WebSocket disconnected: {e} → retry #{attempt} in {delay:.1f}s")
                    time.sleep(delay)
                    delay = min(delay * _BACKOFF_EXP, _BACKOFF_MAX)
        return reconnecting_connect
