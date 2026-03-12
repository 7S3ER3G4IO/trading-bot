"""
hdc_memory.py — Moteur 34 : Hyper-Dimensional Computing (HDC)

Encode chaque tick, signal NLP et signal TDA dans des hyper-vecteurs
bipolaires de 10 000 dimensions. L'association mémoire est instantanée :
matching en O(1) via distance de Hamming, sans entraînement GPU.

Architecture :
  HyperVector         → vecteur bipolaire {-1, +1}^D (D=10000)
  HDCEncoder          → encode les features de marché en hypervecteurs
  AssociativeMemory   → mémoire associative (stocke et retrouve les patterns)
  PatternMatcher      → matching O(1) via produit scalaire cosine
  TemporalBinding     → encodage temporel via permutation circulaire

Opérations fondamentales :
  - Bundling (addition) : A + B → superposition de concepts
  - Binding (multiplication) : A ⊗ B → association de deux concepts
  - Permutation (shift) : ρ(A) → encodage de position/temps

Mémoire : 10000 dims × int8 = 10KB par vecteur.
100 patterns stockés = 1MB. Compatible Apple Silicon.
"""
import time
import threading
from typing import Dict, Optional, List, Tuple
from datetime import datetime, timezone
from loguru import logger
import numpy as np

# ─── Configuration ────────────────────────────────────────────────────────────
_DIMS              = 10_000     # Dimensions de l'espace hyper-dimensionnel
_MAX_MEMORY        = 500        # Patterns max en mémoire (~5MB)
_MATCH_THRESHOLD   = 0.65       # Cosine similarity > 0.65 = match
_SCAN_INTERVAL_S   = 15         # Scan toutes les 15s
_TEMPORAL_WINDOW   = 20         # Derniers N ticks pour l'encodage temporel

# Feature channels pour l'encodage
_FEATURE_CHANNELS = [
    "price_direction",      # UP/DOWN/FLAT
    "volume_regime",        # HIGH/NORMAL/LOW
    "volatility_level",     # EXTREME/HIGH/NORMAL/LOW
    "momentum_state",       # STRONG_UP/UP/FLAT/DOWN/STRONG_DOWN
    "macro_sentiment",      # HAWK/NEUTRAL/DOVE
    "tda_topology",         # CRASH/NORMAL/PUMP
    "swarm_alert",          # ACTIVE/NONE
    "orderbook_imbalance",  # BUY_HEAVY/BALANCED/SELL_HEAVY
]


class HyperVector:
    """
    Vecteur hyper-dimensionnel bipolaire {-1, +1}^D.
    Stocké en int8 pour efficacité mémoire (10KB par vecteur).
    """

    __slots__ = ("data", "dims", "label", "timestamp")

    def __init__(self, dims: int = _DIMS, data: np.ndarray = None,
                 label: str = ""):
        self.dims = dims
        self.label = label
        self.timestamp = datetime.now(timezone.utc)

        if data is not None:
            self.data = data.astype(np.int8)
        else:
            # Vecteur aléatoire bipolaire
            self.data = np.random.choice(
                [-1, 1], size=dims
            ).astype(np.int8)

    def similarity(self, other: "HyperVector") -> float:
        """
        Cosine similarity entre deux hypervecteurs.
        O(D) mais avec NumPy vectorisé = quasi O(1) en pratique.
        """
        dot = np.dot(self.data.astype(np.float32),
                     other.data.astype(np.float32))
        return float(dot / self.dims)

    def hamming_distance(self, other: "HyperVector") -> int:
        """Distance de Hamming (nombre de composantes différentes)."""
        return int(np.sum(self.data != other.data))

    def hamming_similarity(self, other: "HyperVector") -> float:
        """Similarité de Hamming normalisée [0..1]."""
        return 1.0 - self.hamming_distance(other) / self.dims

    @staticmethod
    def bundle(*vectors: "HyperVector") -> "HyperVector":
        """
        Bundling (addition) : superposition de concepts.
        A + B + C → le résultat est similaire à chacun.
        """
        if not vectors:
            return HyperVector()
        dims = vectors[0].dims
        accumulated = np.zeros(dims, dtype=np.float32)
        for v in vectors:
            accumulated += v.data.astype(np.float32)
        # Bipolariser (signe)
        result = np.sign(accumulated).astype(np.int8)
        result[result == 0] = 1  # Tie-break vers +1
        return HyperVector(dims=dims, data=result)

    @staticmethod
    def bind(a: "HyperVector", b: "HyperVector") -> "HyperVector":
        """
        Binding (multiplication) : association de deux concepts.
        A ⊗ B → le résultat est orthogonal à A et B individuellement
        mais le débinding donne B = A ⊗ (A ⊗ B).
        """
        result = (a.data * b.data).astype(np.int8)
        return HyperVector(dims=a.dims, data=result)

    @staticmethod
    def permute(v: "HyperVector", n: int = 1) -> "HyperVector":
        """
        Permutation (shift circulaire) : encodage de position/temps.
        ρⁿ(A) → vecteur quasi-orthogonal à A pour n > 0.
        """
        result = np.roll(v.data, n)
        return HyperVector(dims=v.dims, data=result)


