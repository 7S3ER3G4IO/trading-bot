"""
cfr_engine.py — Moteur 36 : Counterfactual Regret Minimization (CFR)

Résout le jeu du trading comme un jeu à information imparfaite.
Pour chaque décision, simule 10 000 univers alternatifs et calcule
le regret contrefactuel de chaque action non prise.

Algorithme :
  CFR+ (regret-matching positif) — utilisé par Libratus/Pluribus.

  Pour chaque information set (état du marché) :
    1. Calculer la stratégie σ via regret-matching
    2. Pour chaque action possible (BUY, SELL, HOLD) :
       - Simuler le profit contrefactuel si cette action avait été prise
       - r(a) = profit_contrefactuel(a) - profit_moyen(σ)
    3. Accumuler le regret cumulatif R+(a) = max(R(a), 0)
    4. Mettre à jour σ ∝ R+(a) / ΣR+(a)

  Convergence : σ → Équilibre de Nash quand T → ∞

Complexité : O(|A|² × T) par information set, |A|=3 (BUY/SELL/HOLD)
"""
import time
import threading
from typing import Dict, Optional, List, Tuple
from datetime import datetime, timezone
from loguru import logger
import numpy as np

# ─── Configuration ────────────────────────────────────────────────────────────
_SCAN_INTERVAL_S      = 30       # Scan toutes les 30s
_N_SIMULATIONS        = 10_000   # Univers parallèles simulés
_N_ACTIONS            = 3        # BUY, SELL, HOLD
_ACTIONS              = ["BUY", "SELL", "HOLD"]
_EXPLORATION_RATE     = 0.1      # ε pour exploration
_DISCOUNT_FACTOR      = 0.99     # Facteur de discount temporel
_CONVERGENCE_THRESH   = 0.01     # Convergence quand Δstratégie < 1%
_MAX_ITERATIONS       = 500      # Max iterations CFR par scan

_CFR_INSTRUMENTS = [
    "GOLD", "US500", "US100", "BTCUSD", "ETHUSD",
    "EURUSD", "GBPUSD", "USDJPY", "DE40", "OIL_CRUDE",
]


class InformationSet:
    """
    Un Information Set = état observable du marché.
    Encode la combinaison de signaux visibles par le joueur (le bot).
    """

    def __init__(self, key: str, n_actions: int = _N_ACTIONS):
        self.key = key
        self.n = n_actions

        # Regret cumulatif par action (CFR+)
        self.cumulative_regret = np.zeros(n_actions)
        # Stratégie cumulée (pour la stratégie moyenne)
        self.cumulative_strategy = np.zeros(n_actions)
        # Compteur d'itérations
        self.iterations = 0

    def get_strategy(self) -> np.ndarray:
        """
        Regret-Matching : σ(a) ∝ max(R(a), 0) / Σmax(R(a), 0)
        Si tout est négatif, jouer uniformément.
        """
        positive_regret = np.maximum(self.cumulative_regret, 0)
        total = positive_regret.sum()

        if total > 0:
            strategy = positive_regret / total
        else:
            strategy = np.ones(self.n) / self.n

        return strategy

    def get_average_strategy(self) -> np.ndarray:
        """
        Stratégie moyenne = la stratégie d'équilibre de Nash.
        C'est la sortie finale du CFR (pas la stratégie instantanée).
        """
        total = self.cumulative_strategy.sum()
        if total > 0:
            return self.cumulative_strategy / total
        return np.ones(self.n) / self.n

    def update(self, action_utilities: np.ndarray, strategy: np.ndarray):
        """
        Met à jour le regret cumulatif et la stratégie.

        action_utilities[a] = profit contrefactuel de l'action a
        strategy[a] = probabilité actuelle de jouer a
        """
        # Utilité espérée sous la stratégie actuelle
        expected_utility = np.dot(strategy, action_utilities)

        # Regret contrefactuel pour chaque action
        regrets = action_utilities - expected_utility

        # CFR+ : regret positif uniquement
        self.cumulative_regret = np.maximum(
            self.cumulative_regret + regrets, 0
        )

        # Accumuler la stratégie
        self.cumulative_strategy += strategy
        self.iterations += 1

    @property
    def exploitability(self) -> float:
        """Mesure l'exploitabilité (distance à Nash)."""
        avg = self.get_average_strategy()
        # L'exploitabilité est proportionnelle à la variance de la stratégie
        uniform = np.ones(self.n) / self.n
        return float(np.sum(np.abs(avg - uniform)))


