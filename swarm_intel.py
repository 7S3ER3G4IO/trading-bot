"""
swarm_intel.py — Moteur 27 : Swarm Intelligence Orchestration

Architecture Multi-Agent System (MAS) où chaque instrument est un
micro-agent indépendant avec sa propre mémoire dans Redis.

Gossip Protocol : quand un agent détecte un événement critique
(flash-crash, whale move, anomalie), il broadcast un signal radio
à tous les agents corrélés pour une réaction coordonnée.

Architecture :
  SwarmOrchestrator → gère le cycle de vie des micro-agents
  MicroAgent        → agent autonome par instrument (mémoire Redis)
  GossipBus         → Redis pub/sub pour communication inter-agents
  CorrelationMatrix → corrélations dynamiques entre instruments

Exemple :
  Agent BTCUSD détecte whale dump → broadcast "CRYPTO_DUMP" →
  Agent ETHUSD, SOLUSD, AVAXUSD reçoivent et coupent leurs positions.
"""
import time
import threading
import json
import hashlib
from typing import Dict, Optional, List, Tuple, Set
from datetime import datetime, timezone, timedelta
from loguru import logger
import numpy as np

try:
    import redis
    _REDIS_OK = True
except ImportError:
    _REDIS_OK = False

# ─── Configuration ────────────────────────────────────────────────────────────
_GOSSIP_CHANNEL     = "nemesis:swarm:gossip"
_AGENT_PREFIX       = "nemesis:agent:"
_HEARTBEAT_S        = 10       # Heartbeat agent toutes les 10s
_GOSSIP_TTL_S       = 120      # Messages gossip expirent après 2min
_CORRELATION_WINDOW = 50       # Fenêtre de corrélation (bougies)
_CORRELATION_THRESH = 0.6      # Seuil de corrélation pour gossip

# Groupes de corrélation pré-définis (fallback si pas de données)
_CORRELATION_GROUPS = {
    "crypto": ["BTCUSD", "ETHUSD", "BNBUSD", "XRPUSD", "SOLUSD", "AVAXUSD"],
    "forex_major": ["EURUSD", "GBPUSD", "USDJPY", "USDCHF"],
    "forex_cross": ["EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CHFJPY"],
    "forex_commodity": ["AUDUSD", "NZDUSD", "AUDNZD", "AUDCAD"],
    "commodities": ["GOLD", "SILVER", "COPPER"],
    "energy": ["OIL_CRUDE", "OIL_BRENT", "NATURALGAS"],
    "us_indices": ["US500", "US100", "US30"],
    "eu_indices": ["DE40", "FR40", "UK100"],
    "us_stocks": ["AAPL", "TSLA", "NVDA", "MSFT", "META", "GOOGL", "AMZN", "AMD"],
}

# Types de signaux gossip
GOSSIP_CRASH      = "FLASH_CRASH"
GOSSIP_PUMP       = "FLASH_PUMP"
GOSSIP_WHALE_DUMP = "WHALE_DUMP"
GOSSIP_WHALE_BUY  = "WHALE_BUY"
GOSSIP_VOL_SPIKE  = "VOL_SPIKE"
GOSSIP_MACRO      = "MACRO_EVENT"
GOSSIP_CUT_ALL    = "CUT_ALL"


class GossipMessage:
    """Message broadcast entre agents via Redis pub/sub."""

    def __init__(self, sender: str, msg_type: str, severity: float = 0.5,
                 data: dict = None, targets: List[str] = None):
        self.sender = sender
        self.msg_type = msg_type
        self.severity = severity        # [0..1] — 1.0 = critique
        self.data = data or {}
        self.targets = targets or []    # Instruments ciblés (vide = broadcast)
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.msg_id = hashlib.md5(
            f"{sender}{msg_type}{self.timestamp}".encode()
        ).hexdigest()[:12]

    def to_json(self) -> str:
        return json.dumps({
            "id": self.msg_id,
            "sender": self.sender,
            "type": self.msg_type,
            "severity": self.severity,
            "data": self.data,
            "targets": self.targets,
            "ts": self.timestamp,
        })

    @classmethod
    def from_json(cls, raw: str) -> Optional["GossipMessage"]:
        try:
            d = json.loads(raw)
            msg = cls(
                sender=d["sender"],
                msg_type=d["type"],
                severity=d.get("severity", 0.5),
                data=d.get("data", {}),
                targets=d.get("targets", []),
            )
            msg.msg_id = d.get("id", "")
            msg.timestamp = d.get("ts", "")
            return msg
        except Exception:
            return None


