"""
quantum_tensor.py — Moteur 32 : Quantum Tensor Networks & Schrödinger Market States

Modélise le carnet d'ordres comme une fonction d'onde de marché Ψ(S,t).
Utilise un Hamiltonien financier (Black-Scholes PDE en forme quantique)
pour prévoir l'effondrement de la fonction d'onde — la direction du prix.

Mathématiques :
  ∂Ψ/∂t = -½σ²S² ∂²Ψ/∂S² - rS ∂Ψ/∂S + rΨ

  Ψ(S,t) = Amplitude de probabilité que le prix soit S à t
  |Ψ|²    = Densité de probabilité (distribution réelle)
  σ       = Volatilité implicite
  r       = Taux sans risque

Architecture :
  WaveFunction      → fonction d'onde discrétisée sur une grille de prix
  FinancialHamiltonian → opérateur H de l'équation de Schrödinger financière
  TensorMPS         → Matrix Product State pour compression tensorielle
  WaveCollapser     → mesure/effondrement → signal directionnel
  EntanglementTracker → entropie d'intrication entre instruments

Note : Implémentation pure NumPy (pas de qiskit/cirq) pour ARM64 Docker.
"""
import time
import threading
import math
from typing import Dict, Optional, List, Tuple
from datetime import datetime, timezone
from loguru import logger
import numpy as np

# ─── Configuration ────────────────────────────────────────────────────────────
_SCAN_INTERVAL_S   = 45       # Scan toutes les 45s
_GRID_POINTS       = 128      # Points de discrétisation spatiale
_TIME_STEPS        = 50       # Pas de temps pour évolution PDE
_BOND_DIM          = 16       # Dimension de liaison MPS (compression)
_RISK_FREE_RATE    = 0.05     # 5% annualisé
_COLLAPSE_THRESH   = 0.6      # Seuil de confiance pour signal

_QT_INSTRUMENTS = [
    "GOLD", "US500", "US100", "BTCUSD", "ETHUSD",
    "EURUSD", "GBPUSD", "USDJPY", "DE40", "OIL_CRUDE",
]


class WaveFunction:
    """
    Fonction d'onde financière Ψ(S,t) discrétisée.
    |Ψ(S)|² = probabilité que le prix soit S.
    """

    def __init__(self, n_grid: int = _GRID_POINTS):
        self.n = n_grid
        self.psi = np.zeros(n_grid, dtype=np.complex128)
        self.grid = np.zeros(n_grid)  # Prix discrets
        self.center = 0.0
        self.sigma_init = 0.0

    def initialize_gaussian(self, center: float, sigma: float):
        """Initialise Ψ comme un paquet d'onde gaussien centré sur le prix actuel."""
        self.center = center
        self.sigma_init = sigma

        # Grille de prix ± 4σ autour du centre
        s_min = max(center - 4 * sigma, center * 0.8)
        s_max = center + 4 * sigma
        self.grid = np.linspace(s_min, s_max, self.n)

        # Paquet gaussien
        x = self.grid - center
        self.psi = np.exp(-x**2 / (4 * sigma**2)).astype(np.complex128)

        # Normaliser
        self._normalize()

    def _normalize(self):
        """Normalise Ψ pour que ∫|Ψ|²dS = 1."""
        norm = np.sqrt(np.sum(np.abs(self.psi)**2))
        if norm > 1e-15:
            self.psi /= norm

    @property
    def probability(self) -> np.ndarray:
        """|Ψ|² — densité de probabilité."""
        return np.abs(self.psi)**2

    @property
    def expectation(self) -> float:
        """⟨S⟩ = ∫S|Ψ|²dS — prix attendu."""
        prob = self.probability
        total = np.sum(prob)
        if total < 1e-15:
            return self.center
        return float(np.sum(self.grid * prob) / total)

    @property
    def uncertainty(self) -> float:
        """ΔS = √(⟨S²⟩ - ⟨S⟩²) — incertitude quantique."""
        prob = self.probability
        total = np.sum(prob)
        if total < 1e-15:
            return 0.0
        mean = np.sum(self.grid * prob) / total
        var = np.sum((self.grid - mean)**2 * prob) / total
        return float(np.sqrt(max(var, 0)))

    @property
    def entropy(self) -> float:
        """Entropie de Von Neumann S = -Σ p·log(p)."""
        prob = self.probability
        prob = prob[prob > 1e-15]
        prob = prob / prob.sum()
        return float(-np.sum(prob * np.log2(prob)))


