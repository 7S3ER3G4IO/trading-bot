"""
ml_scorer.py — S-4: Lightweight ML Scoring (Logistic Regression)

Self-learning ML model that improves over time:
1. Collects features from each signal (ADX, RSI, vol_ratio, etc.)
2. Records outcome (WIN/LOSS) when the trade closes
3. Trains a LogisticRegression model after 100+ samples
4. predict_proba() returns P(win) → integrated as score multiplier

Before 100 samples: returns 0.5 (neutral, no effect on scoring).
After: adjusts base score by P(win), e.g. score × (0.5 + P(win)) / 1.0

Persistence: saves training data + model to JSON + pickle.
"""

import os
import json
import time
import pickle
import threading
from datetime import datetime, timezone
from typing import Optional, List, Dict
from loguru import logger

import numpy as np

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False
    logger.warning("⚠️ scikit-learn non installé — S-4 ML scoring désactivé")


# Feature names used by the model
FEATURE_NAMES = [
    "adx", "rsi", "vol_ratio", "bb_position", "wick_pct",
    "body_ratio", "atr_norm", "macd_hist", "hour_sin", "hour_cos",
    "score_raw", "mtf_bonus", "spread_ratio",
]

DATA_PATH = os.getenv("ML_DATA_PATH", "ml_training_data.json")
MODEL_PATH = os.getenv("ML_MODEL_PATH", "ml_model.pkl")
MIN_SAMPLES = 100  # Minimum samples before model is active
RETRAIN_INTERVAL = 3600  # Retrain every hour