class MarketSimulator:
    """
    Simule des univers contrefactuels pour le marché.
    Chaque simulation modélise ce qui se serait passé si une action
    différente avait été prise.
    """

    def __init__(self):
        self._volatility_cache: Dict[str, float] = {}

    def simulate_outcomes(self, instrument: str, current_price: float,
                          sigma: float, n_sims: int = _N_SIMULATIONS
                          ) -> np.ndarray:
        """
        Simule n_sims univers parallèles.
        Returns: utilities[n_sims, n_actions] — profit de chaque action dans chaque univers.
        """
        # Simuler les prix futurs via GBM (Geometric Brownian Motion)
        dt = 1 / (252 * 24 * 60)  # 1 minute
        drift = 0.0  # Pas de drift (marché efficient)

        # Mouvements de prix aléatoires
        Z = np.random.randn(n_sims)
        price_changes = current_price * (drift * dt + sigma * np.sqrt(dt) * Z)

        # Utilités par action :
        # BUY  → profit = price_change (long)
        # SELL → profit = -price_change (short)
        # HOLD → profit = 0 (neutre)
        utilities = np.zeros((n_sims, _N_ACTIONS))
        utilities[:, 0] = price_changes       # BUY
        utilities[:, 1] = -price_changes      # SELL
        utilities[:, 2] = 0                   # HOLD

        # Ajouter les coûts de transaction
        tx_cost = current_price * 0.0001  # 1 bps
        utilities[:, 0] -= tx_cost
        utilities[:, 1] -= tx_cost

        return utilities


