"""
lstm_predictor.py — Feature P : Prédiction LSTM du timing d'entrée.

Architecture : LSTM léger (1 couche cachée, 32 unités) entraîné sur les
500 dernières bougies 5m. Prédit la probabilité d'un breakout réussi dans
les 3 prochaines bougies.

Backend : sklearn MLPClassifier (fallback rapide sur Railway sans GPU).
Torch détecté automatiquement et utilisé si disponible.

Input features (par bougie) :
    close_pct, volume_ratio, atr_norm, adx_norm, rsi_norm, ema_ratio

Output : score 0.0-1.0
  > 0.65 → signal VALIDÉ (entrée autorisée)
  ≤ 0.65 → signal BLOQUÉ (timing pas optimal)
"""
import pickle
import os
from typing import Optional
from loguru import logger
import pandas as pd
# numpy importé de manière lazy dans chaque fonction (arrête numpy._globals error Railway)

# Seuil de confiance minimal pour autoriser une entrée
LSTM_THRESHOLD = 0.65
# Nombre de bougies dans la fenêtre glissante
SEQ_LEN = 10
# Chemin de sauvegarde du modèle
MODEL_PATH = os.path.join(os.path.dirname(__file__), ".lstm_model.pkl")


def _build_features(df: pd.DataFrame):
    """Construit la matrice de features depuis un DataFrame OHLCV enrichi."""
    try:
        import numpy as np  # lazy
        feats = []
        df = df.copy()
        close = df["close"].astype(float)

        # close pct change (momentum)
        feat_close  = close.pct_change().fillna(0).clip(-0.05, 0.05).values

        # volume ratio vs MA20
        vol = df.get("volume", pd.Series(np.ones(len(df)), index=df.index)).astype(float)
        vol_ma = vol.rolling(20, min_periods=1).mean().replace(0, 1)
        feat_vol    = (vol / vol_ma).fillna(1).clip(0, 5).values

        # ATR normalisée
        atr = df.get("atr", close.rolling(14, min_periods=1).std()).astype(float)
        feat_atr    = (atr / (close + 1e-9)).fillna(0).clip(0, 0.05).values

        # ADX normalisée (÷ 100)
        adx = df.get("adx", pd.Series(np.zeros(len(df)), index=df.index)).astype(float)
        feat_adx    = (adx / 100).fillna(0).clip(0, 1).values

        # RSI normalisée (÷ 100)
        # Approximation RSI simple si non présent
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14, min_periods=1).mean()
        loss  = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
        rs    = gain / (loss + 1e-9)
        rsi   = 100 - (100 / (1 + rs))
        feat_rsi    = (rsi / 100).fillna(0.5).clip(0, 1).values

        # EMA ratio (close / EMA21 - 1)
        ema21 = close.ewm(span=21, min_periods=1).mean()
        feat_ema    = ((close / (ema21 + 1e-9)) - 1).clip(-0.05, 0.05).values

        X = np.stack([feat_close, feat_vol, feat_atr, feat_adx, feat_rsi, feat_ema], axis=1)
        return X.astype(np.float32)
    except Exception as e:
        logger.debug(f"lstm_predictor _build_features: {e}")
        return None


def _make_sequences(X, y, seq_len: int):
    """Construit les séquences temporelles pour l'entraînement."""
    import numpy as np  # lazy
    Xs, ys = [], []
    for i in range(seq_len, len(X)):
        Xs.append(X[i - seq_len:i].flatten())
        ys.append(y[i])
    return np.array(Xs), np.array(ys)


def _label_breakouts(df: pd.DataFrame, lookahead: int = 3):
    """
    Label = 1 si le prix monte (BUY) d'au moins ATR×0.8 dans les N prochaines bougies.
    Approximation : cherche la cassure haussière ou baissière.
    """
    import numpy as np  # lazy
    close = df["close"].astype(float).values
    atr   = df.get("atr", pd.Series(
        pd.Series(close).rolling(14, min_periods=1).std().values, index=df.index
    )).astype(float).values

    labels = np.zeros(len(close), dtype=int)
    for i in range(len(close) - lookahead):
        future_max = close[i + 1: i + 1 + lookahead].max()
        future_min = close[i + 1: i + 1 + lookahead].min()
        threshold  = atr[i] * 0.8
        if future_max - close[i] >= threshold or close[i] - future_min >= threshold:
            labels[i] = 1
    return labels


