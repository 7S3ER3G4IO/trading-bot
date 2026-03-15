"""
hedge_manager.py — ⚡ APEX PREDATOR T2: Dynamic Hedging Engine

Au lieu d'encaisser un Stop-Loss brut, le bot peut ouvrir un hedge
sur un actif corrélé pour geler le drawdown et attendre une inversion.

Logique:
  1. Un trade atteint son SL
  2. Au lieu de fermer → vérifie si un hedge corrélé est disponible
  3. Ouvre un trade inverse sur l'actif corrélé (ex: LONG EURUSD perd → SHORT GBPUSD)
  4. Le hedge gèle le drawdown total
  5. Quand le marché s'inverse → ferme le hedge en profit

Sécurités:
  - Max 1 hedge par position
  - Hedge fermé automatiquement après 24h (time_stop)
  - Hedge size = 50% de la position originale (pas 100%, pour réduire le risque)
  - Ne hedge pas si trop de positions ouvertes

Usage:
    hedge = self.hedge_mgr.evaluate_hedge(instrument, direction, entry, sl, balance)
    if hedge:
        ref = self.capital.place_market_order(...)
"""

import time
from datetime import datetime, timezone, timedelta
from loguru import logger


# ─── Correlation Map ─────────────────────────────────────────────────────────
# Pairs with correlation > 0.80 (suitable for hedging)
# Format: instrument → [(correlated_instrument, correlation_sign, hedge_direction)]
# correlation_sign: +1 = positive correlation, -1 = negative correlation
CORRELATION_MAP = {
    # EUR cluster (highly correlated)
    "EURUSD":  [("GBPUSD", +1), ("EURCHF", +1), ("EURJPY", +1)],
    "GBPUSD":  [("EURUSD", +1), ("GBPJPY", +1)],
    "EURCHF":  [("EURUSD", +1), ("USDCHF", -1)],
    "EURJPY":  [("GBPJPY", +1), ("EURUSD", +1)],

    # JPY cluster
    "USDJPY":  [("EURJPY", -1), ("GBPJPY", -1)],
    "GBPJPY":  [("EURJPY", +1), ("GBPUSD", +1)],

    # Commodity currencies
    "AUDUSD":  [("NZDUSD", +1)],
    "NZDUSD":  [("AUDUSD", +1)],

    # Commodities
    "GOLD":    [("SILVER", +1), ("EURUSD", +1)],
    "SILVER":  [("GOLD", +1)],
    "OIL_BRENT": [("OIL_CRUDE", +1)],
    "OIL_CRUDE": [("OIL_BRENT", +1)],

    # Indices
    "US500":   [("US100", +1), ("DE40", +1)],
    "US100":   [("US500", +1)],
    "DE40":    [("UK100", +1), ("US500", +1)],
    "UK100":   [("DE40", +1)],

    # Crypto
    "BTCUSD":  [("ETHUSD", +1)],
    "ETHUSD":  [("BTCUSD", +1)],
}

# ─── Configuration ────────────────────────────────────────────────────────────
HEDGE_SIZE_RATIO     = 0.50    # Hedge = 50% of original position
MAX_HEDGES           = 3       # Max simultaneous hedges
HEDGE_TIME_STOP_H    = 24      # Auto-close hedge after 24h
MAX_TOTAL_POSITIONS  = 15      # Don't hedge if more than 15 positions open


