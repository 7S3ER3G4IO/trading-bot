"""
gamification.py — Nemesis Gamification System
Tracks win/loss streaks, achievements, and performance records.
Persistent via logs/gamification.json.
"""
import json
import os
from datetime import datetime, timezone
from typing import List, Dict, Optional
from loguru import logger


GAMIFICATION_FILE = "logs/gamification.json"

# Achievement definitions
ACHIEVEMENTS = {
    "hot_streak_3":   {"name": "🔥 Hot Streak",     "desc": "3 wins d'affilée",        "threshold": 3},
    "hot_streak_5":   {"name": "🔥🔥 On Fire",       "desc": "5 wins d'affilée",        "threshold": 5},
    "hot_streak_10":  {"name": "🔥🔥🔥 Legendary",   "desc": "10 wins d'affilée",       "threshold": 10},
    "sniper":         {"name": "🎯 Sniper",          "desc": "3 trades complets (3/3 TP)", "threshold": 3},
    "iron_shield":    {"name": "🛡️ Iron Shield",     "desc": "0 SL en 10 trades",       "threshold": 10},
    "diamond_day":    {"name": "💎 Diamond Day",     "desc": "Meilleur jour > +100€",   "threshold": 100},
    "consistency":    {"name": "📈 Consistent",      "desc": "5 jours positifs d'affilée", "threshold": 5},
    "first_blood":    {"name": "⚔️ First Blood",     "desc": "Premier trade gagnant",   "threshold": 1},
    "centurion":      {"name": "🏛️ Centurion",       "desc": "100 trades complétés",    "threshold": 100},
}