class FinancialHamiltonian:
    """
    Hamiltonien financier dérivé du Black-Scholes PDE.
    H = -½σ²S² ∂²/∂S² - rS ∂/∂S + r
    """

    def __init__(self, sigma: float, r: float = _RISK_FREE_RATE):
        self.sigma = sigma
        self.r = r

    def apply(self, psi: WaveFunction, dt: float) -> WaveFunction:
        """
        Évolue Ψ d'un pas dt via split-operator :
        Ψ(t+dt) = exp(-iHdt) Ψ(t)
        """
        n = psi.n
        S = psi.grid
        ds = S[1] - S[0] if n > 1 else 1.0

        # ─── Opérateurs différentiels (différences finies) ────────────
        # ∂²Ψ/∂S²
        d2psi = np.zeros_like(psi.psi)
        d2psi[1:-1] = (psi.psi[2:] - 2 * psi.psi[1:-1] + psi.psi[:-2]) / (ds**2 + 1e-15)

        # ∂Ψ/∂S
        dpsi = np.zeros_like(psi.psi)
        dpsi[1:-1] = (psi.psi[2:] - psi.psi[:-2]) / (2 * ds + 1e-15)

        # ─── Hamiltonien H·Ψ ─────────────────────────────────────────
        sigma2 = self.sigma**2
        S_safe = np.maximum(S, 1e-10)

        H_psi = (
            -0.5 * sigma2 * S_safe**2 * d2psi
            - self.r * S_safe * dpsi
            + self.r * psi.psi
        )

        # ─── Évolution temporelle exp(-iHdt) via Euler ────────────────
        # En physique quantique : Ψ(t+dt) = Ψ(t) - i·H·Ψ·dt
        # En finance, on utilise le propagateur réel (diffusion)
        new_psi = WaveFunction(n)
        new_psi.grid = psi.grid.copy()
        new_psi.center = psi.center
        new_psi.sigma_init = psi.sigma_init

        # Propagation (schéma implicite simplifié)
        new_psi.psi = psi.psi - dt * H_psi

        # Conditions aux bords absorbantes
        new_psi.psi[0] = 0
        new_psi.psi[-1] = 0

        new_psi._normalize()
        return new_psi


class TensorMPS:
    """
    Matrix Product State (MPS) pour compression tensorielle.
    Compresse Ψ à N points en une chaîne de tenseurs de rang bond_dim.
    Réduit la mémoire de O(2^N) à O(N·D²).
    """

    def __init__(self, bond_dim: int = _BOND_DIM):
        self.bond_dim = bond_dim
        self.tensors: List[np.ndarray] = []

    def compress(self, psi: np.ndarray) -> float:
        """
        Compresse un vecteur d'état via SVD itérative.
        Retourne l'erreur de troncature.
        """
        n = len(psi)
        if n < 4:
            self.tensors = [psi.reshape(1, -1, 1)]
            return 0.0

        self.tensors = []
        remaining = psi.copy().astype(np.complex128)
        total_error = 0.0

        # Décomposition SVD séquentielle
        current_dim = 1
        chunk_size = max(2, int(np.sqrt(n)))

        for i in range(0, n - chunk_size, chunk_size):
            block = remaining[i:i + chunk_size]
            if len(block) < 2:
                break

            # Reshape en matrice
            rows = min(current_dim, self.bond_dim)
            cols = len(block)
            mat = block.reshape(min(rows, cols), -1) if rows * cols >= len(block) else block.reshape(1, -1)

            try:
                U, S, Vh = np.linalg.svd(mat, full_matrices=False)

                # Tronquer au bond_dim
                k = min(self.bond_dim, len(S))
                truncation_error = np.sum(S[k:]**2) if k < len(S) else 0.0
                total_error += truncation_error

                U_trunc = U[:, :k]
                S_trunc = np.diag(S[:k])
                Vh_trunc = Vh[:k, :]

                self.tensors.append(U_trunc @ S_trunc)
                remaining[i:i + chunk_size] = (Vh_trunc[0] if Vh_trunc.shape[0] > 0
                                               else block)
                current_dim = k
            except Exception:
                self.tensors.append(block.reshape(1, -1))

        # Dernier tenseur
        if len(remaining) > 0:
            self.tensors.append(remaining.reshape(1, -1))

        return total_error

    @property
    def memory_ratio(self) -> float:
        """Ratio de compression mémoire."""
        total_params = sum(t.size for t in self.tensors)
        original = max(sum(t.shape[-1] for t in self.tensors), 1)
        return total_params / max(original, 1)

    @property
    def entanglement_entropy(self) -> float:
        """Entropie d'intrication (via spectre des valeurs singulières)."""
        if not self.tensors:
            return 0.0
        try:
            mid = len(self.tensors) // 2
            if mid >= len(self.tensors):
                return 0.0
            mat = self.tensors[mid]
            if mat.ndim < 2:
                return 0.0
            S = np.linalg.svd(mat, compute_uv=False)
            S = S[S > 1e-15]
            S2 = S**2
            S2 /= S2.sum()
            return float(-np.sum(S2 * np.log2(S2 + 1e-15)))
        except Exception:
            return 0.0


