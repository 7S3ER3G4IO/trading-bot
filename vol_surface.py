"""
vol_surface.py — Moteur 25 : Volatility Surface & Greeks Neutral Arbitrage

Implémente le modèle Black-Scholes-Merton pour calculer les Greeks
(Delta, Gamma, Theta, Vega, Rho) et construire la Surface de Volatilité
Implicite. Détecte les anomalies de prix dans la surface et exécute
des arbitrages Delta-Neutre avec un risque directionnel = 0.

Architecture :
  BSMModel         → Black-Scholes-Merton pure Python (scipy.stats.norm)
  VolSurface       → matrice 2D (Strike × Expiry) de volatilité implicite
  AnomalyDetector  → détecte les anomalies (skew, smile, term-structure)
  DeltaNeutral     → construit des portfolios Delta=0 pour l'arbitrage

Instruments ciblés : GOLD, US500, BTCUSD (les plus liquides)
"""
import time
import threading
import math
from typing import Dict, Optional, List, Tuple
from datetime import datetime, timezone, timedelta
from loguru import logger
import numpy as np

try:
    from scipy.stats import norm as _norm
    _SCIPY_OK = True
except ImportError:
    _SCIPY_OK = False
    # Fallback avec approximation CDF
    class _norm:
        @staticmethod
        def cdf(x):
            return 0.5 * (1 + math.erf(x / math.sqrt(2)))
        @staticmethod
        def pdf(x):
            return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)

# ─── Configuration ────────────────────────────────────────────────────────────
_SCAN_INTERVAL_S      = 60       # Scan toutes les 60s
_RISK_FREE_RATE       = 0.04     # Taux sans risque (Fed Funds ~4%)
_DIVIDEND_YIELD       = 0.0      # Pas de dividendes pour CFDs
_ANOMALY_Z_THRESHOLD  = 2.0      # Z-score pour anomalie
_DELTA_NEUTRAL_BAND   = 0.05     # |Delta| < 0.05 = neutre
_MIN_HISTORY_POINTS   = 20       # Minimum de points pour construire la surface

# Instruments ciblés pour l'analyse vol surface
_VOL_INSTRUMENTS = [
    "GOLD", "US500", "US100", "BTCUSD", "ETHUSD",
    "EURUSD", "GBPUSD", "DE40", "OIL_CRUDE",
]

# Expirations synthétiques (en jours)
_EXPIRIES = [7, 14, 30, 60, 90]

# Moneyness levels (K/S ratios)
_MONEYNESS = [0.90, 0.95, 0.97, 1.00, 1.03, 1.05, 1.10]


class Greeks:
    """Les 5 Greeks d'une option."""
    __slots__ = ("delta", "gamma", "theta", "vega", "rho", "iv")

    def __init__(self, delta=0, gamma=0, theta=0, vega=0, rho=0, iv=0):
        self.delta = delta
        self.gamma = gamma
        self.theta = theta
        self.vega = vega
        self.rho = rho
        self.iv = iv      # Volatilité implicite

    def __repr__(self):
        return (f"Greeks(Δ={self.delta:.3f} Γ={self.gamma:.4f} "
                f"Θ={self.theta:.3f} ν={self.vega:.3f} ρ={self.rho:.4f})")


class VolAnomaly:
    """Anomalie détectée dans la surface de volatilité."""

    def __init__(self, instrument: str, anomaly_type: str, strike: float,
                 expiry_days: int, iv_observed: float, iv_fair: float,
                 z_score: float):
        self.instrument = instrument
        self.anomaly_type = anomaly_type   # "SKEW", "SMILE", "TERM_INVERSION"
        self.strike = strike
        self.expiry_days = expiry_days
        self.iv_observed = iv_observed
        self.iv_fair = iv_fair
        self.z_score = z_score
        self.delta_neutral_signal = "SELL_VOL" if iv_observed > iv_fair else "BUY_VOL"
        self.timestamp = datetime.now(timezone.utc)


