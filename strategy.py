"""
strategy.py — London/NY Open Breakout Strategy

Logique corrigée :
  - Le range pré-session est calculé sur les bougies AVANT l'ouverture :
      London : bougies 06h00-07h45 UTC
      NY     : bougies 12h00-13h15 UTC
  - Pendant la session (08h00-10h00 / 13h30-16h00), on surveille si le prix
    casse le haut ou le bas de ce range
  - Score de confirmation : ADX + Volume + Momentum bougie
  - SL = autre extrémité du range | TP = entrée ± R:R × range

Sessions actives :
  London open  : 08h00 → 10h00 UTC
  NY open      : 13h30 → 16h00 UTC
"""

import os
import pandas as pd
import ta
from loguru import logger
from datetime import datetime, timezone

# ─── Constantes ───────────────────────────────────────────────────────────────
SIGNAL_BUY  = "BUY"
SIGNAL_SELL = "SELL"
SIGNAL_HOLD = "HOLD"

# Sessions de trading (UTC, en minutes depuis minuit)
SESSIONS_UTC = [
    (8 * 60,      10 * 60),   # London open  : 08h00 → 10h00
    (13 * 60 + 30, 16 * 60),  # NY open       : 13h30 → 16h00
]

# Plages pré-session pour calculer le range (UTC, en minutes)
PRE_SESSIONS_UTC = [
    (6 * 60,       7 * 60 + 45),  # Pré-London : 06h00 → 07h45
    (12 * 60,      13 * 60 + 15), # Pré-NY     : 12h00 → 13h15
]

MIN_RANGE_PCT    = 0.03   # Range min = 0.03% du prix (adapté 1H — ranges plus petits)
MAX_RANGE_PCT    = 25.0   # Range max = 25% (1H: crypto 5-15%, stocks 5-10%, forex 0.5-3%)
MIN_SCORE        = 1      # Score minimum sur 7 (assoupli : 1 confirmation suffit)
ADX_MIN          = 15     # ADX assoupli de 18 à 15 (capte plus de tendances)
ATR_PERIOD       = 14
REQUIRED_SCORE   = MIN_SCORE  # Alias pour compatibilité

# Jours de trading autorisés (Lundi=0 ... Dimanche=6)
# Tous les jours ouvrables : les filtres de session et de range filtrent déjà assez
ALLOWED_WEEKDAYS = {0, 1, 2, 3, 4}  # Lundi → Vendredi


def _bar_session_idx(h: int, m: int) -> int:
    """Retourne l'index de session active (0=London, 1=NY) ou -1 si hors session."""
    t = h * 60 + m
    for i, (start, end) in enumerate(SESSIONS_UTC):
        if start <= t <= end:
            return i
    return -1


def _bar_in_presession(h: int, m: int) -> int:
    """Retourne l'index de pré-session (0=pré-London, 1=pré-NY) ou -1."""
    t = h * 60 + m
    for i, (start, end) in enumerate(PRE_SESSIONS_UTC):
        if start <= t <= end:
            return i
    return -1

# Sessions élargies par catégorie d'actif (heures UTC : [(debut_h, debut_m), (fin_h, fin_m)])
# Chaque entrée est une liste de tuples (start, end) en minutes depuis minuit
SESSION_WINDOWS = {
    # Crypto : 24/7 — marchés ouverts en permanence
    "crypto":      [(0, 24 * 60)],
    # Forex BK/TF : London élargi + NY élargi (besoin de volume pour breakout)
    "forex":       [(7 * 60, 10 * 60 + 30),
                    (12 * 60, 16 * 60 + 30)],
    # Forex MR : 24/7 — Mean Reversion fonctionne H24 (RSI/BB ne dépendent pas de session)
    "forex_mr":    [(0, 24 * 60)],
    # Indices : London marchés + NY + after-hours
    "indices":     [(7 * 60, 10 * 60 + 30),
                    (13 * 60, 20 * 60)],
    # Stocks US : NY étendu
    "stocks":      [(13 * 60, 20 * 60)],
    # Commodités : élargi 06h-22h UTC (pétrole, or actifs sur large plage)
    "commodities": [(6 * 60, 22 * 60)],
}


