"""
chart_capital.py — Génère des graphiques de candlestick pour les trades Capital.com.
Utilise mplfinance pour créer une image PNG avec :
  - Les bougies 5m de la session
  - La zone de range pré-session (rectangle)
  - Les lignes SL, TP1, TP2, TP3 et le prix d'entrée
"""
import io
from typing import Optional
import pandas as pd

try:
    import mplfinance as mpf
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

# Thème sombre "Nemesis"
STYLE = {
    "base_mpl_style": "dark_background",
    "marketcolors": {
        "candle":  {"up": "#26a69a", "down": "#ef5350"},
        "edge":    {"up": "#26a69a", "down": "#ef5350"},
        "wick":    {"up": "#26a69a", "down": "#ef5350"},
        "ohlc":    {"up": "#26a69a", "down": "#ef5350"},
        "volume":  {"up": "#26a69a66", "down": "#ef535066"},
        "vcedge":  {"up": "#26a69a", "down": "#ef5350"},
        "vcdopcod": False,
        "alpha": 0.9,
    },
    "mavcolors": ["#FFA726", "#29B6F6"],
    "facecolor": "#131722",
    "gridcolor": "#1e2130",
    "gridstyle": "--",
    "gridaxis":  "horizontal",
    "edgecolor":  "#2a2e39",
    "figcolor":  "#131722",
    "y_on_right": True,
    "rc": {
        "axes.labelcolor":  "#9598a1",
        "axes.edgecolor":   "#2a2e39",
        "xtick.color":      "#9598a1",
        "ytick.color":      "#9598a1",
        "figure.facecolor": "#131722",
        "axes.facecolor":   "#131722",
    },
}


def generate_trade_chart(
    df: pd.DataFrame,
    instrument: str,
    sig: str,
    entry: float,
    sl: float,
    tp1: float,
    tp2: float,
    tp3: float,
    range_high: float,
    range_low: float,
    score: int,
    session: str = "London",
) -> Optional[bytes]:
    """
    Génère un graphique candlestick du trade et retourne les bytes PNG.
    Retourne None si mplfinance n'est pas installé ou si une erreur survient.
    """
    if not HAS_MPL:
        return None

    try:
        # Prend les 60 dernières bougies pour le contexte
        plot_df = df.tail(60).copy()
        plot_df.index = pd.DatetimeIndex(plot_df.index)

        mc = mpf.make_marketcolors(
            up="#26a69a", down="#ef5350",
            edge={"up": "#26a69a", "down": "#ef5350"},
            wick={"up": "#26a69a", "down": "#ef5350"},
            volume="inherit",
        )
        style = mpf.make_mpf_style(
            base_mpl_style="dark_background",
            marketcolors=mc,
            facecolor="#131722",
            edgecolor="#2a2e39",
            gridstyle="--",
            gridcolor="#1e2130",
            y_on_right=True,
            rc={
                "axes.labelcolor": "#9598a1",
                "xtick.color": "#9598a1",
                "ytick.color": "#9598a1",
            },
        )

        # Lignes horizontales
        hlines = {
            "hlines": [entry, sl, tp1, tp2, tp3, range_high, range_low],
            "colors": ["#FFA726", "#ef5350", "#26a69a", "#29B6F6", "#CE93D8",
                       "#ffffff44", "#ffffff44"],
            "linewidths": [2, 1.5, 1.5, 1.5, 1.5, 1, 1],
            "linestyle": ["-", "--", ":", ":", ":", "--", "--"],
        }

        buf = io.BytesIO()
        fig, axes = mpf.plot(
            plot_df,
            type="candle",
            style=style,
            title=f"\n{instrument}  |  {'🟢 LONG' if sig == 'BUY' else '🔴 SHORT'}  "
                  f"|  Score {score}/3  |  Session {session}",
            ylabel="Prix",
            figsize=(12, 7),
            hlines=hlines,
            returnfig=True,
            tight_layout=True,
        )

        # Légende manuelle
        ax = axes[0]
        legend_items = [
            mpatches.Patch(color="#FFA726", label=f"Entrée  {entry:.5f}"),
            mpatches.Patch(color="#ef5350", label=f"SL      {sl:.5f}"),
            mpatches.Patch(color="#26a69a", label=f"TP1     {tp1:.5f}"),
            mpatches.Patch(color="#29B6F6", label=f"TP2     {tp2:.5f}"),
            mpatches.Patch(color="#CE93D8", label=f"TP3     {tp3:.5f}"),
        ]
        ax.legend(handles=legend_items, loc="upper left",
                  facecolor="#1e2130", edgecolor="#2a2e39",
                  labelcolor="white", fontsize=9)

        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                    facecolor="#131722")
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"❌ chart_capital: {e}")
        return None
