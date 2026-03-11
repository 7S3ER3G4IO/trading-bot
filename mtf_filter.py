"""
mtf_filter.py — S-2: Multi-Timeframe Confluence Scoring

Au lieu de BLOQUER les signaux contradictoires HTF, retourne un BONUS de score:
- Triple confluence (signal == 1h == 4h) : +0.15
- Double confluence (signal == 1h OR 4h)  : +0.08
- Neutre (HTF neutre)                     : +0.0
- Contre-signal (signal contradictoire)   : -0.10 (pénalité)

Utilise le cache OHLCV pour les données 4H au lieu de faire des requêtes REST séparées.
"""

import time
import pandas as pd
import ta
from loguru import logger


CACHE_TTL = 300  # 5 minutes


class MTFFilter:

    def __init__(self, capital_client=None):
        self._client = capital_client
        self._cache: dict = {}
        self._cache_ts: dict = {}

    def _get_client(self):
        if self._client:
            return self._client
        from brokers.capital_client import CapitalClient
        self._client = CapitalClient()
        return self._client

    def _fetch_tf(self, symbol: str, timeframe: str, limit: int = 50) -> pd.DataFrame:
        """Fetch recent candles for a given timeframe with caching."""
        key = f"{symbol}_{timeframe}"
        now = time.time()
        if key in self._cache and (now - self._cache_ts.get(key, 0)) < CACHE_TTL:
            return self._cache[key]

        try:
            client = self._get_client()
            df = client.fetch_ohlcv(symbol, timeframe=timeframe, count=limit)
            if df is None or df.empty:
                return pd.DataFrame()

            df["ema9"]  = ta.trend.EMAIndicator(df["close"], window=9).ema_indicator()
            df["ema21"] = ta.trend.EMAIndicator(df["close"], window=21).ema_indicator()
            df["ema200"]= ta.trend.EMAIndicator(df["close"], window=200).ema_indicator()
            df["rsi"]   = ta.momentum.RSIIndicator(df["close"], window=14).rsi()

            self._cache[key]    = df
            self._cache_ts[key] = now
            return df
        except Exception as e:
            logger.debug(f"MTF fetch {symbol} {timeframe}: {e}")
            return pd.DataFrame()

    def _tf_bias(self, df: pd.DataFrame) -> str:
        """Returns BULL / BEAR / NEUTRAL for a given DataFrame."""
        if df.empty or len(df) < 3:
            return "NEUTRAL"
        last  = df.iloc[-1]
        ema9  = last.get("ema9",  0)
        ema21 = last.get("ema21", 0)
        close = last.get("close", 0)
        ema200= last.get("ema200", 0)

        bull = (ema9 > ema21) and (close > ema200)
        bear = (ema9 < ema21) and (close < ema200)

        if bull:   return "BULL"
        if bear:   return "BEAR"
        return "NEUTRAL"

    # ═══════════════════════════════════════════════════════════════════════
    #  S-2: SCORING MODE (replaces blocking mode)
    # ═══════════════════════════════════════════════════════════════════════

    def score_confluence(self, symbol: str, signal: str) -> float:
        """
        S-2: Retourne un bonus/pénalité de score basé sur la confluence MTF.
        
        Returns:
            +0.15 : triple confluence (signal aligns with 1h AND 4h)
            +0.08 : partial confluence (signal aligns with 1h OR 4h)
             0.00 : neutral (HTF neutral)
            -0.10 : contra-signal (HTF contradicts signal)
        """
        if signal == "HOLD":
            return 0.0

        bias_1h = self._tf_bias(self._fetch_tf(symbol, "1h", 60))
        bias_4h = self._tf_bias(self._fetch_tf(symbol, "4h", 60))

        expected_bias = "BULL" if signal == "BUY" else "BEAR"
        opposite_bias = "BEAR" if signal == "BUY" else "BULL"

        match_1h = bias_1h == expected_bias
        match_4h = bias_4h == expected_bias
        contra_1h = bias_1h == opposite_bias
        contra_4h = bias_4h == opposite_bias

        if match_1h and match_4h:
            logger.debug(f"✅ MTF {symbol} {signal} triple confluence: 1h={bias_1h} 4h={bias_4h} → +0.15")
            return 0.15
        elif match_1h or match_4h:
            logger.debug(f"⚡ MTF {symbol} {signal} partial: 1h={bias_1h} 4h={bias_4h} → +0.08")
            return 0.08
        elif contra_1h and contra_4h:
            logger.debug(f"❌ MTF {symbol} {signal} contra: 1h={bias_1h} 4h={bias_4h} → -0.10")
            return -0.10
        elif contra_1h or contra_4h:
            logger.debug(f"⚠️ MTF {symbol} {signal} mixed: 1h={bias_1h} 4h={bias_4h} → -0.05")
            return -0.05
        else:
            return 0.0

    def validate_signal(self, symbol: str, signal: str) -> bool:
        """
        Legacy compatibility: returns True if MTF score >= -0.05.
        Hard-blocks only when BOTH HTF contradict the signal.
        """
        if signal == "HOLD":
            return False
        bonus = self.score_confluence(symbol, signal)
        return bonus >= -0.05  # Block only on full contradiction (-0.10)

    def get_htf_context(self, symbol: str) -> dict:
        """Retourne le contexte complet des timeframes supérieurs."""
        df_1h = self._fetch_tf(symbol, "1h", 100)
        df_4h = self._fetch_tf(symbol, "4h", 100)
        df_1d = self._fetch_tf(symbol, "1d", 30)

        return {
            "1h":  self._tf_bias(df_1h),
            "4h":  self._tf_bias(df_4h),
            "1d":  self._tf_bias(df_1d),
            "aligned": self._tf_bias(df_1h) == self._tf_bias(df_4h),
        }
