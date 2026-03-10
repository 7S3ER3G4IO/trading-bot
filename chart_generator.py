"""
chart_generator.py — Génère un graphique de chandeliers avec indicateurs.
Import de matplotlib protégé pour éviter crash si absent (Railway).
"""

import io
import os
import sys
from typing import Optional
from loguru import logger

# Backend non-interactif AVANT tout import matplotlib
os.environ.setdefault("MPLBACKEND", "Agg")

# Import conditionnel — le bot continue sans chart si librairie absente
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import mplfinance as mpf
    CHART_AVAILABLE = True
    logger.info("📊 Chart generator : mplfinance disponible")
except ImportError as e:
    CHART_AVAILABLE = False
    logger.warning(f"⚠️  Chart generator désactivé ({e}) — bot continue normalement")


class ChartGenerator:
    """Génère et envoie des graphiques de trades sur Telegram."""

    def __init__(self):
        if not CHART_AVAILABLE:
            self._style = None
            return

        self._style = mpf.make_mpf_style(
            base_mpf_style="nightclouds",
            gridstyle="--",
            gridaxis="horizontal",
            gridcolor="#2a2a2a",
            facecolor="#0d1117",
            figcolor="#0d1117",
            edgecolor="#0d1117",
            rc={
                "font.size": 9,
                "font.family": "monospace",
                "axes.labelcolor": "#aaaaaa",
                "xtick.color": "#888888",
                "ytick.color": "#888888",
            }
        )

    def generate_trade_chart(
        self,
        df,
        symbol: str,
        side: str,
        entry: float,
        tp1: float,
        tp2: float,
        tp3: float,
        sl: float,
        score: int,
        indicators_desc: str,
    ) -> Optional[bytes]:
        """Génère un graphique PNG et retourne les bytes (None si indisponible)."""
        if not CHART_AVAILABLE:
            return None

        try:
            import pandas as pd
            plot_df = df.tail(60).copy()

            addplots = [
                mpf.make_addplot(plot_df["ema9"],  color="#00d4ff", width=1.2),
                mpf.make_addplot(plot_df["ema21"], color="#ff8c00", width=1.2),
                mpf.make_addplot(plot_df["macd"],        panel=1, color="#00ff88", width=1, ylabel="MACD"),
                mpf.make_addplot(plot_df["macd_signal"], panel=1, color="#ff4466", width=1),
            ]

            fig, axes = mpf.plot(
                plot_df,
                type="candle",
                style=self._style,
                addplot=addplots,
                returnfig=True,
                figsize=(10, 6),
                tight_layout=True,
                warn_too_much_data=200,
                panel_ratios=(3, 1),
            )
            ax = axes[0]

            # Niveaux SL/TP
            for price, color, label in [
                (sl,  "#ff3333", f"SL  {sl:,.2f}"),
                (tp1, "#ffaa00", f"TP1 {tp1:,.2f}"),
                (tp2, "#00cc44", f"TP2 {tp2:,.2f}"),
                (tp3, "#00ff88", f"TP3 {tp3:,.2f}"),
            ]:
                ax.axhline(y=price, color=color, linewidth=1, linestyle="--", alpha=0.8)
                ax.text(
                    0.01, price, f" ━ {label}",
                    transform=ax.get_yaxis_transform(),
                    color=color, fontsize=8, va="center",
                    bbox=dict(facecolor="#0d1117", edgecolor="none", pad=1)
                )

            # Flèche entrée
            entry_x    = len(plot_df) - 1
            arrow_color = "#00ff00" if side == "BUY" else "#ff0000"
            arrow_label = f"{'↑ ACHAT' if side == 'BUY' else '↓ VENTE'}\n{entry:,.2f}"
            ax.annotate(
                arrow_label, xy=(entry_x, entry),
                fontsize=9, color=arrow_color, fontweight="bold",
                bbox=dict(facecolor="#1a1a2e", edgecolor=arrow_color, pad=3, alpha=0.9),
            )

            pair   = symbol.replace("/", "")
            action = "ACHAT" if side == "BUY" else "VENTE"
            ax.set_title(
                f"⚡ Nemesis | {pair} {action} | Score {score}/3 | {indicators_desc}",
                color="#ffffff", fontsize=10, pad=10
            )

            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor="#0d1117")
            plt.close(fig)
            buf.seek(0)
            return buf.getvalue()

        except Exception as e:
            logger.warning(f"⚠️  Génération chart échouée : {e}")
            return None
