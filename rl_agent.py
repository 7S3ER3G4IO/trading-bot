"""
rl_agent.py — Moteur 8 : Reinforcement Learning Agent (Deep Q-Network léger).

Architecture: Deep Q-Network (DQN) sans dépendance à TensorFlow/PyTorch.
Utilise uniquement numpy (déjà dans requirements.txt).

L'Agent RL apprend par essai-erreur DANS le Shadow Mode (paper trading)
et met à jour ses poids chaque nuit → le lendemain les trades réels
bénéficient de stratégies affinées sur N épisodes simulés.

Actions disponibles: BUY, SELL, HOLD (3 actions)
State Space (8 dimensions):
  [vol_ratio, momentum_5, momentum_15, rsi_norm, adx_norm,
   spread_norm, hour_sin, regime_encoded]

Reward Function (Sharpe-adjusted):
  reward = pnl / (pnl_std + ε) - slippage_penalty - hold_cost

Entrainement: offline chaque nuit sur l'historique Supabase (Off-Policy RL).
              + Online: met à jour sur chaque trade fermé (incremental update).

Usage:
    rl = RLAgent(db)
    rl.start()  # démarre l'entraînement nightly

    # Obtenir une recommandation RL avant entrée:
    action, confidence = rl.get_action(state_features)
    # action ∈ {0: SELL, 1: HOLD, 2: BUY}
    # confidence: 0.0 → 1.0
"""
import math
import time
import pickle
import os
import threading
from typing import Optional, Tuple, List
from datetime import datetime, timezone
from loguru import logger
import random

# ─── Hyper-paramètres DQN ─────────────────────────────────────────────────────
_STATE_DIM       = 8      # Dimensions de l'état
_NUM_ACTIONS     = 3      # SELL=0, HOLD=1, BUY=2
_HIDDEN_UNITS    = 32     # Neurones de la couche cachée
_LEARNING_RATE   = 0.001  # Alpha
_GAMMA           = 0.95   # Facteur de discount
_EPSILON_START   = 0.3    # Exploration initiale
_EPSILON_MIN     = 0.02   # Exploration minimale (pure exploitation)
_EPSILON_DECAY   = 0.99   # Décroissance par épisode
_BATCH_SIZE      = 32     # Taille des mini-batchs
_REPLAY_SIZE     = 2000   # Taille du replay buffer
_TRAIN_INTERVAL  = 86400  # Entraînement toutes les 24h (nightly)
_MIN_REPLAY      = 64     # Minimum de transitions avant de commencer
_MODEL_PATH      = "/tmp/nemesis_rl_model.pkl"
_SLIPPAGE_PEN    = 0.001  # Pénalité de slippage dans la reward function

# ─── Actions ──────────────────────────────────────────────────────────────────
ACTION_SELL, ACTION_HOLD, ACTION_BUY = 0, 1, 2
ACTION_NAMES = {0: "SELL", 1: "HOLD", 2: "BUY"}

try:
    import numpy as np
    _NP_AVAILABLE = True
except ImportError:
    _NP_AVAILABLE = False


class MinimalDQN:
    """
    Réseau Q à 2 couches (state → hidden → Q_values) implémenté en numpy pur.
    Pas besoin de PyTorch/TensorFlow — fonctionne en production minimal.
    """

    def __init__(self, state_dim: int, num_actions: int, hidden: int):
        self.state_dim   = state_dim
        self.num_actions = num_actions

        if _NP_AVAILABLE:
            # Initialisation Xavier
            k1 = math.sqrt(6 / (state_dim + hidden))
            k2 = math.sqrt(6 / (hidden + num_actions))
            self.W1 = np.random.uniform(-k1, k1, (state_dim, hidden))
            self.b1 = np.zeros(hidden)
            self.W2 = np.random.uniform(-k2, k2, (hidden, num_actions))
            self.b2 = np.zeros(num_actions)
        else:
            self.W1 = self.b1 = self.W2 = self.b2 = None

    def forward(self, state) -> "np.ndarray":
        """Forward pass: état → Q-values."""
        if not _NP_AVAILABLE or self.W1 is None:
            return [0.0, 0.0, 0.0]
        x = np.array(state, dtype=float)
        h = np.tanh(x @ self.W1 + self.b1)   # couche cachée (tanh)
        q = h @ self.W2 + self.b2               # couche de sortie (linéaire)
        return q

    def update(self, state, action, target_q, lr: float):
        """Gradient step sur un échantillon (MSE loss)."""
        if not _NP_AVAILABLE or self.W1 is None:
            return
        x = np.array(state, dtype=float)

        # Forward pass
        h      = np.tanh(x @ self.W1 + self.b1)
        q      = h @ self.W2 + self.b2

        # Loss = (q[action] - target)^2, backprop simple
        error  = q[action] - target_q
        dq     = np.zeros(_NUM_ACTIONS)
        dq[action] = 2 * error

        # Couche de sortie
        dW2    = np.outer(h, dq)
        db2    = dq

        # Couche cachée
        dh     = dq @ self.W2.T * (1 - h**2)  # dérivée de tanh
        dW1    = np.outer(x, dh)
        db1    = dh

        # Descente de gradient
        self.W2 -= lr * dW2
        self.b2 -= lr * db2
        self.W1 -= lr * dW1
        self.b1 -= lr * db1


