"""
time_stop.py — M40: Time-Based Capitulation & Dead Capital Detection

Le Nettoyeur : libère le capital bloqué dans des trades stagnants.

Trois mécanismes :
1. Dead Capital : trade ouvert > 60min avec PnL < 0.3R → fermeture
2. Max Hold : dynamique par classe d'actif (CRYPTO=48h, TRADFI=24h)
3. FRIDAY KILL-SWITCH : ferme TOUTES positions TRADFI le vendredi 20h50 UTC

Un trade qui ne fait rien bloque le capital pour des opportunités explosives.
"""

import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from loguru import logger

try:
    from brokers.capital_client import (
        ASSET_CLASS_MAP, RISK_BY_CLASS,
        FRIDAY_KILLSWITCH_HOUR, FRIDAY_KILLSWITCH_MINUTE,
        get_asset_class, get_risk_params, ASSET_PROFILES,
    )
except ImportError:
    # Stubs for standalone testing
    ASSET_CLASS_MAP = {"crypto": "CRYPTO"}
    RISK_BY_CLASS = {
        "CRYPTO": {"time_stop_h": 48, "rr_min": 1.5},
        "TRADFI": {"time_stop_h": 24, "rr_min": 1.2},
    }
    FRIDAY_KILLSWITCH_HOUR = 20
    FRIDAY_KILLSWITCH_MINUTE = 50
    ASSET_PROFILES = {}
    def get_asset_class(i): return "TRADFI"
    def get_risk_params(i): return RISK_BY_CLASS["TRADFI"]


# ─── Configuration ────────────────────────────────────────────────────────────
STAGNATION_MIN_AGE     = 60      # Minutes avant de vérifier la stagnation
STAGNATION_PNL_THRESHOLD = 0.3   # PnL < 0.3R = stagnant
CHECK_INTERVAL         = 300     # Vérifier toutes les 5 minutes


