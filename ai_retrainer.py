#!/usr/bin/env python3
"""
ai_retrainer.py — ⚡ Tâche 5: Auto-Healing AI Pipeline (Cron-Ready)

Script autonome pour Cronjob hebdomadaire (dimanche 02h00 UTC).
Télécharge les données récentes, ré-entraîne M52 RandomForest,
et met à jour silencieusement les règles pour le lundi matin.

Usage (cron):
    0 2 * * 0 cd /app && python3 ai_retrainer.py >> /app/data/logs/ai_retrain.log 2>&1

Usage (docker):
    docker exec nemesis_bot python3 ai_retrainer.py

Usage (manual):
    python3 ai_retrainer.py
"""

import os
import sys
import json
import time
import pickle
import traceback
from datetime import datetime, timezone
from pathlib import Path

# ─── Paths ──────────────────────────────────────────────────────────────────
MODEL_DIR = os.getenv("MODEL_DIR", "/app/data/models")
LAZARUS_RULES = "lazarus_rules.json"
BLACK_OPS_RULES = "black_ops_rules.json"
OPTIMIZED_RULES = "optimized_rules.json"
RETRAIN_LOG = os.path.join(MODEL_DIR, "retrain_history.json")

Path(MODEL_DIR).mkdir(parents=True, exist_ok=True)


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def send_telegram_alert(text: str):
    """Send alert to Risk channel via direct API call."""
    import requests
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def load_m52_instruments() -> list:
    """Load instruments managed by M52 (RandomForest) from Lazarus rules."""
    instruments = []
    for rules_file in [LAZARUS_RULES, BLACK_OPS_RULES, OPTIMIZED_RULES]:
        if not os.path.exists(rules_file):
            continue
        try:
            with open(rules_file, "r") as f:
                rules = json.load(f)
            for inst, config in rules.items():
                engine = config.get("engine", config.get("strat", ""))
                if "M52" in str(engine) or "ML" in str(engine):
                    if inst not in instruments:
                        instruments.append(inst)
        except Exception as e:
            log(f"⚠️ Failed to read {rules_file}: {e}")
    return instruments


def download_fresh_data(instruments: list) -> dict:
    """Download recent OHLCV data from yfinance or Capital.com."""
    data = {}

    try:
        import yfinance as yf

        # Map Capital.com epics → yfinance tickers
        TICKER_MAP = {
            "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
            "USDCHF": "USDCHF=X", "AUDUSD": "AUDUSD=X", "NZDUSD": "NZDUSD=X",
            "EURCHF": "EURCHF=X", "AUDNZD": "AUDNZD=X", "EURGBP": "EURGBP=X",
            "EURAUD": "EURAUD=X", "AUDCAD": "AUDCAD=X", "GBPCAD": "GBPCAD=X",
            "GBPCHF": "GBPCHF=X", "CADCHF": "CADCHF=X",
            "BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD",
            "GOLD": "GC=F", "SILVER": "SI=F",
            "US500": "^GSPC", "US100": "^NDX", "DE40": "^GDAXI",
        }

        for inst in instruments:
            ticker = TICKER_MAP.get(inst, f"{inst}=X")
            try:
                df = yf.download(ticker, period="6mo", interval="1d", progress=False)
                if df is not None and len(df) >= 50:
                    data[inst] = df
                    log(f"  📊 {inst} ({ticker}): {len(df)} candles")
                else:
                    log(f"  ⚠️ {inst}: insufficient data ({len(df) if df is not None else 0})")
            except Exception as e:
                log(f"  ❌ {inst}: {e}")

    except ImportError:
        log("⚠️ yfinance not installed — using synthetic data for retraining")
        # Generate synthetic placeholder data
        import numpy as np
        for inst in instruments:
            n = 200
            closes = 1.0 + np.cumsum(np.random.randn(n) * 0.001)
            data[inst] = {"close": closes, "synthetic": True}
            log(f"  🧪 {inst}: synthetic {n} points")

    return data


def build_features(data: dict) -> tuple:
    """
    Build feature matrix from OHLCV data.
    Features: [lag1..lag5 returns, rolling_vol, dist_to_sma200, day_of_week]
    """
    import numpy as np

    all_X, all_y = [], []

    for inst, df_or_dict in data.items():
        try:
            if isinstance(df_or_dict, dict) and df_or_dict.get("synthetic"):
                closes = df_or_dict["close"]
            else:
                closes = df_or_dict["Close"].values if "Close" in df_or_dict.columns else df_or_dict["close"].values

            if len(closes) < 210:
                continue

            returns = np.diff(closes) / closes[:-1]
            sma200 = np.convolve(closes, np.ones(200)/200, mode='valid')

            for i in range(205, len(closes) - 1):
                # Lag returns
                lags = [returns[i-j] if i-j >= 0 else 0 for j in range(1, 6)]

                # Rolling volatility (20-period)
                vol = np.std(returns[max(0,i-20):i]) if i >= 20 else 0.01

                # Distance to SMA200
                sma_idx = i - 200
                dist_sma = (closes[i] - sma200[sma_idx]) / sma200[sma_idx] if sma_idx >= 0 and sma200[sma_idx] > 0 else 0

                # Day of week (0-4)
                dow = i % 5  # Approximation

                features = lags + [vol, dist_sma, dow]
                all_X.append(features)

                # Target: next day return > 0 → 1, else 0
                next_ret = (closes[i+1] - closes[i]) / closes[i]
                all_y.append(1 if next_ret > 0 else 0)

        except Exception as e:
            log(f"  ⚠️ Feature build {inst}: {e}")

    return np.array(all_X) if all_X else np.array([]), np.array(all_y) if all_y else np.array([])


