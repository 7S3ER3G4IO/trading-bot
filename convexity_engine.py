"""
convexity_engine.py — M38: Convexity & Dynamic Trailing Stop

Le Coupe-Sang : impose l'asymétrie positive (convexité).
- Gate pré-trade : R:R minimum 1:1.0 obligatoire
- SL basé sur ATR14 (1.5× ATR)
- Trailing stop dynamique par paliers de R

OVERRIDE ABSOLU : un trade non validé par M38 est un trade mort.
"""

import time
from dataclasses import dataclass
from typing import Optional, Dict, Tuple
from loguru import logger
import numpy as np


# ─── Configuration ────────────────────────────────────────────────────────────
MIN_RR_RATIO        = 1.0     # R:R minimum (aligné sur optimized_rules.json — backtesté)
ATR_SL_MULTIPLIER   = 1.5     # SL = entry ± ATR × 1.5
ATR_PERIOD           = 14      # Période ATR

# Trailing stop paliers (en multiples de R = risk initial)
# PnL atteint X × R → SL déplacé à entry + Y × R
TRAILING_LEVELS = [
    (0.5, 0.0),     # PnL > 0.5R → SL → Break-Even (entry)
    (1.0, 0.3),     # PnL > 1.0R → SL → entry + 0.3R
    (1.5, 0.8),     # PnL > 1.5R → SL → entry + 0.8R
    (2.0, 1.3),     # PnL > 2.0R → SL → entry + 1.3R
    (2.5, 1.8),     # PnL > 2.5R → SL → entry + 1.8R
    (3.0, 2.5),     # PnL > 3.0R → SL → entry + 2.5R (75%+ locked)
]


@dataclass
class TrailingState:
    """État du trailing stop pour une position."""
    instrument: str
    entry: float
    original_sl: float
    current_sl: float
    direction: str          # "BUY" or "SELL"
    risk_r: float           # 1R = abs(entry - original_sl)
    current_level: int      # Index dans TRAILING_LEVELS atteint
    last_update: float      # timestamp


