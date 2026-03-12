"""
slippage_injector.py — Étape 1 : Reality Slippage Injector.

Simule les conditions réelles du marché en mode DEMO:
  - Market order: dégradation de prix 0.05% → 0.15% selon imbalance simulé
  - Limit order: 10% de probabilité de "partial fill" (ordre effleuré)

Activé automatiquement quand CAPITAL_DEMO=true.

Usage:
    from slippage_injector import SlippageInjector
    inj = SlippageInjector()
    real_entry = inj.apply_market_slippage(entry, direction, ob_imbalance)
    filled, qty = inj.simulate_limit_fill(limit_price, current_price, qty)
"""
import os
import random
import math
from loguru import logger

# ─── Activation ──────────────────────────────────────────────────────────────
IS_DEMO = os.getenv("CAPITAL_DEMO", "true").lower() == "true"

# ─── Paramètres de pénalité ──────────────────────────────────────────────────
_SLIPPAGE_BASE     = 0.0005   # 0.05% minimum (marché calme)
_SLIPPAGE_MAX      = 0.0015   # 0.15% maximum (forte imbalance)
_LIMIT_SKIP_PROB   = 0.10     # 10% de probabilité de non-remplissage limite
_PARTIAL_FILL_PROB = 0.05     # 5% de partial fill (75%)


class SlippageInjector:
    """
    Injecteur de slippage probabiliste pour backtests + demo plus réalistes.

    Le slippage dépend de l'Order Book Imbalance simulé:
        imbalance=0   → slippage baseline (0.05%)
        imbalance=1   → slippage max (0.15%)
        imbalance=0.5 → interpolation linéaire
    """

    def __init__(self):
        self._enabled = IS_DEMO
        # Stats cumulées pour audit
        self._total_market  = 0
        self._total_slippage_cost = 0.0
        self._total_skipped_limits = 0

        if self._enabled:
            logger.info("💉 Slippage Injector ACTIF (mode DEMO) — pénalité 0.05%-0.15%")
        else:
            logger.info("✅ Slippage Injector désactivé (mode LIVE)")

    def apply_market_slippage(self, entry: float, direction: str,
                               ob_imbalance: float = 0.5) -> float:
        """
        Dégrade le prix d'entrée d'un ordre Market selon l'imbalance simulé.

        Args:
            entry: prix demandé
            direction: "BUY" ou "SELL"
            ob_imbalance: 0.0 (calme) → 1.0 (forte pression contre nous)

        Returns:
            Nouveau prix d'entrée dégradé.
        """
        if not self._enabled:
            return entry

        # Slippage proportionnel à l'imbalance + jitter aléatoire
        imbalance_factor = max(0.0, min(1.0, ob_imbalance))
        slippage_pct = _SLIPPAGE_BASE + (_SLIPPAGE_MAX - _SLIPPAGE_BASE) * imbalance_factor
        # Jitter: ±20% du slippage calculé
        slippage_pct *= random.uniform(0.80, 1.20)

        cost = entry * slippage_pct
        # BUY: on paye plus cher ; SELL: on vend moins cher
        if direction == "BUY":
            degraded = round(entry + cost, 5)
        else:
            degraded = round(entry - cost, 5)

        self._total_market        += 1
        self._total_slippage_cost += cost

        logger.debug(
            f"💉 Slippage Market: {direction} {entry:.5f} → {degraded:.5f} "
            f"({slippage_pct:.3%}) | imbalance={imbalance_factor:.2f}"
        )
        return degraded

    def simulate_limit_fill(self, limit_price: float, current_price: float,
                              requested_qty: float, direction: str = "BUY") -> tuple:
        """
        Simule le remplissage d'un ordre Limit.

        Returns:
            (filled_qty, actual_fill_price)
            filled_qty = 0 si ordre non rempli ("effleuré")
            filled_qty < requested_qty si partial fill
        """
        if not self._enabled:
            return requested_qty, limit_price

        price_touch = self._is_limit_touched(limit_price, current_price, direction)
        if not price_touch:
            return requested_qty, limit_price  # Prix pas encore atteint

        # Prix effleuré seulement (pas traversé)
        distance_pct = abs(current_price - limit_price) / limit_price
        is_barely_touched = distance_pct < 0.0003  # Moins de 0.03%

        if is_barely_touched:
            # 10% de chance de non-remplissage si juste effluré
            if random.random() < _LIMIT_SKIP_PROB:
                self._total_skipped_limits += 1
                logger.debug(
                    f"💉 Limit Skip: {direction} @ {limit_price:.5f} effluré "
                    f"(mid={current_price:.5f}) — non rempli (10% rule)"
                )
                return 0.0, limit_price

            # 5% de partial fill: seulement 75% de la quantité
            if random.random() < _PARTIAL_FILL_PROB:
                partial = round(requested_qty * 0.75, 2)
                logger.debug(f"💉 Partial Fill: {partial}/{requested_qty} @ {limit_price:.5f}")
                return partial, limit_price

        return requested_qty, limit_price

    def compute_adjusted_pnl(self, raw_pnl: float, entry: float,
                               direction: str, ob_imbalance: float = 0.5) -> float:
        """
        Recalcule le PnL en appliquant le slippage d'entrée ET de sortie.
        Pour les rapports shadow/démo: montre le PnL dans les pires conditions.
        """
        if not self._enabled:
            return raw_pnl

        # Slippage entrée + slippage sortie (approximation symétrique)
        slippage_pct = _SLIPPAGE_BASE + (_SLIPPAGE_MAX - _SLIPPAGE_BASE) * ob_imbalance
        slippage_cost = entry * slippage_pct * 2  # entrée + sortie
        sign = 1 if direction == "BUY" else -1
        return round(raw_pnl - slippage_cost * sign, 4)

    def stats(self) -> dict:
        avg_slip = (self._total_slippage_cost / self._total_market
                    if self._total_market > 0 else 0.0)
        return {
            "mode": "DEMO (actif)" if self._enabled else "LIVE (désactivé)",
            "market_orders_degraded": self._total_market,
            "avg_slippage_cost": round(avg_slip, 6),
            "limit_orders_skipped": self._total_skipped_limits,
            "total_slippage_cost": round(self._total_slippage_cost, 4),
        }

    def format_status(self) -> str:
        s = self.stats()
        if not self._enabled:
            return "💉 Slippage Injector: désactivé (mode LIVE)"
        return (
            f"💉 Slippage DEMO | Orders dégradés: {s['market_orders_degraded']} "
            f"| Coût moy: {s['avg_slippage_cost']:.5f} "
            f"| Limits skipped: {s['limit_orders_skipped']}"
        )

    # ─── Helpers ─────────────────────────────────────────────────────────────
    @staticmethod
    def _is_limit_touched(limit_price: float, current_price: float,
                           direction: str) -> bool:
        if direction == "BUY":
            return current_price <= limit_price
        return current_price >= limit_price
