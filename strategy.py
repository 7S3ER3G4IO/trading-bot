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

import json, os
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
REQUIRED_SCORE = 5   # Min score sur 6 — 5/6 requis (qualité > quantité)

# Filtre régime de marché — EMA 200 slope
EMA_TREND_PERIOD = 200         # EMA longue période
SLOPE_WINDOW     = 5           # Nombre de bougies pour calculer la pente
SLOPE_THRESHOLD  = 0.0001      # Seuil slope (0.01% / bougie) — compromis :
                                # 0.0002 = trop restrictif | 0.00008 = trop permissif

# ─── Paramètres optimisés par symbole ────────────────────────────────────────
# Chargés depuis symbol_params.json (généré par optimizer.py)
_SYMBOL_PARAMS: dict = {}
_PARAMS_FILE = "symbol_params.json"

def _load_symbol_params():
    global _SYMBOL_PARAMS
    if os.path.exists(_PARAMS_FILE):
        try:
            with open(_PARAMS_FILE) as f:
                _SYMBOL_PARAMS = json.load(f)
            logger.info(f"📊 Params optimisés chargés pour {len(_SYMBOL_PARAMS)} symboles")
        except Exception as e:
            logger.warning(f"⚠️  Impossible de charger {_PARAMS_FILE}: {e}")

_load_symbol_params()


