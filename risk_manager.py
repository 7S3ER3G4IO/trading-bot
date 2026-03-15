"""
risk_manager.py — Risk Management Avancé (R-3 + R-4).

R-4: Dynamic Drawdown — DD limit adapté à la volatilité du marché.
R-3: Kill-Switches Multi-Niveaux — protection granulaire.
"""

import time
import os
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from loguru import logger
from config import (
    RISK_PER_TRADE,
    MAX_OPEN_TRADES,
    DAILY_DRAWDOWN_LIMIT,
    MAX_EFFECTIVE_LEVERAGE,
    ASSET_MARGIN_REQUIREMENTS,
)

# Limite de drawdown mensuel (circuit breaker long terme)
MONTHLY_DRAWDOWN_LIMIT = float(os.getenv("MONTHLY_DD_LIMIT", "15.0"))  # 15% du capital de début de mois

# ─── Conviction Sizing Thresholds ────────────────────────────────────────────
CONVICTION_TIERS = {
    "ELITE":   {"min_pf": 2.5, "risk": 0.015},  # PF≥2.5 → 1.5% risk (GOLD class)
    "STRONG":  {"min_pf": 1.8, "risk": 0.012},  # PF≥1.8 → 1.2% risk
    "SOLID":   {"min_pf": 1.2, "risk": 0.010},  # PF≥1.2 → 1.0% risk
    "BASE":    {"min_pf": 0.0, "risk": 0.010},  # default → 1.0% risk (V1 Ultimate)
}
NLP_BONUS_THRESHOLD   = 0.8    # FinBERT impact score threshold
NLP_BONUS_MULTIPLIER  = 1.20   # +20% size on strong NLP confirmation
MARGIN_SAFETY_BUFFER  = 0.90   # Use max 90% of broker margin (10% safety)

# ─── Tâche 3: Portfolio Heat Manager ─────────────────────────────────────────
# Plafond d'exposition globale : 5% du capital total en risque cumulé (Prop Firm safe)
MAX_PORTFOLIO_RISK    = 0.05    # 5% max risque portefeuille cumulé (Prop Firm rule)
MAX_OPEN_POSITIONS    = 5      # Max positions simultanées
MAX_CORRELATED_SAME_DIR = 3    # Max positions même direction dans un groupe corrélé