class MicroAgent:
    """Agent autonome pour un instrument, avec mémoire Redis."""

    def __init__(self, instrument: str, redis_client=None):
        self.instrument = instrument
        self._redis = redis_client
        self._key = f"{_AGENT_PREFIX}{instrument}"

        # État local (miroir de Redis)
        self.state = {
            "last_price": 0.0,
            "trend": "NEUTRAL",          # UP/DOWN/NEUTRAL
            "volatility": 0.0,
            "position": "NONE",          # LONG/SHORT/NONE
            "gossip_alert": False,
            "gossip_msg": "",
            "last_update": "",
        }

        # Mémoire des derniers prix (pour détection flash-crash)
        self._price_buffer: List[float] = []
        self._max_buffer = 30

        # Messages gossip reçus
        self._inbox: List[GossipMessage] = []

    def update_price(self, price: float):
        """Met à jour le prix et détecte les anomalies."""
        self._price_buffer.append(price)
        if len(self._price_buffer) > self._max_buffer:
            self._price_buffer = self._price_buffer[-self._max_buffer:]

        self.state["last_price"] = price
        self.state["last_update"] = datetime.now(timezone.utc).isoformat()

        # Détection trend
        if len(self._price_buffer) >= 5:
            recent = self._price_buffer[-5:]
            if all(recent[i] < recent[i + 1] for i in range(4)):
                self.state["trend"] = "UP"
            elif all(recent[i] > recent[i + 1] for i in range(4)):
                self.state["trend"] = "DOWN"
            else:
                self.state["trend"] = "NEUTRAL"

        # Calcul volatilité (stddev des returns)
        if len(self._price_buffer) >= 10:
            returns = np.diff(self._price_buffer[-10:]) / np.array(self._price_buffer[-10:-1])
            self.state["volatility"] = round(float(np.std(returns)), 6)

        # Persist to Redis
        self._save_state()

    def detect_flash_event(self) -> Optional[GossipMessage]:
        """Détecte un flash crash/pump et retourne un message gossip."""
        if len(self._price_buffer) < 10:
            return None

        prices = self._price_buffer[-10:]
        change_pct = (prices[-1] - prices[0]) / max(prices[0], 1e-8) * 100

        # Flash crash: > -2% en 10 ticks
        if change_pct < -2.0:
            targets = self._get_correlated_instruments()
            return GossipMessage(
                sender=self.instrument,
                msg_type=GOSSIP_CRASH,
                severity=min(abs(change_pct) / 5, 1.0),
                data={"change_pct": round(change_pct, 2), "price": prices[-1]},
                targets=targets,
            )

        # Flash pump: > +2% en 10 ticks
        if change_pct > 2.0:
            targets = self._get_correlated_instruments()
            return GossipMessage(
                sender=self.instrument,
                msg_type=GOSSIP_PUMP,
                severity=min(change_pct / 5, 1.0),
                data={"change_pct": round(change_pct, 2), "price": prices[-1]},
                targets=targets,
            )

        return None

    def receive_gossip(self, msg: GossipMessage):
        """Reçoit un message gossip d'un autre agent."""
        self._inbox.append(msg)
        if len(self._inbox) > 20:
            self._inbox = self._inbox[-20:]

        self.state["gossip_alert"] = True
        self.state["gossip_msg"] = f"{msg.sender}:{msg.msg_type}"
        self._save_state()

    def get_gossip_alert(self) -> Tuple[bool, str, float]:
        """Retourne l'alerte gossip la plus récente."""
        if not self._inbox:
            return False, "", 0.0
        latest = self._inbox[-1]
        # Auto-expire après 2 min
        try:
            ts = datetime.fromisoformat(latest.timestamp)
            if (datetime.now(timezone.utc) - ts).seconds > _GOSSIP_TTL_S:
                self.state["gossip_alert"] = False
                return False, "", 0.0
        except Exception:
            pass
        return True, latest.msg_type, latest.severity

    def _get_correlated_instruments(self) -> List[str]:
        """Retourne les instruments corrélés (même groupe)."""
        for group, members in _CORRELATION_GROUPS.items():
            if self.instrument in members:
                return [m for m in members if m != self.instrument]
        return []

    def _save_state(self):
        """Sauvegarde l'état dans Redis."""
        if not self._redis:
            return
        try:
            self._redis.hset(self._key, mapping={
                k: str(v) for k, v in self.state.items()
            })
            self._redis.expire(self._key, 600)  # TTL 10 min
        except Exception:
            pass

    def _load_state(self):
        """Charge l'état depuis Redis."""
        if not self._redis:
            return
        try:
            data = self._redis.hgetall(self._key)
            if data:
                for k, v in data.items():
                    key = k.decode() if isinstance(k, bytes) else k
                    val = v.decode() if isinstance(v, bytes) else v
                    if key in self.state:
                        self.state[key] = val
        except Exception:
            pass


