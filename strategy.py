"""
strategy.py — Stratégie renforcée avec 6 filtres + filtre de régime de marché.

Filtres :
  0. RÉGIME DE MARCHÉ (EMA 200 slope) → pré-filtre OBLIGATOIRE
     - Slope EMA200 > +threshold → marché haussier → seulement BUY
     - Slope EMA200 < -threshold → marché baissier → seulement SELL
     - Slope ≈ 0 → marché en range → AUCUN TRADE

  1. EMA 9/21 crossover     → direction de tendance
  2. RSI 14                 → filtre momentum
  3. MACD 12/26/9           → confirmation
  4. ADX > 25               → force de tendance (évite les consolidations)
  5. Volume > MA20           → confirme l'intérêt du marché
  6. HTF (1h) aligné        → EMA 1h dans le même sens

Signal seulement si 5/6 filtres sont positifs ET régime de marché favorable.
"""

import pandas as pd
import ta
from loguru import logger
from config import (
    EMA_FAST, EMA_SLOW,
    RSI_PERIOD, RSI_BUY_MAX, RSI_SELL_MIN,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    ATR_PERIOD, ADX_PERIOD, ADX_MIN,
    VOLUME_MA_PERIOD, AVOID_HOURS_UTC,
    TIMEFRAME, HTF,
)
from datetime import datetime, timezone

SIGNAL_BUY  = "BUY"
SIGNAL_SELL = "SELL"
SIGNAL_HOLD = "HOLD"

# Seuil de confirmations requis sur 6 filtres
REQUIRED_SCORE = 5   # Min score sur 6 — 5/6 requis (backtest: 4/6 = perdant)

# Filtre régime de marché — EMA 200 slope
EMA_TREND_PERIOD = 200         # EMA longue période
SLOPE_WINDOW     = 5           # Nombre de bougies pour calculer la pente
SLOPE_THRESHOLD  = 0.0002      # Seuil de slope (0.02% par bougie = trending)
                                # En-dessous = marché en range → pas de trade