def retrain_model(X, y) -> dict:
    """Train RandomForest and return metrics."""
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score, classification_report

    if len(X) < 50:
        return {"status": "skip", "reason": f"insufficient samples ({len(X)})"}

    # Walk-forward: train on 80%, test on 20% (chronological)
    split = int(len(X) * 0.8)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    model = RandomForestClassifier(
        n_estimators=150,
        max_depth=8,
        min_samples_leaf=5,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    accuracy = accuracy_score(y_test, y_pred)

    # Feature importance
    feature_names = [f"lag{i}" for i in range(1,6)] + ["vol", "dist_sma", "dow"]
    importances = dict(zip(feature_names, [round(f, 4) for f in model.feature_importances_]))

    return {
        "status": "success",
        "accuracy": round(accuracy, 4),
        "samples": len(X),
        "train_samples": len(X_train),
        "test_samples": len(X_test),
        "feature_importance": importances,
        "model": model,
    }


def save_model(model, result: dict):
    """Save model with versioning."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Save versioned model
    version_path = os.path.join(MODEL_DIR, f"m52_{timestamp}.pkl")
    with open(version_path, "wb") as f:
        pickle.dump(model, f)

    # Check if new model is better than current
    current_path = os.path.join(MODEL_DIR, "m52_current.pkl")
    replaced = False

    if os.path.exists(current_path):
        # Load old model accuracy from retrain log
        old_acc = 0.0
        if os.path.exists(RETRAIN_LOG):
            try:
                with open(RETRAIN_LOG, "r") as f:
                    history = json.load(f)
                if history:
                    old_acc = history[-1].get("accuracy", 0.0)
            except Exception:
                pass

        if result["accuracy"] >= old_acc:
            replaced = True
    else:
        replaced = True

    if replaced:
        with open(current_path, "wb") as f:
            pickle.dump(model, f)
        log(f"✅ Model replaced: m52_current.pkl (acc={result['accuracy']:.2%})")
    else:
        log(f"🟡 Model kept: current is better (new={result['accuracy']:.2%})")

    # Append to retrain history
    log_entry = {
        "timestamp": timestamp,
        "accuracy": result["accuracy"],
        "samples": result["samples"],
        "replaced": replaced,
        "features": result.get("feature_importance", {}),
    }
    history = []
    if os.path.exists(RETRAIN_LOG):
        try:
            with open(RETRAIN_LOG, "r") as f:
                history = json.load(f)
        except Exception:
            pass
    history.append(log_entry)
    # Keep last 52 entries (1 year of weekly retrains)
    history = history[-52:]
    with open(RETRAIN_LOG, "w") as f:
        json.dump(history, f, indent=2)

    return replaced


def main():
    log("=" * 60)
    log("🧠 AI RETRAINER — M52 RandomForest Pipeline")
    log("=" * 60)

    start_time = time.time()

    # Step 1: Load M52 instruments
    instruments = load_m52_instruments()
    log(f"📋 M52 Instruments: {instruments}")

    if not instruments:
        log("⚠️ No M52 instruments found — nothing to retrain")
        return

    # Step 2: Download fresh data
    log("\n📥 Downloading fresh data...")
    data = download_fresh_data(instruments)
    log(f"📊 Data for {len(data)} instruments")

    if not data:
        log("❌ No data downloaded — aborting")
        send_telegram_alert("❌ <b>AI RETRAIN FAILED</b>\nNo data downloaded.")
        return

    # Step 3: Build features
    log("\n🔧 Building features...")
    try:
        import numpy as np
        X, y = build_features(data)
        log(f"📐 Features: {X.shape if len(X) > 0 else '(empty)'}")
    except ImportError:
        log("❌ numpy/sklearn not installed — aborting")
        return

    if len(X) < 50:
        log(f"⚠️ Insufficient samples ({len(X)}) — minimum 50 required")
        return

    # Step 4: Retrain
    log("\n🏋️ Training RandomForest...")
    result = retrain_model(X, y)

    if result["status"] != "success":
        log(f"⚠️ Retrain skipped: {result.get('reason', 'unknown')}")
        return

    log(f"🎯 Accuracy: {result['accuracy']:.2%} ({result['samples']} samples)")
    log(f"📊 Feature importance: {result['feature_importance']}")

    # Step 5: Save with versioning
    log("\n💾 Saving model...")
    replaced = save_model(result["model"], result)

    elapsed = time.time() - start_time
    log(f"\n{'='*60}")
    log(f"🏆 RETRAIN COMPLETE in {elapsed:.1f}s")
    log(f"   Accuracy: {result['accuracy']:.2%}")
    log(f"   Samples:  {result['samples']}")
    log(f"   Model:    {'REPLACED ✅' if replaced else 'kept old 🟡'}")
    log(f"{'='*60}")

    # Telegram notification
    emoji = "✅" if replaced else "🟡"
    send_telegram_alert(
        f"🧠 <b>AI RETRAIN {emoji}</b>\n\n"
        f"🎯 Accuracy: <b>{result['accuracy']:.1%}</b>\n"
        f"📊 Samples: <b>{result['samples']}</b>\n"
        f"🔄 Model: {'REMPLACÉ' if replaced else 'conservé'}\n"
        f"⏱ Durée: {elapsed:.1f}s\n"
        f"📋 Instruments: {', '.join(instruments[:5])}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"❌ FATAL: {e}")
        traceback.print_exc()
        send_telegram_alert(f"❌ <b>AI RETRAIN FATAL ERROR</b>\n<code>{e}</code>")
        sys.exit(1)
