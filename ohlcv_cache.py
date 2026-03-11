"""
ohlcv_cache.py — A-2: Cache OHLCV intelligent.

Au lieu de fetch 200 bougies × 48 instruments à chaque tick (48 requêtes REST),
ce cache:
  1. Warmup (au démarrage) : charge 200 bougies par instrument (1 seule fois)
  2. Refresh (chaque tick) : charge uniquement les 10 dernières bougies et merge
  3. Ne re-fetch un instrument que si la dernière bougie est périmée (>= 1h pour 1H TF)

Résultat: 48 requêtes/cycle → ~5-10 requêtes/cycle (uniquement les instruments
dont la dernière bougie a changé).
"""

import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
from loguru import logger
import pandas as pd


class OHLCVCache:
    """
    Cache OHLCV avec merge incrémental.
    
    Usage:
        cache = OHLCVCache(capital_client)
        cache.warmup(instruments, profiles)  # Au démarrage
        df = cache.get(instrument, tf)       # Retourne le DataFrame caché
        cache.refresh_stale()                # Rafraîchit les instruments périmés
    """

    # Durée de validité par timeframe (secondes)
    TF_STALENESS = {
        "1h": 55 * 60,     # 55 minutes → re-fetch les 10 dernières bougies
        "4h": 3.5 * 3600,  # 3h30
        "1d": 22 * 3600,   # 22h
        "15m": 14 * 60,    # 14 minutes
        "5m": 4.5 * 60,    # 4m30
    }

    def __init__(self, capital_client):
        self._client = capital_client
        self._lock = threading.Lock()
        # {instrument: {tf: str, df: DataFrame, last_fetch: float, indicators_done: bool}}
        self._store: Dict[str, dict] = {}
        self._fetch_count = 0  # Compteur requêtes pour monitoring

    def warmup(self, instruments: list, profiles: dict, strategy=None):
        """
        Charge 200 bougies par instrument au démarrage.
        Bloquant — appelé une seule fois dans __init__.
        """
        if not self._client.available:
            logger.warning("⚠️ OHLCVCache warmup skip — Capital.com non disponible")
            return

        total = len(instruments)
        ok = 0
        t0 = time.time()

        for i, instr in enumerate(instruments):
            profile = profiles.get(instr, {})
            tf = profile.get("tf", "1h")
            count = {"1h": 200, "4h": 200, "1d": 100, "5m": 300, "15m": 250}.get(tf, 200)

            try:
                df = self._client.fetch_ohlcv(instr, timeframe=tf, count=count)
                if df is not None and len(df) >= 30:
                    # Compute indicators if strategy available
                    if strategy:
                        df = strategy.compute_indicators(df)

                    with self._lock:
                        self._store[instr] = {
                            "tf": tf,
                            "df": df,
                            "last_fetch": time.time(),
                            "indicators_done": strategy is not None,
                        }
                    ok += 1
                else:
                    logger.debug(f"OHLCVCache warmup {instr}: insufficient data ({len(df) if df is not None else 0} bars)")

                self._fetch_count += 1
                # Anti rate-limit: 0.3s entre chaque requête
                time.sleep(0.3)

            except Exception as e:
                logger.debug(f"OHLCVCache warmup {instr}: {e}")

            # Progress log every 10 instruments
            if (i + 1) % 10 == 0:
                logger.info(f"📦 OHLCVCache warmup: {i+1}/{total} instruments chargés...")

        elapsed = time.time() - t0
        logger.info(
            f"📦 OHLCVCache warmup terminé: {ok}/{total} instruments en {elapsed:.1f}s "
            f"({self._fetch_count} requêtes API)"
        )

    def get(self, instrument: str, strategy=None) -> Optional[pd.DataFrame]:
        """
        Retourne le DataFrame caché pour un instrument.
        Si périmé, fait un refresh incrémental (10 dernières bougies seulement).
        Si absent du cache, fait un fetch complet (200 bougies).
        """
        with self._lock:
            entry = self._store.get(instrument)

        if entry is None:
            # Pas dans le cache → fetch complet
            return self._full_fetch(instrument, strategy)

        # Vérifier si périmé
        staleness = self.TF_STALENESS.get(entry["tf"], 55 * 60)
        age = time.time() - entry["last_fetch"]

        if age < staleness:
            # Cache frais — retourner directement
            return entry["df"]

        # Cache périmé → refresh incrémental
        return self._incremental_refresh(instrument, entry, strategy)

    def _full_fetch(self, instrument: str, strategy=None) -> Optional[pd.DataFrame]:
        """Fetch complet — 200 bougies. Appelé uniquement si pas dans le cache."""
        from brokers.capital_client import ASSET_PROFILES
        profile = ASSET_PROFILES.get(instrument, {})
        tf = profile.get("tf", "1h")
        count = {"1h": 200, "4h": 200, "1d": 100, "5m": 300, "15m": 250}.get(tf, 200)

        try:
            df = self._client.fetch_ohlcv(instrument, timeframe=tf, count=count)
            self._fetch_count += 1

            if df is not None and len(df) >= 30:
                if strategy:
                    df = strategy.compute_indicators(df)

                with self._lock:
                    self._store[instrument] = {
                        "tf": tf,
                        "df": df,
                        "last_fetch": time.time(),
                        "indicators_done": strategy is not None,
                    }
                return df
        except Exception as e:
            logger.debug(f"OHLCVCache full_fetch {instrument}: {e}")

        return None

    def _incremental_refresh(self, instrument: str, entry: dict, strategy=None) -> Optional[pd.DataFrame]:
        """
        Refresh incrémental — fetch seulement les 10 dernières bougies
        et merge avec le DataFrame existant.
        """
        tf = entry["tf"]
        try:
            new_bars = self._client.fetch_ohlcv(instrument, timeframe=tf, count=10)
            self._fetch_count += 1

            if new_bars is None or len(new_bars) == 0:
                # API fail → retourner le cache périmé (mieux que rien)
                return entry["df"]

            old_df = entry["df"]

            # Merge: drop les anciennes bougies qui ont la même timestamp que les nouvelles
            # puis concat et garder les 200 dernières
            merged = pd.concat([old_df, new_bars])
            merged = merged[~merged.index.duplicated(keep="last")]
            merged.sort_index(inplace=True)
            # Garder max 250 bougies (buffer pour les indicateurs longs comme EMA200)
            if len(merged) > 250:
                merged = merged.iloc[-250:]

            # Recompute indicators sur le DataFrame complet (nécessaire car EMA/RSI
            # dépendent de la séquence complète)
            if strategy:
                merged = strategy.compute_indicators(merged)

            with self._lock:
                self._store[instrument] = {
                    "tf": tf,
                    "df": merged,
                    "last_fetch": time.time(),
                    "indicators_done": strategy is not None,
                }

            return merged

        except Exception as e:
            logger.debug(f"OHLCVCache refresh {instrument}: {e}")
            return entry["df"]  # Fallback: cache périmé

    def refresh_stale(self, instruments: list, strategy=None):
        """
        Rafraîchit tous les instruments périmés en batch.
        Appelé au début de chaque cycle _tick().
        Retourne le nombre de requêtes API effectuées.
        """
        refreshed = 0
        for instr in instruments:
            with self._lock:
                entry = self._store.get(instr)

            if entry is None:
                continue

            staleness = self.TF_STALENESS.get(entry["tf"], 55 * 60)
            age = time.time() - entry["last_fetch"]

            if age >= staleness:
                self._incremental_refresh(instr, entry, strategy)
                refreshed += 1
                time.sleep(0.2)  # Anti rate-limit

        if refreshed > 0:
            logger.debug(f"📦 OHLCVCache refresh: {refreshed} instruments mis à jour")
        return refreshed

    @property
    def stats(self) -> dict:
        """Stats pour monitoring/dashboard."""
        with self._lock:
            cached = len(self._store)
            stale = sum(
                1 for e in self._store.values()
                if time.time() - e["last_fetch"] >= self.TF_STALENESS.get(e["tf"], 55 * 60)
            )
        return {
            "cached": cached,
            "stale": stale,
            "total_fetches": self._fetch_count,
        }