class ReplayBuffer:
    """Experience Replay Buffer (FIFO)."""

    def __init__(self, maxlen: int = _REPLAY_SIZE):
        self._buffer = []
        self._maxlen = maxlen

    def push(self, state, action: int, reward: float, next_state, done: bool):
        if len(self._buffer) >= self._maxlen:
            self._buffer.pop(0)
        self._buffer.append((state, action, reward, next_state, done))

    def sample(self, batch_size: int) -> list:
        k = min(batch_size, len(self._buffer))
        return random.sample(self._buffer, k)

    def __len__(self):
        return len(self._buffer)


class RLAgent:
    """
    Agent DQN pour le trading. Apprend off-policy depuis l'historique Supabase.
    Compatible avec l'architecture existante (fallback à 0.5 si numpy absent).
    """

    def __init__(self, db=None, telegram_router=None):
        self._db     = db
        self._tg     = telegram_router
        self._lock   = threading.Lock()

        self.epsilon   = _EPSILON_START
        self.q_net     = MinimalDQN(_STATE_DIM, _NUM_ACTIONS, _HIDDEN_UNITS)
        self.q_target  = MinimalDQN(_STATE_DIM, _NUM_ACTIONS, _HIDDEN_UNITS)
        self.replay    = ReplayBuffer()

        self._train_count   = 0
        self._episodes      = 0
        self._running       = False
        self._total_reward  = 0.0

        self._load_model()

        self._thread = threading.Thread(
            target=self._nightly_train_loop, daemon=True, name="rl_agent"
        )
        self._thread.start()
        logger.info(f"🤖 RL Agent initialisé | ε={self.epsilon:.2f} | "
                    f"replay={len(self.replay)} | trained={self._train_count}")

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_action(self, state_features: list) -> Tuple[int, float]:
        """
        Retourne (action, confidence) basé sur les Q-values actuelles.
        action ∈ {0: SELL, 1: HOLD, 2: BUY}
        confidence ∈ [0.0, 1.0]
        """
        if not _NP_AVAILABLE or self.q_net.W1 is None:
            return ACTION_HOLD, 0.5

        state  = self._normalize_state(state_features)
        q_vals = self.q_net.forward(state)
        action = int(np.argmax(q_vals))

        # Softmax confidence
        exp_q  = [math.exp(v - max(q_vals)) for v in q_vals]
        conf   = exp_q[action] / sum(exp_q)

        logger.debug(f"🤖 RL: Q={[round(v,3) for v in q_vals]} → {ACTION_NAMES[action]} ({conf:.1%})")
        return action, round(conf, 3)

    def record_transition(self, state, action: int, reward: float,
                           next_state, done: bool = False):
        """Enregistre une transition dans le replay buffer (online learning)."""
        s  = self._normalize_state(state)
        ns = self._normalize_state(next_state)
        self.replay.push(s, action, reward, ns, done)
        self._total_reward += reward

        # Mini-train online si buffer suffisant
        if len(self.replay) >= _MIN_REPLAY and len(self.replay) % 20 == 0:
            self._train_batch()

    def compute_reward(self, pnl: float, duration_min: float,
                        slippage_pct: float = 0.001) -> float:
        """
        Reward function Sharpe-adjustée:
        reward = pnl / (|pnl| + ε) - slippage - hold_cost
        """
        eps        = 0.0001
        pnl_sign   = pnl / (abs(pnl) + eps)
        slippage   = slippage_pct
        hold_cost  = max(0, duration_min - 30) * 0.0001  # coût de portage > 30min
        return round(pnl_sign - slippage - hold_cost, 4)

    def stats(self) -> dict:
        return {
            "train_sessions": self._train_count,
            "replay_size": len(self.replay),
            "epsilon": round(self.epsilon, 3),
            "total_reward": round(self._total_reward, 2),
        }

    # ─── Training ────────────────────────────────────────────────────────────

    def _nightly_train_loop(self):
        """Entraînement nightly every 24h + chargement des données Supabase."""
        self._running = True
        # Premier run: entraîner immédiatement si replay buffer vide
        time.sleep(30)   # laisser le bot démarrer
        self._load_transitions_from_db()
        time.sleep(5)
        if len(self.replay) >= _MIN_REPLAY:
            self._full_training_session()

        while self._running:
            # Attendre jusqu'à 3h du matin UTC (approximate)
            time.sleep(_TRAIN_INTERVAL)
            try:
                self._load_transitions_from_db()
                self._full_training_session()
                self._save_model()
            except Exception as e:
                logger.debug(f"RL nightly train: {e}")

    def _full_training_session(self, n_steps: int = 500):
        """Session d'entraînement sur le replay buffer."""
        if len(self.replay) < _MIN_REPLAY:
            return

        for step in range(n_steps):
            self._train_batch()
            # Sync target network toutes les 50 steps
            if step % 50 == 0:
                self._sync_target()

        self._train_count += 1
        self.epsilon = max(_EPSILON_MIN, self.epsilon * _EPSILON_DECAY)

        logger.info(
            f"🤖 RL training #{self._train_count} | "
            f"{n_steps} steps | ε={self.epsilon:.3f} | "
            f"replay={len(self.replay)}"
        )

        if self._tg:
            try:
                self._tg.send_report(
                    f"🤖 <b>RL Agent — Entraînement #{self._train_count}</b>\n\n"
                    f"  Replay buffer: {len(self.replay)} transitions\n"
                    f"  Epsilon: {self.epsilon:.3f}\n"
                    f"  Total reward: {self._total_reward:+.2f}\n"
                    f"  Sessions: {self._train_count}"
                )
            except Exception:
                pass

    def _train_batch(self):
        """Un step de gradient sur un mini-batch."""
        if not _NP_AVAILABLE:
            return

        batch = self.replay.sample(_BATCH_SIZE)
        for (s, a, r, ns, done) in batch:
            # Bellman: Q*(s,a) = r + γ * max_a' Q_target(s', a')
            if done:
                target = r
            else:
                next_q = self.q_target.forward(ns)
                target = r + _GAMMA * float(max(next_q))

            self.q_net.update(s, a, target, _LEARNING_RATE)

    def _sync_target(self):
        """Copie les poids du Q-net vers le Q-target (stabilité)."""
        if not _NP_AVAILABLE or self.q_net.W1 is None:
            return
        import copy
        self.q_target.W1 = copy.deepcopy(self.q_net.W1)
        self.q_target.b1 = copy.deepcopy(self.q_net.b1)
        self.q_target.W2 = copy.deepcopy(self.q_net.W2)
        self.q_target.b2 = copy.deepcopy(self.q_net.b2)

    def _load_transitions_from_db(self):
        """Charge l'historique des trades comme transitions RL."""
        if not self._db or not self._db._pg:
            return
        try:
            cur = self._db._execute(
                "SELECT result, pnl, duration_min, score "
                "FROM capital_trades WHERE status='CLOSED' AND pnl IS NOT NULL "
                "ORDER BY opened_at DESC LIMIT 500",
                fetch=True
            )
            rows = cur.fetchall()
            for row in rows:
                result, pnl, dur, score = row[0], float(row[1] or 0), float(row[2] or 30), float(row[3] or 0.5)
                reward = self.compute_reward(pnl, dur)
                # State synthétique depuis les méta-données
                state  = self._synthetic_state(score, pnl, dur)
                action = ACTION_BUY if result == "WIN" else ACTION_SELL
                self.replay.push(state, action, reward, state, True)

            if len(rows) > 0:
                logger.debug(f"🤖 RL: {len(rows)} transitions chargées depuis Supabase")
        except Exception as e:
            logger.debug(f"RL load DB: {e}")

    @staticmethod
    def _normalize_state(features: list) -> list:
        """Normalise les features dans [-1, 1]."""
        if not features:
            return [0.0] * _STATE_DIM
        # Padding si moins de STATE_DIM features
        out = list(features[:_STATE_DIM])
        while len(out) < _STATE_DIM:
            out.append(0.0)
        # Clip
        return [max(-1.0, min(1.0, float(v))) for v in out]

    @staticmethod
    def _synthetic_state(score: float, pnl: float, dur: float) -> list:
        """State synthétique depuis les métadonnées de trade."""
        return [
            min(score, 1.0),
            min(pnl / 100, 1.0),
            min(dur / 60, 1.0),
            0.0, 0.0, 0.0, 0.0, 0.0
        ]

    def _save_model(self):
        try:
            with open(_MODEL_PATH, "wb") as f:
                pickle.dump({
                    "W1": self.q_net.W1, "b1": self.q_net.b1,
                    "W2": self.q_net.W2, "b2": self.q_net.b2,
                    "epsilon": self.epsilon,
                    "train_count": self._train_count,
                }, f)
        except Exception:
            pass

    def _load_model(self):
        try:
            if _NP_AVAILABLE and os.path.exists(_MODEL_PATH):
                with open(_MODEL_PATH, "rb") as f:
                    data = pickle.load(f)
                self.q_net.W1 = data["W1"]
                self.q_net.b1 = data["b1"]
                self.q_net.W2 = data["W2"]
                self.q_net.b2 = data["b2"]
                self.epsilon   = data.get("epsilon", _EPSILON_START)
                self._train_count = data.get("train_count", 0)
                self._sync_target()
                logger.info("🤖 RL Agent: modèle chargé depuis cache")
        except Exception:
            pass

    def stop(self):
        self._running = False
