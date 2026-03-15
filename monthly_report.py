"""
monthly_report.py — Rapport Mensuel Automatique NEMESIS
Envoyé le 1er de chaque mois à 10h UTC via Discord + Telegram.
Utilise PerformanceReport(days=30) + stats DB supplémentaires.

Usage dans bot_tick.py (déjà déclenché au 1er / 10h) :
    from monthly_report import MonthlyReporter
    reporter = MonthlyReporter()
    reporter.send()
"""
import os
from datetime import datetime, timezone, timedelta, date
from typing import Optional
from loguru import logger

DISCORD_WEBHOOK  = os.getenv("DISCORD_MONITORING_WEBHOOK", "")
DISCORD_WEBHOOK2 = os.getenv("DISCORD_WEBHOOK_MONITORING", "")


class MonthlyReporter:
    """Génère et envoie le rapport mensuel de performance."""

    def __init__(self, db=None, telegram_router=None):
        self._db         = db
        self._router     = telegram_router
        self._last_month: Optional[int] = None

    def should_send(self) -> bool:
        now = datetime.now(timezone.utc)
        return (
            now.day == 1
            and now.hour == 10
            and now.minute < 3
            and self._last_month != now.month
        )

    def mark_sent(self):
        self._last_month = datetime.now(timezone.utc).month

    def build_report(self) -> str:
        """Génère le rapport mensuel complet (mois précédent)."""
        try:
            from performance import PerformanceReport
            perf = PerformanceReport(days=30)
        except Exception:
            perf = None

        now   = datetime.now(timezone.utc)
        month_name = (now - timedelta(days=1)).strftime("%B %Y")

        if perf is None or perf.total_trades == 0:
            return f"📅 <b>RAPPORT MENSUEL — {month_name}</b>\n\n⚠️ Aucune donnée disponible."

        trend = "📈" if perf.total_pnl >= 0 else "📉"
        pnl_class = "positif" if perf.total_pnl >= 0 else "négatif"

        report = (
            f"📅 <b>RAPPORT MENSUEL — {month_name}</b>\n\n"
            f"{trend} <b>PnL : {perf.total_pnl:+.2f}$</b> ({pnl_class})\n"
            f"\n"
            f"📊 <b>Performance</b>\n"
            f"Win Rate    : <code>{perf.win_rate}%</code> "
            f"({perf.wins}W / {perf.losses}L / {perf.total_trades} trades)\n"
            f"R:R moyen   : <code>{perf.risk_reward}</code>\n"
            f"Profit Factor: <code>{perf.profit_factor}</code>\n"
            f"\n"
            f"📐 <b>Métriques Risk-Adjusted</b>\n"
            f"Sharpe      : <code>{perf.sharpe_ratio}</code>\n"
            f"Sortino     : <code>{perf.sortino_ratio}</code>\n"
            f"Max DD      : <code>-{perf.max_drawdown:.2f}$</code>\n"
            f"Pire série  : <code>{perf.consecutive_losses}</code> pertes consécutives\n"
            f"\n"
            f"🏆 Meilleur : <b>{perf.best_instrument}</b>  |  🥺 Pire : <b>{perf.worst_instrument}</b>"
        )

        # Enrichissement DB — top 3 instruments du mois
        try:
            db = self._db or self._get_db()
            if db:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
                rows = db._execute(
                    "SELECT instrument, COUNT(*) as n, "
                    "SUM(CASE WHEN result IN ('WIN','TP1','TP2','TP3') THEN 1 ELSE 0 END) as wins, "
                    "SUM(pnl) as total_pnl "
                    "FROM trades WHERE status='CLOSED' AND opened_at >= %s "
                    "GROUP BY instrument ORDER BY total_pnl DESC LIMIT 5",
                    (cutoff,), fetch=True
                ).fetchall()
                if rows:
                    report += "\n\n📋 <b>Top instruments</b>\n"
                    for row in rows:
                        inst, n, w, pnl = row[0], row[1], row[2], float(row[3] or 0)
                        wr = round(w / n * 100) if n else 0
                        icon = "🟢" if pnl >= 0 else "🔴"
                        report += f"{icon} {inst}: {pnl:+.2f}$ ({wr}% WR, {n} trades)\n"
        except Exception as e:
            logger.debug(f"MonthlyReporter DB extras: {e}")

        return report

    def _get_db(self):
        try:
            from database import get_db
            return get_db()
        except Exception:
            return None

    def send(self):
        """Envoie le rapport sur Discord + Telegram."""
        report = self.build_report()

        # Discord (format markdown)
        discord_report = (
            report
            .replace("<b>", "**").replace("</b>", "**")
            .replace("<code>", "`").replace("</code>", "`")
            .replace("<i>", "*").replace("</i>", "*")
        )

        sent = False
        for webhook_url in filter(None, [DISCORD_WEBHOOK, DISCORD_WEBHOOK2]):
            try:
                import requests
                r = requests.post(
                    webhook_url,
                    json={"content": discord_report[:2000]},
                    timeout=10,
                )
                if r.status_code < 300:
                    logger.info("📅 Rapport mensuel envoyé sur Discord")
                    sent = True
                    break
            except Exception as e:
                logger.debug(f"MonthlyReporter Discord: {e}")

        # Telegram
        try:
            if self._router:
                self._router.send_performance(report)
                sent = True
        except Exception as e:
            logger.debug(f"MonthlyReporter Telegram: {e}")

        if not sent:
            logger.warning("⚠️ MonthlyReporter: aucun canal d'envoi disponible")

        self.mark_sent()
        return report


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    reporter = MonthlyReporter()
    print(reporter.build_report())
