"""
tests/test_mtf_filter.py — Tests unitaires de MTFFilter (sans réseau).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import pandas as pd
import pytest
from unittest.mock import MagicMock

from mtf_filter import MTFFilter


def _make_df(n: int = 60, trend: str = "bull") -> pd.DataFrame:
    """Génère un DataFrame OHLCV avec indicateurs EMA pré-calculés."""
    rng  = np.random.default_rng(7)
    idx  = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    base = 1800.0
    prices = [base]
    for _ in range(n - 1):
        drift = 0.001 if trend == "bull" else -0.001
        prices.append(prices[-1] * (1 + drift + rng.normal(0, 0.001)))
    close = np.array(prices)
    df = pd.DataFrame({
        "open":   close * 0.999,
        "high":   close * 1.002,
        "low":    close * 0.998,
        "close":  close,
        "volume": rng.uniform(100, 500, n),
    }, index=idx)
    import ta
    df["ema9"]   = ta.trend.EMAIndicator(df["close"], window=9).ema_indicator()
    df["ema21"]  = ta.trend.EMAIndicator(df["close"], window=21).ema_indicator()
    df["ema200"] = ta.trend.EMAIndicator(df["close"], window=min(200, n)).ema_indicator()
    df["rsi"]    = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    return df.dropna()


class TestMTFBias:
    def test_bull_bias_on_uptrend(self):
        mtf = MTFFilter()
        df  = _make_df(trend="bull")
        # In a strong bull trend, ema9 > ema21 and close > ema200
        bias = mtf._tf_bias(df)
        assert bias in ("BULL", "NEUTRAL")

    def test_bear_bias_on_downtrend(self):
        mtf = MTFFilter()
        df  = _make_df(60, trend="bear")
        bias = mtf._tf_bias(df)
        assert bias in ("BEAR", "NEUTRAL")

    def test_neutral_on_empty(self):
        mtf  = MTFFilter()
        bias = mtf._tf_bias(pd.DataFrame())
        assert bias == "NEUTRAL"

    def test_neutral_on_short_df(self):
        mtf = MTFFilter()
        df  = pd.DataFrame({"ema9": [1], "ema21": [1], "close": [1], "ema200": [1]})
        assert mtf._tf_bias(df) == "NEUTRAL"


class TestValidateSignal:
    def _mtf_with_mock(self, bias_1h: str, bias_4h: str) -> MTFFilter:
        """Crée un MTFFilter dont _fetch_tf retourne des DataFrames mockés."""
        mtf = MTFFilter()

        def mock_fetch(symbol, timeframe, limit=50):
            df = _make_df(60, trend="bull" if bias_1h == "BULL" else "bear")
            return df

        mtf._fetch_tf = mock_fetch
        mtf._tf_bias  = lambda df: bias_1h  # simplifiation : retourne toujours le même biais
        return mtf

    def test_hold_always_rejected(self):
        mtf = MTFFilter()
        assert mtf.validate_signal("GOLD", "HOLD") is False

    def test_buy_accepted_bullish_htf(self):
        mtf = self._mtf_with_mock("BULL", "BULL")
        # Les deux TF bullish → BUY doit passer
        result = mtf.validate_signal("GOLD", "BUY")
        assert result is True

    def test_sell_rejected_on_bullish_htf(self):
        mtf = self._mtf_with_mock("BULL", "BULL")
        result = mtf.validate_signal("GOLD", "SELL")
        assert result is False

    def test_buy_accepted_neutral_htf(self):
        mtf = self._mtf_with_mock("NEUTRAL", "NEUTRAL")
        result = mtf.validate_signal("EURUSD", "BUY")
        assert result is True

    def test_sell_accepted_bearish_htf(self):
        mtf = self._mtf_with_mock("BEAR", "BEAR")
        result = mtf.validate_signal("GOLD", "SELL")
        assert result is True
