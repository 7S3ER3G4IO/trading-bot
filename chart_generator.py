"""
chart_generator.py — Génère un graphique de chandeliers japonais avec
les indicateurs et les niveaux de trade (entrée, TP1/2/3, SL).

Utilise mplfinance pour le graphique + matplotlib pour les annotations.
Envoie ensuite le PNG directement dans Telegram.
"""

import io
import os
import asyncio
from typing import Optional
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # Backend non-interactif (pas d'écran requis)
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import mplfinance as mpf
from loguru import logger


class ChartGenerator:
    """Génère et envoie des graphiques de trades sur Telegram."""

    def __init__(self):
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
        df: pd.DataFrame,
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
        """
        Génère un graphique PNG et retourne les bytes.

        Args:
            df: DataFrame OHLCV avec colonnes ema9, ema21, macd, adx, rsi
            symbol: ex "BTC/USDT"
            side: "BUY" ou "SELL"
            entry, tp1, tp2, tp3, sl: niveaux de prix
        Returns:
            bytes du PNG ou None si erreur
        """
        try:
            # Garder les 60 dernières bougies
            plot_df = df.tail(60).copy()

            # Lignes EMA
            ema_fast = mpf.make_addplot(
                plot_df[f"ema9"], color="#00d4ff", width=1.2,
                label="EMA 9"
            )
            ema_slow = mpf.make_addplot(
                plot_df[f"ema21"], color="#ff8c00", width=1.2,
                label="EMA 21"
            )

            # MACD au panneau bas
            macd_plot = mpf.make_addplot(
                plot_df["macd"], panel=1, color="#00ff88", width=1, ylabel="MACD"
            )
            macd_sig  = mpf.make_addplot(
                plot_df["macd_signal"], panel=1, color="#ff4466", width=1
            )

            fig, axes = mpf.plot(
                plot_df,
                type="candle",
                style=self._style,
                addplot=[ema_fast, ema_slow, macd_plot, macd_sig],
                returnfig=True,
                figsize=(10, 6),
                tight_layout=True,
                warn_too_much_data=200,
            )

            ax = axes[0]

            # Lignes horizontales SL / TP
            levels = [
                (sl,  "#ff3333", "━━ SL"),
                (tp1, "#ffaa00", "━━ TP1"),
                (tp2, "#00cc44", "━━ TP2"),
                (tp3, "#00ff88", "━━ TP3"),
            ]
            for price, color, label in levels:
                ax.axhline(y=price, color=color, linewidth=1, linestyle="--", alpha=0.8)
                ax.text(
                    0.01, price, f" {label}: {price:,.2f}",
                    transform=ax.get_yaxis_transform(),
                    color=color, fontsize=8, va="center",
                    bbox=dict(facecolor="#0d1117", edgecolor="none", pad=1)
                )

            # Flèche entrée sur la dernière bougie
            entry_x = len(plot_df) - 1
            arrow_color = "#00ff00" if side == "BUY" else "#ff0000"
            arrow_dir   = "↑ ACHAT" if side == "BUY" else "↓ VENTE"
            ax.annotate(
                f" {arrow_dir}\n {entry:,.2f}",
                xy=(entry_x, entry),
                fontsize=9,
                color=arrow_color,
                fontweight="bold",
                bbox=dict(facecolor="#1a1a2e", edgecolor=arrow_color, pad=3, alpha=0.9),
            )

            # Titre
            pair = symbol.replace("/", "")
            action = "ACHAT" if side == "BUY" else "VENTE"
            ax.set_title(
                f"⚡ AlphaTrader | {pair} {action} | Score {score}/6 | {indicators_desc}",
                color="#ffffff", fontsize=10, pad=10
            )

            # Export PNG en mémoire
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor="#0d1117")
            plt.close(fig)
            buf.seek(0)
            return buf.getvalue()

        except Exception as e:
            logger.error(f"❌ Erreur génération chart : {e}")
            return None

    async def send_chart(self, bot, chat_id: str, chart_bytes: bytes, caption: str):
        """Envoie le chart PNG dans Telegram."""
        try:
            buf = io.BytesIO(chart_bytes)
            buf.name = "chart.png"
            await bot.send_photo(
                chat_id=chat_id,
                photo=buf,
                caption=caption,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"❌ Envoi chart Telegram : {e}")