class ConvexityEngine:
    """
    M38 — Convexity & Dynamic Trailing Stop.

    Deux responsabilités :
    1. Pre-trade gate : valide R:R ≥ 1.0 avant toute entrée
    2. In-trade trailing : déplace le SL dynamiquement par paliers de R
    """

    def __init__(self):
        self._trailing: Dict[str, TrailingState] = {}
        self._stats = {
            "trades_validated": 0,
            "trades_rejected_rr": 0,
            "trailing_updates": 0,
            "trailing_be_activated": 0,
        }
        logger.info(
            f"🛡️ M38 Convexity Engine initialisé | "
            f"min_RR={MIN_RR_RATIO} ATR_mult={ATR_SL_MULTIPLIER} "
            f"trailing_levels={len(TRAILING_LEVELS)}"
        )

    # ═══════════════════════════════════════════════════════════════════════
    #  1. PRE-TRADE GATE : R:R VALIDATION
    # ═══════════════════════════════════════════════════════════════════════

    def validate_rr(self, entry: float, sl: float, tp: float,
                    instrument: str = "") -> Tuple[bool, float]:
        """
        Valide que le R:R est ≥ MIN_RR_RATIO.

        Returns: (is_valid, actual_rr)
        """
        risk = abs(entry - sl)
        if risk <= 0:
            logger.warning(f"⛔ M38 {instrument}: risk=0 (entry≈SL) — REJETÉ")
            self._stats["trades_rejected_rr"] += 1
            return False, 0.0

        reward = abs(tp - entry)
        rr = reward / risk

        if rr < MIN_RR_RATIO - 0.001:  # epsilon tolerance for float precision
            logger.warning(
                f"⛔ M38 {instrument}: R:R={rr:.2f} < {MIN_RR_RATIO} — REJETÉ | "
                f"entry={entry:.5f} sl={sl:.5f} tp={tp:.5f}"
            )
            self._stats["trades_rejected_rr"] += 1
            return False, rr

        self._stats["trades_validated"] += 1
        logger.info(
            f"✅ M38 {instrument}: R:R={rr:.2f} ≥ {MIN_RR_RATIO} — VALIDÉ"
        )
        return True, rr

    def compute_atr_sl(self, df, entry: float, direction: str,
                       instrument: str = "") -> float:
        """
        Calcule le SL basé sur ATR14 × multiplier.

        Parameters:
            df: DataFrame with 'high', 'low', 'close' columns
            entry: prix d'entrée
            direction: "BUY" or "SELL"

        Returns: stop loss price
        """
        try:
            high = df["high"].values[-ATR_PERIOD:]
            low = df["low"].values[-ATR_PERIOD:]
            close = df["close"].values[-ATR_PERIOD:]

            if len(high) < ATR_PERIOD:
                logger.debug(f"M38 ATR: pas assez de données ({len(high)}/{ATR_PERIOD})")
                return entry  # fallback

            # True Range = max(H-L, |H-C_prev|, |L-C_prev|)
            tr = np.maximum(
                high[1:] - low[1:],
                np.maximum(
                    np.abs(high[1:] - close[:-1]),
                    np.abs(low[1:] - close[:-1])
                )
            )
            atr = float(np.mean(tr))
            sl_distance = atr * ATR_SL_MULTIPLIER

            if direction == "BUY":
                sl = entry - sl_distance
            else:
                sl = entry + sl_distance

            logger.debug(
                f"📐 M38 ATR SL {instrument}: ATR14={atr:.5f} × {ATR_SL_MULTIPLIER} = "
                f"distance={sl_distance:.5f} → SL={sl:.5f}"
            )
            return sl

        except Exception as e:
            logger.debug(f"M38 compute_atr_sl: {e}")
            return entry

    def compute_atr_tp(self, entry: float, sl: float, direction: str,
                       min_rr: float = MIN_RR_RATIO) -> float:
        """
        Calcule le TP minimum pour respecter le R:R.
        TP = entry ± risk × min_rr.
        """
        risk = abs(entry - sl)
        if direction == "BUY":
            return entry + risk * min_rr
        else:
            return entry - risk * min_rr

    def enforce_minimum_rr(self, entry: float, sl: float, tp: float,
                           direction: str, instrument: str = "") -> Tuple[float, float]:
        """
        Ajuste SL et TP si nécessaire pour garantir R:R ≥ MIN_RR_RATIO.
        Retourne (adjusted_sl, adjusted_tp).
        """
        risk = abs(entry - sl)
        if risk <= 0:
            return sl, tp

        reward = abs(tp - entry)
        current_rr = reward / risk

        if current_rr >= MIN_RR_RATIO:
            return sl, tp

        # Ajuste le TP pour atteindre le R:R minimum
        new_tp = self.compute_atr_tp(entry, sl, direction, MIN_RR_RATIO)
        logger.info(
            f"📐 M38 {instrument}: R:R={current_rr:.2f} → ajusté TP "
            f"{tp:.5f} → {new_tp:.5f} (R:R={MIN_RR_RATIO})"
        )
        return sl, new_tp

    # ═══════════════════════════════════════════════════════════════════════
    #  2. IN-TRADE TRAILING STOP
    # ═══════════════════════════════════════════════════════════════════════

    def register_trade(self, instrument: str, entry: float, sl: float,
                       direction: str):
        """Enregistre un nouveau trade pour le trailing stop."""
        risk_r = abs(entry - sl)
        if risk_r <= 0:
            return

        self._trailing[instrument] = TrailingState(
            instrument=instrument,
            entry=entry,
            original_sl=sl,
            current_sl=sl,
            direction=direction,
            risk_r=risk_r,
            current_level=-1,
            last_update=time.time(),
        )
        logger.debug(f"🎯 M38 trailing registered: {instrument} 1R={risk_r:.5f}")

    def update_trailing(self, instrument: str, current_price: float) -> Optional[float]:
        """
        Met à jour le trailing stop basé sur le prix actuel.

        Returns:
            - new_sl (float) si le SL doit être déplacé
            - None si pas de changement
        """
        state = self._trailing.get(instrument)
        if state is None:
            return None

        # Calcul du PnL en multiples de R
        if state.direction == "BUY":
            pnl_distance = current_price - state.entry
        else:
            pnl_distance = state.entry - current_price

        pnl_r = pnl_distance / state.risk_r if state.risk_r > 0 else 0

        # Chercher le niveau trailing le plus élevé atteint
        new_level = state.current_level
        new_sl = state.current_sl

        for i, (threshold_r, lock_r) in enumerate(TRAILING_LEVELS):
            if pnl_r >= threshold_r and i > state.current_level:
                new_level = i
                # Nouveau SL = entry + lock_r × R (direction-adjusted)
                if state.direction == "BUY":
                    new_sl = state.entry + lock_r * state.risk_r
                else:
                    new_sl = state.entry - lock_r * state.risk_r

        if new_level > state.current_level:
            old_sl = state.current_sl
            state.current_sl = new_sl
            state.current_level = new_level
            state.last_update = time.time()

            self._stats["trailing_updates"] += 1
            if new_level == 0:
                self._stats["trailing_be_activated"] += 1

            threshold_r, lock_r = TRAILING_LEVELS[new_level]
            logger.info(
                f"🔒 M38 Trailing {instrument}: PnL={pnl_r:.1f}R ≥ {threshold_r}R → "
                f"SL {old_sl:.5f} → {new_sl:.5f} (lock {lock_r}R)"
            )
            return new_sl

        return None

    def unregister_trade(self, instrument: str):
        """Supprime un trade fermé du tracking."""
        self._trailing.pop(instrument, None)

    def get_trailing_state(self, instrument: str) -> Optional[TrailingState]:
        """Retourne l'état trailing d'un instrument."""
        return self._trailing.get(instrument)

    # ═══════════════════════════════════════════════════════════════════════
    #  STATS
    # ═══════════════════════════════════════════════════════════════════════

    def stats(self) -> dict:
        return {
            **self._stats,
            "active_trailing": len(self._trailing),
            "min_rr": MIN_RR_RATIO,
            "atr_multiplier": ATR_SL_MULTIPLIER,
            "trailing_levels": len(TRAILING_LEVELS),
        }