class Strategy:

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calcule tous les indicateurs sur le DataFrame OHLCV."""
        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        volume = df["volume"]

        # EMA rapide/lente
        df[f"ema{EMA_FAST}"] = ta.trend.EMAIndicator(close, window=EMA_FAST).ema_indicator()
        df[f"ema{EMA_SLOW}"] = ta.trend.EMAIndicator(close, window=EMA_SLOW).ema_indicator()

        # HTF EMA sur les bougies 1h simulées (toutes les 4 bougies 15m)
        df["ema_htf"] = ta.trend.EMAIndicator(close, window=EMA_SLOW * 4).ema_indicator()

        # EMA 200 — Filtre de régime de marché
        df["ema200"] = ta.trend.EMAIndicator(close, window=EMA_TREND_PERIOD).ema_indicator()

        # Slope de l'EMA 200 : variation % sur N bougies
        df["ema200_slope"] = df["ema200"].pct_change(periods=SLOPE_WINDOW)

        # RSI
        df["rsi"] = ta.momentum.RSIIndicator(close, window=RSI_PERIOD).rsi()

        # MACD
        macd = ta.trend.MACD(close, window_slow=MACD_SLOW, window_fast=MACD_FAST, window_sign=MACD_SIGNAL)
        df["macd"]        = macd.macd()
        df["macd_signal"] = macd.macd_signal()

        # ADX (force de tendance)
        adx_ind = ta.trend.ADXIndicator(high, low, close, window=ADX_PERIOD)
        df["adx"] = adx_ind.adx()

        # Volume MA
        df["vol_ma"] = df["volume"].rolling(VOLUME_MA_PERIOD).mean()

        # ATR
        df["atr"] = ta.volatility.AverageTrueRange(high, low, close, window=ATR_PERIOD).average_true_range()

        return df.dropna()

    def is_session_ok(self) -> bool:
        """Retourne False pendant les heures de faible liquidité."""
        hour_utc = datetime.now(timezone.utc).hour
        if hour_utc in AVOID_HOURS_UTC:
            logger.debug(f"⏰ Heure creuse ({hour_utc}h UTC) — trading suspendu")
            return False
        return True

    def market_regime(self, df: pd.DataFrame) -> str:
        """
        Détecte le régime de marché via la pente de l'EMA 200.

        Returns:
            "BULL"  → marché haussier (slope > +threshold)
            "BEAR"  → marché baissier (slope < -threshold)
            "RANGE" → marché en range (slope ≈ 0) → pas de trade
        """
        if "ema200_slope" not in df.columns or len(df) < 2:
            return "RANGE"

        slope = float(df.iloc[-1]["ema200_slope"])

        if slope > SLOPE_THRESHOLD:
            regime = "BULL"
        elif slope < -SLOPE_THRESHOLD:
            regime = "BEAR"
        else:
            regime = "RANGE"

        logger.debug(f"📈 Régime marché : {regime} | Slope EMA200 = {slope:.4%}")
        return regime

    def get_signal(self, df: pd.DataFrame) -> tuple:
        """
        Retourne (signal, score, confirmations) où :
          signal        : "BUY" / "SELL" / "HOLD"
          score         : int, nombre de confirmations
          confirmations : list[str], descriptions lisibles
        """
        if len(df) < 2:
            return SIGNAL_HOLD, 0, []

        # Filtre session
        if not self.is_session_ok():
            return SIGNAL_HOLD, 0, []

        # ── Pré-filtre OBLIGATOIRE : Régime de marché ─────────────────────
        regime = self.market_regime(df)
        if regime == "RANGE":
            logger.debug("🔇 Marché en range (EMA200 flat) — pas de signal")
            return SIGNAL_HOLD, 0, []

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        # ── Filtre 1 : EMA Crossover ──────────────────────────────────────
        ema_cross_up   = (prev[f"ema{EMA_FAST}"] <= prev[f"ema{EMA_SLOW}"]) and \
                         (curr[f"ema{EMA_FAST}"]  >  curr[f"ema{EMA_SLOW}"])
        ema_cross_down = (prev[f"ema{EMA_FAST}"] >= prev[f"ema{EMA_SLOW}"]) and \
                         (curr[f"ema{EMA_FAST}"]  <  curr[f"ema{EMA_SLOW}"])

        # ── Filtre 2 : RSI ────────────────────────────────────────────────
        rsi_buy  = 30 < curr["rsi"] < RSI_BUY_MAX
        rsi_sell = RSI_SELL_MIN < curr["rsi"] < 70

        # ── Filtre 3 : MACD Crossover ─────────────────────────────────────
        macd_up   = (prev["macd"] <= prev["macd_signal"]) and \
                    (curr["macd"]  >  curr["macd_signal"])
        macd_down = (prev["macd"] >= prev["macd_signal"]) and \
                    (curr["macd"]  <  curr["macd_signal"])

        # ── Filtre 4 : ADX (force de tendance) ────────────────────────────
        adx_ok = curr["adx"] > ADX_MIN

        # ── Filtre 5 : Volume ─────────────────────────────────────────────
        vol_ok = curr["volume"] > curr["vol_ma"]

        # ── Filtre 6 : Higher Timeframe aligné ────────────────────────────
        htf_bull = curr["close"] > curr["ema_htf"]
        htf_bear = curr["close"] < curr["ema_htf"]

        # ── Régime strict : seulement BUY en BULL, SELL en BEAR ──────────
        allow_buy  = (regime == "BULL")
        allow_sell = (regime == "BEAR")

        slope_pct  = float(curr["ema200_slope"]) * 100

        # ── Score BUY ─────────────────────────────────────────────────────
        buy_map = {
            f"EMA {EMA_FAST}/{EMA_SLOW} croisement haussier": ema_cross_up,
            f"RSI {curr['rsi']:.0f} en zone ACHAT (30-{RSI_BUY_MAX})": rsi_buy,
            f"MACD croisement haussier": macd_up,
            f"ADX {curr['adx']:.0f} > {ADX_MIN} (tendance forte)": adx_ok,
            f"Volume supérieur à la MA20": vol_ok,
            f"Tendance 1h haussière (HTF aligné)": htf_bull,
        }
        sell_map = {
            f"EMA {EMA_FAST}/{EMA_SLOW} croisement baissier": ema_cross_down,
            f"RSI {curr['rsi']:.0f} en zone VENTE ({RSI_SELL_MIN}-70)": rsi_sell,
            f"MACD croisement baissier": macd_down,
            f"ADX {curr['adx']:.0f} > {ADX_MIN} (tendance forte)": adx_ok,
            f"Volume supérieur à la MA20": vol_ok,
            f"Tendance 1h baissière (HTF aligné)": htf_bear,
        }

        buy_confs  = [label for label, ok in buy_map.items()  if ok]
        sell_confs = [label for label, ok in sell_map.items() if ok]
        buy_score  = len(buy_confs)
        sell_score = len(sell_confs)

        logger.debug(f"Score BUY={buy_score}/6 | SELL={sell_score}/6 | Régime={regime} | Slope={slope_pct:+.3f}%")

        if allow_buy and buy_score >= REQUIRED_SCORE and buy_score > sell_score:
            regime_conf = f"📈 Régime HAUSSIER (EMA200 slope +{slope_pct:.3f}%)"
            full_confs  = [regime_conf] + buy_confs
            logger.info(f"🟢 Signal ACHAT {buy_score}/6 | RSI={curr['rsi']:.1f} ADX={curr['adx']:.0f} | {regime_conf}")
            return SIGNAL_BUY, buy_score, full_confs

        if allow_sell and sell_score >= REQUIRED_SCORE and sell_score > buy_score:
            regime_conf = f"📉 Régime BAISSIER (EMA200 slope {slope_pct:.3f}%)"
            full_confs  = [regime_conf] + sell_confs
            logger.info(f"🔴 Signal VENTE {sell_score}/6 | RSI={curr['rsi']:.1f} ADX={curr['adx']:.0f} | {regime_conf}")
            return SIGNAL_SELL, sell_score, full_confs

        return SIGNAL_HOLD, 0, []

    def get_atr(self, df: pd.DataFrame) -> float:
        return float(df.iloc[-1]["atr"])
