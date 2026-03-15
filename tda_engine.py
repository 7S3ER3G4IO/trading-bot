"""
tda_engine.py — Moteur 29 : Topological Data Analysis & Chaos Theory

Analyse topologique des données de marché via l'Homologie Persistante.
Au lieu de lire des courbes de prix (1D), le bot analyse la forme
géométrique des nuages de points multidimensionnels (prix, volume,
volatilité, momentum) et détecte les changements de topologie
(apparition/disparition de "trous") qui précèdent les krachs.

Architecture :
  VietorisRips       → construction du complexe simplicial
  PersistentHomology → calcul des diagrammes de persistance (Betti numbers)
  ChaosDetector      → exposants de Lyapunov + dimension fractale
  TopologicalSignal  → signaux basés sur les changements topologiques

Mathématiques :
  - Betti-0 (β₀) : composantes connexes → fragmentation du marché
  - Betti-1 (β₁) : trous 1D → cycles récurrents de prix
  - Betti-2 (β₂) : cavités → structures volumiques cachées
  - Lyapunov > 0  : chaos → sensibilité aux conditions initiales
  - Dimension fractale ≠ entier → structure auto-similaire

Note : Implémentation pure scipy (pas giotto-tda) pour compatibilité ARM64 Docker.
"""
import time
import threading
import math
from typing import Dict, Optional, List, Tuple
from datetime import datetime, timezone, timedelta
from loguru import logger
import numpy as np

try:
    from scipy.spatial.distance import pdist, squareform
    from scipy.sparse.csgraph import minimum_spanning_tree
    _SCIPY_OK = True
except ImportError:
    _SCIPY_OK = False

# ─── Configuration ────────────────────────────────────────────────────────────
_SCAN_INTERVAL_S      = 45       # Scan toutes les 45s
_MIN_POINTS           = 30       # Minimum 30 points pour TDA
_MAX_POINTS           = 150      # Limiter pour la RAM
_BETTI_CRASH_THRESH   = 3        # β₁ > 3 → structure anormale
_LYAPUNOV_CHAOS_THRESH = 0.05    # λ > 0.05 → chaos confirmé
_FRACTAL_ALARM        = 1.7      # Dimension fractale > 1.7 → clustering fractal
_TOPOLOGY_CHANGE_Z    = 2.5      # Z-score changement topologique

# Instruments ciblés pour TDA (les plus liquides)
_TDA_INSTRUMENTS = [
    "GOLD", "US500", "US100", "BTCUSD", "ETHUSD",
    "EURUSD", "GBPUSD", "USDJPY", "DE40", "OIL_CRUDE",
]

# Dimensions de l'espace de plongement (embedding)
_EMBED_DIMS = ["price", "volume", "volatility", "momentum", "rsi_proxy"]


class BettiNumbers:
    """Nombres de Betti d'un complexe simplicial."""
    __slots__ = ("b0", "b1", "b2", "persistence_entropy", "max_persistence")

    def __init__(self, b0=1, b1=0, b2=0, entropy=0, max_pers=0):
        self.b0 = b0     # Composantes connexes
        self.b1 = b1     # Cycles (trous 1D)
        self.b2 = b2     # Cavités (trous 2D)
        self.persistence_entropy = entropy
        self.max_persistence = max_pers

    def __repr__(self):
        return f"Betti(β₀={self.b0} β₁={self.b1} β₂={self.b2} H={self.persistence_entropy:.3f})"


class ChaosState:
    """État chaotique d'une série temporelle."""
    __slots__ = ("lyapunov", "fractal_dim", "hurst", "is_chaotic",
                 "regime", "predictability")

    def __init__(self, lyapunov=0, fractal_dim=1.5, hurst=0.5):
        self.lyapunov = lyapunov
        self.fractal_dim = fractal_dim
        self.hurst = hurst           # Hurst exponent
        self.is_chaotic = lyapunov > _LYAPUNOV_CHAOS_THRESH
        # Régime
        if hurst > 0.6:
            self.regime = "TRENDING"
        elif hurst < 0.4:
            self.regime = "MEAN_REVERT"
        else:
            self.regime = "RANDOM_WALK"
        # Prédictibilité [0..1]
        self.predictability = max(0, min(1, abs(hurst - 0.5) * 2))