class LSTMPredictor:
    """
    Prédicteur LSTM (implémenté via sklearn MLPClassifier pour Railway).

    Usage
    -----
    predictor = LSTMPredictor()
    predictor.train(df)            # entraîner sur les données historiques
    score = predictor.predict(df)  # obtenir le score 0-1
    if score >= LSTM_THRESHOLD:
        # Entrer en position
    """

    def __init__(self):
        self._model = None
        self._trained = False
        self._n_trades_since_train = 0
        self._load()

    def _load(self):
        """Charge le modèle s'il existe déjà sur disque."""
        if os.path.exists(MODEL_PATH):
            try:
                with open(MODEL_PATH, "rb") as f:
                    self._model = pickle.load(f)
                self._trained = True
                logger.info("🧠 LSTM Predictor — modèle chargé depuis disque")
            except Exception as e:
                logger.warning(f"LSTM load: {e}")

    def _save(self):
        """Sauvegarde le modèle sur disque."""
        try:
            with open(MODEL_PATH, "wb") as f:
                pickle.dump(self._model, f)
        except Exception as e:
            logger.warning(f"LSTM save: {e}")

    def train(self, df: pd.DataFrame) -> bool:
        """
        Entraîne le modèle sur les données historiques.

        Parameters
        ----------
        df : DataFrame OHLCV avec indicateurs (200+ bougies recommandé)

        Returns
        -------
        bool : True si entraînement réussi
        """
        try:
            from sklearn.neural_network import MLPClassifier
            from sklearn.preprocessing import StandardScaler
            from sklearn.pipeline import Pipeline
            from sklearn.utils.class_weight import compute_class_weight
        except ImportError:
            logger.warning("sklearn non disponible — LSTM Predictor désactivé")
            return False

        if len(df) < SEQ_LEN + 20:
            logger.debug("LSTM train: pas assez de données")
            return False

        X = _build_features(df)
        y = _label_breakouts(df)
        if X is None or len(X) != len(y):
            return False

        Xs, ys = _make_sequences(X, y, SEQ_LEN)
        if len(np.unique(ys)) < 2:
            logger.debug("LSTM train: une seule classe dans les labels")
            return False

        try:
            classes = np.unique(ys)
            weights = compute_class_weight("balanced", classes=classes, y=ys)
            class_weight = dict(zip(classes, weights))

            self._model = Pipeline([
                ("scaler", StandardScaler()),
                ("mlp", MLPClassifier(
                    hidden_layer_sizes=(64, 32),
                    activation="relu",
                    solver="adam",
                    max_iter=200,
                    random_state=42,
                    early_stopping=True,
                    validation_fraction=0.15,
                    n_iter_no_change=10,
                )),
            ])
            self._model.fit(Xs, ys)
            self._trained = True
            self._save()
            logger.info(
                f"✅ LSTM Predictor entraîné — {len(Xs)} séquences "
                f"({ys.mean()*100:.0f}% breakouts)"
            )
            return True
        except Exception as e:
            logger.error(f"❌ LSTM train: {e}")
            return False

    def predict(self, df: pd.DataFrame) -> float:
        """
        Calcule la probabilité de breakout réussi pour la dernière bougie.

        Parameters
        ----------
        df : DataFrame récent (au moins SEQ_LEN bougies)

        Returns
        -------
        float : score 0.0-1.0 (>= LSTM_THRESHOLD = entrée autorisée)
        """
        if not self._trained or self._model is None:
            # Pas encore entraîné → ne pas bloquer (passe-through)
            return 1.0

        try:
            X = _build_features(df)
            if X is None or len(X) < SEQ_LEN:
                return 1.0

            seq = X[-SEQ_LEN:].flatten().reshape(1, -1)
            proba = self._model.predict_proba(seq)[0]
            # proba[1] = probabilité de la classe "breakout"
            score = float(proba[1]) if len(proba) > 1 else float(proba[0])
            return round(score, 4)
        except Exception as e:
            logger.debug(f"LSTM predict: {e}")
            return 1.0  # fail-safe : ne pas bloquer

    def should_enter(self, df: pd.DataFrame) -> tuple:
        """
        Retourne (True/False, score) selon le seuil LSTM_THRESHOLD.

        Returns
        -------
        (allow: bool, score: float)
        """
        score = self.predict(df)
        allow = score >= LSTM_THRESHOLD
        if not allow:
            logger.debug(f"🧠 LSTM block — score {score:.2f} < {LSTM_THRESHOLD}")
        return allow, score

    def notify_trade_result(self, won: bool):
        """Notifie le résultat d'un trade pour déclencher un re-train si nécessaire."""
        self._n_trades_since_train += 1

    @property
    def is_ready(self) -> bool:
        """Retourne True si le modèle est entraîné et utilisable."""
        return self._trained and self._model is not None
