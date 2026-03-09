"""
hmm_regime.py — Hidden Markov Model · Détecteur de Régime de Marché
Détecte le régime courant parmi 3 états :
  0 = RANGING    (marché en range / indécis)
  1 = TREND_UP   (tendance haussière)
  2 = TREND_DOWN (tendance baissière)

Utilisation dans strategy.py :
  - RANGING       → score réduit, threshold plus élevé
  - TREND_UP      → favorise BUY, pénalise SELL
  - TREND_DOWN    → favorise SELL, pénalise BUY
  
Entraîné en ligne sur les 200 dernières bougies (rolling).
Pas de base de données requise.
"""
import numpy as np
import warnings
from typing import Optional
from loguru import logger

try:
    from hmmlearn.hmm import GaussianHMM
    _HMM_OK = True
except ImportError:
    _HMM_OK = False
    logger.warning("⚠️  hmmlearn non installé — HMM désactivé (pip install hmmlearn)")

# Fallback si hmmlearn absent : scikit-learn GaussianMixture (moins précis)
try:
    from sklearn.mixture import GaussianMixture
    _GMM_OK = True
except ImportError:
    _GMM_OK = False

REGIME_NAMES = {0: "RANGING", 1: "TREND_UP", 2: "TREND_DOWN"}
N_STATES     = 3
TRAIN_WINDOW = 200   # Bougies pour entraîner
MIN_CANDLES  = 30    # Minimum requis


class MarketRegimeHMM:
    """
    Détecteur de régime de marché via HMM (ou GMM en fallback).
    
    Features utilisées :
    - Rendement log de la période
    - Volatilité (std sur 5 périodes)
    - Direction EMA (EMA14 > EMA50 ?)
    """

    def __init__(self):
        self._model  = None
        self._fitted = False
        self._last_regime = None
        self._last_symbol = None

    def _extract_features(self, df) -> Optional[np.ndarray]:
        """Extrait les features depuis le dataframe OHLCV."""
        try:
            close = df["close"].values.astype(float)
            if len(close) < MIN_CANDLES:
                return None

            # Feature 1 : rendement log
            log_ret = np.diff(np.log(close + 1e-8))

            # Feature 2 : volatilité roulante (std sur 5 périodes)
            volatility = np.array([
                log_ret[max(0, i-5):i].std() if i >= 5 else 0.0
                for i in range(1, len(log_ret) + 1)
            ])

            # Feature 3 : direction de tendance (EMA14 vs EMA50)
            def ema(arr, n):
                alpha = 2 / (n + 1)
                result = np.zeros(len(arr))
                result[0] = arr[0]
                for i in range(1, len(arr)):
                    result[i] = alpha * arr[i] + (1 - alpha) * result[i-1]
                return result

            c = close[1:]  # Aligner avec log_ret
            ema14 = ema(c, 14)
            ema50 = ema(c, 50)
            trend = (ema14 - ema50) / (ema50 + 1e-8)

            features = np.column_stack([log_ret, volatility, trend])
            return features

        except Exception as e:
            logger.debug(f"HMM feature extraction: {e}")
            return None

    def _assign_regime_labels(self, states: np.ndarray, log_ret: np.ndarray) -> dict:
        """
        Identifie quel état correspond à quel régime en regardant le
        rendement moyen de chaque état.
        """
        mapping = {}
        state_returns = {}
        for s in range(N_STATES):
            mask = states == s
            if mask.sum() > 0:
                state_returns[s] = log_ret[mask].mean()
            else:
                state_returns[s] = 0.0

        # Trier par rendement moyen
        sorted_states = sorted(state_returns.items(), key=lambda x: x[1])
        mapping[sorted_states[0][0]] = 2   # Plus bas rendement → TREND_DOWN
        mapping[sorted_states[1][0]] = 0   # Rendement médian → RANGING
        mapping[sorted_states[2][0]] = 1   # Plus haut rendement → TREND_UP
        return mapping

    def detect_regime(self, df, symbol: str = "") -> dict:
        """
        Détecte le régime actuel du marché.
        
        Returns:
            dict avec :
            - regime     : 0/1/2
            - name       : "RANGING" / "TREND_UP" / "TREND_DOWN"
            - confidence : float 0-1
            - score_adj  : ajustement score (+1/-1/0) selon le signal
            - description: texte lisible
        """
        default = {
            "regime": 0, "name": "RANGING",
            "confidence": 0.5, "score_adj": 0,
            "description": "Régime inconnu"
        }

        features = self._extract_features(df[-TRAIN_WINDOW:] if len(df) > TRAIN_WINDOW else df)
        if features is None or len(features) < MIN_CANDLES:
            return default

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")

                if _HMM_OK:
                    model = GaussianHMM(
                        n_components=N_STATES, covariance_type="diag",
                        n_iter=50, random_state=42
                    )
                    model.fit(features)
                    states    = model.predict(features)
                    log_prob  = model.score(features[-10:])
                    confidence = min(1.0, max(0.0, (log_prob + 100) / 100))
                elif _GMM_OK:
                    model = GaussianMixture(
                        n_components=N_STATES, covariance_type="diag",
                        random_state=42, max_iter=50
                    )
                    model.fit(features)
                    states     = model.predict(features)
                    confidence = 0.6
                else:
                    return default

            mapping = self._assign_regime_labels(states, features[:, 0])
            current_state   = states[-1]
            current_regime  = mapping.get(current_state, 0)
            self._last_regime = current_regime

            return {
                "regime":      current_regime,
                "name":        REGIME_NAMES[current_regime],
                "confidence":  round(confidence, 2),
                "score_adj":   0,
                "description": f"Régime {REGIME_NAMES[current_regime]} (confiance {confidence:.0%})"
            }

        except Exception as e:
            logger.debug(f"HMM detect error: {e}")
            return default

    def get_signal_adjustment(self, regime_result: dict, signal: str) -> int:
        """
        Retourne l'ajustement de score selon le régime et le signal.
        
        TREND_UP   + BUY  → +1 (dans le sens du trend)
        TREND_UP   + SELL → -1 (contre le trend)
        TREND_DOWN + SELL → +1
        TREND_DOWN + BUY  → -1
        RANGING    + *    → -1 (éviter de trader en range)
        """
        regime = regime_result.get("regime", 0)
        conf   = regime_result.get("confidence", 0.5)

        if conf < 0.4:   # Pas assez confiant → neutre
            return 0

        if regime == 1:   # TREND_UP
            return +1 if signal == "BUY" else -1
        elif regime == 2:  # TREND_DOWN
            return +1 if signal == "SELL" else -1
        else:              # RANGING
            return -1      # Éviter de trader en range confirmé

    @property
    def last_regime_name(self) -> str:
        return REGIME_NAMES.get(self._last_regime, "—") if self._last_regime is not None else "—"