def _in_session_window(h: int, m: int, category: str) -> bool:
    """Vérifie si l'heure UTC est dans la fenêtre de session de la catégorie."""
    t = h * 60 + m
    windows = SESSION_WINDOWS.get(category, SESSION_WINDOWS["forex"])
    for start, end in windows:
        if start <= t < end:
            return True
    return False


class Strategy:

    def compute_indicators(self, df: pd.DataFrame, df_htf: pd.DataFrame = None) -> pd.DataFrame:
        """Calcule les indicateurs : ATR, ADX, Volume MA, EMA, RSI, BB, MACD."""
        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        volume = df["volume"]

        df["atr"] = ta.volatility.AverageTrueRange(
            high, low, close, window=ATR_PERIOD
        ).average_true_range()

        adx_ind  = ta.trend.ADXIndicator(high, low, close, window=14)
        df["adx"] = adx_ind.adx()

        df["vol_ma"]       = volume.rolling(20).mean()
        df["ema200"]       = ta.trend.EMAIndicator(close, window=200).ema_indicator()
        df["ema200_slope"] = df["ema200"].pct_change(periods=5, fill_method=None)
        df["ema9"]         = ta.trend.EMAIndicator(close, window=9).ema_indicator()
        df["ema21"]        = ta.trend.EMAIndicator(close, window=21).ema_indicator()
        df["rsi"]          = ta.momentum.RSIIndicator(close, window=14).rsi()

        # ── V6+V7: Bollinger Bands, MACD, EMAs pour MR+TF ──
        df["ema20"]  = ta.trend.EMAIndicator(close, window=20).ema_indicator()
        df["ema50"]  = ta.trend.EMAIndicator(close, window=50).ema_indicator()
        bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        df["bb_up"]  = bb.bollinger_hband()
        df["bb_lo"]  = bb.bollinger_lband()
        df["bb_mid"] = bb.bollinger_mavg()
        macd_ind = ta.trend.MACD(close, window_slow=26, window_fast=12, window_sign=9)
        df["macd"]   = macd_ind.macd()
        df["macd_s"] = macd_ind.macd_signal()
        # Weekly bias (approximation via long EMAs)
        df["ema100"] = ta.trend.EMAIndicator(close, window=100).ema_indicator()
        df["ema250"] = ta.trend.EMAIndicator(close, window=250).ema_indicator()

        # Don't dropna() on ALL columns — ema100/ema250 need 100-250 bars
        # and may not all be available with limited fetch. Only require cores.
        core_cols = ["atr", "adx", "ema20", "ema50", "rsi", "macd", "macd_s",
                     "bb_up", "bb_lo"]
        available = [c for c in core_cols if c in df.columns]
        return df.dropna(subset=available)

    def is_session_ok(self) -> bool:
        """Retourne True si on est dans une fenêtre de trading quelconque (fallback global)."""
        now = datetime.now(timezone.utc)
        if now.weekday() not in ALLOWED_WEEKDAYS:
            return False
        return _bar_session_idx(now.hour, now.minute) >= 0

    def is_session_ok_for(self, instrument: str, category: str = "forex") -> bool:
        """Retourne True si l'heure UTC est dans la fenêtre de session de cet actif."""
        now = datetime.now(timezone.utc)
        if now.weekday() not in ALLOWED_WEEKDAYS:
            return False
        return _in_session_window(now.hour, now.minute, category)

    def compute_session_range(self, df: pd.DataFrame, range_lookback: int = 6) -> dict:
        """
        Calcule le range pré-session à partir des bougies DE PRÉ-SESSION.
        IMPORTANT: exclut la bougie courante pour que le breakout soit détectable.
        range_lookback: nombre de bougies à utiliser (from ASSET_PROFILES.range_lb).
        """
        last_close = float(df.iloc[-1]["close"])

        # Essai avec filtrage timestamp (backtesting)
        if hasattr(df.index, "hour"):
            presession_bars = df[
                df.index.map(lambda ts: _bar_in_presession(ts.hour, ts.minute) >= 0)
            ]
            if len(presession_bars) >= 3:
                high_range = float(presession_bars["high"].max())
                low_range  = float(presession_bars["low"].min())
                range_size = high_range - low_range
                range_pct  = (range_size / last_close * 100) if last_close > 0 else 0
                return {"high": high_range, "low": low_range, "size": range_size, "pct": range_pct}

        # Fallback : range_lookback bougies AVANT la bougie courante
        # On exclut la dernière bougie pour que le close puisse "casser" le range
        if len(df) > range_lookback:
            recent = df.iloc[-(range_lookback + 1):-1]  # exclut la bougie courante
        else:
            recent = df.iloc[:-1] if len(df) > 1 else df
        high_range = float(recent["high"].max())
        low_range  = float(recent["low"].min())
        range_size = high_range - low_range
        range_pct  = (range_size / last_close * 100) if last_close > 0 else 0
        return {"high": high_range, "low": low_range, "size": range_size, "pct": range_pct}

    def market_regime(self, df: pd.DataFrame, slope_threshold: float = 0.0001) -> str:
        if "ema200_slope" not in df.columns or len(df) < 2:
            return "RANGE"
        slope = float(df.iloc[-1]["ema200_slope"])
        if slope > slope_threshold: return "BULL"
        if slope < -slope_threshold: return "BEAR"
        return "RANGE"

    def compute_session_vwap(self, df: pd.DataFrame) -> float:
        """
        Calcule le VWAP depuis l'ouverture de la session courante.
        Filtre les bougies pré-session et session uniquement.
        Retourne 0.0 si insufficient data.
        """
        try:
            if hasattr(df.index, "hour"):
                # Garde uniquement les bougies depuis le début de la session active
                session_bars = df[
                    df.index.map(lambda ts: _bar_in_presession(ts.hour, ts.minute) >= 0
                                 or _bar_session_idx(ts.hour, ts.minute) >= 0)
                ]
                if len(session_bars) >= 3:
                    typical = (session_bars["high"] + session_bars["low"] + session_bars["close"]) / 3
                    vwap = (typical * session_bars["volume"]).sum() / session_bars["volume"].sum()
                    return float(vwap)
            # Fallback : VWAP sur les 20 dernières bougies
            recent = df.tail(20)
            typical = (recent["high"] + recent["low"] + recent["close"]) / 3
            vol = recent["volume"]
            return float((typical * vol).sum() / vol.sum()) if vol.sum() > 0 else 0.0
        except Exception:
            return 0.0

    def get_signal(
        self,
        df: pd.DataFrame,
        symbol: str = None,
        min_score_override: int = None,
        futures_mode: bool = False,
        asset_profile: dict = None,
    ) -> tuple:
        """
        Signal multi-stratégie V6+V7.
        strat = BK (Breakout) | MR (Mean Reversion) | TF (Trend Following)
        """
        req_score = min_score_override if min_score_override is not None else MIN_SCORE

        if len(df) < 30:
            return SIGNAL_HOLD, 0, []

        # Routing par stratégie
        strat_type = asset_profile.get("strat", "BK") if asset_profile else "BK"
        if strat_type == "MR":
            return self._signal_mr(df, symbol, asset_profile)
        if strat_type == "TF":
            return self._signal_tf(df, symbol, asset_profile)

        # ═══ BREAKOUT (BK) — logique originale ═══
        # Note: le filtre session est géré per-instrument dans bot_tick.py
        # via is_session_ok_for() — plus de gate ici

        curr = df.iloc[-1]

        # ── Range pré-session ────────────────────────────────────────────────
        _range_lb = asset_profile.get("range_lb", 6) if asset_profile else 6
        sr = self.compute_session_range(df, range_lookback=_range_lb)
        range_size = sr["size"]
        range_pct  = sr["pct"]

        if range_pct < MIN_RANGE_PCT:
            logger.info(f"😴 Range trop petit ({range_pct:.3f}%) < {MIN_RANGE_PCT}% — marché calme, skip")
            return SIGNAL_HOLD, 0, []

        if range_pct > MAX_RANGE_PCT:
            logger.info(f"⚠️  Range trop grand ({range_pct:.3f}%) — spread excessif / news, skip")
            return SIGNAL_HOLD, 0, []

        last_close = float(curr["close"])
        high_r     = sr["high"]
        low_r      = sr["low"]

        # ── Détection du breakout ────────────────────────────────────────────
        # Le prix doit clôturer AU-DELÀ du range (pas seulement le toucher)
        _bk_margin = asset_profile.get("bk_margin", 0.10) if asset_profile else 0.10
        BREAKOUT_MARGIN = range_size * _bk_margin
        broke_up   = last_close > high_r + BREAKOUT_MARGIN
        broke_down = last_close < low_r  - BREAKOUT_MARGIN

        if not broke_up and not broke_down:
            # Diagnostic: proximité au breakout (temporaire)
            dist_up = ((high_r + BREAKOUT_MARGIN) - last_close) / last_close * 100
            dist_dn = (last_close - (low_r - BREAKOUT_MARGIN)) / last_close * 100
            logger.debug(
                f"📏 {symbol} range={range_pct:.2f}% | close={last_close:.2f} "
                f"| H={high_r:.2f}(+{dist_up:.2f}%) L={low_r:.2f}(-{dist_dn:.2f}%)"
            )
            return SIGNAL_HOLD, 0, []

        sig = SIGNAL_BUY if broke_up else SIGNAL_SELL

        # ── Confirmations (score 0-3) ────────────────────────────────────────
        confirmations = []

        # C1 — ADX (force de tendance en formation)
        adx_val = float(curr.get("adx", 0))
        _adx_min = asset_profile.get("adx_min", ADX_MIN) if asset_profile else ADX_MIN
        if adx_val > _adx_min:
            confirmations.append(f"ADX {adx_val:.0f}")

        # C2 — Volume > MA20
        vol    = float(curr.get("volume", 0))
        vol_ma = float(curr.get("vol_ma", vol + 1))
        if vol > vol_ma:
            confirmations.append("Volume✓")

        # C3 — Momentum Candle : corps > 60% de l'ATR (bougie conviction forte)
        # Standard institutionnel : la bougie de breakout doit être grande
        atr_val     = float(curr.get("atr", range_size or 1))
        candle_body = abs(float(curr["close"]) - float(curr["open"]))
        momentum_ok = (atr_val > 0 and candle_body >= 0.6 * atr_val) or \
                      (range_size > 0 and (candle_body / range_size) >= 0.3)
        if momentum_ok:
            confirmations.append(f"Momentum {candle_body:.5f} (ATR {atr_val:.5f})")

        # ── UPGRADE : Filtre VWAP (direction par rapport à la valeur équitable) ──
        # BUY  : prix doit être AU-DESSUS du VWAP de session
        # SELL : prix doit être EN-DESSOUS du VWAP de session
        # Un trade contre le VWAP a statistiquement 40% moins de chance de réussir.
        vwap = self.compute_session_vwap(df)
        if vwap > 0:
            vwap_ok = (sig == SIGNAL_BUY  and last_close > vwap) or \
                      (sig == SIGNAL_SELL and last_close < vwap)
            if vwap_ok:
                confirmations.append(f"VWAP✓ ({vwap:.5f})")
            else:
                logger.debug(
                    f"⚠️  VWAP contra-tendance {sig} | prix={last_close:.5f} vs VWAP={vwap:.5f}"
                )
                # Ne bloque pas mais ne compte pas comme confirmation

        # ── UPGRADE : Filtre Wick (bougie à longue mèche = fakeout probable) ──
        # La mèche dans la direction du breakout ne doit pas dépasser 40%
        # du range total de la bougie. Une longue mèche = rejet de prix.
        candle_range = float(curr["high"]) - float(curr["low"])
        if candle_range > 0:
            if sig == SIGNAL_BUY:
                upper_wick = float(curr["high"]) - float(curr["close"])
                wick_pct   = upper_wick / candle_range
            else:
                lower_wick = float(curr["close"]) - float(curr["low"])
                wick_pct   = lower_wick / candle_range

            if wick_pct > 0.40:
                logger.info(
                    f"🕯️  Wick filter {sig} : mèche {wick_pct:.0%} > 40% — fakeout probable, skip"
                )
                return SIGNAL_HOLD, 0, []
            elif wick_pct < 0.20:
                confirmations.append(f"Wick✓ ({wick_pct:.0%})")

        # \u2500\u2500 UPGRADE : Orderflow Imbalance (OFI) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # Mesure la pression acheteur/vendeur sur les 3 dernières bougies.
        # BUY favori si les 3 bougies sont bullish (close > open)
        # SELL favori si les 3 bougies sont bearish (close < open)
        try:
            last3 = df.tail(3)
            if len(last3) >= 3:
                bull_bars = (last3["close"] > last3["open"]).sum()
                bear_bars = (last3["close"] < last3["open"]).sum()
                ofi_ok = (sig == SIGNAL_BUY  and bull_bars >= 2) or \
                         (sig == SIGNAL_SELL and bear_bars >= 2)
                if ofi_ok:
                    ofi_label = f"{'↑'*bull_bars if sig == SIGNAL_BUY else '↓'*bear_bars}"
                    confirmations.append(f"OFI✓ {ofi_label}")
        except Exception:
            pass

        # \u2500\u2500 UPGRADE : London Squeeze Filter \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # Range asiatique (0h-7h UTC) très compressée = London Squeeze probable
        # Les vrais breakouts sont plus puissants après un squeeze asiatique.
        # Confirmation +1 si range_asiatique < 35% de la moyenne 20j.
        try:
            if hasattr(df.index, "hour"):
                asian_bars = df[df.index.map(lambda ts: 0 <= ts.hour < 7)]
                if len(asian_bars) >= 4:
                    asian_range = float(asian_bars["high"].max() - asian_bars["low"].min())
                    avg_range_20 = float((df["high"] - df["low"]).tail(20 * 12).mean())  # 12 bougies/h
                    if avg_range_20 > 0 and asian_range < avg_range_20 * 0.35:
                        confirmations.append(f"Squeeze✓ ({asian_range:.5f})")
                        logger.debug(f"🔵 London Squeeze détecté — range asiat. {asian_range:.5f} < 35% avg")
        except Exception:
            pass

        score = len(confirmations)


        if score < req_score:
            logger.info(f"❌ Score {score}/{req_score} insuffisant sur {symbol} — confirmations: {confirmations}")
            return SIGNAL_HOLD, score, confirmations

        rng_info = f"Range {range_pct:.2f}% | {high_r:.5f}–{low_r:.5f}"
        if sig == SIGNAL_BUY:
            logger.info(f"🟢 BREAKOUT BUY  | {rng_info} | Score {score}/3 | {confirmations}")
        else:
            logger.info(f"🔴 BREAKOUT SELL | {rng_info} | Score {score}/3 | {confirmations}")

        return sig, score, [rng_info] + confirmations

    def get_sl_tp(self, sig: str, entry: float, range_info: dict, rr: float = 2.0) -> dict:
        """SL = autre extrémité du range. TP = entrée ± rr × taille.
        R:R par défaut porté à 2.0 (contre 1.8 avant) pour capturer plus de gains.
        """
        if sig == SIGNAL_BUY:
            sl = range_info["low"] - range_info["size"] * 0.1   # Buffer 10%
            tp = entry + range_info["size"] * rr
        else:
            sl = range_info["high"] + range_info["size"] * 0.1
            tp = entry - range_info["size"] * rr
        return {"sl": sl, "tp": tp, "range": range_info}

    def get_atr(self, df: pd.DataFrame) -> float:
        if "atr" in df.columns and len(df) > 0:
            return float(df.iloc[-1]["atr"])
        return 0.0

    def check_pre_signal(
        self, df: pd.DataFrame, symbol: str = None,
        asset_profile: dict = None,
    ) -> dict:
        """
        Detect if a trade setup is forming (price approaching breakout).
        Returns dict with pre-signal info, or None if no setup.
        Used for "⏳ SETUP EN FORMATION" alerts.
        """
        if len(df) < 30:
            return None

        strat_type = asset_profile.get("strat", "BK") if asset_profile else "BK"

        # --- BK (Breakout) pre-signal ---
        if strat_type == "BK":
            curr = df.iloc[-1]
            _range_lb = asset_profile.get("range_lb", 6) if asset_profile else 6
            sr = self.compute_session_range(df, range_lookback=_range_lb)
            range_size = sr["size"]
            range_pct = sr["pct"]

            if range_pct < MIN_RANGE_PCT or range_pct > MAX_RANGE_PCT:
                return None

            last_close = float(curr["close"])
            high_r = sr["high"]
            low_r = sr["low"]

            _bk_margin = asset_profile.get("bk_margin", 0.10) if asset_profile else 0.10
            margin = range_size * _bk_margin
            breakout_up = high_r + margin
            breakout_dn = low_r - margin

            # Check proximity: within 50% of margin to breakout
            dist_up = breakout_up - last_close
            dist_dn = last_close - breakout_dn

            # Already broken out → not a pre-signal
            if last_close > breakout_up or last_close < breakout_dn:
                return None

            near_up = dist_up <= margin * 1.5 and dist_up > 0
            near_dn = dist_dn <= margin * 1.5 and dist_dn > 0

            if not near_up and not near_dn:
                return None

            # Determine potential direction
            if near_up and near_dn:
                # Very tight range, could go either way — skip
                return None
            direction = "BUY" if near_up else "SELL"

            # Check how many confirmations would be met
            confirmations_met = []
            adx_val = float(curr.get("adx", 0))
            _adx_min = asset_profile.get("adx_min", ADX_MIN) if asset_profile else ADX_MIN
            if adx_val > _adx_min:
                confirmations_met.append(f"ADX {adx_val:.0f}")

            vol = float(curr.get("volume", 0))
            vol_ma = float(curr.get("vol_ma", vol + 1))
            if vol > vol_ma:
                confirmations_met.append("Volume✓")

            atr_val = float(curr.get("atr", range_size or 1))
            candle_body = abs(float(curr["close"]) - float(curr["open"]))
            if atr_val > 0 and candle_body >= 0.4 * atr_val:
                confirmations_met.append("Momentum↗")

            # Need at least 1 confirmation forming to be worth alerting
            if len(confirmations_met) < 1:
                return None

            # Compute estimated entry, SL, TPs
            entry_est = breakout_up if direction == "BUY" else breakout_dn
            if direction == "BUY":
                sl_est = low_r - range_size * 0.1
                tp1_est = entry_est + range_size * 0.8
                tp2_est = entry_est + range_size * 1.8
            else:
                sl_est = high_r + range_size * 0.1
                tp1_est = entry_est - range_size * 0.8
                tp2_est = entry_est - range_size * 1.8

            proximity_pct = (1 - (dist_up if near_up else dist_dn) / (margin * 1.5)) * 100

            return {
                "direction": direction,
                "symbol": symbol,
                "entry_est": round(entry_est, 5),
                "sl_est": round(sl_est, 5),
                "tp1_est": round(tp1_est, 5),
                "tp2_est": round(tp2_est, 5),
                "current_price": round(last_close, 5),
                "proximity_pct": round(proximity_pct, 0),
                "confirmations": confirmations_met,
                "range_pct": round(range_pct, 2),
                "missing": "Breakout candle",
            }

        # MR / TF pre-signal: check RSI extremes approaching
        if strat_type == "MR":
            curr = df.iloc[-1]
            rsi = float(curr.get("rsi", 50))
            rsi_lo = asset_profile.get("rsi_lo", 30) if asset_profile else 30
            rsi_hi = asset_profile.get("rsi_hi", 70) if asset_profile else 70
            c = float(curr["close"])
            bb_lo = float(curr.get("bb_lo", c))
            bb_up = float(curr.get("bb_up", c))

            if rsi <= rsi_lo + 5 and c <= bb_lo * 1.03:
                return {
                    "direction": "BUY",
                    "symbol": symbol,
                    "entry_est": round(c, 5),
                    "sl_est": round(bb_lo * 0.99, 5),
                    "tp1_est": round(float(curr.get("bb_mid", c)), 5),
                    "tp2_est": round(bb_up, 5),
                    "current_price": round(c, 5),
                    "proximity_pct": round((rsi_lo + 5 - rsi) / 5 * 100, 0),
                    "confirmations": [f"RSI {rsi:.0f}"],
                    "range_pct": 0,
                    "missing": f"RSI ≤ {rsi_lo}",
                }
            elif rsi >= rsi_hi - 5 and c >= bb_up * 0.97:
                return {
                    "direction": "SELL",
                    "symbol": symbol,
                    "entry_est": round(c, 5),
                    "sl_est": round(bb_up * 1.01, 5),
                    "tp1_est": round(float(curr.get("bb_mid", c)), 5),
                    "tp2_est": round(bb_lo, 5),
                    "current_price": round(c, 5),
                    "proximity_pct": round((rsi - (rsi_hi - 5)) / 5 * 100, 0),
                    "confirmations": [f"RSI {rsi:.0f}"],
                    "range_pct": 0,
                    "missing": f"RSI ≥ {rsi_hi}",
                }

        return None

    # ═══════════════════════════════════════════════════════════════════════
    # MEAN REVERSION (MR) — RSI survendu/surachetÃ© + Bollinger Bands
    # ═══════════════════════════════════════════════════════════════════════
    def _signal_mr(self, df, symbol, profile):
        curr = df.iloc[-1]
        c = float(curr["close"])
        rsi = float(curr.get("rsi", 50))
        bb_lo = float(curr.get("bb_lo", c))
        bb_up = float(curr.get("bb_up", c))
        atr = float(curr.get("atr", 0))
        if atr <= 0:
            return SIGNAL_HOLD, 0, []

        rsi_lo = profile.get("rsi_lo", 30)
        rsi_hi = profile.get("rsi_hi", 70)

        if rsi <= rsi_lo and c <= bb_lo * 1.015:
            sig = SIGNAL_BUY
        elif rsi >= rsi_hi and c >= bb_up * 0.985:
            sig = SIGNAL_SELL
        else:
            return SIGNAL_HOLD, 0, [f"MR:RSI={rsi:.0f}(need≤{rsi_lo}/≥{rsi_hi})"]

        confirmations = []
        adx_val = float(curr.get("adx", 0))
        if adx_val < 25:
            confirmations.append(f"ADX_low {adx_val:.0f}")
        candle_body = abs(c - float(curr["open"]))
        candle_range = float(curr["high"]) - float(curr["low"])
        if candle_range > 0 and candle_body / candle_range >= 0.35:
            confirmations.append("Body\u2713")
        vol = float(curr.get("volume", 0))
        vol_ma = float(curr.get("vol_ma", vol + 1))
        if vol > vol_ma * 0.8:
            confirmations.append("Volume\u2713")
        ema50 = float(curr.get("ema50", c))
        if c > 0 and abs(c - ema50) / c < 0.03:
            confirmations.append("EMA50_near\u2713")

        score = len(confirmations)
        if score < 1:
            return SIGNAL_HOLD, score, confirmations

        info = f"[MR] RSI={rsi:.0f} | BB={'lo' if sig == SIGNAL_BUY else 'hi'}"
        icon = "🟢" if sig == SIGNAL_BUY else "🔴"
        logger.info(f"{icon} MEAN REV {sig} {symbol} | {info} | Score {score} | {confirmations}")
        return sig, score, [info] + confirmations

    # ═══════════════════════════════════════════════════════════════════════
    # TREND FOLLOWING (TF) — EMA crossover + MACD + ADX
    # ═══════════════════════════════════════════════════════════════════════
    def _signal_tf(self, df, symbol, profile):
        curr = df.iloc[-1]
        c = float(curr["close"])
        ema_fast = float(curr.get("ema20", 0))  # EMA rapide (ema20 inchangé car recalculé)
        ema_slow = float(curr.get("ema50", 0))  # EMA lente
        macd = float(curr.get("macd", 0))
        macd_s = float(curr.get("macd_s", 0))
        adx_val = float(curr.get("adx", 0))

        if ema_fast <= 0 or ema_slow <= 0:
            return SIGNAL_HOLD, 0, []

        if ema_fast > ema_slow and macd > macd_s and adx_val > 12:
            sig = SIGNAL_BUY
        elif ema_fast < ema_slow and macd < macd_s and adx_val > 12:
            sig = SIGNAL_SELL
        else:
            ema_dir = '>' if ema_fast > ema_slow else '<'
            macd_dir = '>' if macd > macd_s else '<'
            return SIGNAL_HOLD, 0, [f"TF:EMA{ema_dir} MACD{macd_dir}S ADX={adx_val:.0f}"]

        # Weekly filter — only for daily TF instruments, skip for 1H
        _tf = profile.get("tf", "1h") if profile else "1h"
        if _tf == "1d":
            ema100 = float(curr.get("ema100", 0))
            ema250 = float(curr.get("ema250", 0))
            if ema100 > 0 and ema250 > 0:
                if sig == SIGNAL_BUY and ema100 < ema250:
                    return SIGNAL_HOLD, 0, []
                if sig == SIGNAL_SELL and ema100 > ema250:
                    return SIGNAL_HOLD, 0, []

        confirmations = []
        if adx_val > 25:
            confirmations.append(f"ADX {adx_val:.0f}")
        rsi = float(curr.get("rsi", 50))
        if (sig == SIGNAL_BUY and 40 < rsi < 70) or (sig == SIGNAL_SELL and 30 < rsi < 60):
            confirmations.append(f"RSI\u2713 {rsi:.0f}")
        vol = float(curr.get("volume", 0))
        vol_ma = float(curr.get("vol_ma", vol + 1))
        if vol > vol_ma * 0.9:
            confirmations.append("Volume\u2713")
        mom3 = float(curr.get("close", 0)) / float(df.iloc[-4]["close"]) - 1 if len(df) > 4 else 0
        if (mom3 > 0.005 and sig == SIGNAL_BUY) or (mom3 < -0.005 and sig == SIGNAL_SELL):
            confirmations.append("Mom3\u2713")

        score = len(confirmations)
        if score < 1:
            return SIGNAL_HOLD, score, confirmations

        arrow = '↑' if macd > macd_s else '↓'
        cmp = '>' if sig == SIGNAL_BUY else '<'
        info = f"[TF] EMA20{cmp}EMA50 | MACD {arrow} | ADX={adx_val:.0f}"
        icon = "🟢" if sig == SIGNAL_BUY else "🔴"
        logger.info(f"{icon} TREND {sig} {symbol} | {info} | Score {score} | {confirmations}")
        return sig, score, [info] + confirmations
