"""
capital_websocket.py — Streaming WebSocket Capital.com.

Flux temps réel → BE activé en < 500ms après TP1 touché.

Architecture :
  - Thread daemon background (ne bloque pas la boucle principale)
  - S'authentifie via les tokens CST/SECURITY-TOKEN existants
  - S'abonne aux prix de chaque instrument avec position ouverte
  - Dès que TP1 est franchi → appelle modify_position_stop() immédiatement

WebSocket Capital.com :
  URL     : wss://api-streaming-capital.backend-capital.com/connect
            (demo : même URL, tokens du compte démo)
  Auth    : JSON {"CST": ..., "SECURITY-TOKEN": ...}
  Subscribe : {"destination": "marketData.subscribe", "payload": {"epics": [...]}}
  Prix    : {"destination": "ohlc.event", "payload": {"epic": ..., "bid": ..., "offer": ...}}
"""
import os
import json
import time
import threading
from typing import Dict, Optional, List, Callable
from loguru import logger

try:
    import websocket   # pip install websocket-client
    HAS_WS = True
except ImportError:
    HAS_WS = False

CAPITAL_DEMO = os.getenv("CAPITAL_DEMO", "true").lower() == "true"

WS_URL = "wss://api-streaming-capital.backend-capital.com/connect"

PING_INTERVAL = 300   # secondes entre chaque PING (Capital.com exige < 10 min)


