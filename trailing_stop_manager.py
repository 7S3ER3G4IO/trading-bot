"""
trailing_stop_manager.py — Trailing Stop Réel MT5
Déplace le SL d'une position ouverte après TP1 en utilisant update_position() MT5.
Surveille les positions dans un thread dédié toutes les 30s.

Usage dans bot_init.py :
    from trailing_stop_manager import TrailingStopManager
    self.trailing = TrailingStopManager(broker=self.broker)
    self.trailing.start()
"""
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict
from loguru import logger

# ATR multiplier pour le trailing stop après TP1
TRAILING_ATR_MULT = 1.5
SCAN_INTERVAL_S   = 30   # vérification toutes les 30s


class TrailingStopManager:
    """Gère le trailing stop réel sur les positions MT5 après TP1.
    
    Fonctionnement :
    - Si tp1_hit=True dans positions_ref → SL déjà au BE
    - Toutes les 30s : compare prix actuel vs SL → si écart > ATR*1.5, monte le SL
    """

    def __init__(self, broker, positions_ref: Optional[dict] = None, db=None):
        """
        Args:
            broker : MT5Client ou CapitalStub
            positions_ref : dict partagé self.positions du bot (ref direct)
            db : instance Database pour lire l'ATR depuis trades (optionnel)
        """
        self._broker   = broker
        self._positions = positions_ref or {}
        self._db        = db
        self._thread    = None
        self._running   = False
        self._lock      = threading.Lock()
        # Cache de SL déjà envoyé (evite les appels API répétés pour le même prix)
        self._last_sl: Dict[str, float] = {}

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name="trailing_stop"
        )
        self._thread.start()
        logger.info("🔄 TrailingStopManager démarré")

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                self._scan_positions()
            except Exception as e:
                logger.debug(f"TrailingStopManager._loop: {e}")
            time.sleep(SCAN_INTERVAL_S)

    def _scan_positions(self):
        """Parcourt toutes les positions ouvertes et remonte le SL si applicable."""
        if not self._broker or not getattr(self._broker, 'available', False):
            return

        with self._lock:
            positions_snapshot = dict(self._positions)

        for instrument, state in positions_snapshot.items():
            if state is None:
                continue
            # Seulement si TP1 a été touché (SL déjà au BE)
            if not state.get("tp1_hit", False):
                continue

            direction = state.get("direction", "BUY")
            entry     = float(state.get("entry", 0))
            sl        = float(state.get("sl", 0))
            atr       = float(state.get("_atr", 0))
            refs      = state.get("refs", [])
            deal_id   = refs[0] if refs else None

            if not deal_id:
                continue

            # Prix actuel
            try:
                price_data = self._broker.get_current_price(instrument)
                if not price_data:
                    continue
                mid = float(price_data.get("mid", 0))
                if mid <= 0:
                    continue
            except Exception:
                continue

            if atr <= 0:
                atr = abs(entry - sl) / 1.2  # fallback ATR estimé depuis SL initial

            # Calcul nouveau SL trailing
            trail_dist = atr * TRAILING_ATR_MULT
            if direction == "BUY":
                new_sl = mid - trail_dist
                # Ne monte le SL que si c'est mieux que l'actuel
                if new_sl <= sl + atr * 0.1:  # buffer 10% ATR pour éviter whipsaws
                    continue
            else:  # SELL
                new_sl = mid + trail_dist
                if new_sl >= sl - atr * 0.1:
                    continue

            # Évite les appels API inutiles (même SL arrondi)
            last = self._last_sl.get(instrument, 0)
            if abs(new_sl - last) < atr * 0.05:
                continue

            # Mise à jour SL sur MT5
            try:
                ok = self._broker.update_position(
                    deal_id=str(deal_id),
                    stop_level=round(new_sl, 5),
                    epic=instrument,
                )
                if ok:
                    self._last_sl[instrument] = new_sl
                    # Mettre à jour l'état local
                    with self._lock:
                        if instrument in self._positions and self._positions[instrument]:
                            self._positions[instrument]["sl"] = new_sl

                    logger.info(
                        f"📈 Trailing SL {instrument} {direction}: "
                        f"{sl:.5f} → {new_sl:.5f} (mid={mid:.5f})"
                    )
            except Exception as e:
                logger.debug(f"TrailingStopManager update {instrument}: {e}")

    def register_position(self, instrument: str, atr: float):
        """Enregistre l'ATR d'une position pour le calcul du trailing."""
        with self._lock:
            if instrument in self._positions and self._positions[instrument]:
                self._positions[instrument]["_atr"] = atr

    def stats(self) -> dict:
        return {
            "running":    self._running,
            "tracked":    sum(1 for s in self._positions.values() if s and s.get("tp1_hit")),
            "last_sl":    dict(self._last_sl),
        }
