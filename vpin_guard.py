"""
vpin_guard.py — Moteur 9 : Order Flow Toxicity & VPIN Shield.

VPIN (Volume-Synchronized Probability of Informed Trading) — Easley, Lopez de Prado, O'Hara (2012).

Principe: Sur les marchés modernes, les trades "toxiques" sont ceux
des institutionnels qui ont de l'information privée. Ils achètent/vendent
en silence, absorbant la liquidité. Quand assez d'informés sont du même côté,
VPIN explose → flash crash imminent.

Algorithme:
  1. Agréger les ticks en "buckets" de volume V/n
  2. Pour chaque bucket: estimer V_buy et V_sell (approx via Tick Rule ou BAR)
  3. VPIN = SUM(|V_buy - V_sell|) / (n * V/n) sur les n derniers buckets
  4. Si VPIN > 0.75 → ALERTE JAUNE (réduire expositions)
  5. Si VPIN > 0.90 → ALERTE ROUGE → FERMETURE FORCÉE DE TOUTES LES POSITIONS

Référence: "Flow Toxicity and Liquidity in a High-frequency World" — Lopez de Prado

Usage:
    vpin = VPINGuard(capital, db, telegram_router)
    vpin.start()  # daemon thread, scan toutes les 30s

    # Appelé avant chaque entrée:
    if vpin.is_toxic(instrument):
        return  # signal bloqué
"""
import time
import math
import threading
from collections import deque
from typing import Dict, Optional, List
from datetime import datetime, timezone
from loguru import logger

# ─── Seuils VPIN ──────────────────────────────────────────────────────────────
_VPIN_YELLOW     = 0.65   # Alerte: réduire l'exposition
_VPIN_RED        = 0.82   # Rouge: fermeture forcée toutes positions
_VPIN_BUCKETS    = 50     # Nombre de buckets pour le calcul (rolling)
_SCAN_INTERVAL_S = 30     # Fréquence de mise à jour VPIN
_BUCKET_SIZE_PCT = 0.0020 # Un bucket = 0.20% de mouvement de prix (proxy volume)
_COOLDOWN_S      = 120    # 2min entre deux alertes rouges pour le même actif

# ─── Action values (pour RL feedback)
VPIN_SAFE    = 0
VPIN_CAUTION = 1
VPIN_TOXIC   = 2


