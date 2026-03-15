"""
mlops_retrainer.py — ⚡ APEX PREDATOR T3: Weekend MLOps Pipeline

Auto-réentraînement des modèles ML tous les samedis soirs.
Le bot se réveille le lundi matin avec un cerveau mis à jour.

Pipeline:
  1. Télécharge les nouvelles bougies de la semaine pour tous les actifs ML
  2. Ré-entraîne les RandomForest locaux
  3. Sauvegarde les modèles (Pickle/Joblib), met à jour les métriques
  4. Envoie un rapport Telegram

Schedule:
  - Samedi 22h UTC (marchés fermés)
  - Thread séparé, inactif la semaine

Usage:
    # Standalone
    python3 mlops_retrainer.py

    # In-bot (auto-schedule)
    from mlops_retrainer import MLOpsRetrainer
    retrainer = MLOpsRetrainer(capital_client, telegram_router)
    retrainer.start_scheduler()  # Background thread, fires Saturday 22h
"""

import os
import sys
import json
import time
import pickle
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from loguru import logger

try:
    import pandas as pd
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import cross_val_score
    from sklearn.metrics import accuracy_score, precision_score
    _ML_OK = True
except ImportError:
    _ML_OK = False

try:
    import ta
    _TA_OK = True
except ImportError:
    _TA_OK = False


# ─── Configuration ────────────────────────────────────────────────────────────
MODEL_DIR       = Path(os.environ.get("MLOPS_MODEL_DIR", "/tmp/mlops_models"))
RULES_FILE      = "lazarus_rules.json"
RETRAIN_DAY     = 5       # Saturday (0=Mon, 5=Sat)
RETRAIN_HOUR    = 22      # 22h UTC
MIN_SAMPLES     = 200     # Minimum OHLCV bars for training
LOOKBACK_BARS   = 500     # Download this many bars for training
N_ESTIMATORS    = 100     # RandomForest trees
MAX_DEPTH       = 8       # Tree depth
CV_FOLDS        = 5       # Cross-validation folds
MIN_ACCURACY    = 0.52    # Minimum accuracy to replace model