class TopologicalSignal:
    """Signal émis par le détecteur topologique."""

    def __init__(self, instrument: str, signal_type: str, severity: float,
                 betti: BettiNumbers, chaos: ChaosState):
        self.instrument = instrument
        self.signal_type = signal_type  # "TOPO_CRASH", "TOPO_PUMP", "CHAOS_SHIFT"
        self.severity = severity        # [0..1]
        self.betti = betti
        self.chaos = chaos
        self.timestamp = datetime.now(timezone.utc)


class TDAEngine:
    """
    Moteur 29 : Topological Data Analysis & Chaos Theory.

    Calcule l'Homologie Persistante des données de marché pour
    détecter les changements de topologie avant les événements extrêmes.
    """

    def __init__(self, db=None, capital_client=None, telegram_router=None):
        self._db = db
        self._capital = capital_client
        self._tg = telegram_router
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # Historique des Betti numbers par instrument
        self._betti_history: Dict[str, List[BettiNumbers]] = {}
        self._chaos_states: Dict[str, ChaosState] = {}
        self._active_signals: Dict[str, TopologicalSignal] = {}

        # Cache des données multidim
        self._point_clouds: Dict[str, np.ndarray] = {}

        # Stats
        self._scans = 0
        self._topology_changes = 0
        self._chaos_detections = 0
        self._signals_fired = 0
        self._last_scan_ms = 0.0

        self._ensure_table()
        logger.info("🔮 M29 TDA Engine initialisé (Persistent Homology + Chaos Theory)")

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="tda_engine"
        )
        self._thread.start()
        logger.info("🔮 M29 TDA Engine démarré (scan toutes les 45s)")

    def stop(self):
        self._running = False

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_tda_signal(self, instrument: str) -> Tuple[str, float, str]:
        """
        Retourne le signal topologique pour un instrument.
        Returns: (signal_type, severity, regime)
        """
        with self._lock:
            sig = self._active_signals.get(instrument)
            chaos = self._chaos_states.get(instrument)

        if sig and sig.severity > 0.3:
            regime = chaos.regime if chaos else "UNKNOWN"
            return sig.signal_type, sig.severity, regime

        return "NONE", 0.0, chaos.regime if chaos else "UNKNOWN"

    def get_betti(self, instrument: str) -> Optional[BettiNumbers]:
        """Retourne les Betti numbers actuels."""
        with self._lock:
            hist = self._betti_history.get(instrument, [])
        return hist[-1] if hist else None

    def get_chaos(self, instrument: str) -> Optional[ChaosState]:
        """Retourne l'état chaotique d'un instrument."""
        with self._lock:
            return self._chaos_states.get(instrument)

    def stats(self) -> dict:
        with self._lock:
            betti_summary = {}
            for inst, hist in self._betti_history.items():
                if hist:
                    b = hist[-1]
                    betti_summary[inst] = f"β({b.b0},{b.b1},{b.b2})"
            chaos_summary = {
                inst: f"{c.regime}(λ={c.lyapunov:.3f})"
                for inst, c in self._chaos_states.items()
            }
            signals = {k: v.signal_type for k, v in self._active_signals.items()}

        return {
            "scans": self._scans,
            "topology_changes": self._topology_changes,
            "chaos_detections": self._chaos_detections,
            "signals_fired": self._signals_fired,
            "betti": betti_summary,
            "chaos": chaos_summary,
            "active_signals": signals,
            "last_scan_ms": round(self._last_scan_ms, 1),
        }

    def format_report(self) -> str:
        s = self.stats()
        betti_str = " | ".join(f"{k}:{v}" for k, v in list(s["betti"].items())[:5]) or "—"
        chaos_str = " | ".join(f"{k}:{v}" for k, v in list(s["chaos"].items())[:5]) or "—"
        return (
            f"🔮 <b>TDA Engine (M29)</b>\n\n"
            f"  Scans: {s['scans']} | Topo Δ: {s['topology_changes']}\n"
            f"  Chaos: {s['chaos_detections']} | Signals: {s['signals_fired']}\n"
            f"  Betti: {betti_str}\n"
            f"  Chaos: {chaos_str}"
        )

    # ─── Scan Loop ───────────────────────────────────────────────────────────

    def _scan_loop(self):
        time.sleep(30)
        while self._running:
            t0 = time.time()
            try:
                self._scan_cycle()
            except Exception as e:
                logger.debug(f"M29 scan: {e}")
            self._last_scan_ms = (time.time() - t0) * 1000
            self._scans += 1
            time.sleep(_SCAN_INTERVAL_S)

    def _scan_cycle(self):
        """Cycle: build point cloud → persistent homology → chaos → signals."""
        for instrument in _TDA_INSTRUMENTS:
            try:
                # 1. Construire le nuage de points multidimensionnel
                cloud = self._build_point_cloud(instrument)
                if cloud is None or len(cloud) < _MIN_POINTS:
                    continue

                with self._lock:
                    self._point_clouds[instrument] = cloud

                # 2. Calculer l'homologie persistante
                betti = self._compute_persistent_homology(cloud)

                with self._lock:
                    if instrument not in self._betti_history:
                        self._betti_history[instrument] = []
                    self._betti_history[instrument].append(betti)
                    # Keep last 50
                    self._betti_history[instrument] = self._betti_history[instrument][-50:]

                # 3. Calculer les indicateurs de chaos
                prices = cloud[:, 0]  # Colonne prix
                chaos = self._compute_chaos_indicators(prices)
                with self._lock:
                    self._chaos_states[instrument] = chaos

                # 4. Détecter les changements topologiques
                self._detect_topology_change(instrument, betti, chaos)

            except Exception:
                pass

    # ─── Point Cloud Construction ────────────────────────────────────────────

    def _build_point_cloud(self, instrument: str) -> Optional[np.ndarray]:
        """
        Construit un nuage de points 5D à partir des données OHLCV.
        Dimensions : [price, volume, volatility, momentum, rsi_proxy]
        """
        if not self._capital:
            return None

        try:
            df = self._capital.fetch_ohlcv(instrument, "5m", _MAX_POINTS)
            if df is None or len(df) < _MIN_POINTS:
                return None

            close = df["close"].values.astype(float)
            high = df["high"].values.astype(float)
            low = df["low"].values.astype(float)
            volume = df["volume"].values.astype(float) if "volume" in df else np.ones(len(close))

            n = len(close)

            # Dim 1: Prix normalisé
            price_norm = (close - close.mean()) / max(close.std(), 1e-8)

            # Dim 2: Volume normalisé (log)
            vol_log = np.log1p(np.abs(volume))
            vol_norm = (vol_log - vol_log.mean()) / max(vol_log.std(), 1e-8)

            # Dim 3: Volatilité (range normalisé)
            volatility = (high - low) / np.maximum(close, 1e-8)
            vol_std = max(volatility.std(), 1e-8)
            volatility_norm = (volatility - volatility.mean()) / vol_std

            # Dim 4: Momentum (return 5 périodes)
            momentum = np.zeros(n)
            momentum[5:] = (close[5:] - close[:-5]) / np.maximum(close[:-5], 1e-8)
            mom_std = max(momentum.std(), 1e-8)
            momentum_norm = (momentum - momentum.mean()) / mom_std

            # Dim 5: RSI proxy (up ratio)
            diff = np.diff(close, prepend=close[0])
            up_ratio = np.zeros(n)
            for i in range(14, n):
                window = diff[i - 14:i]
                ups = np.sum(window[window > 0])
                downs = abs(np.sum(window[window < 0]))
                up_ratio[i] = ups / max(ups + downs, 1e-8)
            rsi_norm = (up_ratio - 0.5) * 2  # Centré sur 0

            cloud = np.column_stack([
                price_norm, vol_norm, volatility_norm, momentum_norm, rsi_norm
            ])

            return cloud

        except Exception:
            return None

    # ─── Persistent Homology ─────────────────────────────────────────────────

    def _compute_persistent_homology(self, cloud: np.ndarray) -> BettiNumbers:
        """
        Calcule l'homologie persistante via le complexe de Vietoris-Rips.
        Implémentation légère via scipy (distance matrix + filtration).
        """
        if not _SCIPY_OK or len(cloud) < 10:
            return BettiNumbers()

        # Sous-échantillonner si trop de points (O(n²) pour la distance)
        if len(cloud) > 80:
            idx = np.random.choice(len(cloud), 80, replace=False)
            cloud = cloud[idx]

        n = len(cloud)

        # 1. Distance matrix
        dist_matrix = squareform(pdist(cloud, metric='euclidean'))

        # 2. Filtration : trouver les seuils de naissance/mort
        # Minimum Spanning Tree → Betti-0 (composantes connexes)
        mst = minimum_spanning_tree(dist_matrix).toarray()
        mst_weights = mst[mst > 0]
        mst_weights = np.sort(mst_weights)

        # β₀ : composantes connexes à un seuil donné
        thresh_median = np.median(dist_matrix[dist_matrix > 0]) if np.any(dist_matrix > 0) else 1.0
        adjacency = (dist_matrix > 0) & (dist_matrix < thresh_median)
        b0 = self._count_components(adjacency, n)

        # β₁ : cycles (trous 1D) — approximé par le nombre de cycles
        # dans le graphe à seuil médian
        n_edges = np.sum(adjacency) // 2
        b1 = max(0, n_edges - n + b0)

        # β₂ : cavités — estimé via la dimension du simplicial complex
        # (nombre de triangles - nombre de "surfaces fermées")
        n_triangles = self._count_triangles(adjacency, n)
        b2 = max(0, n_triangles - n_edges + n - b0) if n_triangles > 0 else 0

        # Persistence diagram (birth-death pairs)
        births = mst_weights[:len(mst_weights) // 2] if len(mst_weights) > 0 else np.array([0])
        deaths = mst_weights[len(mst_weights) // 2:] if len(mst_weights) > 0 else np.array([1])
        persistences = deaths[:min(len(births), len(deaths))] - births[:min(len(births), len(deaths))]
        persistences = persistences[persistences > 0]

        # Persistence entropy
        if len(persistences) > 0:
            total = persistences.sum()
            probs = persistences / max(total, 1e-10)
            entropy = -np.sum(probs * np.log2(probs + 1e-10))
            max_pers = float(persistences.max())
        else:
            entropy = 0.0
            max_pers = 0.0

        return BettiNumbers(
            b0=b0, b1=b1, b2=b2,
            entropy=round(entropy, 4),
            max_pers=round(max_pers, 4),
        )

    @staticmethod
    def _count_components(adjacency: np.ndarray, n: int) -> int:
        """Compte les composantes connexes via BFS."""
        visited = set()
        components = 0
        for start in range(n):
            if start in visited:
                continue
            components += 1
            queue = [start]
            while queue:
                node = queue.pop(0)
                if node in visited:
                    continue
                visited.add(node)
                for neighbor in range(n):
                    if adjacency[node, neighbor] and neighbor not in visited:
                        queue.append(neighbor)
        return components

    @staticmethod
    def _count_triangles(adjacency: np.ndarray, n: int) -> int:
        """Compte le nombre de triangles dans le graphe."""
        count = 0
        for i in range(n):
            for j in range(i + 1, n):
                if not adjacency[i, j]:
                    continue
                for k in range(j + 1, n):
                    if adjacency[j, k] and adjacency[i, k]:
                        count += 1
        return count

    # ─── Chaos Theory ────────────────────────────────────────────────────────

    def _compute_chaos_indicators(self, prices: np.ndarray) -> ChaosState:
        """
        Calcule les indicateurs de chaos :
        - Exposant de Lyapunov (sensibilité aux conditions initiales)
        - Dimension fractale (auto-similarité)
        - Exposant de Hurst (mémoire long terme)
        """
        if len(prices) < 20:
            return ChaosState()

        lyapunov = self._lyapunov_exponent(prices)
        fractal = self._fractal_dimension(prices)
        hurst = self._hurst_exponent(prices)

        return ChaosState(lyapunov=lyapunov, fractal_dim=fractal, hurst=hurst)

    @staticmethod
    def _lyapunov_exponent(series: np.ndarray) -> float:
        """
        Estime l'exposant maximal de Lyapunov.
        λ > 0 → chaos (divergence exponentielle)
        λ < 0 → convergence (attracteur stable)
        """
        n = len(series)
        if n < 20:
            return 0.0

        # Méthode de Rosenstein simplifiée
        # 1. Phase space embedding (delay = 1, dim = 3)
        dim = 3
        delay = 1
        m = n - (dim - 1) * delay
        if m < 10:
            return 0.0

        # Construire les vecteurs reconstruits
        embedded = np.zeros((m, dim))
        for d in range(dim):
            embedded[:, d] = series[d * delay:d * delay + m]

        # 2. Pour chaque point, trouver le plus proche voisin
        lyap_sum = 0.0
        count = 0
        for i in range(m - 1):
            min_dist = float('inf')
            min_j = -1
            for j in range(m - 1):
                if abs(i - j) < 5:  # Exclure les voisins temporels
                    continue
                dist = np.linalg.norm(embedded[i] - embedded[j])
                if 0 < dist < min_dist:
                    min_dist = dist
                    min_j = j

            if min_j >= 0 and min_j + 1 < m and min_dist > 1e-10:
                next_dist = np.linalg.norm(embedded[i + 1] - embedded[min_j + 1])
                if next_dist > 1e-10:
                    lyap_sum += math.log(next_dist / min_dist)
                    count += 1

        return round(lyap_sum / max(count, 1), 4)

    @staticmethod
    def _fractal_dimension(series: np.ndarray) -> float:
        """
        Calcule la dimension fractale via la méthode box-counting (Higuchi).
        D = 1.0 → ligne droite
        D = 1.5 → mouvement brownien
        D = 2.0 → bruit blanc (remplit le plan)
        """
        n = len(series)
        if n < 20:
            return 1.5

        k_max = min(10, n // 4)
        lengths = []
        ks = []

        for k in range(1, k_max + 1):
            L_k = 0.0
            count = 0
            for m in range(k):
                # Longueur de la courbe pour offset m, intervalle k
                indices = np.arange(m, n, k)
                if len(indices) < 2:
                    continue
                sub = series[indices]
                L = np.sum(np.abs(np.diff(sub))) * (n - 1) / (k * (len(sub) - 1))
                L_k += L
                count += 1
            if count > 0:
                lengths.append(L_k / count)
                ks.append(k)

        if len(ks) < 3:
            return 1.5

        # Régression log-log
        log_k = np.log(np.array(ks))
        log_L = np.log(np.array(lengths) + 1e-10)

        # Pente = -dimension fractale
        coeffs = np.polyfit(log_k, log_L, 1)
        D = -coeffs[0]

        return round(max(1.0, min(2.0, D)), 4)

    @staticmethod
    def _hurst_exponent(series: np.ndarray) -> float:
        """
        Calcule l'exposant de Hurst via R/S analysis.
        H > 0.5 → tendance persistante (mémoire longue)
        H = 0.5 → marche aléatoire
        H < 0.5 → mean-reverting (anti-persistent)
        """
        n = len(series)
        if n < 20:
            return 0.5

        max_k = min(n // 2, 50)
        rs_values = []
        ns_values = []

        for k_size in [int(n / k) for k in range(2, min(10, n // 4 + 1))]:
            if k_size < 4:
                continue

            rs_list = []
            for i in range(0, n - k_size + 1, k_size):
                chunk = series[i:i + k_size]
                mean = chunk.mean()
                deviations = chunk - mean
                cumsum = np.cumsum(deviations)
                R = cumsum.max() - cumsum.min()
                S = chunk.std()
                if S > 1e-10:
                    rs_list.append(R / S)

            if rs_list:
                rs_values.append(np.mean(rs_list))
                ns_values.append(k_size)

        if len(ns_values) < 3:
            return 0.5

        log_n = np.log(np.array(ns_values))
        log_rs = np.log(np.array(rs_values) + 1e-10)

        coeffs = np.polyfit(log_n, log_rs, 1)
        H = coeffs[0]

        return round(max(0.0, min(1.0, H)), 4)

    # ─── Signal Detection ────────────────────────────────────────────────────

    def _detect_topology_change(self, instrument: str, betti: BettiNumbers,
                                chaos: ChaosState):
        """Détecte les changements topologiques significatifs."""
        with self._lock:
            hist = self._betti_history.get(instrument, [])

        if len(hist) < 5:
            return

        # Calculer le Z-score du β₁ actuel
        b1_history = [b.b1 for b in hist[:-1]]
        b1_mean = np.mean(b1_history)
        b1_std = max(np.std(b1_history), 0.1)
        b1_z = (betti.b1 - b1_mean) / b1_std

        signal = None

        # 1. Explosion de β₁ → structure cyclique anormale → crash imminent
        if b1_z > _TOPOLOGY_CHANGE_Z and betti.b1 > _BETTI_CRASH_THRESH:
            severity = min(b1_z / 5, 1.0)
            signal = TopologicalSignal(
                instrument=instrument,
                signal_type="TOPO_CRASH",
                severity=severity,
                betti=betti,
                chaos=chaos,
            )
            self._topology_changes += 1
            logger.info(
                f"🔮 M29 TOPO CRASH: {instrument} β₁={betti.b1} (z={b1_z:.2f}) "
                f"λ={chaos.lyapunov:.3f} D={chaos.fractal_dim:.2f}"
            )

        # 2. Collapse de β₁ → simplification topologique → breakout imminent
        elif b1_z < -_TOPOLOGY_CHANGE_Z:
            severity = min(abs(b1_z) / 5, 1.0)
            signal = TopologicalSignal(
                instrument=instrument,
                signal_type="TOPO_PUMP",
                severity=severity,
                betti=betti,
                chaos=chaos,
            )
            self._topology_changes += 1

        # 3. Chaos shift (Lyapunov → positif)
        if chaos.is_chaotic and chaos.lyapunov > _LYAPUNOV_CHAOS_THRESH * 2:
            self._chaos_detections += 1
            if not signal:
                signal = TopologicalSignal(
                    instrument=instrument,
                    signal_type="CHAOS_SHIFT",
                    severity=min(chaos.lyapunov / 0.2, 1.0),
                    betti=betti,
                    chaos=chaos,
                )

        if signal:
            with self._lock:
                self._active_signals[instrument] = signal
            self._signals_fired += 1
            self._persist_signal(signal)

    # ─── Database ────────────────────────────────────────────────────────────

    def _ensure_table(self):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS tda_signals (
                    id            SERIAL PRIMARY KEY,
                    instrument    VARCHAR(20),
                    signal_type   VARCHAR(20),
                    severity      FLOAT,
                    betti_0       INT,
                    betti_1       INT,
                    betti_2       INT,
                    lyapunov      FLOAT,
                    fractal_dim   FLOAT,
                    hurst         FLOAT,
                    detected_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"M29 table: {e}")

    def _persist_signal(self, sig: TopologicalSignal):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            ph = "%s"
            self._db._execute(
                f"INSERT INTO tda_signals "
                f"(instrument,signal_type,severity,betti_0,betti_1,betti_2,"
                f"lyapunov,fractal_dim,hurst) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (sig.instrument, sig.signal_type, float(sig.severity),
                 int(sig.betti.b0), int(sig.betti.b1), int(sig.betti.b2),
                 float(sig.chaos.lyapunov), float(sig.chaos.fractal_dim),
                 float(sig.chaos.hurst))
            )
        except Exception:
            pass
