"""
risk_manager.py — Risk Management Avancé (R-3 + R-4).

R-4: Dynamic Drawdown — DD limit adapté à la volatilité du marché.
R-3: Kill-Switches Multi-Niveaux — protection granulaire.
"""

import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from loguru import logger
from config import (
    RISK_PER_TRADE,
    MAX_OPEN_TRADES,
    DAILY_DRAWDOWN_LIMIT,
)


# ─── R-4: Dynamic DD Configuration ───────────────────────────────────────────
DD_BASE_PCT          = 5.0    # DD de base (marché calme)
DD_MAX_PCT           = 15.0   # DD plafond (marché très volatile)
DD_VOLATILITY_SCALE  = 2.0    # Multiplicateur VIX → DD extension


class RiskManager:
    """Contrôle d'accès au trading + kill-switches + dynamic DD."""

    def __init__(self, initial_balance: float):
        self.initial_balance      = initial_balance
        self.daily_start_balance  = initial_balance
        self._open_trades_count   = 0
        self._open_instruments: set = set()

        # ─── R-3: Kill-Switch State ───────────────────────────────────────
        # R-3a: Hourly consecutive losses
        self._hourly_losses: list      = []   # timestamps of losses in last hour
        self._hourly_pause_until: float = 0   # epoch until which trading is paused

        # R-3b: Category blacklist
        # {category: [loss_timestamps]}
        self._category_losses: dict    = defaultdict(list)
        self._category_blocked: dict   = {}   # {category: unblock_epoch}

        # R-3c: Intraday DD per hour
        self._hour_start_balance: float = initial_balance
        self._hour_start_time: float    = time.time()
        self._hourly_dd_pause_until: float = 0

        # R-3d: Max orders per day
        self._daily_order_count: int    = 0
        self._daily_order_limit: int    = 150  # 30 trades × 3 positions + buffer

        # ─── R-4: Dynamic DD ─────────────────────────────────────────────
        self._current_dd_limit: float   = DAILY_DRAWDOWN_LIMIT  # fallback
        self._vix_synthetic: float      = 0.0

        # ─── R-1: Kelly Fraction Tracker ─────────────────────────────────
        # {instrument: [{"pnl": float, "risk": float}]} — rolling 50 trades
        self._trade_history: dict       = defaultdict(list)
        self._kelly_cache: dict         = {}  # {instrument: kelly_fraction}

        # ─── R-2: Currency Exposure Tracker ──────────────────────────────
        # Tracks net exposure by currency across all open positions
        self._CURRENCY_MAP = {
            "EURUSD": ("EUR", "USD"), "GBPUSD": ("GBP", "USD"), "USDJPY": ("USD", "JPY"),
            "GBPJPY": ("GBP", "JPY"), "EURJPY": ("EUR", "JPY"), "USDCHF": ("USD", "CHF"),
            "AUDNZD": ("AUD", "NZD"), "AUDJPY": ("AUD", "JPY"), "NZDJPY": ("NZD", "JPY"),
            "EURCHF": ("EUR", "CHF"), "CHFJPY": ("CHF", "JPY"), "AUDUSD": ("AUD", "USD"),
            "NZDUSD": ("NZD", "USD"), "EURGBP": ("EUR", "GBP"), "EURAUD": ("EUR", "AUD"),
            "GBPAUD": ("GBP", "AUD"), "AUDCAD": ("AUD", "CAD"), "GBPCAD": ("GBP", "CAD"),
            "GBPCHF": ("GBP", "CHF"), "CADCHF": ("CAD", "CHF"),
        }
        # Current open positions: {instrument: direction}
        self._open_positions: dict      = {}


    # ═══════════════════════════════════════════════════════════════════════
    #  R-4: DYNAMIC DRAWDOWN
    # ═══════════════════════════════════════════════════════════════════════

    def update_vix_synthetic(self, atr_values: dict):
        """
        Recalcule le VIX synthétique à partir des ATR normalisés des instruments.
        
        Parameters
        ----------
        atr_values : dict
            {instrument: (atr, close)} — ATR et close pour normaliser.
        
        Le VIX synthétique = moyenne des (ATR/close) × 100 sur tous les instruments.
        Marché calme : ~0.3-0.5%  |  Marché volatile : ~1.5-3.0%
        """
        if not atr_values:
            return

        normalized = []
        for instr, (atr, close) in atr_values.items():
            if close > 0 and atr > 0:
                normalized.append(atr / close * 100)

        if not normalized:
            return

        self._vix_synthetic = sum(normalized) / len(normalized)

        # R-4: DD limit dynamique
        # Formule : base + (vix - baseline) × scale, clamped [5%, 15%]
        vix_baseline = 0.5  # marché "normal"
        vix_excess = max(0, self._vix_synthetic - vix_baseline)
        dynamic_dd = DD_BASE_PCT + vix_excess * DD_VOLATILITY_SCALE
        self._current_dd_limit = max(DD_BASE_PCT, min(DD_MAX_PCT, dynamic_dd))

        logger.debug(
            f"📊 VIX synth={self._vix_synthetic:.2f}% → DD limit={self._current_dd_limit:.1f}%"
        )

    @property
    def dynamic_dd_limit(self) -> float:
        """Retourne le DD limit dynamique actuel (en %)."""
        return self._current_dd_limit

    @property
    def vix_synthetic(self) -> float:
        return self._vix_synthetic

    # ═══════════════════════════════════════════════════════════════════════
    #  R-3: KILL-SWITCHES
    # ═══════════════════════════════════════════════════════════════════════

    def record_loss(self, instrument: str, category: str = "forex"):
        """Enregistre un trade perdant pour les kill-switches."""
        now = time.time()
        self._hourly_losses.append(now)
        self._category_losses[category].append(now)

        # Cleanup : garder seulement la dernière heure
        cutoff_1h = now - 3600
        self._hourly_losses = [t for t in self._hourly_losses if t > cutoff_1h]
        self._category_losses[category] = [
            t for t in self._category_losses[category] if t > cutoff_1h * 24  # 24h for category
        ]

    def record_order(self):
        """Incrémente le compteur d'ordres journalier."""
        self._daily_order_count += 1

    def check_kill_switches(self, current_balance: float) -> tuple:
        """
        Vérifie tous les kill-switches.
        Retourne (blocked: bool, reason: str).
        """
        now = time.time()

        # R-3a: Hourly consecutive losses (>= 3 en 1h → pause 30min)
        if len(self._hourly_losses) >= 3 and now < self._hourly_pause_until:
            remaining = int((self._hourly_pause_until - now) / 60)
            return True, f"⏸️ Kill-Switch Horaire : 3+ SL/heure — pause {remaining}min restantes"
        if len(self._hourly_losses) >= 3 and self._hourly_pause_until <= now:
            self._hourly_pause_until = now + 30 * 60  # 30 min
            reason = "🚨 Kill-Switch Horaire activé : 3+ SL en 1h — pause 30min"
            logger.warning(reason)
            return True, reason

        # R-3c: Intraday DD per hour (>3% en 1h → pause 1h)
        if now - self._hour_start_time >= 3600:
            self._hour_start_balance = current_balance
            self._hour_start_time = now
        if self._hour_start_balance > 0 and current_balance > 0:
            hourly_dd = (self._hour_start_balance - current_balance) / self._hour_start_balance * 100
            if hourly_dd >= 3.0:
                if now < self._hourly_dd_pause_until:
                    remaining = int((self._hourly_dd_pause_until - now) / 60)
                    return True, f"⏸️ DD Horaire : -{hourly_dd:.1f}% en 1h — pause {remaining}min restantes"
                self._hourly_dd_pause_until = now + 60 * 60  # 1h
                reason = f"🚨 Kill-Switch DD Horaire : -{hourly_dd:.1f}% en 1h — pause 1h"
                logger.warning(reason)
                return True, reason

        # R-3d: Max orders per day
        if self._daily_order_count >= self._daily_order_limit:
            return True, f"⛔ Max ordres/jour atteint ({self._daily_order_count}/{self._daily_order_limit})"

        return False, ""

    def is_category_blocked(self, category: str) -> bool:
        """R-3b: Vérifie si une catégorie est blacklistée."""
        now = time.time()

        # Check existing block
        if category in self._category_blocked:
            if now < self._category_blocked[category]:
                return True
            else:
                del self._category_blocked[category]

        # Check if should block (5+ losses in 24h)
        cutoff_24h = now - 86400
        recent = [t for t in self._category_losses.get(category, []) if t > cutoff_24h]
        if len(recent) >= 5:
            self._category_blocked[category] = now + 86400  # 24h
            logger.warning(f"🚨 Kill-Switch Catégorie : {category} blacklisté 24h ({len(recent)} SL)")
            return True

        return False

    # ═══════════════════════════════════════════════════════════════════════
    #  CONTRÔLE D'ACCÈS (upgraded)
    # ═══════════════════════════════════════════════════════════════════════

    def can_open_trade(self, current_balance: float, instrument: str = "",
                       category: str = "forex") -> bool:
        """Check complet : max trades + DD + kill-switches + category."""
        if self._open_trades_count >= MAX_OPEN_TRADES:
            logger.warning(f"⛔ Max {MAX_OPEN_TRADES} trades simultanés atteint.")
            return False

        if instrument and instrument in self._open_instruments:
            logger.warning(f"⛔ {instrument} : trade déjà ouvert sur cet instrument.")
            return False

        # R-4: Dynamic DD check
        drawdown = (current_balance - self.daily_start_balance) / self.daily_start_balance
        dd_pct = abs(drawdown) * 100
        if drawdown < 0 and dd_pct >= self._current_dd_limit:
            logger.warning(
                f"⛔ DD dynamique atteint ({dd_pct:.1f}% ≥ {self._current_dd_limit:.1f}%). "
                f"VIX={self._vix_synthetic:.2f}%"
            )
            return False

        # R-3: Kill-switches
        blocked, reason = self.check_kill_switches(current_balance)
        if blocked:
            logger.warning(reason)
            return False

        # R-3b: Category blacklist
        if category and self.is_category_blocked(category):
            logger.warning(f"⛔ Catégorie {category} blacklistée — skip {instrument}")
            return False

        return True

    # ═══════════════════════════════════════════════════════════════════════
    #  R-1: KELLY FRACTION ADAPTIVE SIZING
    # ═══════════════════════════════════════════════════════════════════════

    def record_trade_result(self, instrument: str, pnl: float, risk_amount: float):
        """Enregistre le résultat d'un trade pour le Kelly tracker."""
        self._trade_history[instrument].append({"pnl": pnl, "risk": risk_amount})
        # Rolling window : garder les 50 derniers trades
        if len(self._trade_history[instrument]) > 50:
            self._trade_history[instrument] = self._trade_history[instrument][-50:]
        # Invalidate cache
        self._kelly_cache.pop(instrument, None)

    def _compute_kelly(self, instrument: str) -> float:
        """
        Calcule le Kelly fraction pour un instrument.
        K = (WR × avg_win - (1-WR) × avg_loss) / avg_win
        Retourne half-Kelly clamped [0.0025, 0.020] (0.25% - 2.0%).
        """
        if instrument in self._kelly_cache:
            return self._kelly_cache[instrument]

        history = self._trade_history.get(instrument, [])
        if len(history) < 10:
            # Pas assez de données → risk par défaut
            return RISK_PER_TRADE  # 0.005

        wins = [t for t in history if t["pnl"] > 0]
        losses = [t for t in history if t["pnl"] <= 0]

        if not wins or not losses:
            return RISK_PER_TRADE

        wr = len(wins) / len(history)
        avg_win = sum(t["pnl"] for t in wins) / len(wins)
        avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses))

        if avg_win <= 0:
            return RISK_PER_TRADE

        kelly = (wr * avg_win - (1 - wr) * avg_loss) / avg_win
        # Half-Kelly (conservative) clamped [0.25%, 2.0%]
        half_kelly = max(0.0025, min(0.020, kelly / 2))

        self._kelly_cache[instrument] = half_kelly
        return half_kelly

    def compute_risk_pct(self, instrument: str, score: float = 0.5,
                         current_atr: float = 0, avg_atr: float = 0) -> float:
        """
        R-1: Calcule le risk% dynamique pour un trade.
        
        Formule: risk = kelly × score_adj × vol_adj
        - kelly: half-Kelly fraction (0.25% - 2.0%)
        - score_adj: signal score (0.4-1.0 → scaling)
        - vol_adj: avg_ATR / current_ATR (ATR élevé → sizing réduit)
        
        Returns: risk percentage as decimal (e.g., 0.005 = 0.5%)
        """
        kelly = self._compute_kelly(instrument)

        # Score adjustment: faible score → réduction proportionnelle
        # score=0.40 (threshold) → ×0.7 | score=1.0 → ×1.0
        score_adj = 0.5 + score * 0.5  # range [0.7, 1.0] for score [0.4, 1.0]

        # Volatility adjustment: haute volatilité → sizing réduit
        vol_adj = 1.0
        if current_atr > 0 and avg_atr > 0:
            vol_adj = min(1.5, max(0.5, avg_atr / current_atr))

        risk = kelly * score_adj * vol_adj

        # Final clamp [0.25%, 2.0%]
        risk = max(0.0025, min(0.020, risk))

        logger.debug(
            f"📐 R-1 {instrument}: kelly={kelly:.4f} × score={score_adj:.2f} "
            f"× vol={vol_adj:.2f} → risk={risk:.4f} ({risk*100:.2f}%)"
        )
        return risk

    # ═══════════════════════════════════════════════════════════════════════
    #  R-2: CURRENCY EXPOSURE
    # ═══════════════════════════════════════════════════════════════════════

    def check_currency_exposure(self, instrument: str, direction: str,
                                 open_trades: dict) -> bool:
        """
        R-2: Vérifie l'exposition par devise.
        Bloque si > 3 trades dans la même devise, même direction.
        
        Parameters
        ----------
        instrument : str — l'instrument à ouvrir
        direction : str — "BUY" or "SELL"
        open_trades : dict — {instrument: state_dict_or_None}
        
        Returns True if OK to trade, False if blocked.
        """
        currencies = self._CURRENCY_MAP.get(instrument)
        if not currencies:
            return True  # Non-forex → pas de check devise

        base_ccy, quote_ccy = currencies

        # Déterminer l'exposition nette par devise à partir des positions ouvertes
        # BUY EURUSD = long EUR, short USD
        # SELL EURUSD = short EUR, long USD
        exposure = defaultdict(int)  # {currency: net_count} (+1=long, -1=short)

        for instr, state in open_trades.items():
            if state is None:
                continue
            ccys = self._CURRENCY_MAP.get(instr)
            if not ccys:
                continue
            base, quote = ccys
            dir_sign = 1 if state.get("direction") == "BUY" else -1
            exposure[base] += dir_sign
            exposure[quote] -= dir_sign

        # Simuler l'ajout du nouveau trade
        new_sign = 1 if direction == "BUY" else -1
        sim_base = exposure.get(base_ccy, 0) + new_sign
        sim_quote = exposure.get(quote_ccy, 0) - new_sign

        # Bloquer si abs exposure > 3 dans n'importe quelle devise
        MAX_CURRENCY_EXPOSURE = 3
        if abs(sim_base) > MAX_CURRENCY_EXPOSURE:
            logger.info(
                f"⛔ R-2 Exposure {base_ccy}: {sim_base:+d} > ±{MAX_CURRENCY_EXPOSURE} "
                f"— {instrument} {direction} bloqué"
            )
            return False
        if abs(sim_quote) > MAX_CURRENCY_EXPOSURE:
            logger.info(
                f"⛔ R-2 Exposure {quote_ccy}: {sim_quote:+d} > ±{MAX_CURRENCY_EXPOSURE} "
                f"— {instrument} {direction} bloqué"
            )
            return False

        return True

    # ─── COMPTEURS ───────────────────────────────────────────────────────────

    def on_trade_opened(self, instrument: str = ""):
        self._open_trades_count += 1
        if instrument:
            self._open_instruments.add(instrument)
        self.record_order()

    def on_trade_closed(self, instrument: str = ""):
        self._open_trades_count = max(0, self._open_trades_count - 1)
        self._open_instruments.discard(instrument)

    def reset_daily(self, current_balance: float):
        self.daily_start_balance = current_balance
        self._daily_order_count = 0
        self._hourly_losses.clear()
        self._hour_start_balance = current_balance
        self._hour_start_time = time.time()
        logger.info(f"🔄 Balance journalière reset : {current_balance:.2f} €")

    @property
    def open_trades_count(self) -> int:
        return self._open_trades_count

    @property
    def kill_switch_status(self) -> dict:
        """Status pour dashboard/monitoring."""
        now = time.time()
        return {
            "hourly_losses": len(self._hourly_losses),
            "hourly_paused": now < self._hourly_pause_until,
            "dd_limit_pct": self._current_dd_limit,
            "vix_synthetic": self._vix_synthetic,
            "daily_orders": self._daily_order_count,
            "daily_order_limit": self._daily_order_limit,
            "categories_blocked": list(self._category_blocked.keys()),
        }