class HDCEncoder:
    """
    Encode les features de marché multi-sources en hypervecteurs.
    Chaque feature channel a son propre basis vector.
    """

    def __init__(self, dims: int = _DIMS):
        self.dims = dims

        # Basis vectors par channel (fixes, générés une seule fois)
        self._bases: Dict[str, HyperVector] = {}
        for channel in _FEATURE_CHANNELS:
            self._bases[channel] = HyperVector(dims)

        # Level vectors (valeurs possibles pour chaque feature)
        self._levels: Dict[str, Dict[str, HyperVector]] = {}
        self._init_levels()

    def _init_levels(self):
        """Initialise les vecteurs de niveaux pour chaque channel."""
        level_maps = {
            "price_direction": ["UP", "DOWN", "FLAT"],
            "volume_regime": ["HIGH", "NORMAL", "LOW"],
            "volatility_level": ["EXTREME", "HIGH", "NORMAL", "LOW"],
            "momentum_state": ["STRONG_UP", "UP", "FLAT", "DOWN", "STRONG_DOWN"],
            "macro_sentiment": ["HAWK", "NEUTRAL", "DOVE"],
            "tda_topology": ["CRASH", "NORMAL", "PUMP"],
            "swarm_alert": ["ACTIVE", "NONE"],
            "orderbook_imbalance": ["BUY_HEAVY", "BALANCED", "SELL_HEAVY"],
        }
        for channel, levels in level_maps.items():
            self._levels[channel] = {}
            for level in levels:
                self._levels[channel][level] = HyperVector(self.dims)

    def encode(self, features: Dict[str, str], label: str = "") -> HyperVector:
        """
        Encode un ensemble de features en un seul hypervecteur.
        Chaque feature = basis_channel ⊗ level_value.
        Le résultat final = bundling de tous les bindings.
        """
        bound_vectors = []

        for channel, value in features.items():
            basis = self._bases.get(channel)
            level_dict = self._levels.get(channel, {})
            level_vec = level_dict.get(value)

            if basis and level_vec:
                # Binding : channel ⊗ value
                bound = HyperVector.bind(basis, level_vec)
                bound_vectors.append(bound)

        if not bound_vectors:
            return HyperVector(self.dims, label=label)

        # Bundling : superposition de tous les bindings
        result = HyperVector.bundle(*bound_vectors)
        result.label = label
        return result

    def encode_temporal(self, feature_sequence: List[Dict[str, str]],
                        label: str = "") -> HyperVector:
        """
        Encode une séquence temporelle de features.
        Utilise la permutation pour encoder la position temporelle.
        """
        temporal_vectors = []

        for t, features in enumerate(feature_sequence):
            instant_vec = self.encode(features)
            # Permuter proportionnellement au temps (position encoding)
            shifted = HyperVector.permute(instant_vec, t)
            temporal_vectors.append(shifted)

        if not temporal_vectors:
            return HyperVector(self.dims, label=label)

        result = HyperVector.bundle(*temporal_vectors)
        result.label = label
        return result


class AssociativeMemory:
    """
    Mémoire associative hyper-dimensionnelle.
    Stocke les patterns et les retrouve en O(D) (vectorisé = quasi O(1)).
    """

    def __init__(self, max_size: int = _MAX_MEMORY, dims: int = _DIMS):
        self._max_size = max_size
        self._dims = dims

        # Mémoire = matrice (max_size × dims) en int8
        self._memory = np.zeros((max_size, dims), dtype=np.int8)
        self._labels: List[str] = [""] * max_size
        self._outcomes: List[str] = [""] * max_size  # Résultat associé
        self._write_pos = 0
        self._filled = 0

    def store(self, vector: HyperVector, outcome: str = ""):
        """Stocke un pattern + son outcome connu."""
        idx = self._write_pos % self._max_size
        self._memory[idx] = vector.data
        self._labels[idx] = vector.label
        self._outcomes[idx] = outcome
        self._write_pos += 1
        self._filled = min(self._filled + 1, self._max_size)

    def query(self, vector: HyperVector, top_k: int = 3) -> List[Tuple[str, str, float]]:
        """
        Retrouve les patterns les plus similaires.
        Returns: [(label, outcome, similarity), ...]
        O(N×D) mais vectorisé → quasi instantané pour N=500, D=10000.
        """
        if self._filled == 0:
            return []

        # Calcul vectorisé de toutes les similarités
        active = self._memory[:self._filled]
        query_f32 = vector.data.astype(np.float32)

        # Dot product → cosine similarity (vecteurs bipolaires ⇒ norm = √D)
        similarities = active.astype(np.float32) @ query_f32 / self._dims

        # Top-K
        k = min(top_k, self._filled)
        top_indices = np.argpartition(similarities, -k)[-k:]
        top_indices = top_indices[np.argsort(similarities[top_indices])[::-1]]

        results = []
        for idx in top_indices:
            sim = float(similarities[idx])
            if sim > _MATCH_THRESHOLD * 0.5:  # Seuil bas pour retourner
                results.append((
                    self._labels[idx],
                    self._outcomes[idx],
                    round(sim, 4),
                ))

        return results

    def best_match(self, vector: HyperVector) -> Tuple[Optional[str], float]:
        """
        Retourne le meilleur match et sa similarité.
        Returns: (outcome, similarity)
        """
        results = self.query(vector, top_k=1)
        if results and results[0][2] > _MATCH_THRESHOLD:
            return results[0][1], results[0][2]
        return None, 0.0

    @property
    def memory_usage_mb(self) -> float:
        return self._memory.nbytes / (1024 * 1024)


