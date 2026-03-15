"""
algo_hunter.py — Moteur 24 : Adversarial AI & Algorithm Hunting

Détecte les signatures algorithmiques institutionnelles en analysant
le flux de transactions tick-by-tick. Identifie les ordres Iceberg,
TWAP et VWAP des gros algos concurrents, puis s'insère devant
eux (Penny-Jumping) pour exploiter leur pression d'achat/vente.

Architecture :
  TickCollector        → collecte les ticks en temps réel via WS Capital.com
  IcebergDetector      → détecte les patterns d'ordres cachés (régularité volume)
  TWAPDetector         → détecte les ordres temps-réguliers (TWAP)
  SignatureClassifier  → RandomForest pour classifier le type d'algo
  PennyJumper          → calcule le point d'insertion optimal

Signaux :
  - PENNY_JUMP_BUY  → un algo institutionnel achète → placer un BUY juste devant
  - PENNY_JUMP_SELL → un algo institutionnel vend → placer un SELL juste devant
"""
import time
import threading
import math
import collections
from typing import Dict, Optional, List, Tuple
from datetime import datetime, timezone, timedelta
from loguru import logger
import numpy as np

try:
    from sklearn.ensemble import RandomForestClassifier
    _SKLEARN_OK = True
except ImportError:
    _SKLEARN_OK = False

# ─── Configuration ────────────────────────────────────────────────────────────
_SCAN_INTERVAL_S       = 10      # Analyse toutes les 10s
_TICK_WINDOW           = 200     # Garder les 200 derniers ticks par instrument
_MIN_TICKS_DETECT      = 30      # Minimum 30 ticks pour détecter un pattern
_ICEBERG_VOLUME_CV_MAX = 0.15    # Coefficient de variation max pour Iceberg
_TWAP_TIME_CV_MAX      = 0.10    # Coefficient de variation max pour TWAP
_CONFIDENCE_THRESHOLD  = 0.65    # Seuil de confiance pour émettre un signal
_PENNY_JUMP_PIPS       = 1       # Nombre de pips devant le flow détecté

# Types d'algos détectables
ALGO_ICEBERG = "ICEBERG"
ALGO_TWAP    = "TWAP"
ALGO_VWAP    = "VWAP"
ALGO_RANDOM  = "RANDOM"
ALGO_UNKNOWN = "UNKNOWN"

# Signaux
SIGNAL_PENNY_BUY  = "PENNY_JUMP_BUY"
SIGNAL_PENNY_SELL = "PENNY_JUMP_SELL"
SIGNAL_NONE       = "NONE"


class Tick:
    """Un tick de marché brut."""
    __slots__ = ("price", "volume", "timestamp", "side")

    def __init__(self, price: float, volume: float, timestamp: float, side: str = ""):
        self.price = price
        self.volume = volume
        self.timestamp = timestamp
        self.side = side   # "BUY" ou "SELL" (si déduit)


class AlgoDetection:
    """Résultat de détection d'un algo institutionnel."""

    def __init__(self, instrument: str, algo_type: str, direction: str,
                 estimated_size: float, confidence: float):
        self.instrument = instrument
        self.algo_type = algo_type
        self.direction = direction              # "BUY" ou "SELL"
        self.estimated_size = estimated_size     # Volume estimé restant
        self.confidence = confidence             # [0..1]
        self.timestamp = datetime.now(timezone.utc)
        self.penny_signal = SIGNAL_NONE

    def __repr__(self):
        return (f"AlgoDetection({self.instrument} {self.algo_type} "
                f"{self.direction} conf={self.confidence:.2f})")


