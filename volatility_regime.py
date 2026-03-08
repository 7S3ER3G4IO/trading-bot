"""
volatility_regime.py — Volatility Regime Filter (#1)

Détecte si le marché est en haute, normale ou basse volatilité
en utilisant le percentile de l'ATR sur 100 periodes.

Règles :
  ATR percentile < 20% → Marché CALME → NE PAS TRADER (range sans direction)
  ATR percentile > 80% → Marché TRÈS VOLATILE → réduire taille de 50%
  Entre 20-80%         → Conditions normales → taille pleine

Utilité :
  Évite d'entrer en scalping quand le marché ne bouge pas
  Évite de surinvestir quand le marché est chaotique (news, crash)
"""
import sys, time, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

import numpy as np
import pandas as pd
from loguru import logger

LOOKBACK     = 100   # Périodes ATR pour calculer le percentile
LOW_THRESH   = 20    # % → marché trop calme
HIGH_THRESH  = 80    # % → marché trop volatil
CACHE_TTL    = 120   # 2 minutes


class VolatilityRegime:

    def __init__(self):
        self._cache: dict    = {}
        self._cache_ts: dict = {}

    def get_regime(self, df: pd.DataFrame, symbol: str = "") -> dict:
        """
        Calcule le régime de volatilité à partir d'un DataFrame.

        Returns:
            dict avec :
              regime       : "LOW" | "NORMAL" | "HIGH"
              percentile   : 0-100 (position de l'ATR actuel dans son historique)
              atr_current  : valeur ATR actuelle
              size_scale   : facteur multiplicateur pour la taille de position
              should_trade : bool
        """
        key = f"{symbol}_{len(df)}"
        now = time.time()
        if key in self._cache and (now - self._cache_ts.get(key, 0)) < CACHE_TTL:
            return self._cache[key]

        try:
            if len(df) < LOOKBACK + 5:
                return self._neutral()

            # ATR simplifié si pas déjà dans le df
            if "atr" not in df.columns:
                high_low   = df["high"] - df["low"]
                high_close = (df["high"] - df["close"].shift()).abs()
                low_close  = (df["low"]  - df["close"].shift()).abs()
                true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
                df = df.copy()
                df["atr"] = true_range.rolling(14).mean()

            atr_series  = df["atr"].dropna()
            if len(atr_series) < LOOKBACK:
                return self._neutral()

            recent_atr  = float(atr_series.iloc[-1])
            history_atr = atr_series.iloc[-LOOKBACK:]
            percentile  = float(np.sum(history_atr <= recent_atr) / len(history_atr) * 100)

            if percentile < LOW_THRESH:
                regime       = "LOW"
                size_scale   = 0.0    # Ne pas trader
                should_trade = False
                emoji        = "😴"
            elif percentile > HIGH_THRESH:
                regime       = "HIGH"
                size_scale   = 0.5    # Réduire de 50%
                should_trade = True
                emoji        = "🌪️"
            else:
                regime       = "NORMAL"
                size_scale   = 1.0    # Taille pleine
                should_trade = True
                emoji        = "✅"

            result = {
                "regime":       regime,
                "percentile":   round(percentile, 1),
                "atr_current":  round(recent_atr, 6),
                "size_scale":   size_scale,
                "should_trade": should_trade,
                "emoji":        emoji,
            }
            self._cache[key]    = result
            self._cache_ts[key] = now

            if regime == "LOW":
                logger.warning(
                    f"😴 {symbol} Volatilité FAIBLE "
                    f"(ATR percentile={percentile:.0f}%) — trade bloqué"
                )
            elif regime == "HIGH":
                logger.warning(
                    f"🌪️  {symbol} Volatilité HAUTE "
                    f"(ATR percentile={percentile:.0f}%) — taille réduite 50%"
                )
            else:
                logger.debug(f"✅ {symbol} Volatilité normale (percentile={percentile:.0f}%)")

            return result

        except Exception as e:
            logger.debug(f"VolatilityRegime {symbol}: {e}")
            return self._neutral()

    def _neutral(self) -> dict:
        return {
            "regime": "NORMAL", "percentile": 50.0,
            "atr_current": 0.0, "size_scale": 1.0,
            "should_trade": True, "emoji": "✅"
        }

    def get_combined_scale(self, df: pd.DataFrame, symbol: str, base_scale: float) -> float:
        """
        Combine le scale de volatilité avec le scale existant (sentiment, etc.).
        Retourne le scale final à appliquer à la taille de position.
        """
        regime = self.get_regime(df, symbol)
        return base_scale * regime["size_scale"]


if __name__ == "__main__":
    sys.path.insert(0, ".")
    from config import SYMBOLS
    from backtester import fetch_historical, get_exchange

    vr  = VolatilityRegime()
    exc = get_exchange()
    print(f"\n📊 Volatility Regime — AlphaTrader\n")
    for sym in SYMBOLS:
        df  = fetch_historical(exc, sym, "5m", 3)
        res = vr.get_regime(df, sym)
        print(
            f"  {res['emoji']} {sym:<14} "
            f"Régime={res['regime']:<8} "
            f"Percentile={res['percentile']:5.1f}%  "
            f"Scale={res['size_scale']:.1f}x  "
            f"Trade={'✅' if res['should_trade'] else '❌'}"
        )
    print()
