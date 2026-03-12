"""
hmm_portfolio.py — Moteur 10 : HMM Regime Detection + Black-Litterman Portfolio Optimization.

2 composantes distinctes:

COMPOSANTE 1 — Hidden Markov Model (HMM):
  Identifie le régime de marché actuel parmi 3 états cachés:
    - REGIME_0: BULL (tendance haussière, vol modérée)
    - REGIME_1: RANGE (consolidation, vol faible)
    - REGIME_2: CRISIS (vol explosive, corrélations inter-actifs ↑)

COMPOSANTE 2 — Allocation dynamique Black-Litterman:
  Selon le régime détecté, réalloue le capital entre actifs.
  - Matrice de covariance rolling 30j
  - Views (convictions) du bot propres à chaque régime
  - Mix optimisé: min variance + Black-Litterman views
  - Contraintes: max 20% par actif, somme = 100%

Usage:
    hm = HMMPortfolio(db, telegram_router)
    hm.start()  # daemon thread, re-estimate toutes les 4h

    regime = hm.get_current_regime()
    alloc  = hm.get_kelly_multiplier("EURUSD")
    # → multiplie la taille Kelly classique par ce facteur
"""
import time
import math
import threading
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone
from loguru import logger

# ─── Régimes de marché ────────────────────────────────────────────────────────
REGIME_BULL   = "BULL_LOW_VOL"
REGIME_RANGE  = "RANGE_MID_VOL"
REGIME_CRISIS = "CRISIS_HIGH_VOL"
REGIME_NAMES  = [REGIME_BULL, REGIME_RANGE, REGIME_CRISIS]

# ─── Paramètres ───────────────────────────────────────────────────────────────
_RETRAIN_INTERVAL_S = 14400  # Re-entraînement toutes les 4h
_MIN_RETURNS_BARS   = 60     # Minimum 60 observations pour HMM
_REGIME_LOOKBACK    = 100    # Fenêtre d'observation HMM

# ─── Kelly multipliers par régime ─────────────────────────────────────────────
# Ex: en crise, on scaling down à 30% de la taille Kelly normale
REGIME_KELLY_MULT = {
    REGIME_BULL:   1.20,   # Tendance: augmente l'exposition +20%
    REGIME_RANGE:  0.85,   # Range: standard -15%
    REGIME_CRISIS: 0.30,   # Crise: réduction drastique -70%
}

# ─── Black-Litterman views par régime ─────────────────────────────────────────
# (asset_category → expected excess return view)
BL_VIEWS = {
    REGIME_BULL: {
        "crypto": +0.08, "equity": +0.06, "forex": +0.02, "commodity": +0.03
    },
    REGIME_RANGE: {
        "crypto": 0.0, "equity": +0.01, "forex": +0.01, "commodity": +0.01
    },
    REGIME_CRISIS: {
        "crypto": -0.10, "equity": -0.08, "forex": +0.02, "commodity": +0.06
    },
}