class MLScorer:
    """
    S-4: Lightweight ML scorer with self-learning capability.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._model: Optional[object] = None
        self._scaler: Optional[object] = None
        self._training_data: List[Dict] = []
        self._last_train_time: float = 0
        self._active = HAS_SKLEARN

        if self._active:
            self._load_data()
            self._maybe_train()

    # ═══════════════════════════════════════════════════════════════════════
    #  PUBLIC API
    # ═══════════════════════════════════════════════════════════════════════

    def predict_win_probability(self, features: dict) -> float:
        """
        Returns P(win) for the given feature dict.
        
        Returns 0.5 if model is not trained yet (neutral).
        """
        if not self._active or self._model is None:
            return 0.5

        try:
            X = self._features_to_array(features)
            X_scaled = self._scaler.transform(X.reshape(1, -1))
            proba = self._model.predict_proba(X_scaled)[0]
            # proba[1] = P(win), proba[0] = P(loss)
            return float(proba[1]) if len(proba) > 1 else 0.5
        except Exception as e:
            logger.debug(f"ML predict error: {e}")
            return 0.5

    def extract_features(self, df, instrument: str, score: float,
                          mtf_bonus: float = 0.0, spread_ratio: float = 0.0) -> dict:
        """
        Extract feature vector from current market data.
        """
        if df is None or len(df) < 5:
            return {}

        last = df.iloc[-1]
        close = float(last.get("close", 0))
        high = float(last.get("high", 0))
        low = float(last.get("low", 0))
        body = abs(close - float(last.get("open", close)))
        candle_range = high - low if high > low else 0.0001

        # Hour as sin/cos for cyclical encoding
        now = datetime.now(timezone.utc)
        hour_frac = now.hour + now.minute / 60
        hour_sin = np.sin(2 * np.pi * hour_frac / 24)
        hour_cos = np.cos(2 * np.pi * hour_frac / 24)

        # Volume ratio
        vol = float(last.get("volume", 0))
        vol_ma = float(last.get("vol_ma", max(vol, 1)))
        vol_ratio = vol / vol_ma if vol_ma > 0 else 1.0

        # BB position (0 = at lower band, 1 = at upper band)
        bb_up = float(last.get("bb_up", close + 1))
        bb_lo = float(last.get("bb_lo", close - 1))
        bb_range = bb_up - bb_lo if bb_up > bb_lo else 0.0001
        bb_position = (close - bb_lo) / bb_range

        # ATR normalized
        atr = float(last.get("atr", 0))
        atr_norm = atr / close if close > 0 else 0

        # MACD histogram
        macd = float(last.get("macd", 0))
        macd_s = float(last.get("macd_s", 0))
        macd_hist = macd - macd_s

        # Wick percentage
        wick = candle_range - body
        wick_pct = wick / candle_range if candle_range > 0 else 0

        return {
            "adx": float(last.get("adx", 20)),
            "rsi": float(last.get("rsi", 50)),
            "vol_ratio": min(vol_ratio, 5.0),
            "bb_position": np.clip(bb_position, 0, 1),
            "wick_pct": np.clip(wick_pct, 0, 1),
            "body_ratio": body / candle_range if candle_range > 0 else 0.5,
            "atr_norm": min(atr_norm, 0.1),
            "macd_hist": np.clip(macd_hist, -0.01, 0.01),
            "hour_sin": hour_sin,
            "hour_cos": hour_cos,
            "score_raw": score,
            "mtf_bonus": mtf_bonus,
            "spread_ratio": min(spread_ratio, 1.0),
        }

    def record_outcome(self, features: dict, won: bool):
        """
        Record a trade outcome for future training.
        """
        if not self._active or not features:
            return

        sample = {**features, "outcome": 1 if won else 0, "ts": time.time()}
        with self._lock:
            self._training_data.append(sample)

        # Auto-save every 10 new samples
        if len(self._training_data) % 10 == 0:
            self._save_data()

        # Auto-retrain if enough time has passed
        if time.time() - self._last_train_time > RETRAIN_INTERVAL:
            self._maybe_train()

    def score_adjustment(self, base_score: float, features: dict) -> float:
        """
        Returns adjusted score based on ML prediction.
        
        Formula: adjusted = base_score × (0.5 + P(win))
        - P(win) = 0.5 → no change (×1.0)
        - P(win) = 0.8 → boost (×1.3)
        - P(win) = 0.2 → reduce (×0.7)
        """
        p_win = self.predict_win_probability(features)
        multiplier = 0.5 + p_win  # Range: 0.5 to 1.5
        return base_score * multiplier

    # ═══════════════════════════════════════════════════════════════════════
    #  INTERNAL
    # ═══════════════════════════════════════════════════════════════════════

    def _features_to_array(self, features: dict) -> np.ndarray:
        """Convert feature dict to numpy array in correct order."""
        return np.array([features.get(f, 0) for f in FEATURE_NAMES], dtype=float)

    def _maybe_train(self):
        """Train model if enough data is available."""
        if not self._active:
            return

        with self._lock:
            n = len(self._training_data)

        if n < MIN_SAMPLES:
            logger.debug(f"ML scorer: {n}/{MIN_SAMPLES} samples — not enough for training")
            return

        try:
            with self._lock:
                data = list(self._training_data)

            X = np.array([self._features_to_array(d) for d in data])
            y = np.array([d.get("outcome", 0) for d in data])

            # Check for class balance
            n_pos = sum(y)
            n_neg = len(y) - n_pos
            if n_pos < 10 or n_neg < 10:
                logger.debug(f"ML scorer: imbalanced ({n_pos} wins, {n_neg} losses) — skip")
                return

            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X)

            model = LogisticRegression(
                C=1.0, max_iter=500, solver="lbfgs",
                class_weight="balanced",
            )
            model.fit(X_scaled, y)

            # Simple cross-val score (last 20% as test)
            split = int(len(X) * 0.8)
            if split > 50:
                X_test = X_scaled[split:]
                y_test = y[split:]
                accuracy = model.score(X_test, y_test)
                logger.info(f"📊 ML scorer trained: {n} samples, test accuracy={accuracy:.2%}")
            else:
                logger.info(f"📊 ML scorer trained: {n} samples")

            with self._lock:
                self._model = model
                self._scaler = scaler
            self._last_train_time = time.time()
            self._save_model()

            # Wave 17: Notify first training via Telegram
            if not getattr(self, '_first_train_notified', False):
                self._first_train_notified = True
                try:
                    from telegram_capital import notify_ml_trained
                    _acc = accuracy if 'accuracy' in dir() else 0.0
                    notify_ml_trained(samples=n, accuracy=_acc)
                except Exception as _ml_notif_e:
                    logger.debug(f"ML notif: {_ml_notif_e}")

        except Exception as e:
            logger.error(f"❌ ML training error: {e}")

    def _load_data(self):
        """Load training data from JSON file."""
        if os.path.exists(DATA_PATH):
            try:
                with open(DATA_PATH, "r") as f:
                    self._training_data = json.load(f)
                logger.info(f"📊 ML scorer: {len(self._training_data)} samples loaded from {DATA_PATH}")
            except Exception as e:
                logger.debug(f"ML load data: {e}")

        # Also try to load pre-trained model
        if os.path.exists(MODEL_PATH):
            try:
                with open(MODEL_PATH, "rb") as f:
                    saved = pickle.load(f)
                    self._model = saved.get("model")
                    self._scaler = saved.get("scaler")
                if self._model:
                    logger.info(f"📊 ML scorer: pre-trained model loaded from {MODEL_PATH}")
            except Exception as e:
                logger.debug(f"ML load model: {e}")

    def _save_data(self):
        """Save training data to JSON file."""
        try:
            with self._lock:
                data = list(self._training_data)
            with open(DATA_PATH, "w") as f:
                json.dump(data, f)
        except Exception as e:
            logger.debug(f"ML save data: {e}")

    def _save_model(self):
        """Save trained model to pickle file."""
        try:
            with self._lock:
                saved = {"model": self._model, "scaler": self._scaler}
            with open(MODEL_PATH, "wb") as f:
                pickle.dump(saved, f)
        except Exception as e:
            logger.debug(f"ML save model: {e}")

    @property
    def stats(self) -> dict:
        return {
            "active": self._active and self._model is not None,
            "samples": len(self._training_data),
            "min_required": MIN_SAMPLES,
            "model_ready": self._model is not None,
        }
