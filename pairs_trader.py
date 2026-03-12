"""
pairs_trader.py — Moteur 6 : Statistical Arbitrage & Pairs Trading.

Principe mathématique (Mean Reversion):
  1. Scan de corrélation entre 48 actifs (rolling 20 périodes)
  2. Si corr > 0.85: calcule le spread = price_A - beta * price_B
  3. Calcule le z-score du spread: (spread - mean) / std
  4. Si z-score > +2σ → SHORT A, LONG B (spread va revenir à la moyenne)
  5. Si z-score < -2σ → LONG A, SHORT B

Exécution: scan toutes les 60 minutes → positions ouvertes si opportunité.
Fermeture: quand z-score revient à 0 (±0.3) OU time-stop 6h.

Tables Supabase: pairs_trades, pairs_state
"""
import time
import math
import threading
from itertools import combinations
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from loguru import logger

# ─── Paramètres ───────────────────────────────────────────────────────────────
_MIN_CORR          = 0.82    # Corrélation minimale pour considérer une paire
_ZSCORE_ENTRY      = 2.0     # Z-score d'entrée
_ZSCORE_EXIT       = 0.30    # Z-score de sortie (retour à la moyenne)
_SPREAD_WINDOW     = 20      # Fenêtre de calcul z-score (bougies)
_MAX_PAIRS_OPEN    = 3       # Max positions paires simultanées
_PAIR_TIMEOUT_H    = 6.0     # Time-stop 6h
_SCAN_INTERVAL_S   = 3600    # Scan toutes les 60 minutes
_MIN_BARS          = 30      # Bars minimum pour calculer la corrélation