class DeadCapitalDetector:
    """
    M40 — Time-Based Capitulation & Dead Capital Detection.

    Détecte les trades stagnants, applique le time stop dynamique
    par classe d'actif, et exécute le Friday Kill-Switch.
    """

    def __init__(self):
        self._last_check: Dict[str, float] = {}
        self._stats = {
            "stagnation_kills": 0,
            "time_stop_kills": 0,
            "friday_kills": 0,
            "checks_performed": 0,
            "capital_freed": 0,
        }
        logger.info(
            f"⏱️ M40 Dead Capital Detector initialisé | "
            f"CRYPTO={RISK_BY_CLASS['CRYPTO']['time_stop_h']}h "
            f"TRADFI={RISK_BY_CLASS['TRADFI']['time_stop_h']}h "
            f"Friday Kill=Ven {FRIDAY_KILLSWITCH_HOUR}:{FRIDAY_KILLSWITCH_MINUTE:02d} UTC"
        )

    # ─── Friday Kill-Switch ──────────────────────────────────────────────

    def is_friday_killswitch(self, now_utc: datetime = None) -> bool:
        """Retourne True si on est Vendredi >= 20:50 UTC."""
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        # weekday() : 0=Lundi, 4=Vendredi
        if now_utc.weekday() != 4:
            return False
        if now_utc.hour > FRIDAY_KILLSWITCH_HOUR:
            return True
        if (now_utc.hour == FRIDAY_KILLSWITCH_HOUR and
                now_utc.minute >= FRIDAY_KILLSWITCH_MINUTE):
            return True
        return False

    def friday_scan(
        self,
        open_trades: Dict[str, dict],
        now_utc: datetime = None,
    ) -> List[str]:
        """
        FRIDAY KILL-SWITCH : retourne la liste des instruments TRADFI
        qui doivent être fermés immédiatement (vendredi >= 20:50 UTC).

        Returns:
            list of instrument names to close
        """
        if not self.is_friday_killswitch(now_utc):
            return []

        to_kill = []
        for instrument, state in open_trades.items():
            if state is None:
                continue
            cls = get_asset_class(instrument)
            if cls == "TRADFI":
                to_kill.append(instrument)
                self._stats["friday_kills"] += 1
                logger.warning(
                    f"🔴 M40 FRIDAY KILL-SWITCH {instrument} ({cls}) | "
                    f"Vendredi {FRIDAY_KILLSWITCH_HOUR}:{FRIDAY_KILLSWITCH_MINUTE:02d} UTC "
                    f"→ FERMETURE IMMÉDIATE (Weekend Gap Risk)"
                )

        if to_kill:
            logger.warning(
                f"🔴 FRIDAY KILL-SWITCH : {len(to_kill)} positions TRADFI fermées "
                f"({', '.join(to_kill)})"
            )

        return to_kill

    # ─── Dynamic Time Stop ──────────────────────────────────────────────

    def _get_max_hold_min(self, instrument: str, override: int = None) -> int:
        """
        Max hold en minutes, dynamique par classe d'actif.
        CRYPTO = 48h (2880 min), TRADFI = 24h (1440 min).
        """
        if override is not None:
            return override
        risk = get_risk_params(instrument)
        return int(risk["time_stop_h"] * 60)

    # ─── Main Check ──────────────────────────────────────────────────────

    def check_stagnation(
        self,
        instrument: str,
        current_price: float,
        state: dict,
        max_hold_min: int = None,
    ) -> Tuple[bool, str]:
        """
        Vérifie si un trade est stagnant ou a dépassé le max hold.

        Parameters:
            instrument: identifiant de l'instrument
            current_price: prix actuel
            state: dict du trade {entry, sl, direction, open_time, ...}
            max_hold_min: override durée max en minutes (None = dynamique)

        Returns:
            (should_close, reason)
        """
        if state is None:
            return False, ""

        # Rate limit: ne pas checker trop souvent le même instrument
        now = time.time()
        last = self._last_check.get(instrument, 0)
        if now - last < CHECK_INTERVAL:
            return False, ""
        self._last_check[instrument] = now
        self._stats["checks_performed"] += 1

        open_time = state.get("open_time")
        if not open_time:
            return False, ""

        # Calcul de l'âge du trade
        if isinstance(open_time, datetime):
            age_min = (datetime.now(timezone.utc) - open_time).total_seconds() / 60
        else:
            return False, ""

        entry = state.get("entry", 0)
        sl = state.get("sl", entry)
        direction = state.get("direction", "BUY")

        # Calcul du PnL en multiples de R
        risk_r = abs(entry - sl)
        if risk_r <= 0:
            risk_r = abs(entry) * 0.001  # fallback 0.1%

        if direction == "BUY":
            pnl_distance = current_price - entry
        else:
            pnl_distance = entry - current_price

        pnl_r = pnl_distance / risk_r if risk_r > 0 else 0

        cls = get_asset_class(instrument)

        # ─── Check 0: Friday Kill-Switch ────────────────────────────────
        if cls == "TRADFI" and self.is_friday_killswitch():
            self._stats["friday_kills"] += 1
            reason = (
                f"🔴 M40 FRIDAY KILL {instrument} ({cls}): "
                f"PnL={pnl_r:+.2f}R → FERMETURE (Weekend Gap Risk)"
            )
            logger.warning(reason)
            return True, reason

        # ─── Check 1: Max Hold (dynamique par classe) ──────────────────
        effective_max_hold = self._get_max_hold_min(instrument, max_hold_min)
        if age_min >= effective_max_hold:
            self._stats["time_stop_kills"] += 1
            reason = (
                f"⏱️ M40 TIME-STOP {instrument} ({cls}): "
                f"ouvert {age_min:.0f}min ≥ {effective_max_hold}min → FERMETURE"
            )
            logger.warning(reason)
            return True, reason

        # ─── Check 2: Dead Capital (stagnation) ────────────────────────
        if age_min >= STAGNATION_MIN_AGE:
            abs_pnl_r = abs(pnl_r)
            if abs_pnl_r < STAGNATION_PNL_THRESHOLD:
                self._stats["stagnation_kills"] += 1
                reason = (
                    f"💀 M40 DEAD CAPITAL {instrument} ({cls}): "
                    f"{age_min:.0f}min ouvert, PnL={pnl_r:+.2f}R "
                    f"(< {STAGNATION_PNL_THRESHOLD}R) → FERMETURE"
                )
                logger.warning(reason)
                return True, reason

        return False, ""

    def get_dead_capital_report(self, trades: Dict[str, Optional[dict]],
                                 prices: Dict[str, float]) -> List[dict]:
        """
        Scan tous les trades ouverts et retourne les stagnants.

        Returns:
            Liste de dicts {instrument, age_min, pnl_r, status, asset_class}
        """
        dead = []
        now = datetime.now(timezone.utc)

        for instrument, state in trades.items():
            if state is None:
                continue

            open_time = state.get("open_time")
            if not open_time or not isinstance(open_time, datetime):
                continue

            age_min = (now - open_time).total_seconds() / 60
            entry = state.get("entry", 0)
            sl = state.get("sl", entry)
            direction = state.get("direction", "BUY")
            current = prices.get(instrument, entry)

            risk_r = abs(entry - sl) or abs(entry) * 0.001
            if direction == "BUY":
                pnl_r = (current - entry) / risk_r
            else:
                pnl_r = (entry - current) / risk_r

            cls = get_asset_class(instrument)
            max_hold = self._get_max_hold_min(instrument)

            status = "HEALTHY"
            if age_min >= STAGNATION_MIN_AGE and abs(pnl_r) < STAGNATION_PNL_THRESHOLD:
                status = "DEAD_CAPITAL"
            elif age_min >= max_hold * 0.8:
                status = "NEAR_TIMEOUT"

            if status != "HEALTHY":
                dead.append({
                    "instrument": instrument,
                    "age_min": round(age_min),
                    "pnl_r": round(pnl_r, 2),
                    "status": status,
                    "asset_class": cls,
                    "max_hold_h": max_hold / 60,
                })

        return dead

    def stats(self) -> dict:
        return {
            **self._stats,
            "stagnation_min_age": STAGNATION_MIN_AGE,
            "stagnation_threshold_r": STAGNATION_PNL_THRESHOLD,
            "risk_by_class": RISK_BY_CLASS,
        }