class CapitalWebSocket:
    """
    WebSocket temps réel Capital.com.
    Monitore les prix et déclenche le Break-Even instantanément.
    """

    def __init__(self, capital_client, on_be_triggered: Optional[Callable] = None):
        """
        Parameters
        ----------
        capital_client : CapitalClient
            Instance déjà authentifiée du client Capital.com.
        on_be_triggered : callable(instrument, entry)
            Callback appelé quand TP1 est touché.
        """
        self._client    = capital_client
        self._on_be     = on_be_triggered
        self._ws        = None
        self._thread    = None
        self._running   = False

        # Positions surveillées : {epic: {entry, tp1, refs, tp1_hit}}
        self._watched: Dict[str, dict] = {}
        self._lock = threading.Lock()

    # ─── Interface publique ───────────────────────────────────────────────────

    def watch(self, instrument: str, entry: float,
              tp1: float, tp2: float,
              tp1_ref: str, ref2: str, ref3: str):
        """
        Ajoute un instrument à surveiller avec les 2 niveaux de trailing.
        - TP1 franchi → SL pos2 et pos3 à l'entrée (BE)
        - TP2 franchi → SL pos3 à tp1 (lock-in)
        """
        with self._lock:
            self._watched[instrument] = {
                "entry":    entry,
                "tp1":      tp1,
                "tp2":      tp2,
                "tp1_ref":  tp1_ref,
                "ref2":     ref2,
                "ref3":     ref3,
                "tp1_hit":  False,
                "tp2_hit":  False,
            }
        logger.debug(f"🔍 WS watch {instrument}  TP1={tp1:.5f}  TP2={tp2:.5f}")

        if self._ws and self._running:
            self._subscribe([instrument])

    def unwatch(self, instrument: str):
        """Retire un instrument de la surveillance."""
        with self._lock:
            self._watched.pop(instrument, None)
        logger.debug(f"✅ WS unwatch {instrument}")

    def start(self):
        """Démarre le thread WebSocket en arrière-plan."""
        if not HAS_WS:
            logger.warning("⚠️  websocket-client non installé — install: pip install websocket-client")
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()
        logger.info("📡 Capital.com WebSocket démarré (thread daemon)")

    def stop(self):
        """Arrête proprement le WebSocket."""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    # ─── WebSocket internals ──────────────────────────────────────────────────

    def _run_forever(self):
        """Boucle de reconnexion automatique."""
        while self._running:
            try:
                self._connect()
            except Exception as e:
                logger.error(f"❌ WS connexion perdue: {e}")
            if self._running:
                logger.info("🔄 WS reconnexion dans 10s...")
                time.sleep(10)

    def _connect(self):
        """Établit la connexion WebSocket Capital.com."""
        if not self._client.available:
            logger.warning("⚠️  CapitalClient non disponible — WS non démarré")
            time.sleep(30)
            return

        cst   = self._client._cst
        token = self._client._token   # X-SECURITY-TOKEN

        ws = websocket.WebSocketApp(
            WS_URL,
            on_open    = lambda ws: self._on_open(ws, cst, token),
            on_message = self._on_message,
            on_error   = lambda ws, e: logger.error(f"❌ WS error: {e}"),
            on_close   = lambda ws, *_: logger.info("🔌 WS fermé"),
        )
        self._ws = ws

        # run_forever bloque jusqu'à déconnexion
        ws.run_forever(
            ping_interval=PING_INTERVAL,
            ping_timeout=10,
        )

    def _on_open(self, ws, cst: str, token: str):
        """Authentification + abonnement aux instruments surveillés."""
        logger.info("🟢 WS Capital.com connecté")

        # 1. Authentification
        ws.send(json.dumps({
            "destination":   "sessionmanagement.connect",
            "correlationId": "auth",
            "cst":           cst,
            "securityToken": token,
        }))

        # 2. Abonnement aux instruments actuellement surveillés
        with self._lock:
            epics = list(self._watched.keys())
        if epics:
            self._subscribe(epics)

    def _subscribe(self, epics: List[str]):
        """Envoie une demande d'abonnement prix en temps réel."""
        if not self._ws:
            return
        try:
            self._ws.send(json.dumps({
                "destination":   "marketData.subscribe",
                "correlationId": "sub",
                "cst":           self._client._cst,
                "securityToken": self._client._token,
                "payload": {"epics": epics},
            }))
            logger.debug(f"📡 WS abonné : {epics}")
        except Exception as e:
            logger.error(f"❌ WS subscribe: {e}")

    def _on_message(self, ws, message: str):
        """
        Reçoit chaque tick de prix et vérifie si TP1 ou TP2 est franchi.
        Appelé des dizaines de fois par seconde — doit être ultra-rapide.
        """
        try:
            data = json.loads(message)
            dest = data.get("destination", "")

            if "ohlc" not in dest and "marketData" not in dest:
                return

            payload = data.get("payload", {})
            epic    = payload.get("epic", "")
            if not epic:
                return

            bid   = payload.get("bid")
            offer = payload.get("offer")
            if bid is None or offer is None:
                return
            mid = (float(bid) + float(offer)) / 2

            with self._lock:
                state = self._watched.get(epic)
            if state is None:
                return

            entry      = state["entry"]
            tp1        = state["tp1"]
            tp2        = state["tp2"]
            long_trade = tp1 > entry

            # ─── TP1 : BE sur pos2 et pos3 ───
            if not state["tp1_hit"]:
                tp1_hit = (mid >= tp1) if long_trade else (mid <= tp1)
                if tp1_hit:
                    self._trigger_be(epic, state, mid)

            # ─── TP2 : trailing — SL pos3 → niveau TP1 ───
            elif state["tp1_hit"] and not state["tp2_hit"]:
                tp2_hit = (mid >= tp2) if long_trade else (mid <= tp2)
                if tp2_hit:
                    self._trigger_tp2_trailing(epic, state, mid)

        except Exception as e:
            logger.error(f"❌ WS on_message: {e}")

    def _trigger_be(self, epic: str, state: dict, current_price: float):
        """
        TP1 franchi → BE immédiat sur pos2 et pos3 (SL → entrée).
        """
        with self._lock:
            if state["tp1_hit"]:
                return
            state["tp1_hit"] = True

        entry = state["entry"]
        logger.info(f"⚡ WS TP1 franchi {epic} @ {current_price:.5f} — BE pos2+pos3")

        for ref in [state["ref2"], state["ref3"]]:
            if ref:
                success = self._client.modify_position_stop(ref, entry)
                if success:
                    logger.info(f"  ✅ BE activé {ref}")

        if self._on_be:
            try:
                self._on_be(epic, entry)
            except Exception as e:
                logger.error(f"❌ WS callback BE: {e}")

    def _trigger_tp2_trailing(self, epic: str, state: dict, current_price: float):
        """
        TP2 franchi → SL pos3 déplacé au niveau TP1 (lock-in profits).
        """
        with self._lock:
            if state["tp2_hit"]:
                return
            state["tp2_hit"] = True

        tp1   = state["tp1"]
        ref3  = state["ref3"]

        logger.info(f"⚡ WS TP2 franchi {epic} @ {current_price:.5f} — SL pos3 → TP1 ({tp1:.5f})")

        if ref3:
            success = self._client.modify_position_stop(ref3, tp1)
            if success:
                logger.info(f"  ✅ Trailing pos3 activé — SL={tp1:.5f}")

        # Notification via le callback principal (réutilise on_be avec contexte tp2)
        if self._on_be:
            try:
                self._on_be(epic, tp1, "TP2")
            except Exception:
                pass