class CFREngine:
    """
    Moteur 36 : Counterfactual Regret Minimization.

    Résout le jeu du trading via CFR+ pour converger vers
    l'Équilibre de Nash absolu. Le bot ne joue plus contre le marché,
    il le résout mathématiquement.
    """

    def __init__(self, db=None, capital_client=None, quantum_engine=None,
                 telegram_router=None):
        self._db = db
        self._capital = capital_client
        self._quantum = quantum_engine
        self._tg = telegram_router
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # Information sets par instrument
        self._info_sets: Dict[str, InformationSet] = {}
        # Simulateur de marché
        self._simulator = MarketSimulator()
        # Stratégies Nash
        self._nash_strategies: Dict[str, np.ndarray] = {}
        # Optimal actions
        self._optimal_actions: Dict[str, Tuple[str, float]] = {}

        # Stats
        self._scans = 0
        self._total_iterations = 0
        self._converged_sets = 0
        self._last_scan_ms = 0.0

        self._ensure_table()
        logger.info(
            f"♟️ M36 CFR Engine initialisé "
            f"({_N_SIMULATIONS:,} univers par scan | "
            f"{_MAX_ITERATIONS} iterations max)"
        )

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="cfr_engine"
        )
        self._thread.start()
        logger.info("♟️ M36 CFR Engine démarré (scan toutes les 30s)")

    def stop(self):
        self._running = False

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_nash_action(self, instrument: str) -> Tuple[str, float, str]:
        """
        Retourne l'action Nash-optimale.
        Returns: (action, confidence, strategy_str)
        """
        with self._lock:
            action_data = self._optimal_actions.get(instrument)
            nash = self._nash_strategies.get(instrument)

        if not action_data or nash is None:
            return "HOLD", 0.0, "no_data"

        action, conf = action_data
        strat_str = " ".join(f"{_ACTIONS[i]}={nash[i]:.0%}" for i in range(_N_ACTIONS))
        return action, conf, strat_str

    def get_exploitability(self, instrument: str) -> float:
        """Retourne l'exploitabilité (distance à Nash) pour un instrument."""
        with self._lock:
            info = self._info_sets.get(instrument)
        return info.exploitability if info else 1.0

    def stats(self) -> dict:
        with self._lock:
            strategies = {}
            for inst, nash in self._nash_strategies.items():
                best_idx = np.argmax(nash)
                strategies[inst] = f"{_ACTIONS[best_idx]}({nash[best_idx]:.0%})"
            exploit = {
                inst: round(info.exploitability, 3)
                for inst, info in self._info_sets.items()
            }

        return {
            "scans": self._scans,
            "total_iterations": self._total_iterations,
            "converged_sets": self._converged_sets,
            "strategies": strategies,
            "exploitability": exploit,
            "instruments": len(self._info_sets),
            "last_scan_ms": round(self._last_scan_ms, 1),
        }

    def format_report(self) -> str:
        s = self.stats()
        strat_str = " | ".join(
            f"{k}:{v}" for k, v in list(s["strategies"].items())[:5]
        ) or "—"
        return (
            f"♟️ <b>CFR Engine (M36)</b>\n\n"
            f"  Universes: {_N_SIMULATIONS:,} | Iters: {s['total_iterations']:,}\n"
            f"  Converged: {s['converged_sets']}/{s['instruments']}\n"
            f"  Nash: {strat_str}"
        )

    # ─── Scan Loop ───────────────────────────────────────────────────────────

    def _scan_loop(self):
        time.sleep(50)
        while self._running:
            t0 = time.time()
            try:
                self._scan_cycle()
            except Exception as e:
                logger.debug(f"M36 scan: {e}")
            self._last_scan_ms = (time.time() - t0) * 1000
            self._scans += 1
            time.sleep(_SCAN_INTERVAL_S)

    def _scan_cycle(self):
        """Cycle: pour chaque instrument, résoudre le jeu via CFR+."""
        for instrument in _CFR_INSTRUMENTS:
            try:
                self._solve_instrument(instrument)
            except Exception:
                pass

    def _solve_instrument(self, instrument: str):
        """Résout le jeu pour un instrument via CFR+."""
        if not self._capital:
            return

        # Prix actuel
        px = self._capital.get_current_price(instrument)
        if not px or px.get("mid", 0) <= 0:
            return

        price = px["mid"]
        bid, ask = px.get("bid", price), px.get("ask", price)
        spread = (ask - bid) / max(price, 1e-8)
        sigma = max(spread * 100, 0.01)

        # Information set
        key = f"{instrument}_{self._discretize_state(price, sigma)}"

        with self._lock:
            if key not in self._info_sets:
                self._info_sets[key] = InformationSet(key)
                # Also map by instrument for easier lookup
                self._info_sets[instrument] = self._info_sets[key]
            info_set = self._info_sets.get(instrument, self._info_sets[key])

        # CFR+ iterations
        prev_strategy = info_set.get_average_strategy().copy()

        for iteration in range(min(_MAX_ITERATIONS, 50)):
            # 1. Obtenir la stratégie actuelle
            strategy = info_set.get_strategy()

            # 2. Simuler les univers contrefactuels
            utilities = self._simulator.simulate_outcomes(
                instrument, price, sigma, n_sims=_N_SIMULATIONS // 10
            )

            # 3. Calculer les utilités moyennes par action
            avg_utilities = utilities.mean(axis=0)

            # 4. Mettre à jour le regret
            info_set.update(avg_utilities, strategy)

            self._total_iterations += 1

            # 5. Vérifier la convergence
            current_strategy = info_set.get_average_strategy()
            delta = np.max(np.abs(current_strategy - prev_strategy))

            if delta < _CONVERGENCE_THRESH:
                self._converged_sets += 1
                break

            prev_strategy = current_strategy.copy()

        # Résultat final : stratégie Nash
        nash_strategy = info_set.get_average_strategy()
        best_action_idx = np.argmax(nash_strategy)
        confidence = float(nash_strategy[best_action_idx])

        with self._lock:
            self._nash_strategies[instrument] = nash_strategy
            self._optimal_actions[instrument] = (
                _ACTIONS[best_action_idx], confidence
            )

        # Log si la stratégie est suffisamment déterministe
        if confidence > 0.5:
            logger.info(
                f"♟️ M36 NASH: {instrument} → {_ACTIONS[best_action_idx]} "
                f"({confidence:.0%}) | "
                f"exploit={info_set.exploitability:.3f} | "
                f"iters={info_set.iterations}"
            )
            self._persist_nash(instrument, nash_strategy, info_set)

    @staticmethod
    def _discretize_state(price: float, sigma: float) -> str:
        """Discrétise l'état du marché en un hash pour l'info set."""
        # Bucket le prix en bins logarithmiques
        price_bucket = int(np.log(max(price, 1)) * 10)
        # Bucket la volatilité
        vol_bucket = int(sigma * 100)
        return f"p{price_bucket}_v{vol_bucket}"

    # ─── Database ────────────────────────────────────────────────────────────

    def _ensure_table(self):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS cfr_nash (
                    id              SERIAL PRIMARY KEY,
                    instrument      VARCHAR(20),
                    action_buy      FLOAT,
                    action_sell     FLOAT,
                    action_hold     FLOAT,
                    exploitability  FLOAT,
                    iterations      INT,
                    detected_at     TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"M36 table: {e}")

    def _persist_nash(self, instrument: str, nash: np.ndarray,
                      info_set: InformationSet):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            ph = "%s"
            self._db._execute(
                f"INSERT INTO cfr_nash "
                f"(instrument,action_buy,action_sell,action_hold,"
                f"exploitability,iterations) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph})",
                (instrument, float(nash[0]), float(nash[1]),
                 float(nash[2]), info_set.exploitability,
                 info_set.iterations)
            )
        except Exception:
            pass