class PairsTrader:
    """
    Statistical Arbitrage engine basé sur la cointegration des actifs.
    Tourne en daemon thread, indépendant de la boucle principale.
    """

    def __init__(self, capital_client=None, ohlcv_cache=None,
                 db=None, telegram_router=None):
        self._capital  = capital_client
        self._cache    = ohlcv_cache
        self._db       = db
        self._tg       = telegram_router
        self._lock     = threading.Lock()

        # {(a, b): {entry_spread, beta, entry_time, direction_a, ref_a, ref_b}}
        self._open_pairs: Dict[tuple, dict] = {}

        self._running  = False
        self._thread   = None
        self._scan_count    = 0
        self._trade_count   = 0

        self._ensure_tables()

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        """Démarre le daemon thread de scan."""
        self._running = True
        self._thread  = threading.Thread(
            target=self._scan_loop, daemon=True, name="pairs_trader"
        )
        self._thread.start()
        logger.info("📐 Pairs Trader démarré (scan toutes les 60 min)")

    def stop(self):
        self._running = False

    # ─── Core Scan ───────────────────────────────────────────────────────────

    def _scan_loop(self):
        """Boucle principale: scan + monitor."""
        while self._running:
            try:
                self._scan_pairs()
                self._monitor_open_pairs()
            except Exception as e:
                logger.debug(f"PairsTrader scan: {e}")
            time.sleep(_SCAN_INTERVAL_S)

    def _scan_pairs(self):
        """Cherche des opportunités de pairs trading parmi tous les actifs."""
        if not self._cache:
            return

        self._scan_count += 1
        instruments = list(self._cache._cache.keys()) if hasattr(self._cache, '_cache') else []
        if len(instruments) < 2:
            return

        logger.debug(f"📐 Pairs scan #{self._scan_count}: {len(instruments)} actifs, {len(list(combinations(instruments, 2)))} paires éligibles")
        opportunities = []

        for a, b in combinations(instruments, 2):
            try:
                result = self._analyze_pair(a, b)
                if result:
                    opportunities.append(result)
            except Exception:
                pass

        # Trier par z-score absolu (meilleur signal d'abord)
        opportunities.sort(key=lambda x: abs(x["zscore"]), reverse=True)

        # N'ouvrir que si slots disponibles
        with self._lock:
            available_slots = _MAX_PAIRS_OPEN - len(self._open_pairs)

        for opp in opportunities[:available_slots]:
            self._open_pair_trade(opp)

    def _analyze_pair(self, a: str, b: str) -> Optional[dict]:
        """
        Analyse la cointegration entre deux actifs.
        Retourne un dict d'opportunité si z-score > seuil.
        """
        df_a = self._get_prices(a)
        df_b = self._get_prices(b)
        if df_a is None or df_b is None:
            return None

        n = min(len(df_a), len(df_b), _SPREAD_WINDOW + 10)
        if n < _MIN_BARS:
            return None

        prices_a = df_a["close"].iloc[-n:].values
        prices_b = df_b["close"].iloc[-n:].values

        # Corrélation de Pearson
        corr = self._pearson(prices_a, prices_b)
        if abs(corr) < _MIN_CORR:
            return None

        # Regression OLS: price_a = alpha + beta * price_b + epsilon
        beta  = self._ols_beta(prices_a, prices_b)
        alpha = prices_a.mean() - beta * prices_b.mean()

        # Calcul du spread et z-score
        spread_series = prices_a - (alpha + beta * prices_b)
        spread_mean   = spread_series[:-1].mean()
        spread_std    = spread_series[:-1].std()

        if spread_std < 1e-10:
            return None

        current_spread = spread_series[-1]
        zscore = (current_spread - spread_mean) / spread_std

        if abs(zscore) < _ZSCORE_ENTRY:
            return None

        return {
            "pair": (a, b),
            "corr": round(corr, 3),
            "beta": round(beta, 4),
            "alpha": round(alpha, 4),
            "zscore": round(zscore, 2),
            "spread_mean": round(spread_mean, 5),
            "spread_std": round(spread_std, 5),
            "current_spread": round(current_spread, 5),
            "price_a": prices_a[-1],
            "price_b": prices_b[-1],
        }

    # ─── Trade Execution ─────────────────────────────────────────────────────

    def _open_pair_trade(self, opp: dict):
        """Ouvre une paire de trades (long/short simultanément)."""
        a, b = opp["pair"]
        key  = (a, b)

        with self._lock:
            if key in self._open_pairs or len(self._open_pairs) >= _MAX_PAIRS_OPEN:
                return

        zscore = opp["zscore"]
        # z > 0: A surperformé B → SHORT A, LONG B (spread va baisser)
        # z < 0: A sous-performé B → LONG A, SHORT B (spread va monter)
        if zscore > 0:
            dir_a, dir_b = "SELL", "BUY"
        else:
            dir_a, dir_b = "BUY", "SELL"

        logger.info(
            f"📐 Pairs: {a}/{b} | z={zscore:.2f} | corr={opp['corr']} "
            f"→ {dir_a} {a}, {dir_b} {b}"
        )

        ref_a = ref_b = None
        if self._capital:
            try:
                ref_a = self._capital.place_market_order(a, dir_a, 0.1)
                ref_b = self._capital.place_market_order(b, dir_b, 0.1)
            except Exception as e:
                logger.warning(f"PairsTrader order: {e}")

        pair_state = {
            "pair":          key,
            "direction_a":   dir_a,
            "direction_b":   dir_b,
            "entry_spread":  opp["current_spread"],
            "spread_mean":   opp["spread_mean"],
            "spread_std":    opp["spread_std"],
            "beta":          opp["beta"],
            "alpha":         opp["alpha"],
            "entry_zscore":  zscore,
            "ref_a":         ref_a,
            "ref_b":         ref_b,
            "entry_time":    datetime.now(timezone.utc),
            "price_a":       opp["price_a"],
            "price_b":       opp["price_b"],
        }

        with self._lock:
            self._open_pairs[key] = pair_state

        self._trade_count += 1
        self._save_pair_open_async(opp, dir_a, dir_b, zscore)

        if self._tg:
            try:
                self._tg.send_trade(
                    f"📐 <b>Pairs Trade Ouvert</b>\n\n"
                    f"  <b>{dir_a} {a}</b> | <b>{dir_b} {b}</b>\n"
                    f"  Corrélation : {opp['corr']:.2f}\n"
                    f"  Z-Score : <b>{zscore:.2f}σ</b>\n"
                    f"  Spread actuel : {opp['current_spread']:.5f}\n"
                    f"  Cible retour : {opp['spread_mean']:.5f}"
                )
            except Exception:
                pass

    # ─── Monitor ─────────────────────────────────────────────────────────────

    def _monitor_open_pairs(self):
        """Vérifie les pairs ouvertes pour fermeture."""
        now = datetime.now(timezone.utc)
        with self._lock:
            pairs_to_check = dict(self._open_pairs)

        for key, state in pairs_to_check.items():
            a, b = key
            try:
                # Calcul du z-score actuel
                result = self._analyze_pair_live(a, b, state)
                if result is None:
                    continue

                current_z = result["current_zscore"]
                age_h     = (now - state["entry_time"]).total_seconds() / 3600

                # Sortie: z-score revenu à 0 ou time-stop
                if abs(current_z) <= _ZSCORE_EXIT or age_h >= _PAIR_TIMEOUT_H:
                    reason = "mean_reversion" if abs(current_z) <= _ZSCORE_EXIT else "time_stop"
                    pnl_est = result.get("pnl_est", 0.0)
                    self._close_pair(key, state, reason, current_z, pnl_est)

            except Exception as e:
                logger.debug(f"PairsTrader monitor {key}: {e}")

    def _analyze_pair_live(self, a: str, b: str, state: dict) -> Optional[dict]:
        """Calcule le z-score actuel pour une paire ouverte."""
        px_a = self._get_mid(a)
        px_b = self._get_mid(b)
        if px_a is None or px_b is None:
            return None

        current_spread = px_a - (state["alpha"] + state["beta"] * px_b)
        current_z = (current_spread - state["spread_mean"]) / max(state["spread_std"], 1e-10)

        # PnL estimé
        spread_move = state["entry_spread"] - current_spread
        pnl_dir = 1 if state["entry_zscore"] > 0 else -1
        pnl_est = spread_move * pnl_dir * 0.1  # approximation sur 0.1 lot

        return {"current_zscore": current_z, "pnl_est": round(pnl_est, 4)}

    def _close_pair(self, key: tuple, state: dict, reason: str,
                     current_z: float, pnl_est: float):
        """Ferme les deux jambes d'une paire."""
        a, b = key
        logger.info(f"📐 Pairs CLOSE {a}/{b} | z={current_z:.2f} | {reason} | PnL≈{pnl_est:+.4f}")

        if self._capital:
            for inst, ref, direction in [
                (a, state.get("ref_a"), state["direction_a"]),
                (b, state.get("ref_b"), state["direction_b"]),
            ]:
                if ref:
                    try:
                        close_dir = "SELL" if direction == "BUY" else "BUY"
                        self._capital.place_market_order(inst, close_dir, 0.1)
                    except Exception as e:
                        logger.warning(f"PairClose {inst}: {e}")

        with self._lock:
            self._open_pairs.pop(key, None)

        self._save_pair_close_async(key, state, reason, current_z, pnl_est)

        if self._tg:
            try:
                result_icon = "🟢" if pnl_est >= 0 else "🔴"
                self._tg.send_trade(
                    f"📐 <b>Pairs Trade Fermé</b> ({reason})\n\n"
                    f"  <b>{a}/{b}</b>\n"
                    f"  Z-Score final : {current_z:.2f}σ\n"
                    f"  {result_icon} PnL estimé : <b>{pnl_est:+.4f}</b>"
                )
            except Exception:
                pass

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _get_prices(self, instrument: str):
        """Récupère le DataFrame de prix depuis le cache."""
        try:
            if self._cache and hasattr(self._cache, 'get'):
                df = self._cache.get(instrument)
                if df is not None and "close" in df.columns:
                    return df
        except Exception:
            pass
        return None

    def _get_mid(self, instrument: str) -> Optional[float]:
        """Prix mid actuel depuis Capital.com."""
        try:
            if self._capital:
                px = self._capital.get_current_price(instrument)
                if px:
                    return px.get("mid", px.get("bid", 0))
        except Exception:
            pass
        return None

    @staticmethod
    def _pearson(x, y) -> float:
        n   = len(x)
        mx  = x.mean()
        my  = y.mean()
        num = ((x - mx) * (y - my)).sum()
        den = math.sqrt(((x - mx)**2).sum() * ((y - my)**2).sum())
        return num / den if den > 1e-10 else 0.0

    @staticmethod
    def _ols_beta(y, x) -> float:
        """OLS estimate of beta in y = alpha + beta*x."""
        mx  = x.mean()
        my  = y.mean()
        num = ((x - mx) * (y - my)).sum()
        den = ((x - mx)**2).sum()
        return num / den if den > 1e-10 else 1.0

    def status(self) -> dict:
        with self._lock:
            open_n = len(self._open_pairs)
        return {
            "scans": self._scan_count,
            "trades": self._trade_count,
            "open_pairs": open_n,
        }

    # ─── DB Persistence ───────────────────────────────────────────────────────

    def _ensure_tables(self):
        if not self._db:
            return
        try:
            if self._db._pg:
                self._db._execute("""
                    CREATE TABLE IF NOT EXISTS pairs_trades (
                        id          SERIAL PRIMARY KEY,
                        asset_a     VARCHAR(20),
                        asset_b     VARCHAR(20),
                        direction_a VARCHAR(4),
                        direction_b VARCHAR(4),
                        correlation DOUBLE PRECISION,
                        beta        DOUBLE PRECISION,
                        zscore_entry DOUBLE PRECISION,
                        zscore_exit DOUBLE PRECISION DEFAULT 0,
                        pnl_est     DOUBLE PRECISION DEFAULT 0,
                        reason      VARCHAR(20),
                        status      VARCHAR(10) DEFAULT 'OPEN',
                        opened_at   TIMESTAMPTZ DEFAULT NOW(),
                        closed_at   TIMESTAMPTZ
                    )
                """)
        except Exception as e:
            logger.debug(f"pairs_trades table: {e}")

    def _save_pair_open_async(self, opp, dir_a, dir_b, zscore):
        if not self._db:
            return
        self._db.async_write(self._save_pair_open_sync, opp, dir_a, dir_b, zscore)

    def _save_pair_open_sync(self, opp, dir_a, dir_b, zscore):
        try:
            a, b = opp["pair"]
            ph = "%s" if self._db._pg else "?"
            self._db._execute(
                f"INSERT INTO pairs_trades (asset_a,asset_b,direction_a,direction_b,correlation,beta,zscore_entry,status) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},'OPEN')",
                (a, b, dir_a, dir_b, opp["corr"], opp["beta"], zscore)
            )
        except Exception as e:
            logger.debug(f"pairs save open: {e}")

    def _save_pair_close_async(self, key, state, reason, current_z, pnl_est):
        if not self._db:
            return
        self._db.async_write(self._save_pair_close_sync, key, state, reason, current_z, pnl_est)

    def _save_pair_close_sync(self, key, state, reason, current_z, pnl_est):
        try:
            a, b = key
            ph = "%s" if self._db._pg else "?"
            self._db._execute(
                f"UPDATE pairs_trades SET status='CLOSED',reason={ph},zscore_exit={ph},"
                f"pnl_est={ph},closed_at=NOW() "
                f"WHERE asset_a={ph} AND asset_b={ph} AND status='OPEN'",
                (reason, current_z, pnl_est, a, b)
            )
        except Exception as e:
            logger.debug(f"pairs save close: {e}")