class GamificationTracker:
    """Tracks streaks, achievements, and performance records."""

    def __init__(self):
        self.win_streak: int = 0
        self.loss_streak: int = 0
        self.best_win_streak: int = 0
        self.total_wins: int = 0
        self.total_losses: int = 0
        self.total_trades: int = 0
        self.total_pnl: float = 0.0
        self.best_day_pnl: float = 0.0
        self.tp3_completions: int = 0       # trades where all 3 TP hit
        self.trades_since_last_sl: int = 0  # for iron shield
        self.positive_days_streak: int = 0  # consecutive positive days
        self.daily_pnl: float = 0.0         # accumulator for current day
        self._current_day: Optional[str] = None
        self.unlocked: List[str] = []       # list of unlocked achievement IDs
        self._newly_unlocked: List[str] = []  # achievements unlocked this session (not yet announced)
        self._load()

    # ── Core ──────────────────────────────────────────────────────────────────

    def on_trade_closed(self, won: bool, pnl: float, is_tp3_complete: bool = False) -> List[str]:
        """
        Record a trade result. Returns list of newly unlocked achievements.
        """
        self.total_trades += 1
        self.total_pnl += pnl

        # Day tracking
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._current_day != today:
            # New day — finalize previous day
            if self._current_day is not None:
                self._finalize_day()
            self._current_day = today
            self.daily_pnl = 0.0
        self.daily_pnl += pnl

        if won:
            self.total_wins += 1
            self.win_streak += 1
            self.loss_streak = 0
            self.trades_since_last_sl += 1
            if self.win_streak > self.best_win_streak:
                self.best_win_streak = self.win_streak
        else:
            self.total_losses += 1
            self.loss_streak += 1
            self.win_streak = 0
            self.trades_since_last_sl = 0

        if is_tp3_complete:
            self.tp3_completions += 1

        # Check achievements
        newly = self._check_achievements()
        self._save()
        return newly

    def _finalize_day(self):
        """Called when a new day starts — update positive days streak."""
        if self.daily_pnl > 0:
            self.positive_days_streak += 1
        else:
            self.positive_days_streak = 0
        if self.daily_pnl > self.best_day_pnl:
            self.best_day_pnl = self.daily_pnl

    def _check_achievements(self) -> List[str]:
        """Check and unlock new achievements. Returns newly unlocked IDs."""
        newly = []

        checks = {
            "first_blood":    self.total_wins >= 1,
            "hot_streak_3":   self.best_win_streak >= 3,
            "hot_streak_5":   self.best_win_streak >= 5,
            "hot_streak_10":  self.best_win_streak >= 10,
            "sniper":         self.tp3_completions >= 3,
            "iron_shield":    self.trades_since_last_sl >= 10,
            "diamond_day":    self.best_day_pnl >= 100,
            "consistency":    self.positive_days_streak >= 5,
            "centurion":      self.total_trades >= 100,
        }

        for ach_id, condition in checks.items():
            if condition and ach_id not in self.unlocked:
                self.unlocked.append(ach_id)
                newly.append(ach_id)
                self._newly_unlocked.append(ach_id)
                logger.info(f"🏅 Achievement débloqué : {ACHIEVEMENTS[ach_id]['name']}")

        return newly

    def pop_new_achievements(self) -> List[Dict]:
        """Returns and clears newly unlocked achievements (for notification)."""
        result = []
        for ach_id in self._newly_unlocked:
            if ach_id in ACHIEVEMENTS:
                result.append(ACHIEVEMENTS[ach_id])
        self._newly_unlocked.clear()
        return result

    # ── Display ───────────────────────────────────────────────────────────────

    def format_stats_block(self) -> str:
        """Short stats block for embedding in pages."""
        wr = (self.total_wins / self.total_trades * 100) if self.total_trades > 0 else 0
        streak_str = f"🔥 {self.win_streak} wins" if self.win_streak > 0 else ""
        return (
            f"📊 Trades: {self.total_trades} · WR: {wr:.0f}%\n"
            f"💰 PnL total: {self.total_pnl:+.2f}€\n"
            + (f"{streak_str}\n" if streak_str else "")
            + f"🏆 Record: {self.best_win_streak} wins · Best day: {self.best_day_pnl:+.2f}€"
        )

    def format_achievements_block(self) -> str:
        """List of unlocked and locked achievements."""
        lines = []
        for ach_id, ach in ACHIEVEMENTS.items():
            status = "✅" if ach_id in self.unlocked else "🔒"
            lines.append(f"{status} {ach['name']} — {ach['desc']}")
        return "\n".join(lines)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save(self):
        try:
            os.makedirs("logs", exist_ok=True)
            data = {
                "win_streak": self.win_streak,
                "loss_streak": self.loss_streak,
                "best_win_streak": self.best_win_streak,
                "total_wins": self.total_wins,
                "total_losses": self.total_losses,
                "total_trades": self.total_trades,
                "total_pnl": round(self.total_pnl, 2),
                "best_day_pnl": round(self.best_day_pnl, 2),
                "tp3_completions": self.tp3_completions,
                "trades_since_last_sl": self.trades_since_last_sl,
                "positive_days_streak": self.positive_days_streak,
                "daily_pnl": round(self.daily_pnl, 2),
                "current_day": self._current_day,
                "unlocked": self.unlocked,
            }
            with open(GAMIFICATION_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.debug(f"Gamification save: {e}")

    def _load(self):
        try:
            if os.path.exists(GAMIFICATION_FILE):
                with open(GAMIFICATION_FILE) as f:
                    data = json.load(f)
                self.win_streak = data.get("win_streak", 0)
                self.loss_streak = data.get("loss_streak", 0)
                self.best_win_streak = data.get("best_win_streak", 0)
                self.total_wins = data.get("total_wins", 0)
                self.total_losses = data.get("total_losses", 0)
                self.total_trades = data.get("total_trades", 0)
                self.total_pnl = data.get("total_pnl", 0.0)
                self.best_day_pnl = data.get("best_day_pnl", 0.0)
                self.tp3_completions = data.get("tp3_completions", 0)
                self.trades_since_last_sl = data.get("trades_since_last_sl", 0)
                self.positive_days_streak = data.get("positive_days_streak", 0)
                self.daily_pnl = data.get("daily_pnl", 0.0)
                self._current_day = data.get("current_day")
                self.unlocked = data.get("unlocked", [])
                logger.debug(f"🏅 Gamification chargé : {self.total_trades} trades, streak={self.win_streak}")
        except Exception as e:
            logger.debug(f"Gamification load: {e}")
