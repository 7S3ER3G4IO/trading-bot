"""
vol_adjuster.py — Moteur 1 : Volatility-Adjusted TP/SL.

Principe:
    ATR_now / ATR_baseline → vol_ratio
    SL_distance *= clip(vol_ratio, 0.7, 1.8)
    TP_distance *= clip(vol_ratio, 0.7, 1.8)
    Kelly sizing recalculé automatiquement sur le nouveau SL

Si la volatilité explose (ATR +50%), on élargit pour éviter le bruit.
Si le marché est flat (ATR -30%), on resserre pour maximiser le ratio R/R.

Usage:
    from vol_adjuster import VolAdjuster
    adj = VolAdjuster()
    new_sl, new_tp1, new_size = adj.adjust(
        df=df, entry=entry, sl=sl, tp1=tp1,
        direction="BUY", risk_pct=0.002, balance=10000
    )
"""
import numpy as np
from loguru import logger

# ─── Paramètres ────────────────────────────────────────────────────────────
_VOL_LOOKBACK = 20     # bougies pour ATR baseline
_VOL_CLAMP_LO = 0.70   # floor: on resserre au max à 70% du SL original
_VOL_CLAMP_HI = 1.80   # cap:   on élargit au max à 180% du SL original


class VolAdjuster:
    """
    Ajuste SL/TP et sizing according to current market volatility (ATR).
    """

    def adjust(self, df, entry: float, sl: float, tp1: float,
               direction: str, risk_pct: float, balance: float,
               min_size: float = 0.1) -> tuple:
        """
        Retourne (new_sl, new_tp1, new_size) ajustés à la volatilité.

        Si le calcul échoue → retourne les valeurs originales (failsafe).

        Args:
            df: DataFrame avec colonne 'atr'
            entry: prix d'entrée
            sl, tp1: SL et TP originaux
            direction: "BUY" ou "SELL"
            risk_pct: fraction du capital à risquer
            balance: solde actuel
            min_size: taille minimale (pour éviter 0)
        """
        try:
            vol_ratio = self._compute_vol_ratio(df)
            mult = np.clip(vol_ratio, _VOL_CLAMP_LO, _VOL_CLAMP_HI)

            # Distances originales
            sl_dist  = abs(entry - sl)
            tp_dist  = abs(tp1 - entry)

            # Nouvelles distances ajustées
            new_sl_dist  = sl_dist  * mult
            new_tp_dist  = tp_dist  * mult

            # Nouveaux prix
            if direction == "BUY":
                new_sl  = round(entry - new_sl_dist, 5)
                new_tp1 = round(entry + new_tp_dist, 5)
            else:
                new_sl  = round(entry + new_sl_dist, 5)
                new_tp1 = round(entry - new_tp_dist, 5)

            # Recalcul Kelly sur le nouveau SL
            new_size = self._kelly_size(balance, risk_pct, new_sl_dist, entry)
            new_size = max(new_size, min_size)

            if abs(mult - 1.0) > 0.05:
                logger.info(
                    f"📊 VolAdj: ratio={vol_ratio:.2f}x → mult={mult:.2f}x "
                    f"| SL {sl_dist:.5f}→{new_sl_dist:.5f} "
                    f"| TP {tp_dist:.5f}→{new_tp_dist:.5f}"
                )

            return new_sl, new_tp1, new_size

        except Exception as e:
            logger.debug(f"VolAdjuster.adjust failed ({e}) — using original values")
            return sl, tp1, None  # None → caller garde son sizing

    # ─── Internals ───────────────────────────────────────────────────────────

    def _compute_vol_ratio(self, df) -> float:
        """
        ATR_recent (5 dernières bougies) / ATR_baseline (20 dernières bougies).
        Retourne 1.0 si le calcul est impossible.
        """
        if df is None or "atr" not in df.columns or len(df) < _VOL_LOOKBACK:
            return 1.0

        atr_series = df["atr"].dropna()
        if len(atr_series) < _VOL_LOOKBACK:
            return 1.0

        atr_recent   = atr_series.iloc[-5:].mean()
        atr_baseline = atr_series.iloc[-_VOL_LOOKBACK:].mean()

        if atr_baseline <= 0:
            return 1.0

        return float(atr_recent / atr_baseline)

    def _kelly_size(self, balance: float, risk_pct: float,
                    sl_dist: float, entry: float) -> float:
        """
        Calcule la taille de position basée sur le risque en capital.
        taille = capital_at_risk / sl_distance_en_unités
        """
        if sl_dist <= 0 or entry <= 0:
            return 0.1
        capital_at_risk = balance * risk_pct
        raw_size = capital_at_risk / sl_dist
        # Arrondi au dixième proche
        return round(max(raw_size, 0.1), 2)

    def format_status(self, df) -> str:
        try:
            r = self._compute_vol_ratio(df)
            mult = np.clip(r, _VOL_CLAMP_LO, _VOL_CLAMP_HI)
            label = "🔥 HIGH" if r > 1.3 else ("🧊 LOW" if r < 0.8 else "✅ Normal")
            return f"Vol ratio={r:.2f}x ({label}) | TP/SL mult={mult:.2f}x"
        except Exception:
            return "VolAdj: N/A"
