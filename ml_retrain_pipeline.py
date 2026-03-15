"""
ml_retrain_pipeline.py — ⚡ Phase 3: AI & Data Maintenance

Automated pipeline for:
  3.1 Monthly M52 RandomForest retrain with walk-forward validation
  3.2 Model versioning (saves with timestamp, compares accuracy)
  3.3 Performance attribution per strategy engine
  3.4 Pairs co-integration retest

Usage:
    pipeline = MLRetrainPipeline(db, ohlcv_cache, telegram_router)
    pipeline.retrain_m52()  # Monthly retrain
    pipeline.retest_pairs() # Monthly pairs check
    pipeline.attribute_performance() # Generate engine report

Designed to run as a monthly cron or triggered by drift detector:
    docker exec nemesis_bot python3 -c "from ml_retrain_pipeline import MLRetrainPipeline; MLRetrainPipeline().retrain_m52()"
"""

import os
import json
import time
import pickle
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger

MODEL_DIR = "/app/data/models"
LAZARUS_RULES_PATH = "lazarus_rules.json"


class MLRetrainPipeline:
    """Phase 3: Automated ML retrain + performance attribution."""

    def __init__(self, db=None, ohlcv_cache=None, telegram_router=None):
        self._db = db
        self._cache = ohlcv_cache
        self._router = telegram_router
        Path(MODEL_DIR).mkdir(parents=True, exist_ok=True)

    # ═══════════════════════════════════════════════════════════════════════════
    # 3.1 RETRAIN M52 (RandomForest)
    # ═══════════════════════════════════════════════════════════════════════════

    def retrain_m52(self, min_samples: int = 100) -> dict:
        """
        Retrain the M52 RandomForest model with latest OHLCV data.
        Walk-forward: train on 80%, test on 20%, compare with current model.
        """
        try:
            import numpy as np
            from sklearn.ensemble import RandomForestClassifier
            from sklearn.model_selection import train_test_split
            from sklearn.metrics import accuracy_score
        except ImportError:
            logger.warning("⚠️ scikit-learn not installed — M52 retrain skipped")
            return {"status": "skip", "reason": "scikit-learn missing"}

        logger.info("🧠 M52 Retrain: Starting pipeline...")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        # Load Lazarus rules to know which instruments use M52
        try:
            with open(LAZARUS_RULES_PATH, "r") as f:
                lazarus = json.load(f)
            m52_instruments = [k for k, v in lazarus.items()
                             if v.get("engine") == "M52_ML"]
        except Exception as e:
            logger.error(f"M52 retrain: {e}")
            return {"status": "error", "reason": str(e)}

        if not m52_instruments:
            return {"status": "skip", "reason": "no M52 instruments in lazarus_rules"}

        # Build dataset from DB trade history
        all_X, all_y = [], []
        try:
            if self._db and hasattr(self._db, '_execute'):
                cur = self._db._execute(
                    "SELECT instrument, direction, entry, sl, tp1, pnl, score "
                    "FROM capital_trades WHERE status='CLOSED' "
                    "ORDER BY close_time DESC LIMIT 2000",
                    fetch=True
                )
                rows = cur.fetchall() if cur else []
                for row in rows:
                    instrument, direction, entry, sl, tp1, pnl, score = row
                    if not all([entry, sl, tp1]):
                        continue
                    # Features: [direction_num, risk, rr, score]
                    dir_num = 1 if direction == "BUY" else 0
                    risk = abs(entry - sl) / entry if entry > 0 else 0
                    rr = abs(tp1 - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 1
                    all_X.append([dir_num, risk, rr, score or 0.5])
                    all_y.append(1 if (pnl or 0) > 0 else 0)
        except Exception as e:
            logger.debug(f"M52 DB query: {e}")

        if len(all_X) < min_samples:
            logger.info(f"M52: Only {len(all_X)} samples (need {min_samples}) — skipping retrain")
            return {"status": "skip", "reason": f"insufficient data ({len(all_X)}/{min_samples})"}

        import numpy as np
        X = np.array(all_X)
        y = np.array(all_y)

        # Walk-forward split
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, shuffle=False  # Walk-forward: no shuffle
        )

        # Train new model
        model = RandomForestClassifier(
            n_estimators=100, max_depth=6, min_samples_leaf=5,
            random_state=42, n_jobs=-1
        )
        model.fit(X_train, y_train)
        new_acc = accuracy_score(y_test, model.predict(X_test))

        # Load old model accuracy (if exists)
        old_acc = 0.0
        old_model_path = os.path.join(MODEL_DIR, "m52_current.pkl")
        if os.path.exists(old_model_path):
            try:
                with open(old_model_path, "rb") as f:
                    old_model = pickle.load(f)
                old_acc = accuracy_score(y_test, old_model.predict(X_test))
            except Exception:
                pass

        # Save new model with version
        version_path = os.path.join(MODEL_DIR, f"m52_{timestamp}.pkl")
        with open(version_path, "wb") as f:
            pickle.dump(model, f)

        # Replace current if better (or if none exists)
        replaced = False
        if new_acc >= old_acc or not os.path.exists(old_model_path):
            with open(old_model_path, "wb") as f:
                pickle.dump(model, f)
            replaced = True

        result = {
            "status": "success",
            "samples": len(all_X),
            "new_accuracy": round(new_acc, 4),
            "old_accuracy": round(old_acc, 4),
            "replaced": replaced,
            "version": timestamp,
            "instruments": m52_instruments,
        }

        logger.info(
            f"🧠 M52 Retrain: acc={new_acc:.2%} (old={old_acc:.2%}) "
            f"{'→ REPLACED' if replaced else '→ kept old'} | {len(all_X)} samples"
        )

        # Save metadata to DB
        try:
            if self._db and hasattr(self._db, '_execute'):
                self._db._execute(
                    "INSERT INTO nemesis_bot_state (key, value, updated_at) "
                    "VALUES (%s, %s, NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                    (f"m52_retrain_{timestamp}", json.dumps(result))
                )
        except Exception:
            pass

        # Telegram notification
        emoji = "✅" if replaced else "🟡"
        self._send_alert(
            f"🧠 <b>M52 RETRAIN {emoji}</b>\n\n"
            f"📊 Échantillons : <b>{len(all_X)}</b>\n"
            f"🎯 Accuracy : <b>{new_acc:.1%}</b> (ancien: {old_acc:.1%})\n"
            f"🔄 Modèle : {'REMPLACÉ ✅' if replaced else 'conservé (ancien meilleur)'}\n"
            f"📋 Instruments : {', '.join(m52_instruments[:5])}"
        )

        return result

    # ═══════════════════════════════════════════════════════════════════════════
    # 3.3 PAIRS COINTEGRATION RETEST
    # ═══════════════════════════════════════════════════════════════════════════

    def retest_pairs(self) -> dict:
        """Retest co-integration for all M53 pairs."""
        try:
            from statsmodels.tsa.stattools import adfuller
        except ImportError:
            return {"status": "skip", "reason": "statsmodels missing"}

        try:
            with open(LAZARUS_RULES_PATH, "r") as f:
                lazarus = json.load(f)
            pairs = {k: v for k, v in lazarus.items()
                    if v.get("engine") == "M53_PAIRS"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}

        results = {}
        for pair_key, rule in pairs.items():
            asset_a = rule.get("asset_a", "")
            asset_b = rule.get("asset_b", "")
            if not asset_a or not asset_b:
                continue

            try:
                # Load price data from OHLCV cache
                if self._cache:
                    df_a = self._cache.get(asset_a)
                    df_b = self._cache.get(asset_b)
                    if df_a is not None and df_b is not None:
                        import numpy as np
                        prices_a = df_a["close"].values[-200:]
                        prices_b = df_b["close"].values[-200:]
                        if len(prices_a) >= 50 and len(prices_b) >= 50:
                            spread = prices_a[:min(len(prices_a), len(prices_b))] - \
                                    prices_b[:min(len(prices_a), len(prices_b))]
                            adf_stat, p_value = adfuller(spread)[:2]
                            results[pair_key] = {
                                "p_value": round(p_value, 4),
                                "cointegrated": p_value < 0.05,
                                "adf_stat": round(adf_stat, 4),
                            }
            except Exception as e:
                results[pair_key] = {"error": str(e)}

        dead_pairs = [k for k, v in results.items() if not v.get("cointegrated", True)]
        if dead_pairs:
            logger.warning(f"⚠️ Pairs no longer co-integrated: {dead_pairs}")

        return {"status": "success", "pairs": results, "dead": dead_pairs}

    # ═══════════════════════════════════════════════════════════════════════════
    # 3.2 PERFORMANCE ATTRIBUTION
    # ═══════════════════════════════════════════════════════════════════════════

    def attribute_performance(self) -> dict:
        """Generate PnL breakdown by strategy engine."""
        if not self._db or not hasattr(self._db, '_execute'):
            return {"status": "skip", "reason": "db not available"}

        try:
            # Query trade results grouped by engine
            from brokers.capital_client import ASSET_PROFILES
            cur = self._db._execute(
                "SELECT instrument, pnl, result FROM capital_trades "
                "WHERE status='CLOSED' AND close_time > NOW() - INTERVAL '30 days' "
                "ORDER BY close_time",
                fetch=True
            )
            rows = cur.fetchall() if cur else []

            engine_stats = {}
            for instrument, pnl, result in rows:
                profile = ASSET_PROFILES.get(instrument, {})
                engine = profile.get("god_engine", profile.get("strat", "UNKNOWN"))
                source = profile.get("god_source", "unknown")
                key = f"{engine} [{source}]"

                if key not in engine_stats:
                    engine_stats[key] = {
                        "trades": 0, "wins": 0, "pnl": 0,
                        "instruments": set()
                    }
                engine_stats[key]["trades"] += 1
                engine_stats[key]["pnl"] += pnl or 0
                if result == "WIN":
                    engine_stats[key]["wins"] += 1
                engine_stats[key]["instruments"].add(instrument)

            # Format
            for k, v in engine_stats.items():
                v["wr"] = round(v["wins"] / v["trades"] * 100, 1) if v["trades"] > 0 else 0
                v["instruments"] = list(v["instruments"])
                v["pnl"] = round(v["pnl"], 2)

            return {"status": "success", "engines": engine_stats, "period": "30d"}

        except Exception as e:
            return {"status": "error", "reason": str(e)}

    # ─── Telegram ─────────────────────────────────────────────────────────────

    def _send_alert(self, text: str):
        if self._router:
            try:
                self._router.send_to("stats", text)
            except Exception as e:
                logger.error(f"MLPipeline Telegram: {e}")
