"""
smart_router.py — Moteur 7 : Smart Routing & Iceberg Orders (TWAP/Execution).

Les fonds institutionnels n'entrent JAMAIS en one-shot sur un marché.
Ils découpent les ordres en micro-tranches pour:
  1. Être invisibles (ne pas déplacer le marché)
  2. Obtenir un meilleur prix moyen (TWAP = Time-Weighted Average Price)
  3. Limiter le slippage par rapport à un market order large

Algorithme TWAP implémenté:
  - Ordre de taille N divisé en K slices
  - Chaque slice placée à intervalle T secondes
  - Si prix se dégrade > 0.2% entre slices → pause + alert
  - Si prix s'améliore → accélère les slices restantes (+50% speed)
  - Log chaque slice dans Supabase `order_slices`

Usage:
    router = SmartRouter(capital_client, db)
    # Remplace place_market_order() directement:
    result = router.execute_twap(
        epic="EURUSD", direction="BUY", total_size=1.0,
        num_slices=5, interval_s=15
    )
    # → exécute 5 × 0.2 lots sur 75 secondes
"""
import time
import threading
from typing import List, Optional, Dict
from datetime import datetime, timezone
from loguru import logger

# ─── Paramètres ───────────────────────────────────────────────────────────────
_DEFAULT_SLICES    = 5       # Nombre de micro-ordres par défaut
_DEFAULT_INTERVAL  = 12      # Secondes entre chaque slice (60s total)
_MAX_PRICE_DRIFT   = 0.002   # 0.2% drift max avant pause
_ACCEL_THRESHOLD   = 0.001   # Si prix améliore de 0.1% → accélère
_MIN_SLICE_SIZE    = 0.01    # Taille minimum par slice
_TWAP_TIMEOUT_S    = 300     # Time-out global 5 minutes pour un ordre