class VPINGuard:
    """
    Bouclier VPIN: détecte la toxicité du flux d'ordres (manipulation institutionnelle).
    Si VPIN > seuil rouge → fermeture forcée de toutes les positions.
    Tourne en daemon thread indépendant.
    """

    def __init__(self, capital_client=None, capital_trades_ref: dict = None,
                 db=None, telegram_router=None, close_fn=None):
        self._capital  = capital_client
        self._trades   = capital_trades_ref   # pointeur dict des trades ouverts
        self._db       = db
        self._tg       = telegram_router
        self._close_fn = close_fn  # fn(instrument, reason) pour fermer une position

        # {instrument: deque([(ts, buy_vol_frac, sell_vol_frac), ...], maxlen=50)}
        self._buckets:  Dict[str, deque] = {}
        # {instrument: vpin_score}
        self._vpin_scores: Dict[str, float] = {}
        # {instrument: last_price} pour le calcul tick-rule
        self._last_prices: Dict[str, float] = {}
        # {instrument: last_alert_ts}
        self._last_alert: Dict[str, float] = {}

        # Statistiques
        self._toxic_events   = 0
        self._positions_closed = 0
        self._scan_count     = 0

        self._running = False
        self._lock    = threading.Lock()
        self._thread  = None

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._scan_loop, daemon=True, name="vpin_guard"
        )
        self._thread.start()
        logger.info("🛡️ VPIN Guard démarré (scan toutes les 30s)")

    def stop(self):
        self._running = False

    # ─── Public API ──────────────────────────────────────────────────────────

    def is_toxic(self, instrument: str) -> tuple:
        """
        Vérifie si un instrument a un flux toxique (blocage d'entrée).
        Returns: (is_toxic: bool, vpin_score: float, level: str)
        """
        with self._lock:
            score = self._vpin_scores.get(instrument, 0.0)

        if score >= _VPIN_RED:
            return True, score, "TOXIC"
        elif score >= _VPIN_YELLOW:
            return True, score, "CAUTION"
        return False, score, "SAFE"

    def get_all_scores(self) -> dict:
        with self._lock:
            return dict(self._vpin_scores)

    def status(self) -> dict:
        return {
            "scans": self._scan_count,
            "toxic_events": self._toxic_events,
            "positions_closed": self._positions_closed,
            "instruments_tracked": len(self._vpin_scores),
        }

    # ─── Scan Loop ────────────────────────────────────────────────────────────

    def _scan_loop(self):
        while self._running:
            try:
                self._update_all()
                self._check_emergency()
                self._scan_count += 1
            except Exception as e:
                logger.debug(f"VPIN scan: {e}")
            time.sleep(_SCAN_INTERVAL_S)

    def _update_all(self):
        """Met à jour VPIN pour tous les instruments actuellement tradés."""
        instruments = []
        if self._trades:
            instruments = [k for k, v in self._trades.items() if v is not None]

        # Aussi traquer quelques actifs clés même sans position ouverte
        if self._capital:
            try:
                key_assets = ["BTCUSD", "ETHUSD", "EURUSD", "GOLD", "OIL_CRUDE"]
                instruments = list(set(instruments + key_assets))
            except Exception:
                pass

        for instrument in instruments[:20]:  # max 20 instruments en parallèle
            try:
                self._update_vpin(instrument)
            except Exception as e:
                logger.debug(f"VPIN update {instrument}: {e}")

    def _update_vpin(self, instrument: str):
        """Calcule le VPIN pour un instrument via l'approximation BAR (Bulk Volume Classification)."""
        if not self._capital:
            return

        # Récupère prix mid actuel
        px_data = self._capital.get_current_price(instrument)
        if not px_data:
            return

        bid  = px_data.get("bid", 0)
        ask  = px_data.get("ask", 0)
        mid  = px_data.get("mid", (bid + ask) / 2 if bid and ask else 0)

        if mid <= 0:
            return

        with self._lock:
            last_px = self._last_prices.get(instrument, mid)
            self._last_prices[instrument] = mid

        # === Tick Rule: direction du "flux" ===
        # Si le prix monte → ordres buy implicites (pressione acheteuse)
        # Si le prix baisse → ordres sell implicites (pression vendeuse)
        price_change = (mid - last_px) / last_px if last_px > 0 else 0.0
        spread_pct   = (ask - bid) / mid if mid > 0 and ask > bid else 0.001

        # BAR: P_buy = Φ((close - low) / (high - low)) ≈ Φ(normalized_change)
        # Approximation car pas de tick data complet sur CFD
        p_buy  = self._normal_cdf(price_change / max(spread_pct, 0.0001))
        p_sell = 1.0 - p_buy

        # Un bucket = mouvement relatif ≥ _BUCKET_SIZE_PCT
        if abs(price_change) >= _BUCKET_SIZE_PCT:
            with self._lock:
                if instrument not in self._buckets:
                    self._buckets[instrument] = deque(maxlen=_VPIN_BUCKETS)
                self._buckets[instrument].append((time.time(), p_buy, p_sell))

        # Calcul VPIN sur les N derniers buckets
        with self._lock:
            buckets = list(self._buckets.get(instrument, []))

        if len(buckets) < 5:
            # Pas assez de data → score neutre
            with self._lock:
                self._vpin_scores[instrument] = 0.0
            return

        imbalances = [abs(b - s) for (_, b, s) in buckets]
        vpin = sum(imbalances) / len(imbalances)

        with self._lock:
            self._vpin_scores[instrument] = round(vpin, 4)

        if vpin >= _VPIN_YELLOW:
            logger.debug(
                f"🛡️ VPIN {instrument}: {vpin:.3f} "
                f"({'🔴 TOXIC' if vpin >= _VPIN_RED else '⚠️ CAUTION'})"
            )

    def _check_emergency(self):
        """Déclenche l'urgence si un actif dépasse le seuil VPIN rouge."""
        if not self._trades:
            return

        with self._lock:
            scores = dict(self._vpin_scores)

        for instrument, vpin in scores.items():
            if vpin < _VPIN_RED:
                continue

            # Cooldown
            now = time.monotonic()
            if now - self._last_alert.get(instrument, 0) < _COOLDOWN_S:
                continue
            self._last_alert[instrument] = now

            # Y a-t-il une position ouverte sur cet actif?
            if self._trades.get(instrument) is None:
                continue

            self._toxic_events += 1
            logger.warning(
                f"🛡️ VPIN ROUGE: {instrument} VPIN={vpin:.3f} > {_VPIN_RED} — "
                f"FERMETURE D'URGENCE"
            )

            # Fermeture forcée
            if self._close_fn:
                try:
                    self._close_fn(instrument, reason="vpin_emergency")
                    self._positions_closed += 1
                except Exception as e:
                    logger.error(f"VPIN emergency close {instrument}: {e}")

            # Alerte Telegram
            if self._tg:
                try:
                    self._tg.send_trade(
                        f"🚨 <b>VPIN URGENCE — {instrument}</b>\n\n"
                        f"  Toxicité flux: <b>{vpin:.1%}</b> (seuil: {_VPIN_RED:.0%})\n"
                        f"  → Position fermée d'urgence\n"
                        f"  → Entrées bloquées sur cet actif pendant 2min\n\n"
                        f"  <i>Manipulation institutionnelle détectée</i>"
                    )
                except Exception:
                    pass

            # Log Supabase
            self._log_toxic_event_async(instrument, vpin)

    # ─── Math Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _normal_cdf(x: float) -> float:
        """CDF de la loi normale standard (approx Abramowitz & Stegun)."""
        if x >= 5: return 1.0
        if x <= -5: return 0.0
        t = 1.0 / (1.0 + 0.2316419 * abs(x))
        d = 0.3989422820 * math.exp(-0.5 * x * x)
        p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.7814779 +
            t * (-1.8212560 + t * 1.3302744))))
        return (1.0 - p) if x >= 0 else p

    # ─── DB Logging ──────────────────────────────────────────────────────────

    def _log_toxic_event_async(self, instrument, vpin):
        if not self._db:
            return
        self._db.async_write(self._log_toxic_event_sync, instrument, vpin)

    def _log_toxic_event_sync(self, instrument, vpin):
        try:
            if self._db._pg:
                ph = "%s"
                self._db._execute(
                    f"INSERT INTO vpin_events (instrument,vpin_score,action,logged_at) "
                    f"VALUES ({ph},{ph},'EMERGENCY_CLOSE',NOW())",
                    (instrument, vpin)
                )
        except Exception:
            pass  # table peut ne pas exister encore

    def ensure_table(self):
        """Crée la table vpin_events si elle n'existe pas."""
        if not self._db or not self._db._pg:
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS vpin_events (
                    id          SERIAL PRIMARY KEY,
                    instrument  VARCHAR(20),
                    vpin_score  DOUBLE PRECISION,
                    action      VARCHAR(30),
                    logged_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"vpin_events table: {e}")
