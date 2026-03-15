"""
performance.py — Rapport de performance NEMESIS
Calcule Sharpe, Sortino, Win Rate, R:R moyen, Max Drawdown
depuis la table `trades` de PostgreSQL (ou SQLite en fallback).

Usage :
    from performance import PerformanceReport
    r = PerformanceReport()
    print(r.summary())          # texte console
    r.send_to_discord()         # envoi webhook
    r.to_dict()                 # dict pour API
"""
import os
import math
import statistics
from datetime import datetime, timezone, timedelta, date
from typing import Optional
from loguru import logger

# ─── Config ───────────────────────────────────────────────────────────────────

DISCORD_WEBHOOK = os.getenv("DISCORD_MONITORING_WEBHOOK", "")
RISK_FREE_RATE   = 0.05   # 5% annuel (US T-Bill)
TRADING_DAYS     = 252


# ─── Helper ───────────────────────────────────────────────────────────────────

def _get_db():
    try:
        from database import get_db
        return get_db()
    except Exception as e:
        logger.warning(f"Performance: DB non disponible — {e}")
        return None


# ─── PerformanceReport ────────────────────────────────────────────────────────

class PerformanceReport:
    """Calcule les métriques de performance depuis la table trades."""

    def __init__(self, days: int = 30):
        self.days = days
        self._db  = _get_db()
        self._trades = []
        self._pnls   = []
        self._load()

    # ── Chargement ────────────────────────────────────────────────────────────

    def _load(self):
        if not self._db:
            return
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=self.days)).isoformat()
            rows = self._db._execute(
                "SELECT pnl, result, instrument, opened_at, closed_at "
                "FROM trades WHERE status='CLOSED' AND opened_at >= %s ORDER BY opened_at",
                (cutoff,), fetch=True
            ).fetchall()
            for r in rows:
                self._trades.append({
                    "pnl":        float(r[0] or 0),
                    "result":     r[1] or "UNKNOWN",
                    "instrument": r[2],
                    "opened_at":  r[3],
                    "closed_at":  r[4],
                })
            self._pnls = [t["pnl"] for t in self._trades]
        except Exception as e:
            logger.warning(f"Performance._load: {e}")

    # ── Métriques ─────────────────────────────────────────────────────────────

    @property
    def total_trades(self) -> int:
        return len(self._trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self._trades if t["result"] in ("WIN", "TP1", "TP2", "TP3"))

    @property
    def losses(self) -> int:
        return sum(1 for t in self._trades if t["result"] in ("LOSS", "SL"))

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return round(self.wins / self.total_trades * 100, 1)

    @property
    def total_pnl(self) -> float:
        return round(sum(self._pnls), 2)

    @property
    def avg_win(self) -> float:
        wins = [t["pnl"] for t in self._trades if t["pnl"] > 0]
        return round(statistics.mean(wins), 2) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t["pnl"] for t in self._trades if t["pnl"] < 0]
        return round(statistics.mean(losses), 2) if losses else 0.0

    @property
    def risk_reward(self) -> float:
        """R:R moyen = avg_win / abs(avg_loss)."""
        if self.avg_loss == 0:
            return 0.0
        return round(self.avg_win / abs(self.avg_loss), 2)

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t["pnl"] for t in self._trades if t["pnl"] > 0)
        gross_loss   = abs(sum(t["pnl"] for t in self._trades if t["pnl"] < 0))
        if gross_loss == 0:
            return float("inf")
        return round(gross_profit / gross_loss, 2)

    @property
    def max_drawdown(self) -> float:
        """Max Drawdown en $ depuis le pic de capital cumulé."""
        if not self._pnls:
            return 0.0
        peak = 0.0
        cumul = 0.0
        max_dd = 0.0
        for pnl in self._pnls:
            cumul += pnl
            if cumul > peak:
                peak = cumul
            dd = peak - cumul
            if dd > max_dd:
                max_dd = dd
        return round(max_dd, 2)

    @property
    def sharpe_ratio(self) -> float:
        """Sharpe annualisé (returns journaliers approximés depuis les trades)."""
        if len(self._pnls) < 3:
            return 0.0
        try:
            mean   = statistics.mean(self._pnls)
            stddev = statistics.stdev(self._pnls)
            if stddev == 0:
                return 0.0
            daily_rf = RISK_FREE_RATE / TRADING_DAYS
            # Annualise en supposant ~trades_par_jour
            trades_per_day = max(self.total_trades / max(self.days, 1), 0.1)
            annualization  = math.sqrt(TRADING_DAYS * trades_per_day)
            sharpe = (mean - daily_rf) / stddev * annualization
            return round(sharpe, 2)
        except Exception:
            return 0.0

    @property
    def sortino_ratio(self) -> float:
        """Sortino : ne pénalise que la volatilité négative."""
        if len(self._pnls) < 3:
            return 0.0
        try:
            mean      = statistics.mean(self._pnls)
            neg_pnls  = [p for p in self._pnls if p < 0]
            if len(neg_pnls) < 2:
                return float("inf")
            downside_std = statistics.stdev(neg_pnls)
            if downside_std == 0:
                return 0.0
            trades_per_day = max(self.total_trades / max(self.days, 1), 0.1)
            annualization  = math.sqrt(TRADING_DAYS * trades_per_day)
            return round(mean / downside_std * annualization, 2)
        except Exception:
            return 0.0

    @property
    def consecutive_losses(self) -> int:
        """Pire série de pertes consécutives."""
        max_streak = 0
        streak = 0
        for pnl in self._pnls:
            if pnl < 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        return max_streak

    @property
    def best_instrument(self) -> str:
        from collections import defaultdict
        by_inst = defaultdict(float)
        for t in self._trades:
            by_inst[t["instrument"]] += t["pnl"]
        if not by_inst:
            return "—"
        return max(by_inst, key=by_inst.get)

    @property
    def worst_instrument(self) -> str:
        from collections import defaultdict
        by_inst = defaultdict(float)
        for t in self._trades:
            by_inst[t["instrument"]] += t["pnl"]
        if not by_inst:
            return "—"
        return min(by_inst, key=by_inst.get)

    # ── Formatage ─────────────────────────────────────────────────────────────

    def summary(self, period_label: str = "") -> str:
        label = period_label or f"{self.days}j"
        return (
            f"📊 **Performance NEMESIS — {label}**\n"
            f"\n"
            f"🏆 **P&L total** : {self.total_pnl:+.2f}$\n"
            f"📈 **Win Rate** : {self.win_rate}% ({self.wins}W / {self.losses}L / {self.total_trades} trades)\n"
            f"⚖️ **R:R moyen** : {self.risk_reward}\n"
            f"💡 **Profit Factor** : {self.profit_factor}\n"
            f"\n"
            f"📐 **Sharpe** : {self.sharpe_ratio}  |  **Sortino** : {self.sortino_ratio}\n"
            f"📉 **Max DD** : -{self.max_drawdown:.2f}$\n"
            f"🔴 **Pire série** : {self.consecutive_losses} pertes consécutives\n"
            f"\n"
            f"🥇 Meilleur : {self.best_instrument}  |  🥺 Pire : {self.worst_instrument}"
        )

    def to_dict(self) -> dict:
        return {
            "period_days":         self.days,
            "total_trades":        self.total_trades,
            "wins":                self.wins,
            "losses":              self.losses,
            "win_rate":            self.win_rate,
            "total_pnl":          self.total_pnl,
            "avg_win":             self.avg_win,
            "avg_loss":            self.avg_loss,
            "risk_reward":         self.risk_reward,
            "profit_factor":       self.profit_factor,
            "max_drawdown":        self.max_drawdown,
            "sharpe_ratio":        self.sharpe_ratio,
            "sortino_ratio":       self.sortino_ratio,
            "consecutive_losses":  self.consecutive_losses,
            "best_instrument":     self.best_instrument,
            "worst_instrument":    self.worst_instrument,
        }

    def send_to_discord(self, webhook_url: str = ""):
        """Envoie le rapport sur Discord."""
        url = webhook_url or DISCORD_WEBHOOK
        if not url:
            logger.warning("Performance.send_to_discord: DISCORD_MONITORING_WEBHOOK non défini")
            return
        try:
            import requests
            requests.post(url, json={"content": self.summary()}, timeout=10)
            logger.info("📊 Rapport performance envoyé sur Discord")
        except Exception as e:
            logger.error(f"Performance.send_to_discord: {e}")


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="NEMESIS Performance Report")
    parser.add_argument("--days", type=int, default=30, help="Période en jours (défaut: 30)")
    parser.add_argument("--discord", action="store_true", help="Envoie sur Discord")
    args = parser.parse_args()

    report = PerformanceReport(days=args.days)
    print(report.summary(f"{args.days} derniers jours"))
    print("\nDétails JSON:")
    import json
    print(json.dumps(report.to_dict(), indent=2))
    if args.discord:
        report.send_to_discord()
