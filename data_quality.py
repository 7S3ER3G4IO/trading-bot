"""
data_quality.py — Contrôle qualité des données OHLCV
Détecte les gaps (bougies manquantes), les prix aberrants, les volumes nuls.

Usage dans ohlcv_cache.py ou bot_signals.py :
    from data_quality import DataQualityChecker
    checker = DataQualityChecker()
    ok, report = checker.check(df, instrument="EURUSD", timeframe="1h")
    if not ok:
        logger.warning(report)
        return  # skip signal generation
"""
from datetime import timezone
from typing import Optional
from loguru import logger

try:
    import pandas as pd
    _PD_OK = True
except ImportError:
    _PD_OK = False


# ─── Paramètres ──────────────────────────────────────────────────────────────

# Minutes par timeframe
TF_MINUTES = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}

MAX_ALLOWED_GAP_BARS  = 3    # max bougies manquantes consécutives tolérées
MAX_PRICE_SPIKE_PCT   = 5.0  # spike > 5% en une bougie = aberrant
MAX_CONSECUTIVE_DOJI  = 10   # trop de dojis = données figées


class DataQualityChecker:
    """Vérifie la qualité des données OHLCV avant génération de signal."""

    def __init__(
        self,
        max_gap_bars:    int   = MAX_ALLOWED_GAP_BARS,
        max_spike_pct:   float = MAX_PRICE_SPIKE_PCT,
        max_doji_count:  int   = MAX_CONSECUTIVE_DOJI,
    ):
        self.max_gap_bars   = max_gap_bars
        self.max_spike_pct  = max_spike_pct
        self.max_doji_count = max_doji_count

    def check(self, df, instrument: str = "", timeframe: str = "1h") -> tuple[bool, str]:
        """
        Vérifie la qualité du DataFrame OHLCV.

        Returns:
            (True, "") si données OK
            (False, raison) si données suspectes
        """
        if not _PD_OK:
            return True, ""
        if df is None or len(df) < 10:
            return False, f"{instrument}: DataFrame vide ou insuffisant ({len(df) if df is not None else 0} lignes)"

        issues = []

        # ── 1. Gaps (bougies manquantes) ─────────────────────────────────
        if "time" in df.columns:
            issues += self._check_gaps(df, instrument, timeframe)

        # ── 2. Prix aberrants (spikes) ────────────────────────────────────
        issues += self._check_price_spikes(df, instrument)

        # ── 3. Bougies figées (dojis consécutifs) ─────────────────────────
        issues += self._check_frozen_data(df, instrument)

        # ── 4. Valeurs négatives ou nulles ────────────────────────────────
        issues += self._check_null_prices(df, instrument)

        if issues:
            report = f"⚠️ DataQuality {instrument} ({timeframe}): " + " | ".join(issues)
            return False, report

        return True, ""

    def _check_gaps(self, df, instrument: str, timeframe: str) -> list:
        """Détecte les gaps temporels > max_gap_bars bougies."""
        tf_min = TF_MINUTES.get(timeframe, 60)
        issues = []
        try:
            times = pd.to_datetime(df["time"], utc=True, errors="coerce")
            diffs = times.diff().dropna()
            expected_delta = pd.Timedelta(minutes=tf_min)
            max_delta      = expected_delta * (self.max_gap_bars + 1)

            large_gaps = diffs[diffs > max_delta]
            if len(large_gaps) > 0:
                worst = large_gaps.max()
                n_missing = int(worst / expected_delta) - 1
                issues.append(f"{n_missing} bougies manquantes (gap max={worst})")
        except Exception as e:
            logger.debug(f"DataQuality._check_gaps {instrument}: {e}")
        return issues

    def _check_price_spikes(self, df, instrument: str) -> list:
        """Détecte les variations de prix > max_spike_pct en une bougie."""
        issues = []
        try:
            close = df["close"].astype(float)
            pct_changes = close.pct_change().abs() * 100
            # Marchés crypto tolèrent 5%, forex beaucoup moins — seuil unifié ici
            spikes = pct_changes[pct_changes > self.max_spike_pct]
            if len(spikes) > 2:
                worst = spikes.max()
                issues.append(f"{len(spikes)} spikes > {self.max_spike_pct}% (pire={worst:.1f}%)")
        except Exception as e:
            logger.debug(f"DataQuality._check_price_spikes {instrument}: {e}")
        return issues

    def _check_frozen_data(self, df, instrument: str) -> list:
        """Détecte des données figées (dojis parfaits consécutifs = API bloquée)."""
        issues = []
        try:
            close = df["close"].astype(float)
            # Cherche des séquences de prix identiques
            is_same = (close == close.shift(1))
            streak  = is_same.groupby((~is_same).cumsum()).cumsum().max()
            if streak >= self.max_doji_count:
                issues.append(f"Données figées ({streak} bougies identiques)")
        except Exception as e:
            logger.debug(f"DataQuality._check_frozen {instrument}: {e}")
        return issues

    def _check_null_prices(self, df, instrument: str) -> list:
        """Détecte les prix nuls ou négatifs."""
        issues = []
        try:
            for col in ["open", "high", "low", "close"]:
                if col not in df.columns:
                    continue
                nulls    = (df[col].isna() | (df[col] <= 0)).sum()
                null_pct = nulls / len(df) * 100
                if null_pct > 2:
                    issues.append(f"{nulls} valeurs nulles dans '{col}' ({null_pct:.0f}%)")
        except Exception as e:
            logger.debug(f"DataQuality._check_nulls {instrument}: {e}")
        return issues

    def summary(self, df, instrument: str = "", timeframe: str = "1h") -> str:
        ok, report = self.check(df, instrument, timeframe)
        return f"✅ {instrument} OK" if ok else report


# ─── Instance globale réutilisable ───────────────────────────────────────────

_CHECKER = None

def get_quality_checker() -> DataQualityChecker:
    global _CHECKER
    if _CHECKER is None:
        _CHECKER = DataQualityChecker()
    return _CHECKER
