"""
daily_reporter.py — Enregistre tous les trades du jour et envoie un bilan
à 21h00 CET (style Station X).

Format du bilan :
  📊 BILAN DU JOUR — 07/03
  7/03 ACHAT BTC  +450 pips  ✅
  7/03 VENTE ETH  -200 pips  ❌
  BILAN TRADES : 2/3
  PnL net : +42.30 USDT
"""

import json
import os
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import List
from loguru import logger

REPORT_FILE = "logs/daily_trades.json"
REPORT_HOUR_UTC = 20  # 21h CET = 20h UTC


@dataclass
class TradeRecord:
    """Enregistrement d'un trade clôturé."""
    date_str:   str    # ex: "07/03"
    symbol:     str    # ex: "BTC/USDT"
    side:       str    # "BUY" ou "SELL"
    result:     str    # "TP1", "TP2", "TP3", "BE", "SL"
    pnl:        float  # PnL en USDT
    pips:       float  # Distance en prix (approximation pips)


class DailyReporter:
    """Gère le suivi journalier et envoie le bilan de performance."""

    def __init__(self):
        os.makedirs("logs", exist_ok=True)
        self._trades: List[TradeRecord] = []
        self._report_sent_today = False
        self._load()

    # ─── Enregistrement ──────────────────────────────────────────────────────

    def record_trade(
        self,
        symbol: str,
        side: str,
        result: str,
        pnl: float,
        entry: float,
        exit_price: float,
    ):
        """Ajoute un trade au journal du jour."""
        now = datetime.now(timezone(timedelta(hours=1)))  # CET
        pips = abs(exit_price - entry)
        record = TradeRecord(
            date_str=now.strftime("%d/%m"),
            symbol=symbol.replace("/USDT", ""),
            side=side,
            result=result,
            pnl=round(pnl, 2),
            pips=round(pips, 0),
        )
        self._trades.append(record)
        self._save()
        logger.info(f"📝 Trade enregistré : {record.symbol} {result} | PnL={pnl:+.2f}")

    # ─── Bilan ────────────────────────────────────────────────────────────────

    def should_send_report(self) -> bool:
        """Retourne True une fois par jour à l'heure de rapport UTC."""
        now = datetime.now(timezone.utc)
        return (
            now.hour == REPORT_HOUR_UTC
            and now.minute < 2   # Fenêtre de 2 minutes
            and not self._report_sent_today
        )

    def build_report(self) -> str:
        """Construit le message bilan du jour (style Station X)."""
        if not self._trades:
            return "📊 *Bilan du jour* — Aucun trade aujourd'hui."

        now        = datetime.now(timezone(timedelta(hours=1)))
        date_label = now.strftime("%d/%m")
        lines      = []
        wins       = 0
        total_pnl  = 0.0

        for t in self._trades:
            if t.result == "SL":
                emoji  = "❌"
                suffix = f"-{t.pips:.0f} pips"
            elif t.result == "BE":
                emoji  = "✅"
                suffix = "(BE)"
                wins  += 1
            else:
                emoji  = "✅"
                suffix = f"+{t.pips:.0f} pips"
                wins  += 1
            total_pnl += t.pnl

            action = "ACHAT" if t.side == "BUY" else "VENTE"
            lines.append(f"{t.date_str} {action} {t.symbol}  {suffix}  {emoji}")

        score   = f"{wins}/{len(self._trades)}"
        pnl_str = f"{total_pnl:+.2f} USDT"
        pct     = f"{wins / len(self._trades) * 100:.0f}%"

        report = (
            f"📊 *BILAN DU JOUR — {date_label}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        )
        for line in lines:
            report += f"`{line}`\n"
        report += (
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 BILAN TRADES : *{score}*\n"
            f"📈 Taux réussite : *{pct}*\n"
            f"💵 PnL net : *{pnl_str}*"
        )
        return report

    def mark_report_sent(self):
        self._report_sent_today = True
        logger.info("📤 Bilan journalier envoyé.")

    def reset_for_new_day(self):
        """Réinitialise le tracker pour le nouveau jour."""
        self._trades = []
        self._report_sent_today = False
        self._save()
        logger.info("🔄 Journal journalier réinitialisé.")

    # ─── Persistance ─────────────────────────────────────────────────────────

    def _save(self):
        try:
            with open(REPORT_FILE, "w") as f:
                json.dump([asdict(t) for t in self._trades], f, indent=2)
        except Exception as e:
            logger.error(f"❌ Sauvegarde journal : {e}")

    def _load(self):
        try:
            if os.path.exists(REPORT_FILE):
                with open(REPORT_FILE) as f:
                    data = json.load(f)
                    self._trades = [TradeRecord(**d) for d in data]
        except Exception:
            self._trades = []
