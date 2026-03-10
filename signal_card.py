"""
signal_card.py — Feature K : Signal Cards visuels via Telegram
Génère un chart mplfinance 5m (2 dernières heures) annoté avec
Entry / SL / TP1 / TP2 / TP3 et les méta-données du trade.
"""
import io
from typing import Optional
import pandas as pd
import numpy as np
from loguru import logger

try:
    import mplfinance as mpf
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    HAS_MPF = True
except ImportError:
    HAS_MPF = False


def generate_signal_card(
    df: pd.DataFrame,
    instrument: str,
    direction: str,
    entry: float,
    sl: float,
    tp1: float,
    tp2: float,
    tp3: float,
    score: int = 0,
    confirmations: list = None,
    regime: str = "RANGING",
    fear_greed: Optional[int] = None,
    session: str = "",
) -> Optional[bytes]:
    """
    Génère une image PNG Signal Card professionnelle.

    Parameters
    ----------
    df           : DataFrame OHLCV avec index DatetimeIndex (bougies 5m)
    instrument   : épique Capital.com (ex: 'GOLD')
    direction    : 'BUY' | 'SELL'
    entry/sl/tp* : niveaux de prix
    score        : score de confirmation (0-7)
    confirmations: liste de strings (ex: ['ADX 22', 'VWAP✓', 'OFI✓ ↑↑'])
    regime       : 'RANGING' | 'TREND_UP' | 'TREND_DOWN'
    fear_greed   : valeur 0-100 ou None
    session      : 'London' | 'NY' | ''

    Returns
    -------
    bytes PNG ou None en cas d'erreur
    """
    if not HAS_MPF:
        logger.warning("mplfinance non disponible — signal card skipped")
        return None

    try:
        # ─── Préparer les données : 24 dernières bougies (2h de 5m) ───────────
        df_plot = df.tail(24).copy()
        if not isinstance(df_plot.index, pd.DatetimeIndex):
            logger.debug("signal_card: index non DatetimeIndex — skip")
            return None

        # Assurer les colonnes OHLCV en minuscules pour mplfinance
        df_plot.columns = [c.lower() for c in df_plot.columns]
        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(df_plot.columns):
            logger.debug(f"signal_card: colonnes manquantes {required - set(df_plot.columns)}")
            return None

        df_plot = df_plot[["open", "high", "low", "close", "volume"]].copy()
        df_plot = df_plot.astype(float).dropna()

        # ─── Style dark mplfinance ────────────────────────────────────────────
        mc = mpf.make_marketcolors(
            up="#22d3a0", down="#ff4f6e",
            edge="inherit",
            wick={"up": "#22d3a0", "down": "#ff4f6e"},
            volume={"up": "#22d3a054", "down": "#ff4f6e54"},
        )
        style = mpf.make_mpf_style(
            base_mpf_style="nightclouds",
            marketcolors=mc,
            facecolor="#060911",
            edgecolor="#1e2a45",
            figcolor="#060911",
            gridstyle="--",
            gridcolor="#1e2a4540",
            rc={
                "axes.labelcolor": "#5a6a8a",
                "xtick.color": "#5a6a8a",
                "ytick.color": "#5a6a8a",
                "font.family": "monospace",
            },
        )

        # ─── Lignes horizontales annotées ─────────────────────────────────────
        color_entry = "#4f9eff"
        color_sl    = "#ff4f6e"
        color_tp1   = "#22d3a0"
        color_tp2   = "#7c5cfc"
        color_tp3   = "#f0b429"

        hlines = dict(
            hlines=[entry, sl, tp1, tp2, tp3],
            colors=[color_entry, color_sl, color_tp1, color_tp2, color_tp3],
            linestyle=["--", "--", "-.", "-.", "-."],
            linewidths=[1.5, 1.5, 1.0, 1.0, 1.0],
        )

        # ─── Générer la figure ─────────────────────────────────────────────────
        fig, axes = mpf.plot(
            df_plot,
            type="candle",
            style=style,
            volume=True,
            figsize=(10, 6),
            returnfig=True,
            panel_ratios=(3, 1),
            **hlines,
            tight_layout=True,
        )

        ax_main = axes[0]

        # ─── Annotations des niveaux ──────────────────────────────────────────
        price_decimals = 5 if entry < 10 else (2 if entry < 1000 else 0)
        fmt = f"{{:.{price_decimals}f}}"
        x_end = len(df_plot) - 0.5

        def _annotate(ax, price, label, color, xpos=None):
            xpos = xpos or x_end
            ax.annotate(
                f" {label}: {fmt.format(price)}",
                xy=(xpos, price),
                xytext=(xpos + 0.2, price),
                color=color,
                fontsize=7,
                fontweight="bold",
                va="center",
                ha="left",
            )

        _annotate(ax_main, entry, "ENTRY", color_entry)
        _annotate(ax_main, sl,    "SL",    color_sl)
        _annotate(ax_main, tp1,   "TP1",   color_tp1)
        _annotate(ax_main, tp2,   "TP2",   color_tp2)
        _annotate(ax_main, tp3,   "TP3",   color_tp3)

        # ─── Titre + sous-titre ───────────────────────────────────────────────
        dir_emoji  = "🟢 BUY" if direction == "BUY" else "🔴 SELL"
        regime_str = {"TREND_UP": "📈 TREND_UP", "TREND_DOWN": "📉 TREND_DOWN"}.get(regime, "⬛ RANGING")
        fg_str     = f" | F&G {fear_greed}" if fear_greed is not None else ""
        session_str = f" | {session}" if session else ""
        conf_str   = " · ".join(confirmations[:4]) if confirmations else ""

        title = (
            f"{dir_emoji}  {instrument}  |  Score {score}/7  "
            f"|  {regime_str}{fg_str}{session_str}"
        )
        ax_main.set_title(title, color="#c8d6f0", fontsize=10, pad=6, loc="left")

        if conf_str:
            ax_main.text(
                0.01, 0.97, conf_str,
                transform=ax_main.transAxes,
                color="#5a6a8a", fontsize=7.5,
                va="top", ha="left",
            )

        # RR annotation
        rr = abs(tp2 - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
        ax_main.text(
            0.01, 0.02, f"R:R ≈ {rr:.1f}x",
            transform=ax_main.transAxes,
            color="#f0b429", fontsize=8, va="bottom", ha="left",
        )

        # ─── Export PNG ───────────────────────────────────────────────────────
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        logger.info(f"📸 Signal card générée — {instrument} {direction} score={score}")
        return buf.read()

    except Exception as e:
        logger.error(f"❌ signal_card: {e}")
        return None
