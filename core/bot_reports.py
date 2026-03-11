"""
bot_reports.py — Rapports : wallet stats + rapport journalier PNG
"""
from .imports import *


class BotReportsMixin:

    def _post_wallet_stats(self, balance: float):
        """Envoie les stats portefeuille Capital.com via tgc.send_daily_dashboard."""
        try:
            closed_today = self._capital_closed_today
            wr_by_instr: dict = {}
            for t in closed_today:
                instr = t.get("instrument", "?")
                name  = CAPITAL_NAMES.get(instr, instr)
                wins_i  = sum(1 for x in closed_today if x.get("instrument") == instr and x.get("pnl", 0) > 0)
                total_i = sum(1 for x in closed_today if x.get("instrument") == instr)
                wr_by_instr[name] = (wins_i / total_i * 100) if total_i > 0 else 0.0

            tgc.send_daily_dashboard(
                balance=balance,
                initial_balance=self.initial_balance,
                day_trades=closed_today,
                win_rate_instrument=wr_by_instr,
            )
        except Exception as e:
            logger.warning(f"⚠️  _post_wallet_stats : {e}")

    def _send_daily_report(self) -> None:
        """
        Sprint 5 — Rapport visuel journalier.
        Génère un PNG 2 panneaux via matplotlib.
        """
        import io
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.colors as mcolors
        import numpy as np

        try:
            balance = self.capital.get_balance() if self.capital.available else 0.0
            pnl_total = round(balance - self.initial_balance, 2) if balance > 0 else 0.0
            wins  = sum(1 for t in self._capital_closed_today if t.get("pnl", 0) > 0)
            total = len(self._capital_closed_today)
            wr    = round(wins / total * 100, 1) if total else 0.0

            # ─ 1. PnL par instrument ─────────────────────────────────────────
            pnl_by_inst: dict = {}
            for t in self._capital_closed_today:
                sym = t.get("instrument", t.get("symbol", "?"))
                pnl_by_inst[sym] = pnl_by_inst.get(sym, 0) + t.get("pnl", 0)

            # ─ 2. Heatmap data (instrument × heure) ─────────────────────────
            instruments = list(CAPITAL_INSTRUMENTS)
            hours = list(range(7, 21))
            heat_matrix = np.zeros((len(instruments), len(hours)))
            for i, inst in enumerate(instruments):
                for j, h in enumerate(hours):
                    pnls = self._heatmap_data.get(inst, {}).get(h, [])
                    heat_matrix[i, j] = sum(pnls) if pnls else 0.0

            # ─ Figure ────────────────────────────────────────────────────────
            fig = plt.figure(figsize=(14, 8), facecolor="#060911")

            # Panneau 1 : barres PnL
            ax1 = fig.add_subplot(2, 1, 1)
            ax1.set_facecolor("#0d1220")
            if pnl_by_inst:
                labels = list(pnl_by_inst.keys())
                values = list(pnl_by_inst.values())
                colors = ["#22d3a0" if v >= 0 else "#ff4f6e" for v in values]
                bars = ax1.bar(labels, values, color=colors, edgecolor="#1e2a45", linewidth=0.8)
                ax1.axhline(0, color="#5a6a8a", linewidth=0.8, linestyle="--")
                for bar, val in zip(bars, values):
                    ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.01,
                             f"{val:+.2f}€", ha="center", va="bottom" if val >= 0 else "top",
                             color="#c8d6f0", fontsize=8)
            ax1.set_title(
                f"📊 NEMESIS — Rapport Journalier {datetime.now(timezone.utc).strftime('%d/%m/%Y')} | "
                f"PnL: {pnl_total:+.2f}€ | WR: {wr:.0f}% | {total} trades",
                color="#c8d6f0", fontsize=10, pad=8
            )
            ax1.set_ylabel("PnL (€)", color="#5a6a8a", fontsize=9)
            ax1.tick_params(colors="#5a6a8a", labelsize=8)
            for spine in ax1.spines.values():
                spine.set_color("#1e2a45")

            # Panneau 2 : Heatmap
            ax2 = fig.add_subplot(2, 1, 2)
            ax2.set_facecolor("#0d1220")
            cmap = mcolors.LinearSegmentedColormap.from_list(
                "nemesis", ["#ff4f6e", "#141a2e", "#22d3a0"]
            )
            abs_max = max(abs(heat_matrix).max(), 0.01)
            im = ax2.imshow(heat_matrix, cmap=cmap, aspect="auto",
                            vmin=-abs_max, vmax=abs_max)
            ax2.set_xticks(range(len(hours)))
            ax2.set_xticklabels([f"{h}h" for h in hours], color="#5a6a8a", fontsize=7)
            ax2.set_yticks(range(len(instruments)))
            ax2.set_yticklabels(instruments, color="#c8d6f0", fontsize=8)
            ax2.set_title("🔥 Heatmap Performance (Instrument × Heure UTC)", color="#c8d6f0", fontsize=9)
            for i in range(len(instruments)):
                for j in range(len(hours)):
                    val = heat_matrix[i, j]
                    if val != 0:
                        ax2.text(j, i, f"{val:+.1f}", ha="center", va="center",
                                 color="white", fontsize=6, fontweight="bold")

            plt.tight_layout(pad=1.5)

            buf = io.BytesIO()
            plt.savefig(buf, format="png", dpi=120, facecolor=fig.get_facecolor())
            plt.close(fig)
            buf.seek(0)

            caption = (
                f"📊 <b>Rapport Journalier Nemesis</b>\n"
                f"💰 PnL total : <b>{pnl_total:+.2f}€</b>\n"
                f"🎯 Win Rate  : <b>{wr:.0f}%</b> ({wins}/{total} trades)\n"
                f"📅 {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC"
            )
            # Send to Performance channel instead of main chat
            import io as _io
            from config import CHANNELS
            import requests as _rq
            _token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            _perf_id = CHANNELS.get("performance", {}).get("id", "")
            if _token and _perf_id:
                try:
                    _api = f"https://api.telegram.org/bot{_token}"
                    _files = {"photo": ("report.png", _io.BytesIO(buf.read()), "image/png")}
                    _data = {"chat_id": _perf_id, "caption": caption, "parse_mode": "HTML"}
                    _rq.post(f"{_api}/sendPhoto", data=_data, files=_files, timeout=30)
                except Exception:
                    pass
            logger.info("📊 Rapport journalier PNG envoyé → Performance")
        except Exception as _rp_e:
            logger.error(f"❌ Daily report: {_rp_e}")