class SwarmIntelligence:
    """
    Moteur 27 : Swarm Intelligence Orchestration.

    Multi-Agent System avec communication gossip via Redis pub/sub.
    Chaque instrument = 1 micro-agent autonome.
    """

    def __init__(self, db=None, capital_client=None, capital_ws=None,
                 telegram_router=None, instruments: list = None):
        self._db = db
        self._capital = capital_client
        self._ws = capital_ws
        self._tg = telegram_router
        self._running = False
        self._thread = None
        self._gossip_thread = None
        self._lock = threading.Lock()

        # Redis connection
        self._redis = None
        self._pubsub = None
        self._init_redis()

        # Create micro-agents
        self._instruments = instruments or []
        self._agents: Dict[str, MicroAgent] = {}
        for inst in self._instruments:
            self._agents[inst] = MicroAgent(inst, self._redis)

        # Gossip stats
        self._gossips_sent = 0
        self._gossips_received = 0
        self._flash_events = 0
        self._seen_msg_ids: Set[str] = set()

        logger.info(
            f"🐝 M27 Swarm Intelligence initialisé ({len(self._agents)} micro-agents)"
        )

    # ─── Redis ───────────────────────────────────────────────────────────────

    def _init_redis(self):
        if not _REDIS_OK:
            return
        try:
            import os
            redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
            self._redis = redis.from_url(redis_url, decode_responses=False)
            self._redis.ping()
            logger.debug("🐝 M27 Redis connecté pour Swarm")
        except Exception as e:
            logger.debug(f"M27 Redis: {e}")
            self._redis = None

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True

        # Thread principal : update agents
        self._thread = threading.Thread(
            target=self._agent_loop, daemon=True, name="swarm_main"
        )
        self._thread.start()

        # Thread gossip listener
        if self._redis:
            self._gossip_thread = threading.Thread(
                target=self._gossip_listener, daemon=True, name="swarm_gossip"
            )
            self._gossip_thread.start()

        logger.info("🐝 M27 Swarm Intelligence démarré (gossip protocol actif)")

    def stop(self):
        self._running = False
        if self._pubsub:
            try:
                self._pubsub.unsubscribe()
            except Exception:
                pass

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_swarm_signal(self, instrument: str) -> Tuple[bool, str, float]:
        """
        Retourne le signal swarm pour un instrument.
        Returns: (has_alert, alert_type, severity)
        """
        agent = self._agents.get(instrument)
        if not agent:
            return False, "", 0.0
        return agent.get_gossip_alert()

    def get_agent_state(self, instrument: str) -> dict:
        """Retourne l'état d'un micro-agent."""
        agent = self._agents.get(instrument)
        return dict(agent.state) if agent else {}

    def broadcast_event(self, sender: str, msg_type: str, severity: float = 0.5,
                       data: dict = None, targets: list = None):
        """Broadcast un message gossip depuis un composant externe."""
        msg = GossipMessage(sender, msg_type, severity, data, targets)
        self._publish_gossip(msg)

    def stats(self) -> dict:
        active_agents = sum(1 for a in self._agents.values()
                           if a.state["last_price"] != 0)
        alerts = {inst: a.state["gossip_msg"]
                  for inst, a in self._agents.items()
                  if a.state["gossip_alert"]}
        return {
            "agents_total": len(self._agents),
            "agents_active": active_agents,
            "gossips_sent": self._gossips_sent,
            "gossips_received": self._gossips_received,
            "flash_events": self._flash_events,
            "active_alerts": alerts,
            "redis_connected": self._redis is not None,
        }

    def format_report(self) -> str:
        s = self.stats()
        alerts_str = " | ".join(
            f"{k}:{v}" for k, v in s["active_alerts"].items()
        ) or "—"
        return (
            f"🐝 <b>Swarm Intelligence (M27)</b>\n\n"
            f"  Agents: {s['agents_active']}/{s['agents_total']} actifs\n"
            f"  Gossips: ↑{s['gossips_sent']} ↓{s['gossips_received']}\n"
            f"  Flash Events: {s['flash_events']}\n"
            f"  Redis: {'✅' if s['redis_connected'] else '❌'}\n"
            f"  Alertes: {alerts_str}"
        )

    # ─── Agent Loop ──────────────────────────────────────────────────────────

    def _agent_loop(self):
        time.sleep(25)
        while self._running:
            try:
                self._update_all_agents()
            except Exception as e:
                logger.debug(f"M27 agent loop: {e}")
            time.sleep(_HEARTBEAT_S)

    def _update_all_agents(self):
        """Met à jour tous les micro-agents avec les prix courants."""
        if not self._capital:
            return

        for inst, agent in self._agents.items():
            try:
                px = self._capital.get_current_price(inst)
                if px and px.get("mid", 0) > 0:
                    agent.update_price(px["mid"])

                    # Détecter les flash events
                    gossip = agent.detect_flash_event()
                    if gossip:
                        self._flash_events += 1
                        self._publish_gossip(gossip)
                        self._dispatch_gossip_local(gossip)

                        logger.info(
                            f"🐝 M27 FLASH: {inst} → {gossip.msg_type} "
                            f"sev={gossip.severity:.2f} → "
                            f"{len(gossip.targets)} agents notifiés"
                        )
            except Exception:
                pass

    # ─── Gossip Protocol ─────────────────────────────────────────────────────

    def _publish_gossip(self, msg: GossipMessage):
        """Publie un message gossip sur Redis pub/sub."""
        if not self._redis:
            return
        try:
            self._redis.publish(_GOSSIP_CHANNEL, msg.to_json().encode())
            self._gossips_sent += 1
        except Exception:
            pass

    def _gossip_listener(self):
        """Écoute les messages gossip sur Redis pub/sub."""
        if not self._redis:
            return
        try:
            self._pubsub = self._redis.pubsub()
            self._pubsub.subscribe(_GOSSIP_CHANNEL)
            for raw_msg in self._pubsub.listen():
                if not self._running:
                    break
                if raw_msg["type"] != "message":
                    continue
                try:
                    data = raw_msg["data"]
                    if isinstance(data, bytes):
                        data = data.decode()
                    gossip = GossipMessage.from_json(data)
                    if gossip:
                        self._dispatch_gossip_local(gossip)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"M27 gossip listener: {e}")

    def _dispatch_gossip_local(self, msg: GossipMessage):
        """Distribue un message gossip aux agents locaux concernés."""
        # Dedup
        if msg.msg_id in self._seen_msg_ids:
            return
        self._seen_msg_ids.add(msg.msg_id)
        if len(self._seen_msg_ids) > 500:
            self._seen_msg_ids = set(list(self._seen_msg_ids)[-300:])

        self._gossips_received += 1

        # Si targets spécifiques → dispatcher uniquement à eux
        if msg.targets:
            for target in msg.targets:
                agent = self._agents.get(target)
                if agent:
                    agent.receive_gossip(msg)
        else:
            # Broadcast à tous sauf le sender
            for inst, agent in self._agents.items():
                if inst != msg.sender:
                    agent.receive_gossip(msg)