# Groupes de corrélation (même direction = risque concentré)
CORRELATION_GROUPS = {
    "usd_majors":  {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"},
    "jpy_crosses": {"USDJPY", "EURJPY", "GBPJPY", "CADJPY", "CHFJPY", "NZDJPY"},
    "chf_crosses": {"USDCHF", "EURCHF", "GBPCHF", "CADCHF", "AUDCHF"},
    "cad_crosses": {"USDCAD", "AUDCAD", "GBPCAD", "NZDCAD"},
    "us_indices":  {"US500", "US100"},
    "oil":         {"OIL_BRENT", "OIL_WTI"},
    "major_crypto":{"BTCUSD", "ETHUSD"},
}


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

        # ─── Conviction PF cache ──────────────────────────────────────
        self._pf_cache: dict            = {}  # {instrument: profit_factor}
        self._pf_loaded: bool           = False

        # ─── Monthly DD circuit breaker ───────────────────────────────────
        self._monthly_start_balance: float = initial_balance
        self._monthly_dd_limit: float      = MONTHLY_DRAWDOWN_LIMIT

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
        cutoff_1h  = now - 3600
        cutoff_24h = now - 86400  # FIX: était cutoff_1h*24 (incorrect)
        self._hourly_losses = [t for t in self._hourly_losses if t > cutoff_1h]
        self._category_losses[category] = [
            t for t in self._category_losses[category] if t > cutoff_24h
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
        from config import MAX_OPEN_TRADES as _MAX_TRADES  # source de vérité unique
        if self._open_trades_count >= _MAX_TRADES:
            logger.warning(f"⛔ Max {MAX_OPEN_TRADES} trades simultanés atteint.")
            return False

        if instrument and instrument in self._open_instruments:
            logger.warning(f"⛔ {instrument} : trade déjà ouvert sur cet instrument.")
            return False

        # R-4: Dynamic DD check (journalier)
        if self.daily_start_balance > 0:
            drawdown = (current_balance - self.daily_start_balance) / self.daily_start_balance
            dd_pct = abs(drawdown) * 100
            if drawdown < 0 and dd_pct >= self._current_dd_limit:
                logger.warning(
                    f"DD dynamique journalier atteint ({dd_pct:.1f}% >= {self._current_dd_limit:.1f}%). "
                    f"VIX={self._vix_synthetic:.2f}%"
                )
                return False

        # R-4b: Total DD kill switch (Prop Firm rule — depuis balance initiale)
        from config import TOTAL_DRAWDOWN_LIMIT
        if self.initial_balance > 0 and current_balance > 0:
            total_dd = (self.initial_balance - current_balance) / self.initial_balance * 100
            if total_dd >= TOTAL_DRAWDOWN_LIMIT:
                logger.critical(
                    f"🚨 TOTAL DD KILL SWITCH : {total_dd:.2f}% >= {TOTAL_DRAWDOWN_LIMIT:.0f}% "
                    f"(balance initiale={self.initial_balance:.2f} / actuelle={current_balance:.2f}) "
                    f"— TRADING DEFINITIVEMENT BLOQUE (Prop Firm rule)"
                )
                return False

        # Monthly DD circuit breaker
        if self._monthly_start_balance > 0 and current_balance > 0:
            monthly_dd = (self._monthly_start_balance - current_balance) / self._monthly_start_balance * 100
            if monthly_dd >= self._monthly_dd_limit:
                logger.warning(
                    f"DD mensuel atteint ({monthly_dd:.1f}% >= {self._monthly_dd_limit:.1f}%) "
                    f"— trading bloque jusqu'au debut du mois prochain."
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
    #  TÂCHE 3: PORTFOLIO HEAT CHECK
    # ═══════════════════════════════════════════════════════════════════════

    def portfolio_heat_check(self, instrument: str, direction: str,
                             open_trades: dict, risk_pct: float = 0.005) -> tuple:
        """
        Vérifie l'exposition globale du portefeuille.

        Rules:
          1. Max 5 positions simultanées
          2. Max 5% risque cumulé
          3. Max 3 positions même direction dans un groupe corrélé

        Returns: (ok: bool, reason: str)
        """
        # Count open positions
        open_count = sum(1 for v in open_trades.values() if v is not None)

        # Rule 1: Max positions
        if open_count >= MAX_OPEN_POSITIONS:
            reason = f"Portfolio Heat: {open_count}/{MAX_OPEN_POSITIONS} positions max"
            logger.warning(f"🔥 {reason}")
            return False, reason

        # Rule 2: Cumulative risk
        cum_risk = (open_count + 1) * risk_pct
        if cum_risk > MAX_PORTFOLIO_RISK:
            reason = (
                f"Portfolio Risk: {cum_risk:.1%} > {MAX_PORTFOLIO_RISK:.0%} "
                f"({open_count+1} × {risk_pct:.2%})"
            )
            logger.warning(f"🔥 {reason}")
            return False, reason

        # Rule 3: Correlation group — same direction cap
        for group_name, members in CORRELATION_GROUPS.items():
            if instrument not in members:
                continue
            same_dir_count = 0
            for m in members:
                if m == instrument:
                    continue
                state = open_trades.get(m)
                if state is not None and state.get("direction") == direction:
                    same_dir_count += 1
            if same_dir_count >= MAX_CORRELATED_SAME_DIR:
                reason = (
                    f"Correlated Heat: {same_dir_count} {direction} dans '{group_name}' "
                    f"(max {MAX_CORRELATED_SAME_DIR})"
                )
                logger.warning(f"🔥 {reason}")
                return False, reason

        # Rule 4: Sector exposure — max 30% du capital sur une meme categorie d'actif
        # (forex / crypto / indices / commodities / stocks)
        MAX_SECTOR_POSITIONS = 3  # max 3 trades sur le meme secteur (≈30% si 10 slots actifs)
        try:
            try:
                from brokers.capital_client import ASSET_PROFILES as _AP
            except ImportError:
                logger.warning("⚠️  capital_client non disponible — fallback ASSET_PROFILES core.imports")
                from core.imports import ASSET_PROFILES as _AP  # fallback
            new_cat = _AP.get(instrument, {}).get("cat", "forex")
            sector_count = 0
            for _instr, _state in open_trades.items():
                if _state is None:
                    continue
                _cat = _AP.get(_instr, {}).get("cat", "forex")
                if _cat == new_cat:
                    sector_count += 1
            if sector_count >= MAX_SECTOR_POSITIONS:
                reason = (
                    f"Sector Heat: {sector_count}/{MAX_SECTOR_POSITIONS} positions "
                    f"cat='{new_cat}' — max 30% sur un seul secteur"
                )
                logger.warning(f"🏭 {reason}")
                return False, reason
        except Exception:
            pass

        return True, ""

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

    # ═══════════════════════════════════════════════════════════════════════
    #  CONVICTION SIZING ENGINE
    # ═══════════════════════════════════════════════════════════════════════

    def _load_pf_cache(self):
        """Load Profit Factor per instrument from optimized_rules.json."""
        if self._pf_loaded:
            return
        self._pf_loaded = True
        try:
            rules_file = "optimized_rules.json"
            if os.path.exists(rules_file):
                with open(rules_file) as f:
                    rules = json.load(f)
                for instr, data in rules.items():
                    if isinstance(data, dict):
                        pf = data.get("pf", data.get("profit_factor", 0))
                        if pf and float(pf) > 0:
                            self._pf_cache[instr] = float(pf)
                logger.info(f"📊 Conviction PF loaded: {len(self._pf_cache)} instruments")
        except Exception as e:
            logger.warning(f"⚠️ Could not load PF cache: {e}")

    def get_conviction_tier(self, instrument: str) -> tuple[str, float, float]:
        """
        Return (tier_name, base_risk) based on instrument's historical PF.
        """
        self._load_pf_cache()
        pf = self._pf_cache.get(instrument, 0)

        for tier_name, tier in CONVICTION_TIERS.items():
            if pf >= tier["min_pf"]:
                return tier_name, tier["risk"], pf

        return "BASE", CONVICTION_TIERS["BASE"]["risk"], pf

    def _compute_kelly(self, instrument: str) -> float:
        """
        Calcule le Kelly fraction pour un instrument.
        K = (WR × avg_win - (1-WR) × avg_loss) / avg_win
        Retourne half-Kelly clamped [0.005, 0.050] (0.5% - 5.0%).
        """
        if instrument in self._kelly_cache:
            return self._kelly_cache[instrument]

        history = self._trade_history.get(instrument, [])
        if len(history) < 10:
            # Pas assez de données → risk par défaut from conviction
            return RISK_PER_TRADE  # 2.5%

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
        # Half-Kelly clamped [0.5%, 5.0%] — Aggressive Growth Mode
        half_kelly = max(0.008, min(0.020, kelly / 2))

        self._kelly_cache[instrument] = half_kelly
        return half_kelly

    def compute_risk_pct(self, instrument: str, score: float = 0.5,
                         current_atr: float = 0, avg_atr: float = 0,
                         nlp_impact: float = 0.0) -> float:
        """
        R-1 + Conviction: Calcule le risk% dynamique pour un trade.

        Pipeline:
          1. Conviction base → PF tier lookup (2.5% - 5.0%)
          2. Kelly override → if enough trade history, use half-Kelly
          3. Score adjustment → signal strength scaling
          4. Volatility adjustment → ATR ratio scaling
          5. NLP bonus → +20% if FinBERT impact > 0.8
          6. Final clamp [0.5%, 5.0%]

        Returns: risk percentage as decimal (e.g., 0.025 = 2.5%)
        """
        # Step 1: Conviction base from PF
        tier_name, conviction_risk, pf_val = self.get_conviction_tier(instrument)

        # Step 2: Kelly override (if we have trade history)
        kelly = self._compute_kelly(instrument)
        # Use the higher of conviction or Kelly (aggressive)
        base_risk = max(conviction_risk, kelly)

        # Step 3: Score adjustment
        score_adj = 0.5 + score * 0.5  # [0.7, 1.0] for score [0.4, 1.0]

        # Step 4: Volatility adjustment
        vol_adj = 1.0
        if current_atr > 0 and avg_atr > 0:
            vol_adj = min(1.5, max(0.5, avg_atr / current_atr))

        risk = base_risk * score_adj * vol_adj

        # Step 5: NLP Adrenaline Bonus
        nlp_applied = False
        if nlp_impact >= NLP_BONUS_THRESHOLD:
            risk *= NLP_BONUS_MULTIPLIER
            nlp_applied = True

        # Step 6: Final clamp [0.5%, 5.0%] — Aggressive Growth Mode
        risk = max(0.008, min(0.020, risk))

        logger.info(
            f"💉 SIZING {instrument}: tier={tier_name} (PF={pf_val:.1f}) | "
            f"base={conviction_risk:.1%} | kelly={kelly:.3f} | "
            f"score×{score_adj:.2f} | vol×{vol_adj:.2f}"
            + (f" | NLP🔥+20%" if nlp_applied else "")
            + f" → RISK={risk:.3f} ({risk*100:.1f}%)"
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
        logger.info(f"Reset balance journaliere : {current_balance:.2f} EUR")

    def reset_monthly(self, current_balance: float):
        """Reset du circuit breaker mensuel (appele le 1er de chaque mois)."""
        self._monthly_start_balance = current_balance
        logger.info(f"Reset balance mensuelle : {current_balance:.2f} EUR (DD mensuel reset)")

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