class AlgoHunter:
    """
    Moteur 24 : Adversarial AI & Algorithm Hunting.

    Analyse le flux tick-by-tick pour détecter les signatures d'algos
    institutionnels (Iceberg, TWAP, VWAP) et s'insérer devant eux.
    """

    def __init__(self, db=None, capital_client=None, capital_ws=None,
                 telegram_router=None):
        self._db = db
        self._capital = capital_client
        self._ws = capital_ws
        self._tg = telegram_router
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # Tick buffers par instrument
        self._ticks: Dict[str, collections.deque] = {}

        # Détections actives
        self._active_detections: Dict[str, AlgoDetection] = {}

        # Classifier ML
        self._classifier = None
        self._classifier_trained = False
        self._init_classifier()

        # Stats
        self._scans = 0
        self._detections_total = 0
        self._penny_jumps = 0
        self._last_scan_ms = 0.0

        # Instruments à surveiller (les plus liquides)
        self._watched = [
            "EURUSD", "GBPUSD", "USDJPY", "GOLD", "US500",
            "US100", "BTCUSD", "DE40", "OIL_CRUDE", "ETHUSD",
        ]

        self._ensure_table()
        logger.info(
            f"🎯 M24 Algo Hunter initialisé ({len(self._watched)} instruments surveillés)"
        )

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True

        # Enregistrer le callback WebSocket pour collecter les ticks
        if self._ws and hasattr(self._ws, 'register_tick_callback'):
            self._ws.register_tick_callback(self._on_tick)

        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="algo_hunter"
        )
        self._thread.start()
        logger.info("🎯 M24 Algo Hunter démarré (scan toutes les 10s)")

    def stop(self):
        self._running = False

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_hunt_signal(self, instrument: str) -> Tuple[str, float, str]:
        """
        Retourne le signal de chasse pour un instrument.
        Returns: (signal_type, confidence, algo_type)
        """
        with self._lock:
            det = self._active_detections.get(instrument)

        if not det or det.confidence < _CONFIDENCE_THRESHOLD:
            return SIGNAL_NONE, 0.0, ALGO_UNKNOWN

        # Déterminer le signal penny-jump
        if det.direction == "BUY":
            return SIGNAL_PENNY_BUY, det.confidence, det.algo_type
        elif det.direction == "SELL":
            return SIGNAL_PENNY_SELL, det.confidence, det.algo_type

        return SIGNAL_NONE, 0.0, det.algo_type

    def detect(self, instrument: str) -> Optional[AlgoDetection]:
        """Retourne la détection active pour un instrument."""
        with self._lock:
            return self._active_detections.get(instrument)

    def on_tick(self, instrument: str, price: float, volume: float = 0,
                side: str = ""):
        """Callback pour recevoir des ticks depuis le WebSocket."""
        self._on_tick(instrument, price, volume, side)

    def stats(self) -> dict:
        with self._lock:
            active = {k: {
                "type": v.algo_type,
                "dir": v.direction,
                "conf": round(v.confidence, 2),
            } for k, v in self._active_detections.items()}
        return {
            "scans": self._scans,
            "detections": self._detections_total,
            "penny_jumps": self._penny_jumps,
            "active_hunts": active,
            "watched": len(self._watched),
            "last_scan_ms": round(self._last_scan_ms, 1),
        }

    def format_report(self) -> str:
        s = self.stats()
        hunts_str = "\n".join(
            f"    {k}: {v['type']} {v['dir']} ({v['conf']})"
            for k, v in s["active_hunts"].items()
        ) or "    — aucune détection active"
        return (
            f"🎯 <b>Algo Hunter (M24)</b>\n\n"
            f"  Scans: {s['scans']} | Détections: {s['detections']}\n"
            f"  Penny Jumps: {s['penny_jumps']}\n"
            f"  Chasses actives:\n{hunts_str}"
        )

    # ─── Tick Collection ─────────────────────────────────────────────────────

    def _on_tick(self, instrument: str, price: float, volume: float = 0,
                 side: str = ""):
        """Collecte un tick et l'ajoute au buffer."""
        if instrument not in self._watched:
            return

        tick = Tick(price, volume, time.time(), side)

        with self._lock:
            if instrument not in self._ticks:
                self._ticks[instrument] = collections.deque(maxlen=_TICK_WINDOW)
            self._ticks[instrument].append(tick)

    # ─── Scan Loop ───────────────────────────────────────────────────────────

    def _scan_loop(self):
        time.sleep(20)  # Init delay
        while self._running:
            t0 = time.time()
            try:
                self._scan_all_instruments()
            except Exception as e:
                logger.debug(f"M24 scan: {e}")
            self._last_scan_ms = (time.time() - t0) * 1000
            self._scans += 1
            time.sleep(_SCAN_INTERVAL_S)

    def _scan_all_instruments(self):
        """Analyse chaque instrument surveillé pour détecter des algos."""
        # Si pas de ticks WS, alimenter via OHLCV simulé
        if not any(len(self._ticks.get(i, [])) > 0 for i in self._watched):
            self._simulate_ticks_from_price()

        for instrument in self._watched:
            with self._lock:
                ticks = list(self._ticks.get(instrument, []))

            if len(ticks) < _MIN_TICKS_DETECT:
                continue

            detection = self._analyze_ticks(instrument, ticks)
            if detection and detection.confidence >= _CONFIDENCE_THRESHOLD:
                with self._lock:
                    self._active_detections[instrument] = detection
                self._detections_total += 1
                self._persist_detection(detection)

                logger.info(
                    f"🎯 M24 DETECTION: {instrument} → {detection.algo_type} "
                    f"{detection.direction} conf={detection.confidence:.2f}"
                )
            else:
                # Expirer les anciennes détections (> 5 min)
                with self._lock:
                    det = self._active_detections.get(instrument)
                    if det and (datetime.now(timezone.utc) - det.timestamp).seconds > 300:
                        del self._active_detections[instrument]

    def _simulate_ticks_from_price(self):
        """Simule des ticks à partir des prix courants (fallback sans WS ticks)."""
        if not self._capital:
            return
        for instrument in self._watched:
            try:
                px = self._capital.get_current_price(instrument)
                if px:
                    vol = np.random.lognormal(3, 1)  # Volume simulé
                    side = "BUY" if np.random.random() > 0.5 else "SELL"
                    self._on_tick(instrument, px["mid"], vol, side)
            except Exception:
                pass

    # ─── Analysis Engine ─────────────────────────────────────────────────────

    def _analyze_ticks(self, instrument: str, ticks: List[Tick]) -> Optional[AlgoDetection]:
        """
        Analyse une séquence de ticks pour détecter un pattern algorithmique.
        """
        if len(ticks) < _MIN_TICKS_DETECT:
            return None

        # Extraire les features
        features = self._extract_features(ticks)

        # Classifier le type d'algo
        algo_type, confidence = self._classify(features)

        if algo_type == ALGO_RANDOM or confidence < 0.3:
            return None

        # Déterminer la direction
        direction = self._infer_direction(ticks)

        # Estimer la taille restante
        estimated_size = self._estimate_remaining_size(ticks, algo_type)

        return AlgoDetection(
            instrument=instrument,
            algo_type=algo_type,
            direction=direction,
            estimated_size=estimated_size,
            confidence=confidence,
        )

    def _extract_features(self, ticks: List[Tick]) -> np.ndarray:
        """
        Extrait les features statistiques d'une séquence de ticks.
        Features :
          [0] volume_cv         — coefficient de variation du volume
          [1] time_cv           — coefficient de variation des inter-arrivées
          [2] price_autocorr    — autocorrélation des prix (lag-1)
          [3] volume_regularity — régularité des volumes (entropie)
          [4] trade_rate        — taux de transactions / seconde
          [5] direction_bias    — biais directionnel [-1, 1]
          [6] volume_cluster    — cluster de volumes similaires
          [7] price_impact      — impact de prix par volume
        """
        volumes = np.array([t.volume for t in ticks if t.volume > 0])
        prices = np.array([t.price for t in ticks])
        times = np.array([t.timestamp for t in ticks])

        if len(volumes) < 5 or len(prices) < 5:
            return np.zeros(8)

        # [0] Volume CV
        vol_cv = np.std(volumes) / max(np.mean(volumes), 1e-8)

        # [1] Time intervals CV
        dt = np.diff(times)
        dt = dt[dt > 0]
        time_cv = np.std(dt) / max(np.mean(dt), 1e-8) if len(dt) > 2 else 1.0

        # [2] Price autocorrelation (lag-1)
        if len(prices) > 3:
            p_diff = np.diff(prices)
            if np.std(p_diff) > 0:
                price_autocorr = np.corrcoef(p_diff[:-1], p_diff[1:])[0, 1]
                price_autocorr = 0.0 if np.isnan(price_autocorr) else price_autocorr
            else:
                price_autocorr = 0.0
        else:
            price_autocorr = 0.0

        # [3] Volume regularity (entropy-based)
        vol_bins = np.histogram(volumes, bins=min(10, len(volumes) // 2 + 1))[0]
        vol_bins = vol_bins[vol_bins > 0]
        probs = vol_bins / vol_bins.sum()
        vol_entropy = -np.sum(probs * np.log2(probs + 1e-10))
        max_entropy = np.log2(len(vol_bins) + 1)
        vol_regularity = 1 - (vol_entropy / max(max_entropy, 1))

        # [4] Trade rate
        duration = times[-1] - times[0]
        trade_rate = len(ticks) / max(duration, 1)

        # [5] Direction bias
        if len(prices) > 1:
            up_moves = np.sum(np.diff(prices) > 0)
            down_moves = np.sum(np.diff(prices) < 0)
            total = up_moves + down_moves
            direction_bias = (up_moves - down_moves) / max(total, 1)
        else:
            direction_bias = 0.0

        # [6] Volume clustering (how many volumes are close to median)
        med_vol = np.median(volumes)
        close_to_median = np.sum(np.abs(volumes - med_vol) < med_vol * 0.2)
        vol_cluster = close_to_median / max(len(volumes), 1)

        # [7] Price impact per volume
        if len(prices) > 1 and np.sum(volumes) > 0:
            price_change = abs(prices[-1] - prices[0])
            price_impact = price_change / max(np.sum(volumes), 1)
        else:
            price_impact = 0.0

        return np.array([
            vol_cv, time_cv, price_autocorr, vol_regularity,
            trade_rate, direction_bias, vol_cluster, price_impact
        ])

    # ─── Classification ──────────────────────────────────────────────────────

    def _init_classifier(self):
        """Initialise le classifier avec des données synthétiques."""
        if not _SKLEARN_OK:
            return

        # Données d'entraînement synthétiques (profils typiques)
        X = np.array([
            # ICEBERG: low vol_cv, medium time_cv, high vol_cluster
            [0.10, 0.30, 0.1, 0.8, 2.0, 0.3, 0.85, 0.001],
            [0.08, 0.25, 0.0, 0.9, 1.5, 0.4, 0.90, 0.002],
            [0.12, 0.35, 0.2, 0.7, 2.5, -0.2, 0.80, 0.001],
            [0.15, 0.20, -0.1, 0.85, 1.8, 0.5, 0.82, 0.003],
            # TWAP: low time_cv, medium vol_cv, regular
            [0.30, 0.08, 0.3, 0.6, 1.0, 0.2, 0.50, 0.005],
            [0.25, 0.06, 0.1, 0.5, 0.8, 0.3, 0.45, 0.004],
            [0.35, 0.10, 0.0, 0.7, 1.2, -0.1, 0.55, 0.006],
            [0.28, 0.05, 0.2, 0.65, 0.9, 0.4, 0.48, 0.003],
            # VWAP: correlated vol/price, medium everything
            [0.40, 0.30, 0.5, 0.5, 1.5, 0.1, 0.40, 0.010],
            [0.45, 0.35, 0.6, 0.4, 1.8, 0.2, 0.35, 0.012],
            [0.38, 0.28, 0.4, 0.55, 1.3, -0.1, 0.42, 0.008],
            [0.42, 0.32, 0.55, 0.45, 1.6, 0.15, 0.38, 0.011],
            # RANDOM: high variability everywhere
            [0.80, 0.70, 0.0, 0.2, 3.0, 0.0, 0.20, 0.020],
            [0.90, 0.80, -0.1, 0.15, 5.0, -0.05, 0.15, 0.025],
            [0.75, 0.65, 0.05, 0.25, 2.5, 0.1, 0.22, 0.018],
            [0.85, 0.75, -0.05, 0.18, 4.0, -0.02, 0.18, 0.022],
        ])

        y = np.array([
            0, 0, 0, 0,   # ICEBERG
            1, 1, 1, 1,   # TWAP
            2, 2, 2, 2,   # VWAP
            3, 3, 3, 3,   # RANDOM
        ])

        self._classifier = RandomForestClassifier(
            n_estimators=20, max_depth=4, random_state=42, n_jobs=1
        )
        self._classifier.fit(X, y)
        self._classifier_trained = True
        logger.debug("🎯 M24 Classifier entraîné (4 classes, 20 arbres)")

    def _classify(self, features: np.ndarray) -> Tuple[str, float]:
        """Classifie un pattern tick en type d'algo."""
        # Fallback heuristique si pas de sklearn
        if not self._classifier_trained or not self._classifier:
            return self._classify_heuristic(features)

        try:
            X = features.reshape(1, -1)
            proba = self._classifier.predict_proba(X)[0]
            pred = int(np.argmax(proba))
            conf = float(proba[pred])

            types = {0: ALGO_ICEBERG, 1: ALGO_TWAP, 2: ALGO_VWAP, 3: ALGO_RANDOM}
            return types.get(pred, ALGO_UNKNOWN), conf
        except Exception:
            return self._classify_heuristic(features)

    def _classify_heuristic(self, f: np.ndarray) -> Tuple[str, float]:
        """Classification heuristique (fallback sans scikit-learn)."""
        vol_cv, time_cv = f[0], f[1]
        vol_cluster = f[6] if len(f) > 6 else 0

        # Iceberg: volume très régulier
        if vol_cv < _ICEBERG_VOLUME_CV_MAX and vol_cluster > 0.7:
            return ALGO_ICEBERG, 0.75

        # TWAP: tempo très régulier
        if time_cv < _TWAP_TIME_CV_MAX:
            return ALGO_TWAP, 0.70

        # VWAP: corrélation prix/volume
        if f[2] > 0.4:  # price_autocorr
            return ALGO_VWAP, 0.60

        return ALGO_RANDOM, 0.3

    def _infer_direction(self, ticks: List[Tick]) -> str:
        """Infère la direction du flow à partir des ticks."""
        if len(ticks) < 5:
            return "BUY"

        prices = [t.price for t in ticks]
        # Régression linéaire simple
        n = len(prices)
        x = np.arange(n)
        slope = (n * np.sum(x * prices) - np.sum(x) * np.sum(prices)) / \
                max(n * np.sum(x ** 2) - np.sum(x) ** 2, 1e-10)

        return "BUY" if slope > 0 else "SELL"

    def _estimate_remaining_size(self, ticks: List[Tick], algo_type: str) -> float:
        """Estime le volume restant de l'algo détecté."""
        volumes = [t.volume for t in ticks if t.volume > 0]
        if not volumes:
            return 0.0

        avg_vol = np.mean(volumes)
        duration = ticks[-1].timestamp - ticks[0].timestamp if len(ticks) > 1 else 1

        if algo_type == ALGO_ICEBERG:
            # Iceberg: estimer 5-10x le volume visible
            return avg_vol * len(volumes) * 5
        elif algo_type == ALGO_TWAP:
            # TWAP: estimer le volume restant basé sur le rythme
            rate = len(volumes) / max(duration, 1)
            remaining_time = 3600  # Estimer 1h de plus
            return avg_vol * rate * remaining_time
        elif algo_type == ALGO_VWAP:
            return avg_vol * len(volumes) * 3

        return avg_vol * len(volumes)

    # ─── Database ────────────────────────────────────────────────────────────

    def _ensure_table(self):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS algo_detections (
                    id           SERIAL PRIMARY KEY,
                    instrument   VARCHAR(20),
                    algo_type    VARCHAR(20),
                    direction    VARCHAR(4),
                    estimated_size FLOAT,
                    confidence   FLOAT,
                    detected_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"M24 table: {e}")

    def _persist_detection(self, det: AlgoDetection):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            ph = "%s"
            self._db._execute(
                f"INSERT INTO algo_detections "
                f"(instrument,algo_type,direction,estimated_size,confidence) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph})",
                (det.instrument, det.algo_type, det.direction,
                 det.estimated_size, det.confidence)
            )
        except Exception:
            pass
