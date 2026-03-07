"""
strategy.py — Calcule les indicateurs techniques et génère les signaux de trading.

Stratégie Triple Confluence :
  - EMA 9 / EMA 21  → Direction de la tendance
  - RSI 14           → Filtre momentum (évite surachat/survente)
  - MACD 12/26/9     → Confirmation du signal
  
Règle : Au moins 2/3 conditions doivent être remplies pour générer un signal.
"""

import pandas as pd
import ta
from loguru import logger
from config import (
    EMA_FAST, EMA_SLOW,
    RSI_PERIOD, RSI_BUY_MAX, RSI_SELL_MIN,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    ATR_PERIOD,
)

# Constantes de signal
SIGNAL_BUY  = "BUY"
SIGNAL_SELL = "SELL"
SIGNAL_HOLD = "HOLD"


class Strategy:
    """Moteur de stratégie — calcul des indicateurs et génération de signaux."""

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Ajoute toutes les colonnes d'indicateurs au DataFrame OHLCV."""
        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        # ── EMA ─────────────────────────────────────────────────────────────
        df[f"ema{EMA_FAST}"] = ta.trend.EMAIndicator(close, window=EMA_FAST).ema_indicator()
        df[f"ema{EMA_SLOW}"] = ta.trend.EMAIndicator(close, window=EMA_SLOW).ema_indicator()

        # ── RSI ─────────────────────────────────────────────────────────────
        df["rsi"] = ta.momentum.RSIIndicator(close, window=RSI_PERIOD).rsi()

        # ── MACD ────────────────────────────────────────────────────────────
        macd_indicator     = ta.trend.MACD(close, window_slow=MACD_SLOW, window_fast=MACD_FAST, window_sign=MACD_SIGNAL)
        df["macd"]         = macd_indicator.macd()
        df["macd_signal"]  = macd_indicator.macd_signal()
        df["macd_hist"]    = macd_indicator.macd_diff()

        # ── ATR (Stop-Loss dynamique) ────────────────────────────────────────
        df["atr"] = ta.volatility.AverageTrueRange(high, low, close, window=ATR_PERIOD).average_true_range()

        return df.dropna()

    def get_signal(self, df: pd.DataFrame) -> str:
        """
        Analyse la dernière bougie et retourne BUY / SELL / HOLD.
        Nécessite que compute_indicators() ait été appelé avant.
        """
        if len(df) < 2:
            return SIGNAL_HOLD

        curr = df.iloc[-1]   # Bougie actuelle
        prev = df.iloc[-2]   # Bougie précédente

        # ── Conditions ACHAT ───────────────────────────────────────────────
        ema_cross_up    = (prev[f"ema{EMA_FAST}"] <= prev[f"ema{EMA_SLOW}"]) and \
                          (curr[f"ema{EMA_FAST}"]  >  curr[f"ema{EMA_SLOW}"])
        rsi_buy_ok      = 30 < curr["rsi"] < RSI_BUY_MAX
        macd_cross_up   = (prev["macd"] <= prev["macd_signal"]) and \
                          (curr["macd"]  >  curr["macd_signal"])

        buy_conditions  = [ema_cross_up, rsi_buy_ok, macd_cross_up]
        buy_score       = sum(buy_conditions)

        # ── Conditions VENTE ───────────────────────────────────────────────
        ema_cross_down  = (prev[f"ema{EMA_FAST}"] >= prev[f"ema{EMA_SLOW}"]) and \
                          (curr[f"ema{EMA_FAST}"]  <  curr[f"ema{EMA_SLOW}"])
        rsi_sell_ok     = RSI_SELL_MIN < curr["rsi"] < 70
        macd_cross_down = (prev["macd"] >= prev["macd_signal"]) and \
                          (curr["macd"]  <  curr["macd_signal"])

        sell_conditions = [ema_cross_down, rsi_sell_ok, macd_cross_down]
        sell_score      = sum(sell_conditions)

        # ── Décision (au moins 2/3 conditions) ────────────────────────────
        logger.debug(
            f"Score BUY={buy_score}/3 "
            f"[EMA↑={ema_cross_up}, RSI={curr['rsi']:.1f} ok={rsi_buy_ok}, MACD↑={macd_cross_up}] | "
            f"Score SELL={sell_score}/3 "
            f"[EMA↓={ema_cross_down}, RSI ok={rsi_sell_ok}, MACD↓={macd_cross_down}]"
        )

        if buy_score >= 2 and buy_score > sell_score:
            logger.info(f"🟢 Signal ACHAT  — score {buy_score}/3 | RSI={curr['rsi']:.1f} | ATR={curr['atr']:.2f}")
            return SIGNAL_BUY

        if sell_score >= 2 and sell_score > buy_score:
            logger.info(f"🔴 Signal VENTE  — score {sell_score}/3 | RSI={curr['rsi']:.1f} | ATR={curr['atr']:.2f}")
            return SIGNAL_SELL

        logger.debug("⏸️  Signal HOLD  — pas de confluence suffisante")
        return SIGNAL_HOLD

    def get_atr(self, df: pd.DataFrame) -> float:
        """Retourne la valeur ATR de la dernière bougie."""
        return float(df.iloc[-1]["atr"])
