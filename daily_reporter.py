"""
daily_reporter.py — Bilan journalier et hebdomadaire.
"""
import json
import os
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import List, Optional
from loguru import logger

REPORT_FILE          = "logs/daily_trades.json"
REPORT_HOUR_UTC      = 20   # 21h CET
WEEKLY_REPORT_HOUR   = 21   # 22h CET
WEEKLY_REPORT_DOW    = 6    # Dimanche (0=lundi)
BINANCE_FEE_RATE     = 0.001  # 0.1% par ordre


@dataclass
class TradeRecord:
    date_str:   str
    symbol:     str
    side:       str
    result:     str
    pnl_gross:  float
    pnl_net:    float   # après frais
    fees:       float
    pips:       float


class DailyReporter:
    def __init__(self):
        os.makedirs("logs", exist_ok=True)
        self._trades: List[TradeRecord] = []
        self._report_sent_today       = False
        self._weekly_report_sent      = False
        self._last_weekly_check_day   = -1
        self._load()

    # ─── Enregistrement ──────────────────────────────────────────────────────

    def record_trade(
        self,
        symbol: str,
        side: str,
        result: str,
        pnl_gross: float,
        entry: float,
        exit_price: float,
        amount: float = 0.0,
    ):
        now  = datetime.now(timezone(timedelta(hours=1)))
        pips = abs(exit_price - entry)
        # Frais = 2 ordres (entrée + sortie) × 0.1% × montant total en USDT
        fees = round(entry * amount * BINANCE_FEE_RATE * 2, 4)
        pnl_net = round(pnl_gross - fees, 4)

        rec = TradeRecord(
            date_str=now.strftime("%d/%m"),
            symbol=symbol.replace("/USDT", ""),
            side=side,
            result=result,
            pnl_gross=round(pnl_gross, 2),
            pnl_net=pnl_net,
            fees=fees,
            pips=round(pips, 2),
        )
        self._trades.append(rec)
        self._save()
        logger.info(f"📝 Trade enregistré : {rec.symbol} {result} | PnL net={pnl_net:+.2f} (frais={fees:.2f})")

    # ─── Bilan journalier ─────────────────────────────────────────────────────

    def should_send_report(self) -> bool:
        now = datetime.now(timezone.utc)
        return now.hour == REPORT_HOUR_UTC and now.minute < 2 and not self._report_sent_today

    def build_report(self) -> str:
        if not self._trades:
            return "📊 *Bilan du jour* — Aucun trade aujourd'hui."

        now        = datetime.now(timezone(timedelta(hours=1)))
        date_label = now.strftime("%d/%m")
        wins, total_gross, total_net, total_fees = 0, 0.0, 0.0, 0.0
        lines = []

        for t in self._trades:
            if t.result == "SL":
                emoji, suffix = "❌", f"-{t.pips:.0f} pts"
            elif t.result == "BE":
                emoji, suffix = "✅", "(BE)"
                wins += 1
            else:
                emoji, suffix = "✅", f"+{t.pips:.0f} pts"
                wins += 1
            total_gross += t.pnl_gross
            total_net   += t.pnl_net
            total_fees  += t.fees
            action = "ACHAT" if t.side == "BUY" else "VENTE"
            lines.append(f"`{t.date_str} {action} {t.symbol}  {suffix}  {emoji}`")

        score = f"{wins}/{len(self._trades)}"
        pct   = f"{wins / len(self._trades) * 100:.0f}%"

        rpt = (
            f"📊 *BILAN DU JOUR — {date_label}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        )
        for l in lines:
            rpt += l + "\n"
        rpt += (
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 BILAN TRADES : *{score}* | *{pct}*\n"
            f"💵 PnL brut : `{total_gross:+.2f} USDT`\n"
            f"💸 Frais Binance : `-{total_fees:.2f} USDT`\n"
            f"✅ *PnL net : `{total_net:+.2f} USDT`*"
        )
        return rpt

    def build_report_lines(self) -> list:
        """
        Format minimaliste pour le bilan Telegram.
        Retourne [(date_str, action, ticker, result_str, pnl_net), ...]
        result_str = "+680 pips" | "-310 pips" | "BE"
        pnl_net    = float en USDT
        """
        lines = []
        for t in self._trades:
            action = "ACHAT" if t.side == "BUY" else "VENTE"
            if t.result == "BE":
                result_str = "BE"
            elif t.result == "SL":
                result_str = f"-{t.pips:.0f} pips"
            else:
                result_str = f"+{t.pips:.0f} pips"
            lines.append((t.date_str, action, t.symbol, result_str, t.pnl_net))
        return lines

    def mark_report_sent(self):
        self._report_sent_today = True


    # ─── Bilan hebdomadaire ───────────────────────────────────────────────────

    def should_send_weekly(self) -> bool:
        cet = datetime.now(timezone(timedelta(hours=1)))
        if (cet.weekday() == WEEKLY_REPORT_DOW
                and cet.hour == WEEKLY_REPORT_HOUR
                and cet.minute < 2
                and self._last_weekly_check_day != cet.day):
            return True
        return False

    def build_weekly_report(self) -> str:
        if not self._trades:
            return "📅 *Bilan hebdomadaire* — Aucun trade cette semaine."

        cet = datetime.now(timezone(timedelta(hours=1)))
        wins       = sum(1 for t in self._trades if t.result != "SL")
        total_net  = sum(t.pnl_net for t in self._trades)
        total_fees = sum(t.fees for t in self._trades)
        best  = max(self._trades, key=lambda t: t.pnl_net)
        worst = min(self._trades, key=lambda t: t.pnl_net)
        score = f"{wins}/{len(self._trades)}"
        pct   = f"{wins / len(self._trades) * 100:.0f}%"

        return (
            f"📅 *BILAN DE LA SEMAINE*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 Trades : *{score}* | Win rate : *{pct}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 PnL net : `{total_net:+.2f} USDT`\n"
            f"💸 Frais cumulés : `-{total_fees:.2f} USDT`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🌟 Meilleur trade : `{best.symbol}` `{best.pnl_net:+.2f} USDT`\n"
            f"📉 Pire trade : `{worst.symbol}` `{worst.pnl_net:+.2f} USDT`"
        )

    def mark_weekly_sent(self):
        cet = datetime.now(timezone(timedelta(hours=1)))
        self._last_weekly_check_day = cet.day

    # ─── Reset & persistance ──────────────────────────────────────────────────

    def reset_for_new_day(self):
        self._trades = []
        self._report_sent_today = False
        self._save()

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