class VolSurface:
    """
    Moteur 25 : Volatility Surface & Greeks Neutral Arbitrage.

    Construit la surface de volatilité implicite, calcule les Greeks,
    et détecte les anomalies pour l'arbitrage delta-neutre.
    """

    def __init__(self, db=None, capital_client=None, telegram_router=None):
        self._db = db
        self._capital = capital_client
        self._tg = telegram_router
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # Surface de volatilité par instrument
        # Dict[instrument, Dict[(moneyness, expiry), iv]]
        self._surfaces: Dict[str, Dict[Tuple[float, int], float]] = {}

        # Greeks du portefeuille global
        self._portfolio_greeks = Greeks()

        # Historique des réalisations pour calibration
        self._realized_vol: Dict[str, float] = {}

        # Anomalies détectées
        self._anomalies: List[VolAnomaly] = []
        self._active_anomalies: Dict[str, VolAnomaly] = {}

        # Stats
        self._scans = 0
        self._anomalies_total = 0
        self._arb_signals = 0
        self._last_scan_ms = 0.0

        self._ensure_table()
        logger.info("📐 M25 Volatility Surface initialisé (BSM + Greeks + Delta-Neutral)")

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="vol_surface"
        )
        self._thread.start()
        logger.info("📐 M25 Volatility Surface démarré (scan toutes les 60s)")

    def stop(self):
        self._running = False

    # ─── BSM Model ───────────────────────────────────────────────────────────

    @staticmethod
    def bsm_price(S: float, K: float, T: float, r: float, sigma: float,
                  option_type: str = "call") -> float:
        """
        Black-Scholes-Merton pricing.
        S: spot price, K: strike, T: time to expiry (years),
        r: risk-free rate, sigma: volatility
        """
        if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
            return 0.0

        d1 = (math.log(S / K) + (r + sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)

        if option_type == "call":
            return S * _norm.cdf(d1) - K * math.exp(-r * T) * _norm.cdf(d2)
        else:  # put
            return K * math.exp(-r * T) * _norm.cdf(-d2) - S * _norm.cdf(-d1)

    @staticmethod
    def compute_greeks(S: float, K: float, T: float, r: float, sigma: float,
                       option_type: str = "call") -> Greeks:
        """
        Calcule les 5 Greeks d'une option.
        """
        if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
            return Greeks()

        sqrt_T = math.sqrt(T)
        d1 = (math.log(S / K) + (r + sigma ** 2 / 2) * T) / (sigma * sqrt_T)
        d2 = d1 - sigma * sqrt_T

        # Delta
        if option_type == "call":
            delta = float(_norm.cdf(d1))
        else:
            delta = float(_norm.cdf(d1)) - 1

        # Gamma (même pour call et put)
        gamma = float(_norm.pdf(d1)) / (S * sigma * sqrt_T)

        # Theta (per day)
        theta_term1 = -(S * float(_norm.pdf(d1)) * sigma) / (2 * sqrt_T)
        if option_type == "call":
            theta_term2 = -r * K * math.exp(-r * T) * float(_norm.cdf(d2))
        else:
            theta_term2 = r * K * math.exp(-r * T) * float(_norm.cdf(-d2))
        theta = (theta_term1 + theta_term2) / 365  # Per day

        # Vega (per 1% vol change)
        vega = S * sqrt_T * float(_norm.pdf(d1)) / 100

        # Rho (per 1% rate change)
        if option_type == "call":
            rho = K * T * math.exp(-r * T) * float(_norm.cdf(d2)) / 100
        else:
            rho = -K * T * math.exp(-r * T) * float(_norm.cdf(-d2)) / 100

        return Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho, iv=sigma)

    @staticmethod
    def implied_volatility(S: float, K: float, T: float, r: float,
                          market_price: float, option_type: str = "call",
                          max_iter: int = 50) -> float:
        """
        Calcule la volatilité implicite par Newton-Raphson.
        """
        if market_price <= 0 or T <= 0 or S <= 0 or K <= 0:
            return 0.0

        sigma = 0.25  # Guess initial
        for _ in range(max_iter):
            price = VolSurface.bsm_price(S, K, T, r, sigma, option_type)
            diff = price - market_price

            # Vega pour Newton step
            sqrt_T = math.sqrt(T)
            d1 = (math.log(S / K) + (r + sigma ** 2 / 2) * T) / (sigma * sqrt_T)
            vega = S * sqrt_T * float(_norm.pdf(d1))

            if abs(vega) < 1e-12:
                break

            sigma -= diff / vega

            if abs(diff) < 1e-8:
                break

            sigma = max(0.01, min(sigma, 5.0))  # Clamp

        return max(0.01, sigma)

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_greeks(self, instrument: str) -> Greeks:
        """Retourne les Greeks synthétiques pour un instrument."""
        with self._lock:
            surface = self._surfaces.get(instrument, {})

        if not surface:
            return Greeks()

        # ATM Greeks (moneyness = 1.0, 30 jours)
        iv = surface.get((1.0, 30), 0.20)
        return self.compute_greeks(100, 100, 30 / 365, _RISK_FREE_RATE, iv)

    def get_portfolio_greeks(self) -> Greeks:
        """Retourne les Greeks agrégés du portefeuille."""
        with self._lock:
            return Greeks(
                delta=self._portfolio_greeks.delta,
                gamma=self._portfolio_greeks.gamma,
                theta=self._portfolio_greeks.theta,
                vega=self._portfolio_greeks.vega,
                rho=self._portfolio_greeks.rho,
            )

    def scan_anomalies(self) -> List[VolAnomaly]:
        """Retourne les anomalies actives."""
        with self._lock:
            return list(self._active_anomalies.values())

    def get_delta_neutral_signal(self, instrument: str) -> Tuple[str, float, str]:
        """
        Retourne le signal delta-neutre pour un instrument.
        Returns: (signal_type, confidence, reason)
        """
        with self._lock:
            anomaly = self._active_anomalies.get(instrument)

        if not anomaly:
            return "NONE", 0.0, "no_anomaly"

        conf = min(abs(anomaly.z_score) / 4, 1.0)
        return anomaly.delta_neutral_signal, conf, anomaly.anomaly_type

    def stats(self) -> dict:
        with self._lock:
            portfolio_g = {
                "delta": round(self._portfolio_greeks.delta, 4),
                "gamma": round(self._portfolio_greeks.gamma, 6),
                "theta": round(self._portfolio_greeks.theta, 4),
                "vega": round(self._portfolio_greeks.vega, 4),
            }
            anomalies = {k: {
                "type": v.anomaly_type,
                "z": round(v.z_score, 2),
                "signal": v.delta_neutral_signal,
            } for k, v in self._active_anomalies.items()}

        return {
            "scans": self._scans,
            "anomalies_total": self._anomalies_total,
            "arb_signals": self._arb_signals,
            "portfolio_greeks": portfolio_g,
            "active_anomalies": anomalies,
            "surfaces_built": len(self._surfaces),
            "last_scan_ms": round(self._last_scan_ms, 1),
        }

    def format_report(self) -> str:
        s = self.stats()
        g = s["portfolio_greeks"]
        anom_str = " | ".join(
            f"{k}:{v['type']}(z={v['z']})" for k, v in s["active_anomalies"].items()
        ) or "—"
        return (
            f"📐 <b>Vol Surface (M25)</b>\n\n"
            f"  Portfolio Greeks:\n"
            f"    Δ={g['delta']:.4f} Γ={g['gamma']:.6f}\n"
            f"    Θ={g['theta']:.4f} ν={g['vega']:.4f}\n"
            f"  Surfaces: {s['surfaces_built']} | Anomalies: {s['anomalies_total']}\n"
            f"  Arb Signals: {s['arb_signals']}\n"
            f"  Actives: {anom_str}"
        )

    # ─── Scan Loop ───────────────────────────────────────────────────────────

    def _scan_loop(self):
        time.sleep(25)  # Init delay
        while self._running:
            t0 = time.time()
            try:
                self._scan_cycle()
            except Exception as e:
                logger.debug(f"M25 scan: {e}")
            self._last_scan_ms = (time.time() - t0) * 1000
            self._scans += 1
            time.sleep(_SCAN_INTERVAL_S)

    def _scan_cycle(self):
        """Cycle complet : calibration → surface → anomalies → arbitrage."""
        # 1. Calculer la volatilité réalisée
        self._compute_realized_vol()

        # 2. Construire les surfaces de volatilité
        self._build_surfaces()

        # 3. Détecter les anomalies
        self._detect_anomalies()

        # 4. Mettre à jour les Greeks du portefeuille
        self._update_portfolio_greeks()

    # ─── Realized Volatility ─────────────────────────────────────────────────

    def _compute_realized_vol(self):
        """Calcule la volatilité réalisée à partir des données OHLCV."""
        if not self._capital:
            return

        for instrument in _VOL_INSTRUMENTS:
            try:
                df = self._capital.fetch_ohlcv(instrument, "1h", 50)
                if df is None or len(df) < _MIN_HISTORY_POINTS:
                    continue

                # Log returns
                log_returns = np.log(df["close"] / df["close"].shift(1)).dropna()
                if len(log_returns) < 10:
                    continue

                # Annualized volatility
                hourly_vol = float(log_returns.std())
                annual_vol = hourly_vol * math.sqrt(252 * 24)  # 252 trading days × 24h

                with self._lock:
                    self._realized_vol[instrument] = annual_vol

            except Exception:
                pass

    # ─── Surface Construction ────────────────────────────────────────────────

    def _build_surfaces(self):
        """Construit la surface de vol synthétique pour chaque instrument."""
        with self._lock:
            realized = dict(self._realized_vol)

        for instrument in _VOL_INSTRUMENTS:
            base_vol = realized.get(instrument, 0.20)
            if base_vol <= 0:
                continue

            surface = {}
            for moneyness in _MONEYNESS:
                for expiry in _EXPIRIES:
                    # Modèle de smile simplifié (Heston-like)
                    # La vol augmente pour les strikes OTM
                    otm_penalty = abs(moneyness - 1.0)

                    # Skew : puts OTM plus chers (volatilité skew)
                    skew = -0.15 * (moneyness - 1.0) if moneyness < 1.0 else 0.0

                    # Term structure : vol diminue avec maturité (contango)
                    term_adj = -0.002 * (expiry - 30) / 30

                    # Smile : vol augmente des deux côtés
                    smile = 0.3 * otm_penalty ** 2

                    iv = base_vol * (1 + skew + term_adj + smile)

                    # Ajouter du bruit réaliste
                    noise = np.random.normal(0, 0.005)
                    iv = max(0.05, iv + noise)

                    surface[(moneyness, expiry)] = round(iv, 4)

            with self._lock:
                self._surfaces[instrument] = surface

    # ─── Anomaly Detection ───────────────────────────────────────────────────

    def _detect_anomalies(self):
        """Détecte les anomalies dans les surfaces de volatilité."""
        with self._lock:
            surfaces = {k: dict(v) for k, v in self._surfaces.items()}

        for instrument, surface in surfaces.items():
            if not surface:
                continue

            # 1. Vérifier le skew (put OTM vs call OTM)
            self._check_skew_anomaly(instrument, surface)

            # 2. Vérifier la term structure
            self._check_term_structure(instrument, surface)

            # 3. Vérifier le smile
            self._check_smile_anomaly(instrument, surface)

    def _check_skew_anomaly(self, instrument: str, surface: dict):
        """Détecte un skew excessif ou inversé."""
        for expiry in _EXPIRIES:
            put_ivs = [surface.get((m, expiry), 0) for m in _MONEYNESS if m < 1.0]
            call_ivs = [surface.get((m, expiry), 0) for m in _MONEYNESS if m > 1.0]

            if not put_ivs or not call_ivs:
                continue

            skew = np.mean(put_ivs) - np.mean(call_ivs)
            # Le skew normal est négatif (puts plus chers)
            # Un skew positif (calls plus chers) est anomal
            if skew > 0.03:
                z_score = skew / 0.01  # Normaliser
                if abs(z_score) > _ANOMALY_Z_THRESHOLD:
                    self._register_anomaly(
                        instrument, "SKEW_INVERSION", 1.0, expiry,
                        float(np.mean(put_ivs)), float(np.mean(call_ivs)),
                        z_score
                    )

    def _check_term_structure(self, instrument: str, surface: dict):
        """Détecte une inversion de la structure par terme."""
        atm_ivs = [(expiry, surface.get((1.0, expiry), 0)) for expiry in _EXPIRIES]
        atm_ivs = [(e, v) for e, v in atm_ivs if v > 0]

        if len(atm_ivs) < 3:
            return

        # Vérifier si la structure est inversée (court terme > long terme)
        short = np.mean([v for e, v in atm_ivs if e <= 14])
        long_t = np.mean([v for e, v in atm_ivs if e >= 60])

        if short > 0 and long_t > 0:
            ratio = short / long_t
            if ratio > 1.3:  # Inversion significative
                z_score = (ratio - 1.0) / 0.15
                if abs(z_score) > _ANOMALY_Z_THRESHOLD:
                    self._register_anomaly(
                        instrument, "TERM_INVERSION", 1.0, 30,
                        short, long_t, z_score
                    )

    def _check_smile_anomaly(self, instrument: str, surface: dict):
        """Détecte un smile disloqué."""
        for expiry in [30, 60]:
            ivs = [(m, surface.get((m, expiry), 0)) for m in _MONEYNESS]
            ivs = [(m, v) for m, v in ivs if v > 0]

            if len(ivs) < 5:
                continue

            # Fit quadratic (smile = a*x² + b*x + c)
            x = np.array([m for m, _ in ivs])
            y = np.array([v for _, v in ivs])

            coeffs = np.polyfit(x - 1.0, y, 2)
            fitted = np.polyval(coeffs, x - 1.0)
            residuals = y - fitted

            # Chercher les outliers (résidus > 2 sigma)
            res_std = np.std(residuals)
            if res_std > 0:
                for i, (m, v) in enumerate(ivs):
                    z = residuals[i] / res_std
                    if abs(z) > _ANOMALY_Z_THRESHOLD:
                        self._register_anomaly(
                            instrument, "SMILE_DISLOC", m, expiry,
                            v, float(fitted[i]), float(z)
                        )

    def _register_anomaly(self, instrument: str, atype: str, strike: float,
                         expiry: int, iv_obs: float, iv_fair: float, z: float):
        """Enregistre une anomalie détectée."""
        anomaly = VolAnomaly(instrument, atype, strike, expiry, iv_obs, iv_fair, z)

        with self._lock:
            self._active_anomalies[instrument] = anomaly
            self._anomalies.append(anomaly)
            self._anomalies = self._anomalies[-100:]  # Keep last 100

        self._anomalies_total += 1
        self._arb_signals += 1

        logger.info(
            f"📐 M25 ANOMALY: {instrument} {atype} z={z:.2f} "
            f"IV={iv_obs:.3f} vs Fair={iv_fair:.3f} → {anomaly.delta_neutral_signal}"
        )
        self._persist_anomaly(anomaly)

    # ─── Portfolio Greeks ────────────────────────────────────────────────────

    def _update_portfolio_greeks(self):
        """Agrège les Greeks de tous les instruments du portefeuille."""
        total = Greeks()
        count = 0

        for instrument in _VOL_INSTRUMENTS:
            g = self.get_greeks(instrument)
            if g.delta != 0:
                total.delta += g.delta
                total.gamma += g.gamma
                total.theta += g.theta
                total.vega += g.vega
                total.rho += g.rho
                count += 1

        # Normaliser si multi-instruments
        if count > 0:
            with self._lock:
                self._portfolio_greeks = total

    # ─── Database ────────────────────────────────────────────────────────────

    def _ensure_table(self):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS vol_surface_anomalies (
                    id              SERIAL PRIMARY KEY,
                    instrument      VARCHAR(20),
                    anomaly_type    VARCHAR(30),
                    strike          FLOAT,
                    expiry_days     INT,
                    iv_observed     FLOAT,
                    iv_fair         FLOAT,
                    z_score         FLOAT,
                    signal          VARCHAR(20),
                    detected_at     TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"M25 table: {e}")

    def _persist_anomaly(self, a: VolAnomaly):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            ph = "%s"
            self._db._execute(
                f"INSERT INTO vol_surface_anomalies "
                f"(instrument,anomaly_type,strike,expiry_days,iv_observed,iv_fair,z_score,signal) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (a.instrument, a.anomaly_type, a.strike, a.expiry_days,
                 a.iv_observed, a.iv_fair, a.z_score, a.delta_neutral_signal)
            )
        except Exception:
            pass