class HDCMemory:
    """
    Moteur 34 : Hyper-Dimensional Computing Memory.

    Encode le marché en hypervecteurs 10K-dim et retrouve les patterns
    historiques en temps constant via mémoire associative.
    """

    def __init__(self, db=None, capital_client=None, macro_nlp=None,
                 tda_engine=None, swarm=None, telegram_router=None):
        self._db = db
        self._capital = capital_client
        self._macro = macro_nlp
        self._tda = tda_engine
        self._swarm = swarm
        self._tg = telegram_router
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # HDC components
        self._encoder = HDCEncoder(_DIMS)
        self._memory = AssociativeMemory(_MAX_MEMORY, _DIMS)
        self._feature_history: Dict[str, List[Dict[str, str]]] = {}

        # Active predictions
        self._predictions: Dict[str, Tuple[str, float]] = {}  # inst → (outcome, confidence)

        # Stats
        self._scans = 0
        self._patterns_stored = 0
        self._matches_found = 0
        self._predictions_made = 0
        self._last_scan_ms = 0.0

        self._ensure_table()
        logger.info(
            f"🧠 M34 HDC Memory initialisé "
            f"(D={_DIMS:,} dims | {_MAX_MEMORY} slots | "
            f"{self._memory.memory_usage_mb:.1f}MB)"
        )

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="hdc_memory"
        )
        self._thread.start()
        logger.info("🧠 M34 HDC Memory démarré (scan toutes les 15s)")

    def stop(self):
        self._running = False

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_hdc_prediction(self, instrument: str) -> Tuple[str, float]:
        """
        Retourne la prédiction HDC pour un instrument.
        Returns: (predicted_outcome, confidence)
        """
        with self._lock:
            return self._predictions.get(instrument, ("NONE", 0.0))

    def store_outcome(self, instrument: str, features: Dict[str, str],
                      outcome: str):
        """Stocke un pattern avec son résultat connu (apprentissage)."""
        label = f"{instrument}_{int(time.time())}"
        vector = self._encoder.encode(features, label=label)
        self._memory.store(vector, outcome=outcome)
        self._patterns_stored += 1

    def stats(self) -> dict:
        with self._lock:
            preds = {k: f"{v[0]}({v[1]:.0%})" for k, v in self._predictions.items()
                     if v[1] > 0.3}
        return {
            "scans": self._scans,
            "patterns_stored": self._patterns_stored,
            "matches_found": self._matches_found,
            "predictions_made": self._predictions_made,
            "memory_mb": round(self._memory.memory_usage_mb, 1),
            "memory_fill": f"{self._memory._filled}/{_MAX_MEMORY}",
            "dims": _DIMS,
            "predictions": preds,
            "last_scan_ms": round(self._last_scan_ms, 1),
        }

    def format_report(self) -> str:
        s = self.stats()
        pred_str = " | ".join(
            f"{k}:{v}" for k, v in s["predictions"].items()
        ) or "—"
        return (
            f"🧠 <b>HDC Memory (M34)</b>\n\n"
            f"  Dims: {s['dims']:,} | Memory: {s['memory_mb']}MB\n"
            f"  Patterns: {s['memory_fill']}\n"
            f"  Matches: {s['matches_found']} | Predictions: {s['predictions_made']}\n"
            f"  Actifs: {pred_str}"
        )

    # ─── Scan Loop ───────────────────────────────────────────────────────────

    def _scan_loop(self):
        time.sleep(45)
        while self._running:
            t0 = time.time()
            try:
                self._scan_cycle()
            except Exception as e:
                logger.debug(f"M34 scan: {e}")
            self._last_scan_ms = (time.time() - t0) * 1000
            self._scans += 1
            time.sleep(_SCAN_INTERVAL_S)

    def _scan_cycle(self):
        """Cycle: extract features → encode → query memory → predict."""
        if not self._capital:
            return

        instruments = [
            "GOLD", "US500", "US100", "BTCUSD", "ETHUSD",
            "EURUSD", "GBPUSD", "USDJPY", "DE40", "OIL_CRUDE",
        ]

        for inst in instruments:
            try:
                features = self._extract_features(inst)
                if not features:
                    continue

                # Accumuler l'historique des features
                if inst not in self._feature_history:
                    self._feature_history[inst] = []
                self._feature_history[inst].append(features)
                self._feature_history[inst] = self._feature_history[inst][-_TEMPORAL_WINDOW:]

                # Encoder le pattern actuel (avec contexte temporel)
                if len(self._feature_history[inst]) >= 3:
                    current_vec = self._encoder.encode_temporal(
                        self._feature_history[inst][-5:],
                        label=f"{inst}_{int(time.time())}"
                    )
                else:
                    current_vec = self._encoder.encode(features, label=inst)

                # Stocker le pattern pour apprentissage futur
                self._memory.store(current_vec, outcome="PENDING")
                self._patterns_stored += 1

                # Chercher un match dans la mémoire
                outcome, similarity = self._memory.best_match(current_vec)

                if outcome and outcome != "PENDING" and similarity > _MATCH_THRESHOLD:
                    self._matches_found += 1
                    self._predictions_made += 1

                    with self._lock:
                        self._predictions[inst] = (outcome, similarity)

                    logger.info(
                        f"🧠 M34 MATCH: {inst} → {outcome} "
                        f"sim={similarity:.3f} "
                        f"(pattern #{self._memory._filled})"
                    )

            except Exception:
                pass

    # ─── Feature Extraction ──────────────────────────────────────────────────

    def _extract_features(self, instrument: str) -> Optional[Dict[str, str]]:
        """Extrait les features multi-sources pour un instrument."""
        features = {}

        # 1. Prix — direction
        try:
            px = self._capital.get_current_price(instrument)
            if not px or px.get("mid", 0) <= 0:
                return None
            bid, ask = px.get("bid", 0), px.get("ask", 0)
            spread_pct = (ask - bid) / max(px["mid"], 1e-8) * 100

            if spread_pct > 0.1:
                features["price_direction"] = "DOWN" if bid < ask * 0.999 else "UP"
            else:
                features["price_direction"] = "FLAT"
        except Exception:
            return None

        # 2. Volume — régime
        features["volume_regime"] = "NORMAL"  # Default sans données tick-level

        # 3. Volatilité
        if spread_pct > 0.5:
            features["volatility_level"] = "EXTREME"
        elif spread_pct > 0.2:
            features["volatility_level"] = "HIGH"
        elif spread_pct > 0.05:
            features["volatility_level"] = "NORMAL"
        else:
            features["volatility_level"] = "LOW"

        # 4. Momentum
        features["momentum_state"] = "FLAT"  # Enrichi par d'autres moteurs

        # 5. Macro sentiment (M26)
        if self._macro:
            try:
                sent = self._macro.get_current_sentiment()
                label = sent.get("label", "NEUTRAL")
                features["macro_sentiment"] = label
            except Exception:
                features["macro_sentiment"] = "NEUTRAL"
        else:
            features["macro_sentiment"] = "NEUTRAL"

        # 6. TDA topology (M29)
        if self._tda:
            try:
                sig, sev, regime = self._tda.get_tda_signal(instrument)
                if "CRASH" in sig:
                    features["tda_topology"] = "CRASH"
                elif "PUMP" in sig:
                    features["tda_topology"] = "PUMP"
                else:
                    features["tda_topology"] = "NORMAL"
            except Exception:
                features["tda_topology"] = "NORMAL"
        else:
            features["tda_topology"] = "NORMAL"

        # 7. Swarm alert (M27)
        if self._swarm:
            try:
                has_alert, _, _ = self._swarm.get_swarm_signal(instrument)
                features["swarm_alert"] = "ACTIVE" if has_alert else "NONE"
            except Exception:
                features["swarm_alert"] = "NONE"
        else:
            features["swarm_alert"] = "NONE"

        # 8. Orderbook imbalance
        features["orderbook_imbalance"] = "BALANCED"  # Enrichi par M24

        return features

    # ─── Database ────────────────────────────────────────────────────────────

    def _ensure_table(self):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS hdc_matches (
                    id           SERIAL PRIMARY KEY,
                    instrument   VARCHAR(20),
                    outcome      VARCHAR(30),
                    similarity   FLOAT,
                    pattern_id   INT,
                    detected_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"M34 table: {e}")