class HedgeManager:
    """
    Dynamic Hedging Engine.

    Remplace le Stop-Loss brutal par un hedge corrélé qui gèle le drawdown
    et permet une récupération sans réaliser la perte.
    """

    def __init__(self, capital_client=None, telegram_router=None, broker=None):
        self._broker  = broker or capital_client   # broker actif (MT5 ou Capital)
        self._capital = capital_client             # gardé pour compatibilité
        self._router = telegram_router

        # Active hedges: {instrument: hedge_state}
        self._active_hedges: dict[str, dict] = {}

        # Stats
        self._hedges_opened = 0
        self._hedges_closed_profit = 0
        self._hedges_closed_loss = 0
        self._hedges_expired = 0
        self._total_hedge_pnl = 0.0

    # ═══════════════════════════════════════════════════════════════════════
    #  EVALUATE: Should we hedge instead of SL?
    # ═══════════════════════════════════════════════════════════════════════

    def evaluate_hedge(self, instrument: str, direction: str,
                       entry: float, sl: float, current_price: float,
                       size: float, balance: float,
                       open_positions: dict = None) -> dict | None:
        """
        Evaluate if a hedge is viable when a trade approaches its SL.

        Parameters:
            instrument: the losing instrument
            direction: original trade direction ("BUY"/"SELL")
            entry: original entry price
            sl: stop-loss price
            current_price: current market price
            size: original position size
            balance: account balance
            open_positions: dict of all open positions

        Returns:
            Hedge instruction dict or None if hedging not viable.
        """
        # ─── Safety checks ────────────────────────────────────────────────
        # Don't stack hedges
        if instrument in self._active_hedges:
            logger.debug(f"Hedge: {instrument} already has an active hedge")
            return None

        # Max hedges limit
        if len(self._active_hedges) >= MAX_HEDGES:
            logger.debug(f"Hedge: max hedges ({MAX_HEDGES}) reached")
            return None

        # Don't hedge if too many positions open
        if open_positions:
            open_count = sum(1 for s in open_positions.values() if s is not None)
            if open_count >= MAX_TOTAL_POSITIONS:
                logger.debug(f"Hedge: too many positions ({open_count})")
                return None

        # ─── Find correlated instrument ───────────────────────────────────
        correlations = CORRELATION_MAP.get(instrument, [])
        if not correlations:
            logger.debug(f"Hedge: no correlation map for {instrument}")
            return None

        # Find best available hedge instrument
        hedge_instrument = None
        correlation_sign = 0

        for corr_inst, corr_sign in correlations:
            # Check if correlated instrument is not already in use
            if open_positions and open_positions.get(corr_inst) is not None:
                continue
            # Check if we can get its price
            if self._broker and self._broker.available:
                try:
                    px = self._broker.get_current_price(corr_inst)
                    if px:
                        hedge_instrument = corr_inst
                        correlation_sign = corr_sign
                        break
                except Exception:
                    continue

        if not hedge_instrument:
            logger.debug(f"Hedge: no available correlated instrument for {instrument}")
            return None

        # ─── Compute hedge direction ──────────────────────────────────────
        # If original is BUY and correlation is +1 → hedge is SELL
        # If original is BUY and correlation is -1 → hedge is BUY
        if direction == "BUY":
            hedge_direction = "SELL" if correlation_sign > 0 else "BUY"
        else:
            hedge_direction = "BUY" if correlation_sign > 0 else "SELL"

        # ─── Compute hedge size ───────────────────────────────────────────
        hedge_size = round(size * HEDGE_SIZE_RATIO, 2)
        hedge_size = max(hedge_size, 0.01)

        # ─── Get hedge instrument price for SL/TP ─────────────────────────
        try:
            hedge_px = self._broker.get_current_price(hedge_instrument)
            hedge_entry = float(hedge_px.get("mid", 0))
            if hedge_entry <= 0:
                return None

            # ATR-based SL/TP for hedge (conservative)
            # Use 1% of price as rough ATR proxy
            atr_proxy = hedge_entry * 0.01
            if hedge_direction == "BUY":
                hedge_sl = round(hedge_entry - atr_proxy * 2, 5)
                hedge_tp = round(hedge_entry + atr_proxy * 3, 5)
            else:
                hedge_sl = round(hedge_entry + atr_proxy * 2, 5)
                hedge_tp = round(hedge_entry - atr_proxy * 3, 5)

        except Exception as e:
            logger.debug(f"Hedge price fetch {hedge_instrument}: {e}")
            return None

        hedge_instruction = {
            "type": "HEDGE",
            "original_instrument": instrument,
            "original_direction": direction,
            "hedge_instrument": hedge_instrument,
            "hedge_direction": hedge_direction,
            "hedge_size": hedge_size,
            "hedge_entry": hedge_entry,
            "hedge_sl": hedge_sl,
            "hedge_tp": hedge_tp,
            "correlation_sign": correlation_sign,
            "reason": f"Hedge {instrument} {direction} via {hedge_instrument} {hedge_direction}",
            "timestamp": time.time(),
            "expiry": time.time() + HEDGE_TIME_STOP_H * 3600,
        }

        logger.info(
            f"🛡️ HEDGE VIABLE: {instrument} {direction} → "
            f"{hedge_instrument} {hedge_direction} (corr={correlation_sign:+d}) | "
            f"size={hedge_size}"
        )

        return hedge_instruction

    # ═══════════════════════════════════════════════════════════════════════
    #  EXECUTE: Open the hedge
    # ═══════════════════════════════════════════════════════════════════════

    def execute_hedge(self, hedge: dict) -> str | None:
        """
        Execute a hedge order.

        Returns order reference or None if failed.
        """
        if not self._broker or not self._broker.available:
            return None

        try:
            ref = self._broker.place_market_order(
                epic=hedge["hedge_instrument"],
                direction=hedge["hedge_direction"],
                size=hedge["hedge_size"],
                sl_price=hedge["hedge_sl"],
                tp_price=hedge["hedge_tp"],
            )
            if ref:
                self._hedges_opened += 1
                self._active_hedges[hedge["original_instrument"]] = {
                    "ref": ref,
                    "hedge": hedge,
                    "opened_at": time.time(),
                }
                logger.info(
                    f"🛡️ HEDGE OPENED: {hedge['hedge_instrument']} "
                    f"{hedge['hedge_direction']} size={hedge['hedge_size']} | "
                    f"ref={ref}"
                )
                self._send_alert(
                    f"🛡️ <b>HEDGE OUVERT</b>\n\n"
                    f"📊 Original: {hedge['original_instrument']} {hedge['original_direction']}\n"
                    f"🔄 Hedge: <b>{hedge['hedge_instrument']} {hedge['hedge_direction']}</b>\n"
                    f"📏 Taille: {hedge['hedge_size']}\n"
                    f"⏱ Expiration: {HEDGE_TIME_STOP_H}h"
                )
                return ref
        except Exception as e:
            logger.error(f"Hedge execute failed: {e}")

        return None

    # ═══════════════════════════════════════════════════════════════════════
    #  MONITOR: Check and manage active hedges
    # ═══════════════════════════════════════════════════════════════════════

    def tick(self):
        """Called every tick to monitor active hedges."""
        now = time.time()
        expired = []

        for original_inst, state in self._active_hedges.items():
            hedge = state["hedge"]

            # Time-stop: auto-close after HEDGE_TIME_STOP_H
            if now > hedge["expiry"]:
                expired.append(original_inst)
                self._hedges_expired += 1
                logger.info(
                    f"⏰ HEDGE EXPIRED: {hedge['hedge_instrument']} "
                    f"(was hedging {original_inst}) — closing"
                )
                # Close the hedge
                try:
                    if self._broker and self._broker.available:
                        close_dir = "SELL" if hedge["hedge_direction"] == "BUY" else "BUY"
                        self._broker.place_market_order(
                            epic=hedge["hedge_instrument"],
                            direction=close_dir,
                            size=hedge["hedge_size"],
                        )
                except Exception as e:
                    logger.debug(f"Hedge close failed: {e}")

        for inst in expired:
            self._active_hedges.pop(inst, None)

    def on_hedge_closed(self, original_instrument: str, pnl: float):
        """Called when a hedge is closed (TP hit, SL hit, or manual)."""
        self._active_hedges.pop(original_instrument, None)
        self._total_hedge_pnl += pnl
        if pnl > 0:
            self._hedges_closed_profit += 1
        else:
            self._hedges_closed_loss += 1

    def is_hedged(self, instrument: str) -> bool:
        """Check if an instrument has an active hedge."""
        return instrument in self._active_hedges

    # ─── Status ──────────────────────────────────────────────────────────

    def format_status(self) -> str:
        active = len(self._active_hedges)
        return (
            f"🛡️ <b>Hedge Manager</b>\n"
            f"  🔄 Active: {active}/{MAX_HEDGES}\n"
            f"  📊 Opened: {self._hedges_opened}\n"
            f"  ✅ Profit: {self._hedges_closed_profit}\n"
            f"  ❌ Loss: {self._hedges_closed_loss}\n"
            f"  ⏰ Expired: {self._hedges_expired}\n"
            f"  💰 PnL: {self._total_hedge_pnl:+.2f}€"
        )

    @property
    def stats(self) -> dict:
        return {
            "active_hedges": len(self._active_hedges),
            "max_hedges": MAX_HEDGES,
            "opened": self._hedges_opened,
            "closed_profit": self._hedges_closed_profit,
            "closed_loss": self._hedges_closed_loss,
            "expired": self._hedges_expired,
            "total_pnl": self._total_hedge_pnl,
        }

    def _send_alert(self, text: str):
        if self._router:
            try:
                self._router.send_to("risk", text)
            except Exception:
                pass
