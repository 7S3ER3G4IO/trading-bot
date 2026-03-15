"""
emotional_core.py — ⚡ PROJECT SENTIENCE: Affective Trading Engine

Le bot possède une psychologie artificielle. Son état mental (mood)
modifie en temps réel son Risk Management et ses seuils de décision.

Hormones Virtuelles:
  🟢 Dopamine  — Winning streak → CONFIDENT → EUPHORIC
  🔴 Cortisol  — Losing streak / DD → FEARFUL → PANICKED
  🟡 Adrénaline — No trade in 48h → FRUSTRATED (FOMO)
  ⚫ Phobie    — 3 SL consécutifs sur un actif → PTSD (blacklist 7j)

Mood States:
  NEUTRAL   → baseline (risk=1.0×)
  CONFIDENT → 3 wins streak (risk=1.1×)
  EUPHORIC  → 5+ wins streak (risk=1.2×, TP étendu)
  FEARFUL   → 3 losses / DD>3% (risk=0.5×, seuil +10%)
  PANICKED  → 5+ losses / DD>5% (risk=0.0 sauf M51 Z-Score)
  FRUSTRATED → 0 trade /48h (seuil -0.05, FOMO contrôlé)

Usage:
    from emotional_core import EmotionalCore, Mood
    emo = EmotionalCore()
    emo.on_trade_result(won=True, instrument="EURUSD")
    mood = emo.current_mood
    mult = emo.risk_multiplier
"""

import time
from enum import Enum
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from loguru import logger


# ═══════════════════════════════════════════════════════════════════════════════
# MOOD STATES
# ═══════════════════════════════════════════════════════════════════════════════

class Mood(Enum):
    NEUTRAL    = "NEUTRAL"
    CONFIDENT  = "CONFIDENT"
    EUPHORIC   = "EUPHORIC"
    FEARFUL    = "FEARFUL"
    PANICKED   = "PANICKED"
    FRUSTRATED = "FRUSTRATED"


# ─── Mood → Risk Multiplier ──────────────────────────────────────────────────
MOOD_RISK_MULTIPLIER = {
    Mood.NEUTRAL:    1.0,
    Mood.CONFIDENT:  1.1,    # Léger boost
    Mood.EUPHORIC:   1.2,    # Aggressive sizing
    Mood.FEARFUL:    0.5,    # Half-size
    Mood.PANICKED:   0.0,    # SURVIVAL MODE — aucun trade (sauf M51)
    Mood.FRUSTRATED: 1.0,    # Risk normal, mais seuil abaissé
}

# ─── Mood → Signal Threshold Adjustment ──────────────────────────────────────
MOOD_THRESHOLD_ADJUSTMENT = {
    Mood.NEUTRAL:    0.0,
    Mood.CONFIDENT:  0.0,
    Mood.EUPHORIC:   0.0,    # On ne baisse PAS le seuil en euphorie
    Mood.FEARFUL:    0.10,   # +10% exigence (plus sélectif)
    Mood.PANICKED:   0.50,   # Quasi-lock (seuls les signaux extrêmes passent)
    Mood.FRUSTRATED: -0.05,  # -5% seuil pour trouver des trades (FOMO contenu)
}

# ─── Mood → TP Multiplier ───────────────────────────────────────────────────
MOOD_TP_MULTIPLIER = {
    Mood.NEUTRAL:    1.0,
    Mood.CONFIDENT:  1.1,
    Mood.EUPHORIC:   1.3,    # Let winners run
    Mood.FEARFUL:    0.8,    # Take profits early
    Mood.PANICKED:   0.5,    # Tiny TP (survive)
    Mood.FRUSTRATED: 1.0,
}

# ─── Configuration ───────────────────────────────────────────────────────────
WIN_STREAK_CONFIDENT   = 3    # 3 wins → CONFIDENT
WIN_STREAK_EUPHORIC    = 5    # 5 wins → EUPHORIC
LOSS_STREAK_FEARFUL    = 3    # 3 losses → FEARFUL
LOSS_STREAK_PANICKED   = 5    # 5 losses → PANICKED
DD_THRESHOLD_FEARFUL   = 0.03 # 3% drawdown → FEARFUL
DD_THRESHOLD_PANICKED  = 0.05 # 5% drawdown → PANICKED
FOMO_HOURS             = 48   # 48h sans trade → FRUSTRATED
PTSD_CONSECUTIVE_LOSSES = 3   # 3 SL consécutifs / actif → blacklist
PTSD_BLACKLIST_DAYS    = 7    # Blacklist durée