class Strategy:

    def compute_indicators(self, df: pd.DataFrame, df_htf: pd.DataFrame = None) -> pd.DataFrame:
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

        # ── SMC Liquidity Sweep ──────────────────────────────────────────────────────────
        # Chasse aux stops institutionnels (Smart Money Concepts)
        # SSL Swept : mèche perce un swing low sur N bougies puis close revient au-dessus → BUY
        # BSL Swept : mèche perce un swing high  sur N bougies puis close revient en-dessous → SELL
        _SWING_N = 12
        h_arr = high.values
        l_arr = low.values
        c_arr = close.values
        sweep_col = ["NONE"] * len(df)
        for _i in range(_SWING_N + 2, len(df)):
            _sh = h_arr[_i - _SWING_N - 1: _i - 1].max()
            _sl = l_arr[_i - _SWING_N - 1: _i - 1].min()
            _ph, _pl = h_arr[_i - 1], l_arr[_i - 1]
            _cc = c_arr[_i]
            if _ph > _sh and _cc < _sh:
                sweep_col[_i] = "BSL_SWEPT"
            elif _pl < _sl and _cc > _sl:
                sweep_col[_i] = "SSL_SWEPT"
        df["sweep"] = sweep_col

        return df.dropna()

    def is_session_ok(self) -> bool:
        """Retourne True uniquement pendant les sessions London et NY open."""
        hour_utc = datetime.now(timezone.utc).hour
        # Heures interdites (nuit profonde)
        if hour_utc in AVOID_HOURS_UTC:
            logger.debug(f"⏰ Nuit profonde ({hour_utc}h UTC) — trading suspendu")
            return False
        # Scalping actif uniquement pendant London+NY open (backtesté optimal)
        try:
            from config import SESSION_HOURS
            if hour_utc not in SESSION_HOURS:
                logger.debug(f"⏰ Hors session ({hour_utc}h UTC) — attente London/NY open")
                return False
        except ImportError:
            pass
        return True

    def market_regime(self, df: pd.DataFrame, slope_threshold: float = None) -> str:
        """
        Détecte le régime de marché via la pente de l'EMA 200.
        slope_threshold optionnel pour override per-symbole.
        Returns:
            "BULL"  → marché haussier (slope > +threshold)
            "BEAR"  → marché baissier (slope < -threshold)
            "RANGE" → marché en range (slope ≈ 0) → pas de trade
        """
        thr = slope_threshold if slope_threshold is not None else SLOPE_THRESHOLD
        if "ema200_slope" not in df.columns or len(df) < 2:
            return "RANGE"

        slope = float(df.iloc[-1]["ema200_slope"])

        if slope > thr:
            regime = "BULL"
        elif slope < -thr:
            regime = "BEAR"
        else:
            regime = "RANGE"

        logger.debug(f"📈 Régime marché : {regime} | Slope EMA200 = {slope:.4%}")
        return regime

    def get_signal(self, df: pd.DataFrame, symbol: str = None) -> tuple:
        """
        Retourne (signal, score, confirmations) où :
          signal        : "BUY" / "SELL" / "HOLD"
          score         : int, nombre de confirmations
          confirmations : list[str], descriptions lisibles

        Si symbol est fourni, charge les paramètres optimisés depuis symbol_params.json.
        """
        # Chargement params per-symbole si disponibles
        sym_p    = _SYMBOL_PARAMS.get(symbol, {}) if symbol else {}
        req_score = sym_p.get("required_score", REQUIRED_SCORE)
        slope_thr = sym_p.get("slope_threshold", SLOPE_THRESHOLD)
        adx_min_  = sym_p.get("adx_min", ADX_MIN)
        rsi_max_  = sym_p.get("rsi_buy_max", RSI_BUY_MAX)

        if len(df) < 2:
            return SIGNAL_HOLD, 0, []

        # Filtre session
        if not self.is_session_ok():
            return SIGNAL_HOLD, 0, []

        # ── Pré-filtre OBLIGATOIRE : Régime de marché ─────────────────────
        regime = self.market_regime(df, slope_threshold=slope_thr)
        if regime == "RANGE":
            logger.debug("🔇 Marché en range (EMA200 flat) — pas de signal")
            return SIGNAL_HOLD, 0, []

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        # ── #3 Filtre Volatilité ATR ───────────────────────────────────
        # Rejette le trade si l'ATR est trop faible vs le prix (frais > move)
        # ATR < 0.05% du prix = marché trop calme, pas de scalping
        atr_val = float(curr.get("atr", 0))
        price   = float(curr["close"])
        atr_pct = (atr_val / price) * 100 if price > 0 else 0
        MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.05"))  # 0.05% du prix
        if atr_pct < MIN_ATR_PCT:
            logger.debug(f"📊 ATR trop faible ({atr_pct:.3f}% < {MIN_ATR_PCT}%) — marché trop calme")
            return SIGNAL_HOLD, 0, []

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
            f"RSI {curr['rsi']:.0f} en zone ACHAT (30-{rsi_max_})": rsi_buy,
            f"MACD croisement haussier": macd_up,
            f"ADX {curr['adx']:.0f} > {adx_min_} (tendance forte)": adx_ok,
            f"Volume supérieur à la MA20": vol_ok,
            f"Tendance 1h haussière (HTF aligné)": htf_bull,
        }
        sell_map = {
            f"EMA {EMA_FAST}/{EMA_SLOW} croisement baissier": ema_cross_down,
            f"RSI {curr['rsi']:.0f} en zone VENTE ({RSI_SELL_MIN}-70)": rsi_sell,
            f"MACD croisement baissier": macd_down,
            f"ADX {curr['adx']:.0f} > {adx_min_} (tendance forte)": adx_ok,
            f"Volume supérieur à la MA20": vol_ok,
            f"Tendance 1h baissière (HTF aligné)": htf_bear,
        }

        buy_confs  = [label for label, ok in buy_map.items()  if ok]
        sell_confs = [label for label, ok in sell_map.items() if ok]
        buy_score  = len(buy_confs)
        sell_score = len(sell_confs)

        logger.debug(f"Score BUY={buy_score}/6 | SELL={sell_score}/6 | Régime={regime} | Slope={slope_pct:+.3f}%")

        if allow_buy and buy_score >= req_score and buy_score > sell_score:
            regime_conf = f"📈 Régime HAUSSIER (EMA200 slope +{slope_pct:.3f}%)"
            full_confs  = [regime_conf] + buy_confs
            logger.info(f"🟢 Signal ACHAT {buy_score}/6 | RSI={curr['rsi']:.1f} ADX={curr['adx']:.0f} | {regime_conf}")
            return SIGNAL_BUY, buy_score, full_confs

        if allow_sell and sell_score >= req_score and sell_score > buy_score:
            regime_conf = f"📉 Régime BAISSIER (EMA200 slope {slope_pct:.3f}%)"
            full_confs  = [regime_conf] + sell_confs
            logger.info(f"🔴 Signal VENTE {sell_score}/6 | RSI={curr['rsi']:.1f} ADX={curr['adx']:.0f} | {regime_conf}")
            return SIGNAL_SELL, sell_score, full_confs

        return SIGNAL_HOLD, max(buy_score, sell_score), []

    def get_atr(self, df: pd.DataFrame) -> float:
        """Retourne l'ATR de la dernière bougie."""
        if "atr" in df.columns and len(df) > 0:
            return float(df.iloc[-1]["atr"])
        return 0.0

    def ml_signal_boost(self, df: pd.DataFrame, signal: str) -> int:
        """
        #9 — Boost ML (RandomForest) : +1 si le ML confirme le signal.
        Entraîné sur les 200 dernières bougies (features : RSI, MACD, ADX, volume).
        Retourne 1 si le ML confirme, 0 sinon. Silencieux si sklearn absent.
        """
        try:
            from sklearn.ensemble import RandomForestClassifier
            import numpy as np

            if len(df) < 50:
                return 0

            recent = df.tail(200).copy()
            features = ["rsi", "adx", "macd", "vol_ma", "ema200_slope", "atr"]
            available = [f for f in features if f in recent.columns]
            if len(available) < 3:
                return 0

            X = recent[available].fillna(0).values[:-1]
            # Label : 1 si le close suivant est plus haut, 0 sinon
            y = (recent["close"].shift(-1) > recent["close"]).astype(int).values[:-1]
            if len(X) < 30:
                return 0

            clf = RandomForestClassifier(n_estimators=20, max_depth=4, random_state=42, n_jobs=1)
            clf.fit(X, y)
            last = df[available].iloc[-1].fillna(0).values.reshape(1, -1)
            pred = clf.predict(last)[0]  # 1=bullish, 0=bearish

            if signal == SIGNAL_BUY  and pred == 1:
                logger.debug("🤖 ML: confirme signal ACHAT +1")
                return 1
            if signal == SIGNAL_SELL and pred == 0:
                logger.debug("🤖 ML: confirme signal VENTE +1")
                return 1
            return 0
        except ImportError:
            return 0  # sklearn non disponible — pas de boost
        except Exception:
            return 0
        # Score intermédiaire retourné pour le pre-alert (même en HOLD)
        if allow_buy and buy_score > sell_score:
            return SIGNAL_HOLD, buy_score, []
        if allow_sell and sell_score > buy_score:
            return SIGNAL_HOLD, sell_score, []
        return SIGNAL_HOLD, 0, []

    def get_atr(self, df: pd.DataFrame) -> float:
        return float(df.iloc[-1]["atr"])