class QuantumTensorEngine:
    """
    Moteur 32 : Quantum Tensor Networks & Schrödinger Market States.

    Modélise le marché comme une fonction d'onde quantique Ψ(S,t)
    et prédit l'effondrement (direction) via l'évolution hamiltonienne.
    """

    def __init__(self, db=None, capital_client=None, telegram_router=None):
        self._db = db
        self._capital = capital_client
        self._tg = telegram_router
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # Wave functions par instrument
        self._waves: Dict[str, WaveFunction] = {}
        self._mps: Dict[str, TensorMPS] = {}
        self._predictions: Dict[str, dict] = {}  # inst → {dir, conf, expect, uncert}
        self._hamiltonians: Dict[str, FinancialHamiltonian] = {}

        # Stats
        self._scans = 0
        self._collapses = 0
        self._signals_fired = 0
        self._last_scan_ms = 0.0

        self._ensure_table()
        logger.info("🌌 M32 Quantum Tensor initialisé (Schrödinger Market States + MPS)")

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="quantum_tensor"
        )
        self._thread.start()
        logger.info("🌌 M32 Quantum Tensor démarré (scan toutes les 45s)")

    def stop(self):
        self._running = False

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_quantum_signal(self, instrument: str) -> Tuple[str, float, str]:
        """
        Retourne le signal quantique pour un instrument.
        Returns: (direction, confidence, state_info)
        """
        with self._lock:
            pred = self._predictions.get(instrument)
        if not pred:
            return "NONE", 0.0, "no_wave"
        return pred["direction"], pred["confidence"], pred["state"]

    def get_wave_state(self, instrument: str) -> dict:
        """Retourne l'état de la fonction d'onde."""
        with self._lock:
            wave = self._waves.get(instrument)
            mps = self._mps.get(instrument)

        if not wave:
            return {}

        return {
            "expectation": round(wave.expectation, 4),
            "uncertainty": round(wave.uncertainty, 4),
            "entropy": round(wave.entropy, 3),
            "entanglement": round(mps.entanglement_entropy, 3) if mps else 0,
            "mps_compression": round(mps.memory_ratio, 2) if mps else 0,
        }

    def stats(self) -> dict:
        with self._lock:
            wave_states = {}
            for inst, wave in self._waves.items():
                wave_states[inst] = {
                    "⟨S⟩": round(wave.expectation, 2),
                    "ΔS": round(wave.uncertainty, 4),
                    "H": round(wave.entropy, 2),
                }
            preds = {k: f"{v['direction']}({v['confidence']:.0%})"
                     for k, v in self._predictions.items()
                     if v["confidence"] > 0.3}

        return {
            "scans": self._scans,
            "collapses": self._collapses,
            "signals": self._signals_fired,
            "waves": wave_states,
            "predictions": preds,
            "last_scan_ms": round(self._last_scan_ms, 1),
        }

    def format_report(self) -> str:
        s = self.stats()
        wave_str = " | ".join(
            f"{k}:⟨{v['⟨S⟩']}⟩±{v['ΔS']}" for k, v in list(s["waves"].items())[:5]
        ) or "—"
        pred_str = " | ".join(
            f"{k}:{v}" for k, v in s["predictions"].items()
        ) or "—"
        return (
            f"🌌 <b>Quantum Tensor (M32)</b>\n\n"
            f"  Scans: {s['scans']} | Collapses: {s['collapses']}\n"
            f"  Ψ: {wave_str}\n"
            f"  Signaux: {pred_str}"
        )

    # ─── Scan Loop ───────────────────────────────────────────────────────────

    def _scan_loop(self):
        time.sleep(35)
        while self._running:
            t0 = time.time()
            try:
                self._scan_cycle()
            except Exception as e:
                logger.debug(f"M32 scan: {e}")
            self._last_scan_ms = (time.time() - t0) * 1000
            self._scans += 1
            time.sleep(_SCAN_INTERVAL_S)

    def _scan_cycle(self):
        """Cycle: init Ψ → evolve H → compress MPS → collapse → signal."""
        for instrument in _QT_INSTRUMENTS:
            try:
                self._process_instrument(instrument)
            except Exception:
                pass

    def _process_instrument(self, instrument: str):
        """Traite un instrument : évolution quantique complète."""
        if not self._capital:
            return

        # 1. Obtenir le prix et la volatilité
        px = self._capital.get_current_price(instrument)
        if not px or px.get("mid", 0) <= 0:
            return

        price = px["mid"]
        bid = px.get("bid", price)
        ask = px.get("ask", price)
        spread = (ask - bid) / max(price, 1e-8)

        # Volatilité implicite approximée (spread-based + historical proxy)
        sigma = max(spread * 50, 0.01)  # Proxy : spread × scaling

        # 2. Initialiser la fonction d'onde Ψ(S,0)
        psi = WaveFunction(_GRID_POINTS)
        psi.initialize_gaussian(center=price, sigma=price * sigma)

        # 3. Créer le Hamiltonien
        H = FinancialHamiltonian(sigma=sigma, r=_RISK_FREE_RATE)

        # 4. Évoluer Ψ dans le temps (propagation de Schrödinger)
        dt = 1.0 / _TIME_STEPS
        for _ in range(_TIME_STEPS):
            psi = H.apply(psi, dt)

        # 5. Compression MPS
        mps = TensorMPS(_BOND_DIM)
        trunc_error = mps.compress(psi.psi)

        # 6. Mesure / Effondrement
        expected = psi.expectation
        uncertainty = psi.uncertainty
        direction_delta = (expected - price) / max(price, 1e-8)

        # Confiance = inverse de l'incertitude relative
        rel_uncertainty = uncertainty / max(price, 1e-8)
        confidence = max(0, min(1, 1 - rel_uncertainty * 10))

        # Direction
        if direction_delta > 0.0001 and confidence > _COLLAPSE_THRESH:
            direction = "QUANTUM_LONG"
            self._collapses += 1
        elif direction_delta < -0.0001 and confidence > _COLLAPSE_THRESH:
            direction = "QUANTUM_SHORT"
            self._collapses += 1
        else:
            direction = "SUPERPOSITION"

        state_info = f"Ψ→{expected:.2f}±{uncertainty:.4f}"

        prediction = {
            "direction": direction,
            "confidence": confidence,
            "state": state_info,
            "expectation": expected,
            "uncertainty": uncertainty,
            "entropy": psi.entropy,
            "entanglement": mps.entanglement_entropy,
            "delta": direction_delta,
        }

        with self._lock:
            self._waves[instrument] = psi
            self._mps[instrument] = mps
            self._hamiltonians[instrument] = H
            self._predictions[instrument] = prediction

        if direction != "SUPERPOSITION" and confidence > _COLLAPSE_THRESH:
            self._signals_fired += 1
            logger.info(
                f"🌌 M32 COLLAPSE: {instrument} {direction} "
                f"⟨S⟩={expected:.2f} ΔS={uncertainty:.4f} "
                f"conf={confidence:.0%} entropy={psi.entropy:.2f}"
            )
            self._persist_collapse(instrument, prediction)

    # ─── Database ────────────────────────────────────────────────────────────

    def _ensure_table(self):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS quantum_collapses (
                    id            SERIAL PRIMARY KEY,
                    instrument    VARCHAR(20),
                    direction     VARCHAR(20),
                    confidence    FLOAT,
                    expectation   FLOAT,
                    uncertainty   FLOAT,
                    entropy       FLOAT,
                    entanglement  FLOAT,
                    detected_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"M32 table: {e}")

    def _persist_collapse(self, instrument: str, pred: dict):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            ph = "%s"
            self._db._execute(
                f"INSERT INTO quantum_collapses "
                f"(instrument,direction,confidence,expectation,uncertainty,"
                f"entropy,entanglement) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (instrument, pred["direction"], pred["confidence"],
                 pred["expectation"], pred["uncertainty"],
                 pred["entropy"], pred["entanglement"])
            )
        except Exception:
            pass