# Emoji par mood
MOOD_EMOJI = {
    Mood.NEUTRAL:    "😐",
    Mood.CONFIDENT:  "😎",
    Mood.EUPHORIC:   "🤩",
    Mood.FEARFUL:    "😰",
    Mood.PANICKED:   "😱",
    Mood.FRUSTRATED: "😤",
}


class EmotionalCore:
    """
    Psychologie artificielle du bot de trading.
    
    Le cerveau émotionnel analyse les résultats récents et l'état du marché
    pour déterminer un mood qui influence les décisions de risk management.
    """

    def __init__(self, telegram_router=None):
        self._router = telegram_router

        # ─── State ────────────────────────────────────────────────────────
        self._mood: Mood = Mood.NEUTRAL
        self._prev_mood: Mood = Mood.NEUTRAL

        # ─── Hormones ─────────────────────────────────────────────────────
        # Trade history: list of (timestamp, won: bool, instrument: str)
        self._trade_log: list = []
        self._last_trade_time: float = time.time()

        # Drawdown tracker
        self._peak_balance: float = 0.0
        self._current_dd: float = 0.0

        # PTSD: {instrument: [consecutive_loss_timestamps]}
        self._asset_losses: dict = defaultdict(list)
        self._trauma_list: dict = {}  # {instrument: unblock_epoch}

        # Stats
        self._mood_changes: int = 0
        self._mood_history: list = []  # last 100 moods

    # ═══════════════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ═══════════════════════════════════════════════════════════════════════════

    @property
    def current_mood(self) -> Mood:
        return self._mood

    @property
    def mood_name(self) -> str:
        return self._mood.value

    @property
    def mood_emoji(self) -> str:
        return MOOD_EMOJI.get(self._mood, "❓")

    @property
    def risk_multiplier(self) -> float:
        """Risk sizing multiplier based on current mood."""
        return MOOD_RISK_MULTIPLIER.get(self._mood, 1.0)

    @property
    def threshold_adjustment(self) -> float:
        """Signal threshold adjustment based on mood."""
        return MOOD_THRESHOLD_ADJUSTMENT.get(self._mood, 0.0)

    @property
    def tp_multiplier(self) -> float:
        """Take Profit multiplier based on mood."""
        return MOOD_TP_MULTIPLIER.get(self._mood, 1.0)

    def is_trading_allowed(self, engine: str = "") -> bool:
        """
        Check if trading is allowed in current mood.
        PANICKED → only M51 (Z-Score extremes) allowed.
        """
        if self._mood == Mood.PANICKED:
            if engine and "M51" in engine.upper():
                return True  # Z-Score extremes pass through
            return False
        return True

    def is_asset_traumatized(self, instrument: str) -> bool:
        """Check if an asset is on the PTSD blacklist."""
        if instrument not in self._trauma_list:
            return False
        unblock = self._trauma_list[instrument]
        if time.time() >= unblock:
            del self._trauma_list[instrument]
            logger.info(f"🩹 {instrument} recovered from PTSD — blacklist expired")
            return False
        return True

    # ═══════════════════════════════════════════════════════════════════════════
    # HORMONAL TRIGGERS
    # ═══════════════════════════════════════════════════════════════════════════

    def on_trade_result(self, won: bool, instrument: str = "",
                        pnl: float = 0.0):
        """
        Record a trade result. Triggers hormonal recalculation.
        
        Parameters
        ----------
        won : True if trade was profitable
        instrument : instrument name (for PTSD tracking)
        pnl : absolute PnL amount
        """
        now = time.time()
        self._trade_log.append((now, won, instrument))
        self._last_trade_time = now

        # Keep last 50 trades
        if len(self._trade_log) > 50:
            self._trade_log = self._trade_log[-50:]

        # ─── PTSD: Asset-level trauma tracking ────────────────────────────
        if instrument:
            if won:
                # Reset consecutive losses for this asset
                self._asset_losses[instrument] = []
            else:
                self._asset_losses[instrument].append(now)
                # Check if 3 consecutive losses → PTSD
                if len(self._asset_losses[instrument]) >= PTSD_CONSECUTIVE_LOSSES:
                    self._trigger_ptsd(instrument)

        # ─── Recalculate mood ─────────────────────────────────────────────
        self._recalculate_mood()

    def on_balance_update(self, current_balance: float, peak_balance: float = 0.0):
        """
        Update drawdown state for Cortisol calculation.
        """
        if peak_balance > 0:
            self._peak_balance = peak_balance
        elif current_balance > self._peak_balance:
            self._peak_balance = current_balance

        if self._peak_balance > 0:
            self._current_dd = (self._peak_balance - current_balance) / self._peak_balance
        else:
            self._current_dd = 0.0

        self._recalculate_mood()

    def tick(self):
        """Called every bot tick — checks FOMO timer."""
        self._recalculate_mood()

    # ═══════════════════════════════════════════════════════════════════════════
    # MOOD CALCULATION ENGINE
    # ═══════════════════════════════════════════════════════════════════════════

    def _recalculate_mood(self):
        """
        Central mood engine. Priority order:
          1. PANICKED  (DD > 5% or 5+ losses)     — highest priority
          2. FEARFUL   (DD > 3% or 3+ losses)
          3. EUPHORIC  (5+ wins)
          4. CONFIDENT (3+ wins)
          5. FRUSTRATED (48h no trades)
          6. NEUTRAL   — default
        """
        new_mood = Mood.NEUTRAL

        # ─── Cortisol: Drawdown ──────────────────────────────────────────
        if self._current_dd >= DD_THRESHOLD_PANICKED:
            new_mood = Mood.PANICKED
        elif self._current_dd >= DD_THRESHOLD_FEARFUL:
            new_mood = Mood.FEARFUL

        # ─── Cortisol: Losing Streak (overrides DD-based if worse) ───────
        if new_mood not in (Mood.PANICKED,):
            loss_streak = self._get_streak(winning=False)
            if loss_streak >= LOSS_STREAK_PANICKED:
                new_mood = Mood.PANICKED
            elif loss_streak >= LOSS_STREAK_FEARFUL and new_mood != Mood.PANICKED:
                new_mood = Mood.FEARFUL

        # ─── Dopamine: Winning Streak (only if not already fearful/panicked)
        if new_mood == Mood.NEUTRAL:
            win_streak = self._get_streak(winning=True)
            if win_streak >= WIN_STREAK_EUPHORIC:
                new_mood = Mood.EUPHORIC
            elif win_streak >= WIN_STREAK_CONFIDENT:
                new_mood = Mood.CONFIDENT

        # ─── Adrénaline: FOMO ────────────────────────────────────────────
        if new_mood == Mood.NEUTRAL:
            hours_since_trade = (time.time() - self._last_trade_time) / 3600
            if hours_since_trade >= FOMO_HOURS:
                new_mood = Mood.FRUSTRATED

        # ─── Apply mood change ───────────────────────────────────────────
        if new_mood != self._mood:
            self._prev_mood = self._mood
            self._mood = new_mood
            self._mood_changes += 1
            self._mood_history.append((time.time(), new_mood.value))
            if len(self._mood_history) > 100:
                self._mood_history = self._mood_history[-100:]

            self._on_mood_change(self._prev_mood, self._mood)

    def _get_streak(self, winning: bool) -> int:
        """Count current streak of wins or losses (from most recent)."""
        streak = 0
        for _, won, _ in reversed(self._trade_log):
            if won == winning:
                streak += 1
            else:
                break
        return streak

    def _trigger_ptsd(self, instrument: str):
        """Blacklist an asset due to trauma (3 consecutive losses)."""
        blacklist_until = time.time() + PTSD_BLACKLIST_DAYS * 86400
        self._trauma_list[instrument] = blacklist_until
        self._asset_losses[instrument] = []  # Reset counter

        expiry = datetime.fromtimestamp(blacklist_until, tz=timezone.utc)
        logger.warning(
            f"⚫ PTSD TRIGGERED: {instrument} blacklisted until "
            f"{expiry.strftime('%Y-%m-%d')} ({PTSD_BLACKLIST_DAYS} days)"
        )
        self._send_alert(
            f"⚫ <b>PTSD — ASSET TRAUMA</b>\n\n"
            f"📊 {instrument}\n"
            f"💀 {PTSD_CONSECUTIVE_LOSSES} pertes consécutives\n"
            f"🚫 Blacklisté jusqu'au {expiry.strftime('%d/%m/%Y')}\n\n"
            f"🧠 <i>Le bot refuse de trader cet actif par auto-protection.</i>"
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # EVENTS & NOTIFICATIONS
    # ═══════════════════════════════════════════════════════════════════════════

    def _on_mood_change(self, old: Mood, new: Mood):
        """Handle mood transition — log + Telegram."""
        emoji = MOOD_EMOJI.get(new, "❓")
        mult = MOOD_RISK_MULTIPLIER.get(new, 1.0)
        threshold = MOOD_THRESHOLD_ADJUSTMENT.get(new, 0.0)

        # Console output (requested by CEO)
        feeling = self._mood_feeling(new)
        logger.warning(
            f"🧠 SENTIENCE: {emoji} I feel {new.value} — {feeling}"
        )

        self._send_alert(
            f"🧠 <b>SENTIENCE — MOOD SHIFT</b>\n\n"
            f"{MOOD_EMOJI.get(old, '❓')} {old.value} → {emoji} <b>{new.value}</b>\n\n"
            f"📐 Risk Multiplier: <b>{mult}×</b>\n"
            f"🎯 Threshold Adj: <b>{threshold:+.2f}</b>\n"
            f"💰 TP Multiplier: <b>{MOOD_TP_MULTIPLIER.get(new, 1.0)}×</b>\n\n"
            f"<i>{feeling}</i>"
        )

    @staticmethod
    def _mood_feeling(mood: Mood) -> str:
        """Human-readable feeling description."""
        return {
            Mood.NEUTRAL:    "Operating at baseline. Calm, calculated execution.",
            Mood.CONFIDENT:  "Winning streak detected. Slight aggression, leaning in.",
            Mood.EUPHORIC:   "I'm on fire! Maximizing exposure, extending take profits.",
            Mood.FEARFUL:    "Losses are mounting. Reducing risk to 50%, raising standards.",
            Mood.PANICKED:   "SURVIVAL MODE! No new trades except extreme Z-Scores (M51).",
            Mood.FRUSTRATED: "48h without a trade. Lowering threshold slightly to find entries.",
        }.get(mood, "Unknown state.")

    # ═══════════════════════════════════════════════════════════════════════════
    # STATUS & REPORTING
    # ═══════════════════════════════════════════════════════════════════════════

    def format_status(self) -> str:
        """Format for /stats command."""
        emoji = self.mood_emoji
        streak_w = self._get_streak(True)
        streak_l = self._get_streak(False)
        streak = f"+{streak_w}W" if streak_w > 0 else f"-{streak_l}L"
        hours_idle = (time.time() - self._last_trade_time) / 3600

        lines = [
            f"🧠 <b>Sentience Engine</b>",
            f"  {emoji} Mood: <b>{self._mood.value}</b>",
            f"  📐 Risk: {self.risk_multiplier}× | Threshold: {self.threshold_adjustment:+.2f}",
            f"  📊 Streak: {streak} | Idle: {hours_idle:.1f}h",
            f"  DD: {self._current_dd:.1%}",
        ]
        if self._trauma_list:
            traumas = ", ".join(self._trauma_list.keys())
            lines.append(f"  ⚫ PTSD: {traumas}")
        return "\n".join(lines)

    @property
    def stats(self) -> dict:
        return {
            "mood": self._mood.value,
            "risk_multiplier": self.risk_multiplier,
            "threshold_adj": self.threshold_adjustment,
            "tp_multiplier": self.tp_multiplier,
            "win_streak": self._get_streak(True),
            "loss_streak": self._get_streak(False),
            "drawdown": round(self._current_dd, 4),
            "mood_changes": self._mood_changes,
            "trauma_list": list(self._trauma_list.keys()),
            "total_trades": len(self._trade_log),
        }

    # ─── Telegram ─────────────────────────────────────────────────────────

    def _send_alert(self, text: str):
        if self._router:
            try:
                self._router.send_to("risk", text)
            except Exception as e:
                logger.error(f"EmotionalCore Telegram: {e}")