class MLOpsRetrainer:
    """
    Weekend auto-retraining pipeline for ML models.
    Runs every Saturday at 22h UTC via background thread.
    """

    def __init__(self, capital_client=None, telegram_router=None):
        self._capital = capital_client
        self._router = telegram_router
        self._thread = None
        self._running = False

        # Stats
        self._last_run = None
        self._last_results = {}
        self._total_retrains = 0

        MODEL_DIR.mkdir(parents=True, exist_ok=True)

    def start_scheduler(self):
        """Start background scheduler (Saturday 22h UTC)."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._schedule_loop, daemon=True, name="mlops-retrainer"
        )
        self._thread.start()
        logger.info("🤖 MLOps Retrainer: scheduler started (fires Saturday 22h UTC)")

    def stop(self):
        self._running = False

    def _schedule_loop(self):
        """Background loop: check if it's Saturday 22h and fire."""
        last_fired_week = -1
        while self._running:
            now = datetime.now(timezone.utc)
            week = now.isocalendar()[1]

            if (now.weekday() == RETRAIN_DAY
                    and now.hour == RETRAIN_HOUR
                    and week != last_fired_week):
                last_fired_week = week
                logger.info(f"🤖 MLOps: Saturday 22h UTC — launching retraining pipeline")
                try:
                    self.run_pipeline()
                except Exception as e:
                    logger.error(f"MLOps pipeline error: {e}")
                    self._send_alert(
                        f"❌ <b>MLOps Pipeline Error</b>\n\n{str(e)[:200]}"
                    )

            time.sleep(300)  # Check every 5 minutes

    # ═══════════════════════════════════════════════════════════════════════
    #  MAIN PIPELINE
    # ═══════════════════════════════════════════════════════════════════════

    def run_pipeline(self) -> dict:
        """
        Full retraining pipeline.

        1. Load ML instruments from lazarus_rules.json
        2. Download fresh data
        3. Feature engineering
        4. Train RandomForest
        5. Evaluate (cross-validation)
        6. Save if improved
        7. Report
        """
        if not _ML_OK:
            logger.error("MLOps: scikit-learn not available")
            return {"error": "sklearn not installed"}

        t0 = time.time()
        self._last_run = datetime.now(timezone.utc).isoformat()
        results = {}

        # ─── Step 1: Load ML instruments ──────────────────────────────────
        ml_instruments = self._load_ml_instruments()
        if not ml_instruments:
            logger.warning("MLOps: no ML instruments found")
            return {"error": "no instruments"}

        logger.info(f"🤖 MLOps: {len(ml_instruments)} instruments to retrain")

        # ─── Step 2-6: Per-instrument pipeline ────────────────────────────
        improved = 0
        failed = 0
        skipped = 0

        for instrument, config in ml_instruments.items():
            try:
                result = self._retrain_instrument(instrument, config)
                results[instrument] = result
                if result.get("improved"):
                    improved += 1
                elif result.get("skipped"):
                    skipped += 1
                else:
                    failed += 1
            except Exception as e:
                logger.error(f"MLOps {instrument}: {e}")
                results[instrument] = {"error": str(e)}
                failed += 1

        # ─── Step 7: Report ───────────────────────────────────────────────
        elapsed = time.time() - t0
        self._total_retrains += 1
        self._last_results = results

        report = (
            f"🤖 <b>MLOps Retraining Complete</b> — IC Markets MT5\n\n"
            f"📊 Instruments: {len(ml_instruments)}\n"
            f"✅ Improved: {improved}\n"
            f"⏭ Skipped: {skipped}\n"
            f"❌ Failed: {failed}\n"
            f"⏱ Duration: {elapsed:.0f}s\n\n"
        )

        for inst, res in results.items():
            if res.get("improved"):
                report += (
                    f"  ✅ {inst}: acc {res.get('old_accuracy', 0):.1%} → "
                    f"<b>{res.get('new_accuracy', 0):.1%}</b>\n"
                )
            elif res.get("error"):
                report += f"  ❌ {inst}: {res['error'][:50]}\n"
            else:
                report += f"  ⏭ {inst}: no improvement\n"

        logger.info(report.replace("<b>", "").replace("</b>", ""))
        self._send_alert(report)

        return {
            "improved": improved,
            "failed": failed,
            "skipped": skipped,
            "elapsed": elapsed,
            "details": results,
        }

    def _load_ml_instruments(self) -> dict:
        """Load ML-eligible instruments from lazarus_rules.json."""
        all_rules = {}
        for f in ["lazarus_rules.json", "optimized_rules.json", "black_ops_rules.json"]:
            if os.path.exists(f):
                try:
                    with open(f) as fh:
                        data = json.load(fh)
                        all_rules.update(data)
                except Exception:
                    pass

        # Filter: only M52_ML engine instruments
        ml_instruments = {}
        for inst, config in all_rules.items():
            engine = config.get("engine", config.get("strat", ""))
            if "ML" in engine.upper() or "M52" in engine.upper():
                ml_instruments[inst] = config

        return ml_instruments

    def _retrain_instrument(self, instrument: str, config: dict) -> dict:
        """Retrain a single instrument's RandomForest model."""
        tf = config.get("tf", "1d")

        # ─── Download data — yfinance 24/7, pas de dépendance Capital.com ───────
        df = self._fetch_data_yfinance(instrument, tf, LOOKBACK_BARS)
        if df is None:
            # Fallback Capital.com si yfinance échoue
            if self._capital and self._capital.available:
                try:
                    df = self._capital.fetch_ohlcv(
                        instrument, timeframe=tf, count=LOOKBACK_BARS
                    )
                except Exception as e:
                    return {"error": f"fetch failed: {e}", "skipped": True}

        if df is None or len(df) < MIN_SAMPLES:
            return {"error": f"insufficient data ({len(df) if df is not None else 0} bars)",
                    "skipped": True}

        # ─── Feature engineering ──────────────────────────────────────────
        df = self._compute_features(df)
        if df is None or len(df) < MIN_SAMPLES:
            return {"error": "feature computation failed", "skipped": True}

        # ─── Create labels ────────────────────────────────────────────────
        horizon = config.get("horizon", 3)
        df["target"] = (
            df["close"].shift(-horizon) > df["close"]
        ).astype(int)
        df = df.dropna()

        feature_cols = [
            "rsi", "adx", "atr_pct", "bb_width", "macd_hist",
            "vol_ratio", "ema_trend", "zscore",
        ]
        available_features = [c for c in feature_cols if c in df.columns]
        if len(available_features) < 4:
            return {"error": f"insufficient features ({len(available_features)})",
                    "skipped": True}

        X = df[available_features].values
        y = df["target"].values

        # ─── Train/Test split ─────────────────────────────────────────────
        split = int(len(X) * 0.8)
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]

        if len(X_train) < 50 or len(X_test) < 20:
            return {"error": "not enough train/test data", "skipped": True}

        # ─── Train new model ──────────────────────────────────────────────
        n_est = config.get("n_est", N_ESTIMATORS)
        depth = config.get("depth", MAX_DEPTH)

        model = RandomForestClassifier(
            n_estimators=n_est,
            max_depth=depth,
            random_state=42,
            n_jobs=-1,
        )
        model.fit(X_train, y_train)

        # ─── Evaluate ─────────────────────────────────────────────────────
        y_pred = model.predict(X_test)
        new_accuracy = accuracy_score(y_test, y_pred)

        # Cross-validation on full dataset
        cv_scores = cross_val_score(model, X, y, cv=CV_FOLDS, scoring="accuracy")
        cv_mean = cv_scores.mean()

        # ─── Compare with existing model ──────────────────────────────────
        model_path = MODEL_DIR / f"{instrument}_rf.pkl"
        old_accuracy = config.get("win_rate", 50) / 100

        if model_path.exists():
            try:
                with open(model_path, "rb") as f:
                    old_model = pickle.load(f)
                old_pred = old_model.predict(X_test)
                old_accuracy = accuracy_score(y_test, old_pred)
            except Exception:
                pass

        # ─── Save if improved ─────────────────────────────────────────────
        improved = new_accuracy > old_accuracy and new_accuracy >= MIN_ACCURACY

        if improved:
            with open(model_path, "wb") as f:
                pickle.dump(model, f)

            # Save metadata
            meta_path = MODEL_DIR / f"{instrument}_rf_meta.json"
            meta = {
                "instrument": instrument,
                "accuracy": round(new_accuracy, 4),
                "cv_mean": round(cv_mean, 4),
                "n_estimators": n_est,
                "max_depth": depth,
                "features": available_features,
                "train_size": len(X_train),
                "test_size": len(X_test),
                "trained_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(meta_path, "w") as f:
                json.dump(meta, f, indent=2)

            logger.info(
                f"✅ MLOps {instrument}: accuracy {old_accuracy:.1%} → "
                f"{new_accuracy:.1%} (CV: {cv_mean:.1%}) — MODEL SAVED"
            )

        return {
            "improved": improved,
            "old_accuracy": old_accuracy,
            "new_accuracy": new_accuracy,
            "cv_mean": cv_mean,
            "train_size": len(X_train),
            "features": available_features,
        }

    def _compute_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute ML features from OHLCV data."""
        if not _TA_OK:
            return None
        try:
            close = df["close"]
            high = df["high"]
            low = df["low"]
            vol = df["volume"]

            df["rsi"] = ta.momentum.RSIIndicator(close, window=14).rsi()
            df["adx"] = ta.trend.ADXIndicator(high, low, close, window=14).adx()

            atr = ta.volatility.AverageTrueRange(high, low, close, window=14)
            df["atr_raw"] = atr.average_true_range()
            df["atr_pct"] = df["atr_raw"] / close * 100

            bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
            df["bb_up"] = bb.bollinger_hband()
            df["bb_lo"] = bb.bollinger_lband()
            df["bb_width"] = (df["bb_up"] - df["bb_lo"]) / close * 100

            macd_ind = ta.trend.MACD(close)
            df["macd_hist"] = macd_ind.macd_diff()

            df["vol_ma"] = vol.rolling(20).mean()
            df["vol_ratio"] = vol / df["vol_ma"].replace(0, 1)

            ema20 = ta.trend.EMAIndicator(close, window=20).ema_indicator()
            ema50 = ta.trend.EMAIndicator(close, window=50).ema_indicator()
            df["ema_trend"] = (ema20 - ema50) / close * 100

            sma20 = close.rolling(20).mean()
            std20 = close.rolling(20).std()
            df["zscore"] = (close - sma20) / std20.replace(0, float("nan"))

            return df.dropna()
        except Exception as e:
            logger.error(f"MLOps feature engineering: {e}")
            return None

    # ─── Status ──────────────────────────────────────────────────────────

    def format_status(self) -> str:
        last = self._last_run or "never"
        return (
            f"🤖 <b>MLOps Retrainer</b>\n"
            f"  📅 Last run: {last}\n"
            f"  🔄 Total retrains: {self._total_retrains}\n"
            f"  📊 Schedule: Saturday 22h UTC"
        )

    def _fetch_data_yfinance(self, instrument: str, tf: str, count: int):
        """Fetch OHLCV via yfinance — fonctionne 24/7, pas besoin de Capital.com."""
        try:
            import yfinance as yf
            TICKER_MAP = {
                "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X",
                "USDJPY": "JPY=X",    "AUDUSD": "AUDUSD=X",
                "USDCAD": "CAD=X",    "USDCHF": "CHF=X",
                "AUDNZD": "AUDNZD=X", "EURCHF": "EURCHF=X",
                "GBPCHF": "GBPCHF=X", "NZDUSD": "NZDUSD=X",
                "EURGBP": "EURGBP=X", "EURJPY": "EURJPY=X",
                "GOLD":   "GC=F",     "SILVER": "SI=F",
                "OIL_BRENT": "BZ=F",  "OIL_WTI": "CL=F",
                "COPPER": "HG=F",     "NATGAS": "NG=F",
                "US500":  "^GSPC",    "US100":  "^NDX",
                "DE40":   "^GDAXI",   "UK100":  "^FTSE",
                "JP225":  "^N225",    "BTCUSD": "BTC-USD",
                "ETHUSD": "ETH-USD",  "XRPUSD": "XRP-USD",
            }
            TF_MAP     = {"1h": "1h", "4h": "1h", "1d": "1d", "15m": "15m", "5m": "5m"}
            PERIOD_MAP = {"1d": "2y", "1h": "60d", "4h": "60d", "15m": "7d", "5m": "5d"}
            ticker = TICKER_MAP.get(instrument.upper(), instrument)
            yf_tf  = TF_MAP.get(tf, "1d")
            period = PERIOD_MAP.get(tf, "2y")
            df = yf.download(ticker, period=period, interval=yf_tf,
                             progress=False, auto_adjust=True)
            if df is None or len(df) < 50:
                return None
            # Flatten MultiIndex columns if present (yfinance >=0.2)
            if hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)
            df = df.rename(columns={
                "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume",
            })
            df = df[[c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]].dropna()
            logger.debug(f"yfinance {instrument} ({ticker}): {len(df)} barres")
            return df.tail(count)
        except Exception as e:
            logger.debug(f"yfinance {instrument}: {e}")
            return None

        return {
            "last_run": self._last_run,
            "total_retrains": self._total_retrains,
            "last_results": self._last_results,
            "running": self._running,
        }

    def _send_alert(self, text: str):
        """Envoie en privé via Discord webhook monitoring (pas Telegram public)."""
        import requests as _req, os as _os
        webhook = _os.getenv("DISCORD_WEBHOOK_MONITORING", "")
        if not webhook:
            return
        try:
            # Convertir balises HTML Telegram → Markdown Discord
            clean = text.replace("<b>", "**").replace("</b>", "**")
            clean = clean.replace("<i>", "*").replace("</i>", "*")
            clean = clean.replace("<code>", "`").replace("</code>", "`")
            _req.post(webhook, json={"content": clean[:2000]}, timeout=10)
        except Exception as _e:
            logger.debug(f"MLOps Discord alert: {_e}")


# ─── Standalone execution ────────────────────────────────────────────────────
if __name__ == "__main__":
    sys.path.insert(0, ".")
    logger.info("🤖 MLOps Retrainer — standalone mode")

    try:
        from brokers.capital_client import CapitalClient
        capital = CapitalClient()
    except Exception as e:
        logger.error(f"CapitalClient: {e}")
        capital = None

    retrainer = MLOpsRetrainer(capital_client=capital)
    results = retrainer.run_pipeline()

    print(f"\n{'='*50}")
    print(f"  MLOps Pipeline Results")
    print(f"{'='*50}")
    print(f"  Improved: {results.get('improved', 0)}")
    print(f"  Skipped:  {results.get('skipped', 0)}")
    print(f"  Failed:   {results.get('failed', 0)}")
    print(f"  Duration: {results.get('elapsed', 0):.0f}s")