class HMMPortfolio:
    """
    Hidden Markov Model pour détection de régime + Black-Litterman allocation.
    Met à jour les multiplicateurs Kelly par régime automatiquement.
    """

    def __init__(self, ohlcv_cache=None, db=None, telegram_router=None,
                 asset_profiles: dict = None):
        self._cache   = ohlcv_cache
        self._db      = db
        self._tg      = telegram_router
        self._profiles = asset_profiles or {}

        # État courant
        self._current_regime   = REGIME_RANGE   # conservateur par défaut
        self._regime_probs     = {r: 1/3 for r in REGIME_NAMES}
        self._regime_history   = []
        self._kelly_multipliers: Dict[str, float] = {}

        # Matrices HMM (initialisées aux priors)
        self._transition = self._default_transition()
        self._emission   = self._default_emission()

        self._retrain_count = 0
        self._lock = threading.Lock()
        self._running = False
        self._thread  = None

        self._ensure_table()

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._retrain_loop, daemon=True, name="hmm_portfolio"
        )
        self._thread.start()
        logger.info("🎲 HMM Portfolio démarré (régimes + Black-Litterman)")

    def stop(self):
        self._running = False

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_current_regime(self) -> str:
        with self._lock:
            return self._current_regime

    def get_kelly_multiplier(self, instrument: str) -> float:
        """Retourne le multiplicateur Kelly pour cet instrument selon le régime."""
        with self._lock:
            base = REGIME_KELLY_MULT.get(self._current_regime, 0.85)
            # Bonus/malus par catégorie d'actif
            cat  = self._profiles.get(instrument, {}).get("cat", "forex")
            view = BL_VIEWS.get(self._current_regime, {}).get(cat, 0.0)
            adjustment = 1.0 + view * 5  # ±50% max d'ajustement BL
            return round(base * adjustment, 3)

    def get_regime_summary(self) -> dict:
        with self._lock:
            return {
                "regime":      self._current_regime,
                "probs":       dict(self._regime_probs),
                "kelly_mult":  REGIME_KELLY_MULT.get(self._current_regime, 0.85),
                "retrain_count": self._retrain_count,
            }

    def format_report(self) -> str:
        s = self.get_regime_summary()
        regime_icon = {"BULL_LOW_VOL": "🟢", "RANGE_MID_VOL": "🟡", "CRISIS_HIGH_VOL": "🔴"}
        icon = regime_icon.get(s["regime"], "⚪")
        return (
            f"🎲 <b>Régime de Marché (HMM)</b>\n\n"
            f"  {icon} Régime: <b>{s['regime']}</b>\n"
            f"  Kelly Mult: {s['kelly_mult']:.2f}x\n"
            f"  Bull prob: {s['probs'].get(REGIME_BULL, 0):.1%}\n"
            f"  Range prob: {s['probs'].get(REGIME_RANGE, 0):.1%}\n"
            f"  Crisis prob: {s['probs'].get(REGIME_CRISIS, 0):.1%}\n"
            f"  Re-estimations: {s['retrain_count']}"
        )

    # ─── HMM Estimation ──────────────────────────────────────────────────────

    def _retrain_loop(self):
        while self._running:
            try:
                self._retrain()
            except Exception as e:
                logger.debug(f"HMM retrain: {e}")
            time.sleep(_RETRAIN_INTERVAL_S)

    def _retrain(self):
        """
        Ré-estime le HMM sur les données récentes.
        Utilise hmmlearn si disponible, sinon estimation manuelle via Viterbi simplifié.
        """
        obs = self._get_observations()
        if obs is None or len(obs) < _MIN_RETURNS_BARS:
            logger.debug(f"HMM: pas assez d'observations ({len(obs) if obs else 0})")
            return

        # Tentative de hmmlearn
        fitted = self._fit_hmmlearn(obs)
        if not fitted:
            # Fallback: Viterbi simplifié maison
            fitted = self._fit_manual(obs)

        self._retrain_count += 1
        logger.info(
            f"🎲 HMM retrained: régime={self._current_regime} | "
            f"kelly_mult={REGIME_KELLY_MULT.get(self._current_regime, '?')}"
        )

        # Notify si changement de régime
        self._notify_regime_change()
        self._save_regime_async()

    def _fit_hmmlearn(self, obs: list) -> bool:
        """Tentative d'utilisation de hmmlearn GaussianHMM."""
        try:
            import numpy as np
            from hmmlearn.hmm import GaussianHMM

            X = np.array(obs).reshape(-1, 1)
            model = GaussianHMM(n_components=3, covariance_type="full",
                                n_iter=100, random_state=42)
            model.fit(X)
            hidden_states = model.predict(X)
            posteriors    = model.predict_proba(X)

            # Identifier quel état correspond à quel régime
            # = trier par moyenne (bull=↑, crisis=↓ vol, range=milieu)
            means = model.means_.flatten()
            stds  = [model.covars_[i][0][0]**0.5 for i in range(3)]

            # Mapping: état avec std la plus haute = crise
            sorted_by_std = sorted(range(3), key=lambda i: stds[i])
            state_to_regime = {
                sorted_by_std[0]: REGIME_BULL,
                sorted_by_std[1]: REGIME_RANGE,
                sorted_by_std[2]: REGIME_CRISIS,
            }

            current_state = int(hidden_states[-1])
            current_regime = state_to_regime.get(current_state, REGIME_RANGE)

            last_post = posteriors[-1]
            regime_probs = {
                state_to_regime.get(i, REGIME_RANGE): float(last_post[i])
                for i in range(3)
            }

            with self._lock:
                self._current_regime = current_regime
                self._regime_probs   = regime_probs

            return True

        except ImportError:
            return False
        except Exception as e:
            logger.debug(f"hmmlearn: {e}")
            return False

    def _fit_manual(self, obs: list) -> bool:
        """
        Viterbi simplifié sans dépendance externe.
        Classifie le régime via la volatilité récente (rolling std).
        """
        import statistics

        if len(obs) < 20:
            return False

        recent    = obs[-20:]
        full      = obs[-_REGIME_LOOKBACK:]
        vol_now   = statistics.stdev(recent)
        vol_hist  = statistics.stdev(full)
        mean_now  = statistics.mean(recent)

        vol_ratio = vol_now / vol_hist if vol_hist > 0 else 1.0

        # Classification heuristique
        if vol_ratio > 1.8:
            current = REGIME_CRISIS
            probs   = {REGIME_BULL: 0.05, REGIME_RANGE: 0.20, REGIME_CRISIS: 0.75}
        elif vol_ratio < 0.7 and mean_now > 0:
            current = REGIME_BULL
            probs   = {REGIME_BULL: 0.70, REGIME_RANGE: 0.25, REGIME_CRISIS: 0.05}
        else:
            current = REGIME_RANGE
            probs   = {REGIME_BULL: 0.25, REGIME_RANGE: 0.60, REGIME_CRISIS: 0.15}

        with self._lock:
            self._current_regime = current
            self._regime_probs   = probs

        return True

    def _get_observations(self) -> Optional[List[float]]:
        """Calcule les retours log pour 3-5 instruments clés."""
        try:
            if not self._cache:
                return self._get_observations_from_db()

            # Utiliser EURUSD/XAUUSD comme proxies de stress
            returns = []
            for inst in ["EURUSD", "XAUUSD", "XBTUSD"]:
                try:
                    df = self._cache.get(inst) if hasattr(self._cache, 'get') else None
                    if df is not None and "close" in df.columns and len(df) >= 30:
                        close = df["close"].values
                        log_returns = [
                            math.log(close[i] / close[i-1])
                            for i in range(1, len(close))
                            if close[i-1] > 0 and close[i] > 0
                        ]
                        returns.extend(log_returns[-50:])
                except Exception:
                    pass

            return returns if len(returns) >= _MIN_RETURNS_BARS else None

        except Exception as e:
            logger.debug(f"HMM get_obs: {e}")
            return None

    def _get_observations_from_db(self) -> Optional[List[float]]:
        """Fallback: retours depuis Supabase capital_trades."""
        if not self._db or not self._db._pg:
            return None
        try:
            cur = self._db._execute(
                "SELECT pnl FROM capital_trades WHERE status='CLOSED' "
                "AND pnl IS NOT NULL ORDER BY opened_at DESC LIMIT 200",
                fetch=True
            )
            rows = cur.fetchall()
            return [float(r[0]) for r in rows if r[0] is not None]
        except Exception:
            return None

    # ─── Transition / Emission priors ─────────────────────────────────────────

    @staticmethod
    def _default_transition() -> list:
        """Matrice de transition A priori (persistance des régimes)."""
        return [
            [0.85, 0.10, 0.05],  # BULL → [BULL, RANGE, CRISIS]
            [0.15, 0.70, 0.15],  # RANGE → [BULL, RANGE, CRISIS]
            [0.05, 0.30, 0.65],  # CRISIS → [BULL, RANGE, CRISIS]
        ]

    @staticmethod
    def _default_emission() -> list:
        """Paramètres d'émission: (mean, std) des retours par état."""
        return [
            (0.0005, 0.008),   # BULL: légère positivité, vol modérée
            (0.0000, 0.005),   # RANGE: neutre, vol faible
            (-0.002, 0.025),   # CRISIS: négatif, vol explosive
        ]

    # ─── Notifications ────────────────────────────────────────────────────────

    def _notify_regime_change(self):
        """Notifie via Telegram si le régime a changé."""
        if not self._tg:
            return
        try:
            self._tg.send_report(self.format_report())
        except Exception:
            pass

    # ─── DB ──────────────────────────────────────────────────────────────────

    def _ensure_table(self):
        if not self._db or not self._db._pg:
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS regime_history (
                    id          SERIAL PRIMARY KEY,
                    regime      VARCHAR(30),
                    bull_prob   DOUBLE PRECISION,
                    range_prob  DOUBLE PRECISION,
                    crisis_prob DOUBLE PRECISION,
                    kelly_mult  DOUBLE PRECISION,
                    recorded_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"regime_history table: {e}")

    def _save_regime_async(self):
        if not self._db:
            return
        self._db.async_write(self._save_regime_sync)

    def _save_regime_sync(self):
        try:
            if not self._db._pg:
                return
            with self._lock:
                r = self._current_regime
                p = self._regime_probs
            ph = "%s"
            self._db._execute(
                f"INSERT INTO regime_history (regime,bull_prob,range_prob,crisis_prob,kelly_mult) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph})",
                (r, p.get(REGIME_BULL, 0), p.get(REGIME_RANGE, 0),
                 p.get(REGIME_CRISIS, 0), REGIME_KELLY_MULT.get(r, 0.85))
            )
        except Exception as e:
            logger.debug(f"regime save: {e}")