class SmartRouter:
    """
    Exécuteur d'ordres TWAP/Iceberg pour entrées institutionnelles.
    Chaque ordre est haché en N slices placées dans le temps.
    """

    def __init__(self, capital_client=None, db=None, telegram_router=None, broker=None):
        self._broker  = broker or capital_client   # broker actif (MT5 ou Capital)
        self._capital = capital_client             # gardé pour compatibilité prix
        self._db      = db
        self._tg      = telegram_router

        # Stats globales
        self._total_orders   = 0
        self._total_slices   = 0
        self._total_paused   = 0

        self._ensure_table()

    # ─── Public API ──────────────────────────────────────────────────────────

    def execute_twap(self, epic: str, direction: str, total_size: float,
                      num_slices: int = _DEFAULT_SLICES,
                      interval_s: float = _DEFAULT_INTERVAL,
                      sl_price: float = None, tp_price: float = None,
                      blocking: bool = False) -> dict:
        """
        Lance l'exécution TWAP d'un ordre.

        Args:
            epic: code de l'instrument (ex: "EURUSD")
            direction: "BUY" ou "SELL"
            total_size: taille totale à exécuter
            num_slices: nombre de micro-tranches
            interval_s: secondes entre chaque tranche
            sl_price, tp_price: SL/TP (appliqués à la première slice)
            blocking: si True, attend la fin (sinon, thread daemon)

        Returns dict avec refs, avg_price, filled_size, status.
        """
        slice_size = max(round(total_size / num_slices, 2), _MIN_SLICE_SIZE)
        order_id   = f"twap_{epic}_{int(time.time())}"

        meta = {
            "order_id":   order_id,
            "epic":        epic,
            "direction":   direction,
            "total_size":  total_size,
            "slice_size":  slice_size,
            "num_slices":  num_slices,
            "interval_s":  interval_s,
            "sl_price":    sl_price,
            "tp_price":    tp_price,
            "refs":        [],
            "prices":      [],
            "filled_size": 0.0,
            "status":      "RUNNING",
            "started_at":  datetime.now(timezone.utc),
        }

        if blocking:
            self._execute_slices(meta)
        else:
            t = threading.Thread(
                target=self._execute_slices,
                args=(meta,),
                daemon=True,
                name=f"twap_{epic}"
            )
            t.start()

        self._total_orders += 1
        logger.info(
            f"🧊 TWAP {direction} {epic}: {num_slices}×{slice_size} "
            f"sur {num_slices * interval_s:.0f}s"
        )
        return meta

    def execute_single(self, epic: str, direction: str, size: float,
                        sl_price: float = None, tp_price: float = None) -> Optional[str]:
        """
        Ordre unique classique (pass-through, TWAP à 1 slice).
        Remplace direct les appels à place_market_order.
        """
        if not self._broker:
            return None
        try:
            ref = self._broker.place_market_order(
                epic=epic, direction=direction,
                size=size, sl_price=sl_price, tp_price=tp_price
            )
            return ref
        except Exception as e:
            logger.warning(f"SmartRouter single {epic}: {e}")
            return None

    # ─── Iceberg Execution ───────────────────────────────────────────────────

    def _execute_slices(self, meta: dict):
        """Thread: exécute les slices TWAP une par une."""
        epic        = meta["epic"]
        direction   = meta["direction"]
        slice_size  = meta["slice_size"]
        interval_s  = meta["interval_s"]
        num_slices  = meta["num_slices"]
        t0          = time.monotonic()

        reference_price = self._get_mid(epic)
        if reference_price is None:
            reference_price = 0.0

        for i in range(num_slices):
            # Timeout global
            if time.monotonic() - t0 > _TWAP_TIMEOUT_S:
                logger.warning(f"🧊 TWAP {epic}: timeout {_TWAP_TIMEOUT_S}s — {i}/{num_slices} slices exécutées")
                meta["status"] = "TIMEOUT"
                break

            # Vérification du drift de prix
            current_price = self._get_mid(epic)
            if current_price and reference_price > 0:
                drift_pct = abs(current_price - reference_price) / reference_price
                price_dir = (current_price - reference_price) / reference_price

                # Prix s'est dégradé → pause courte
                if drift_pct > _MAX_PRICE_DRIFT and (
                    (direction == "BUY" and price_dir > 0) or
                    (direction == "SELL" and price_dir < 0)
                ):
                    self._total_paused += 1
                    logger.debug(f"🧊 TWAP {epic}: drift {drift_pct:.3%} > seuil → pause 5s")
                    time.sleep(5)
                    continue

                # Prix s'est amélioré → on accélère
                if drift_pct > _ACCEL_THRESHOLD and (
                    (direction == "BUY" and price_dir < 0) or
                    (direction == "SELL" and price_dir > 0)
                ):
                    logger.debug(f"🧊 TWAP {epic}: favorable drift → interval réduit")
                    interval_s = max(interval_s * 0.5, 2.0)

            # Placement de la slice
            sl  = meta["sl_price"] if i == 0 else None   # SL seulement sur la 1ère
            tp  = meta["tp_price"] if i == 0 else None   # TP seulement sur la 1ère
            ref = None

            if self._broker:
                try:
                    ref = self._broker.place_market_order(
                        epic=epic, direction=direction,
                        size=slice_size, sl_price=sl, tp_price=tp
                    )
                except Exception as e:
                    logger.warning(f"🧊 TWAP slice {i+1}/{num_slices} {epic}: {e}")

            meta["refs"].append(ref)
            meta["prices"].append(current_price or 0.0)
            meta["filled_size"] = round(meta["filled_size"] + slice_size, 2)
            self._total_slices += 1

            idx_log = i + 1
            logger.debug(
                f"🧊 TWAP {epic} slice {idx_log}/{num_slices}: "
                f"{slice_size} @ ~{current_price:.5f} | ref={ref}"
            )
            self._save_slice_async(meta, i, slice_size, current_price, ref)

            # Attente avant prochaine slice (sauf dernière)
            if i < num_slices - 1:
                time.sleep(interval_s)

        # Fin de l'exécution
        if meta["status"] == "RUNNING":
            meta["status"] = "DONE"

        prices = [p for p in meta["prices"] if p > 0]
        avg_px = sum(prices) / len(prices) if prices else 0
        meta["avg_price"] = round(avg_px, 5)

        logger.info(
            f"🧊 TWAP {epic} TERMINÉ | "
            f"{meta['filled_size']:.2f}/{meta['total_size']:.2f} lots filled "
            f"| avg_px={avg_px:.5f} | status={meta['status']}"
        )

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _get_mid(self, epic: str) -> Optional[float]:
        """Prix mid actuel."""
        try:
            if self._broker:
                px = self._broker.get_current_price(epic)
                if px:
                    return px.get("mid", None)
        except Exception:
            pass
        return None

    def stats(self) -> dict:
        return {
            "total_orders": self._total_orders,
            "total_slices": self._total_slices,
            "pauses":       self._total_paused,
        }

    # ─── DB ──────────────────────────────────────────────────────────────────

    def _ensure_table(self):
        if not self._db:
            return
        try:
            if self._db._pg:
                self._db._execute("""
                    CREATE TABLE IF NOT EXISTS order_slices (
                        id          SERIAL PRIMARY KEY,
                        order_id    VARCHAR(50),
                        instrument  VARCHAR(20),
                        direction   VARCHAR(4),
                        slice_n     INTEGER,
                        size        DOUBLE PRECISION,
                        price       DOUBLE PRECISION,
                        ref         VARCHAR(50),
                        sliced_at   TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
            else:
                self._db._execute("""
                    CREATE TABLE IF NOT EXISTS order_slices (
                        id          INTEGER PRIMARY KEY,
                        order_id    TEXT,
                        instrument  TEXT,
                        direction   TEXT,
                        slice_n     INTEGER,
                        size        REAL,
                        price       REAL,
                        ref         TEXT,
                        sliced_at   TEXT
                    )
                """)
        except Exception as e:
            logger.debug(f"order_slices table: {e}")

    def _save_slice_async(self, meta, idx, size, price, ref):
        if not self._db:
            return
        self._db.async_write(self._save_slice_sync, meta, idx, size, price, ref)

    def _save_slice_sync(self, meta, idx, size, price, ref):
        try:
            ph = "%s" if self._db._pg else "?"
            self._db._execute(
                f"INSERT INTO order_slices (order_id,instrument,direction,slice_n,size,price,ref) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (meta["order_id"], meta["epic"], meta["direction"],
                 idx + 1, size, price or 0.0, ref or "")
            )
        except Exception as e:
            logger.debug(f"slice save: {e}")
