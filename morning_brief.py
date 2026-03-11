#!/usr/bin/env python3
"""
morning_brief.py — Matinale Nemesis style Station X

Génère chaque matin :
  1. Un graphique chandelier de chaque actif (dernières 48h)
  2. Une analyse technique détaillée par actif :
     - Tendance (EMA 9/21/200)
     - Momentum (RSI + MACD)
     - Volatilité (ATR)
     - Niveaux clés (support/résistance)
     - Biais haussier / baissier
  3. Envoi Telegram avec photo + texte long comme Station X

Appelé depuis main.py chaque matin à 07:00 UTC (session London).
"""
import sys, io, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")   # headless — pas de fenêtre
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import mplfinance as mpf
from datetime import datetime, timezone, timedelta
from loguru import logger

try:
    from brokers.capital_client import CapitalClient
    from strategy import Strategy
    _brief_strategy = Strategy()
except ImportError:
    logger.error("morning_brief: capital_client/strategy introuvable")


# ─── Graphique chandelier ─────────────────────────────────────────────────────

def generate_chart(df: pd.DataFrame, symbol: str, bias: str) -> bytes:
    """
    Génère un graphique chandelier professionnel.
    Retourne les bytes PNG de l'image.
    """
    # TASK-065 : guard contre DataFrame vide (Capital.com peut retourner None/vide)
    if df is None or df.empty or len(df) < 5:
        logger.warning(f"⚠️ generate_chart({symbol}) : DataFrame vide ou insuffisant, chart ignoré.")
        return b""

    ticker = symbol  # Capital.com : GOLD, EURUSD, etc. (pas de /USDT)

    # Garde les 96 dernières bougies de 5min = 8h
    df_plot = df.tail(96).copy()

    # Couleur du titre selon biais
    title_color = "#00c896" if "haussier" in bias.lower() else \
                  "#ff4560" if "baissier" in bias.lower() else "#f0b429"

    # Style dark custom
    mc = mpf.make_marketcolors(
        up     = "#00c896",
        down   = "#ff4560",
        edge   = {"up": "#00c896", "down": "#ff4560"},
        wick   = {"up": "#00c896", "down": "#ff4560"},
        volume = {"up": "#00c896aa", "down": "#ff4560aa"},
    )
    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        marketcolors=mc,
        facecolor="#1a1a2e",
        edgecolor="#2d2d4e",
        figcolor="#1a1a2e",
        gridcolor="#2d2d4e",
        gridstyle="--",
        gridaxis="both",
        y_on_right=True,
        rc={
            "font.family":       "monospace",
            "axes.labelcolor":   "#9999bb",
            "xtick.color":       "#9999bb",
            "ytick.color":       "#9999bb",
            "figure.facecolor":  "#1a1a2e",
            "axes.facecolor":    "#1a1a2e",
        }
    )

    # Ajouter EMA 9 et 21 si dispo
    add_plots = []
    if "ema9" in df_plot.columns and "ema21" in df_plot.columns:
        add_plots.append(mpf.make_addplot(df_plot["ema9"],  color="#f0b429", width=1.2, alpha=0.9))
        add_plots.append(mpf.make_addplot(df_plot["ema21"], color="#8b5cf6", width=1.2, alpha=0.9))

    fig, axes = mpf.plot(
        df_plot,
        type="candle",
        style=style,
        title=f"\n  ⚡ Nemesis — {symbol} | Biais : {bias}",
        volume=True,
        addplot=add_plots if add_plots else None,
        datetime_format="%H:%M",
        tight_layout=True,
        figsize=(12, 7),
        returnfig=True,
    )

    axes[0].set_title(
        f"\u26a1 Nemesis — {symbol} | Biais : {bias}",
        color=title_color,
        fontsize=13,
        fontweight="bold",
        pad=10,
    )

    # Timestamp
    fig.text(
        0.99, 0.01,
        f"Nemesis v2.0 | {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC",
        ha="right", va="bottom",
        fontsize=8, color="#666688",
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor="#1a1a2e", edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ─── Analyse technique par actif ──────────────────────────────────────────────

def analyze_asset(symbol: str, df: pd.DataFrame) -> dict:
    """
    Retourne un dictionnaire d'analyse technique pour un actif.
    """
    ticker = symbol  # Capital.com : instrument epic (GOLD, EURUSD, etc.)
    last   = df.iloc[-1]
    prev   = df.iloc[-2] if len(df) > 1 else last

    price   = float(last["close"])
    ema9    = float(last["ema9"])   if "ema9"    in df.columns else price
    ema21   = float(last["ema21"])  if "ema21"   in df.columns else price
    ema200  = float(last["ema200"]) if "ema200"  in df.columns else price
    rsi     = float(last["rsi"])    if "rsi"     in df.columns else 50
    atr     = float(last["atr"])    if "atr"     in df.columns else 0
    macd    = float(last["macd"])   if "macd"    in df.columns else 0
    macd_s  = float(last["macd_sig"]) if "macd_sig" in df.columns else 0
    adx     = float(last["adx"])   if "adx"     in df.columns else 0
    volume  = float(last["volume"])
    vol_ma  = float(last["vol_ma"]) if "vol_ma" in df.columns else volume

    # ─── Support & Résistance (rolling 48 bougies) ───────────────────────────
    recent = df.tail(96)
    support    = float(recent["low"].rolling(20).min().iloc[-1])
    resistance = float(recent["high"].rolling(20).max().iloc[-1])
    pivot      = (resistance + support + price) / 3

    # ─── Détection des niveaux clés ──────────────────────────────────────────
    # FVG simulé : gap > 2× ATR entre deux bougies
    fvg_zones = []
    for i in range(len(df) - 3, max(len(df) - 50, 0), -1):
        if abs(df.iloc[i]["close"] - df.iloc[i+1]["open"]) > atr * 1.5:
            fvg_zones.append(round((df.iloc[i]["close"] + df.iloc[i+1]["open"]) / 2, 4))
        if len(fvg_zones) >= 2:
            break

    # ─── Biais ───────────────────────────────────────────────────────────────
    bull_points = 0
    bear_points = 0

    # Tendance EMA
    if ema9 > ema21 and ema21 > ema200:
        bull_points += 2
    elif ema9 < ema21 and ema21 < ema200:
        bear_points += 2
    elif ema9 > ema21:
        bull_points += 1
    else:
        bear_points += 1

    # RSI
    if rsi > 55:
        bull_points += 1
    elif rsi < 45:
        bear_points += 1

    # MACD
    if macd > macd_s:
        bull_points += 1
    else:
        bear_points += 1

    # Prix vs EMA 200
    if price > ema200:
        bull_points += 1
    else:
        bear_points += 1

    # Biais final
    if bull_points >= 4:
        bias     = "Haussier 🟢"
        bias_txt = "haussier"
    elif bear_points >= 4:
        bias     = "Baissier 🔴"
        bias_txt = "baissier"
    else:
        bias     = "Neutre ⚪"
        bias_txt = "neutre"

    # ─── Tendance courte ─────────────────────────────────────────────────────
    trend_1h = "↗ Hausse" if price > float(df.tail(12)["close"].iloc[0]) else "↘ Baisse"
    trend_4h = "↗ Hausse" if price > float(df.tail(48)["close"].iloc[0]) else "↘ Baisse"

    # ─── État du marché ──────────────────────────────────────────────────────
    if adx > 30:
        marche = "Marché en tendance forte"
    elif adx > 20:
        marche = "Tendance modérée"
    else:
        marche = "Marché en range (consolidation)"

    # ─── Volume ──────────────────────────────────────────────────────────────
    vol_signal = "Volume au-dessus de la moyenne 📈" if volume > vol_ma else "Volume faible"

    return {
        "ticker":     ticker,
        "price":      price,
        "ema9":       ema9,
        "ema21":      ema21,
        "ema200":     ema200,
        "rsi":        rsi,
        "adx":        adx,
        "atr":        atr,
        "atr_pct":    atr / price * 100,
        "support":    support,
        "resistance": resistance,
        "pivot":      pivot,
        "fvg_zones":  fvg_zones,
        "bias":       bias,
        "bias_txt":   bias_txt,
        "trend_1h":   trend_1h,
        "trend_4h":   trend_4h,
        "marche":     marche,
        "vol_signal": vol_signal,
        "macd_up":    macd > macd_s,
        # BUG FIX #P : supprimer la 2ème clé 'rsi' qui écrasait silencieusement la 1ère
    }


def format_analysis(a: dict) -> str:
    """
    Formate l'analyse dans le style Station X.
    Long, précis, professionnel.
    """
    t = a["ticker"]
    p = a["price"]
    fvg_str = ""
    if a["fvg_zones"]:
        fvg_str = "  • " + " | ".join([f"{z:,.4f}" for z in a["fvg_zones"]])

    return (
        f"📌 <b>{t}</b>\n"
        f"\n"
        f"💵 Prix actuel : <b>{p:,.4f}</b>\n"
        f"📈 Tendance 1h : <b>{a['trend_1h']}</b>  |  4h : <b>{a['trend_4h']}</b>\n"
        f"\n"
        f"📊 <b>Indicateurs</b>\n"
        f"  • EMA 9 / 21 / 200 : {a['ema9']:,.4f} / {a['ema21']:,.4f} / {a['ema200']:,.4f}\n"
        f"  • RSI : <b>{a['rsi']:.0f}</b>  {'(zone de sur-achat ⚠️)' if a['rsi'] > 70 else '(zone de sur-vente ⚠️)' if a['rsi'] < 30 else '(neutre)'}\n"
        f"  • MACD : <b>{'Haussier ↗' if a['macd_up'] else 'Baissier ↘'}</b>\n"
        f"  • ADX : <b>{a['adx']:.0f}</b>  —  {a['marche']}\n"
        f"  • ATR : {a['atr']:.4f}  ({a['atr_pct']:.2f}% du prix)\n"
        f"\n"
        f"🗺 <b>Niveaux clés</b>\n"
        f"  • Résistance : <b>{a['resistance']:,.4f}</b>\n"
        f"  • Pivot      : <b>{a['pivot']:,.4f}</b>\n"
        f"  • Support    : <b>{a['support']:,.4f}</b>\n"
        + (f"  • FVG zones  :\n{fvg_str}\n" if fvg_str else "")
        + f"\n"
        f"  • {a['vol_signal']}\n"
        f"\n"
        f"Biais : <b>{a['bias']}</b>"
    )


# ─── Matinale complète ────────────────────────────────────────────────────────

def generate_morning_brief(symbols: list, telegram_notifier=None) -> None:
    """
    Génère et envoie la matinale complète.
    Pour chaque actif : 1 photo + analyse détaillée.
    Données fetchées depuis Capital.com (instruments réellement tradés).
    """
    try:
        client = CapitalClient()
        if not client.available:
            logger.error("🌅 Matinale — Capital.com non disponible")
            return
    except Exception as e:
        logger.error(f"🌅 Matinale — erreur client: {e}")
        return

    d = datetime.now(timezone.utc)
    months = ["Jan","Fév","Mar","Avr","Mai","Juin",
              "Juil","Août","Sep","Oct","Nov","Déc"]
    date_str = f"{d.day} {months[d.month - 1]}"

    logger.info(f"🌅 Matinale du {date_str} — génération en cours...")

    # ─── Message d'intro ─────────────────────────────────────────────────────
    if telegram_notifier:
        if telegram_notifier.router:
            telegram_notifier.router.send_briefing(
                f"☀️ <b>Matinale du {date_str}</b>\n"
                f"\n"
                f"Bonjour à tous ! C'est reparti pour le point marché du matin.\n"
                f"Voici l'analyse complète de chaque actif pour la session "
                f"<b>London ({d.hour:02d}h UTC)</b>.\n"
                f"\n"
                f"Prenez le temps de lire chaque analyse avant de trader. "
                f"Les niveaux clés sont à surveiller de près. 🎯"
            )

    # ─── Analyse par actif ───────────────────────────────────────────────────
    analyses = []
    for symbol in symbols:
        try:
            # 5m, 96 bougies = 8h d'historique pour l'analyse visuelle
            df = client.fetch_ohlcv(symbol, timeframe="5m", count=300)
            if df is None or df.empty:
                logger.warning(f"🌅 Matinale — {symbol} : pas de données")
                continue
            df = _brief_strategy.compute_indicators(df)
            a  = analyze_asset(symbol, df)
            analyses.append((symbol, df, a))
        except Exception as e:
            logger.error(f"🌅 Matinale — {symbol} erreur: {e}")
            continue

    # ─── Envoi photo + analyse par actif ─────────────────────────────────────
    for symbol, df, a in analyses:
        try:
            chart_bytes = generate_chart(df, symbol, a["bias_txt"])
            analysis    = format_analysis(a)

            if telegram_notifier:
                if telegram_notifier.router and chart_bytes:
                    telegram_notifier.router.send_photo_to(
                        "briefing", chart_bytes, caption=analysis,
                    )
                elif telegram_notifier.router:
                    telegram_notifier.router.send_briefing(analysis)
            else:
                print(f"\n{'='*60}")
                print(analysis)
        except Exception as e:
            logger.error(f"🌅 Matinale chart {symbol}: {e}")
            # Envoie au moins l'analyse texte si la photo échoue
            if telegram_notifier and telegram_notifier.router:
                telegram_notifier.router.send_briefing(format_analysis(a))

    # ─── Conclusion globale ───────────────────────────────────────────────────
    if telegram_notifier and analyses:
        bull_count = sum(1 for _, _, a in analyses if "haussier" in a["bias"].lower())
        bear_count = sum(1 for _, _, a in analyses if "baissier" in a["bias"].lower())

        if bull_count >= 3:
            global_bias = "HAUSSIER 🟢 — Conditions favorables aux longs"
        elif bear_count >= 3:
            global_bias = "BAISSIER 🔴 — Prudence, conditions favorables aux shorts"
        else:
            global_bias = "MIXTE ⚪ — Sélectif, attendre les confirmations"

        # Best/worst performers by ATR volatility
        sorted_by_atr = sorted(analyses, key=lambda x: x[2].get("atr_pct", 0), reverse=True)
        hot_assets = [a[2]["ticker"] for a in sorted_by_atr[:3]]
        cold_assets = [a[2]["ticker"] for a in sorted_by_atr[-2:]]

        actifs_summary = "\n".join([
            f"{'🟢' if 'haussier' in a['bias'].lower() else '🔴' if 'baissier' in a['bias'].lower() else '⚪'} "
            f"<b>{a['ticker']}</b> — {a['bias']} | Support : {a['support']:,.4f}"
            for _, _, a in analyses
        ])

        # System health section
        system_lines = "\n\n🤖 <b>Santé Système</b>"
        try:
            from ml_scorer import MLScorer
            ml = MLScorer()
            ml_stats = ml.stats
            if ml_stats.get("model_ready"):
                system_lines += f"\n  🧠 ML : actif ({ml_stats['samples']} samples)"
            else:
                system_lines += f"\n  🧠 ML : entraînement ({ml_stats['samples']}/{ml_stats['min_required']})"
        except Exception:
            system_lines += "\n  🧠 ML : —"

        system_lines += f"\n  🔥 Top volatilité : {', '.join(hot_assets)}"
        system_lines += f"\n  ❄️ Faible activité : {', '.join(cold_assets)}"

        telegram_notifier.router.send_briefing(
            f"📋 <b>SYNTHÈSE — Matinale du {date_str}</b>\n"
            f"\n"
            f"{actifs_summary}\n"
            f"\n"
            f"Biais global : <b>{global_bias}</b>\n"
            f"{system_lines}\n"
            f"\n"
            f"Bonne session à tous, on reste disciplinés ! 💪\n"
            f"Les alertes de trade arriveront automatiquement dès qu'un signal est confirmé ✅"
        )

    logger.info(f"🌅 Matinale envoyée — {len(analyses)} actifs analysés")


# ─── Test standalone ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    from brokers.capital_client import CAPITAL_INSTRUMENTS
    print("\n🌅 Test Matinale Nemesis (Capital.com)\n")
    generate_morning_brief(CAPITAL_INSTRUMENTS, telegram_notifier=None)
