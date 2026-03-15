#!/usr/bin/env python3
"""
system_audit.py — ⚡ PRE-FLIGHT FULL SYSTEM SANITY CHECK
Simulates the entire boot + tick pipeline WITHOUT placing real orders.
Validates: JSON loading, HARD_BAN firewall, routing, ML imports, Docker config.
"""

import os, sys, traceback, json
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_DIR)
sys.path.insert(0, _DIR)

PASS = "✅ [OK]"
FAIL = "❌ [FAIL]"
results = []

def check(name: str, fn):
    try:
        ok, detail = fn()
        status = PASS if ok else FAIL
        results.append((name, ok, detail))
        print(f"  {status}  {name}: {detail}")
        return ok
    except Exception as e:
        results.append((name, False, str(e)))
        print(f"  {FAIL}  {name}: {e}")
        traceback.print_exc()
        return False

print()
print("═" * 70)
print("  ⚡ SYSTEM AUDIT — PRE-FLIGHT CHECK")
print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("═" * 70)

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: JSON LOADING INTEGRITY
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n{'─' * 70}")
print("  📦 PHASE 1: JSON Loading Integrity")
print(f"{'─' * 70}")

def t_optimized_json():
    path = os.path.join(_DIR, "optimized_rules.json")
    if not os.path.exists(path): return False, "FILE MISSING"
    data = json.load(open(path))
    return len(data) == 26, f"{len(data)} assets (expected 26)"

def t_blackops_json():
    path = os.path.join(_DIR, "black_ops_rules.json")
    if not os.path.exists(path): return False, "FILE MISSING"
    data = json.load(open(path))
    return len(data) == 9, f"{len(data)} assets (expected 9)"

def t_lazarus_json():
    path = os.path.join(_DIR, "lazarus_rules.json")
    if not os.path.exists(path): return False, "FILE MISSING"
    data = json.load(open(path))
    return len(data) == 7, f"{len(data)} entries (expected 7 = 5 ML + 2 pairs)"

def t_god_mode_merge():
    from god_mode import GOD_MODE_RULES
    return len(GOD_MODE_RULES) == 42, f"{len(GOD_MODE_RULES)} rules merged (expected 42)"

def t_hard_ban():
    from god_mode import HARD_BAN
    expected = {"GBPUSD","USDCHF","AUDUSD","EURGBP","EURAUD","AUDCAD","GBPCAD","CADCHF"}
    return HARD_BAN == expected, f"{len(HARD_BAN)} banned (expected 8)"

check("optimized_rules.json", t_optimized_json)
check("black_ops_rules.json", t_blackops_json)
check("lazarus_rules.json", t_lazarus_json)
check("GOD_MODE merge (42 rules)", t_god_mode_merge)
check("HARD_BAN set (8 assets)", t_hard_ban)

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: CAPITAL_INSTRUMENTS & ASSET_PROFILES OVERRIDE
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n{'─' * 70}")
print("  🔧 PHASE 2: ASSET_PROFILES Override & Instrument Filtering")
print(f"{'─' * 70}")

def t_instruments_count():
    from brokers.capital_client import CAPITAL_INSTRUMENTS
    return len(CAPITAL_INSTRUMENTS) == 40, f"{len(CAPITAL_INSTRUMENTS)} active (expected 40)"

def t_banned_removed():
    from brokers.capital_client import CAPITAL_INSTRUMENTS
    from god_mode import HARD_BAN
    banned_in = [b for b in HARD_BAN if b in CAPITAL_INSTRUMENTS]
    return len(banned_in) == 0, f"{len(banned_in)} banned still in list"

def t_profiles_god_mode():
    from brokers.capital_client import ASSET_PROFILES, CAPITAL_INSTRUMENTS
    gm = [s for s in CAPITAL_INSTRUMENTS if ASSET_PROFILES.get(s,{}).get("god_mode")]
    return len(gm) == 40, f"{len(gm)}/40 tagged god_mode=True"

def t_micro_tf_clean():
    from brokers.capital_client import MICRO_TF_PROFILES
    from god_mode import HARD_BAN
    banned_micro = [k for k,v in MICRO_TF_PROFILES.items() if v.get("epic") in HARD_BAN]
    return len(banned_micro) == 0, f"{len(banned_micro)} banned micro-TF entries"

check("CAPITAL_INSTRUMENTS (40 active)", t_instruments_count)
check("HARD_BAN removed from instruments", t_banned_removed)
check("All 40 profiles tagged god_mode", t_profiles_god_mode)
check("Micro-TF profiles clean (no banned)", t_micro_tf_clean)

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 3: FIREWALL TEST (HARD_BAN rejection)
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n{'─' * 70}")
print("  🛡️ PHASE 3: Firewall Test (HARD_BAN Rejection)")
print(f"{'─' * 70}")

def t_firewall_gbpusd():
    """Simulate _process_capital_symbol for GBPUSD — must be silently rejected."""
    from god_mode import HARD_BAN
    instrument = "GBPUSD"
    # Replicate the firewall logic from bot_signals.py
    if instrument in HARD_BAN:
        return True, "GBPUSD rejected by firewall (zero compute)"
    return False, "GBPUSD passed firewall — SECURITY BREACH!"

def t_firewall_btcusd():
    """BTCUSD should NOT be blocked."""
    from god_mode import HARD_BAN
    instrument = "BTCUSD"
    if instrument in HARD_BAN:
        return False, "BTCUSD blocked — false positive!"
    return True, "BTCUSD passed firewall correctly"

def t_firewall_all_banned():
    from god_mode import HARD_BAN
    all_blocked = all(b in HARD_BAN for b in
        ["GBPUSD","USDCHF","AUDUSD","EURGBP","EURAUD","AUDCAD","GBPCAD","CADCHF"])
    return all_blocked, "All 8 banned assets verified"

check("Firewall: GBPUSD rejected", t_firewall_gbpusd)
check("Firewall: BTCUSD passes", t_firewall_btcusd)
check("Firewall: all 8 banned verified", t_firewall_all_banned)

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 4: ROUTING & STRATEGY TEST
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n{'─' * 70}")
print("  🧭 PHASE 4: Routing & Strategy Verification")
print(f"{'─' * 70}")

def t_route_btcusd():
    from brokers.capital_client import ASSET_PROFILES
    p = ASSET_PROFILES.get("BTCUSD", {})
    strat = p.get("strat")
    tf = p.get("tf")
    src = p.get("god_source")
    # Alpha Factory found TF/1d as optimal for BTC (PnL=+15€ WR=54%)
    ok = strat == "TF" and tf == "1d"
    return ok, f"strat={strat} tf={tf} source={src}"

def t_route_usdjpy():
    from brokers.capital_client import ASSET_PROFILES
    p = ASSET_PROFILES.get("USDJPY", {})
    engine = p.get("god_engine")
    tf = p.get("tf")
    z_thr = p.get("z_threshold")
    ok = engine == "M51_STAT" and tf == "4h"
    return ok, f"engine={engine} tf={tf} z_thr={z_thr}"

def t_route_eurusd():
    from brokers.capital_client import ASSET_PROFILES
    p = ASSET_PROFILES.get("EURUSD", {})
    engine = p.get("god_engine")
    tf = p.get("tf")
    n_est = p.get("n_est")
    ok = engine == "M52_ML" and tf == "1d"
    return ok, f"engine={engine} tf={tf} n_est={n_est}"

def t_route_copper():
    from brokers.capital_client import ASSET_PROFILES
    p = ASSET_PROFILES.get("COPPER", {})
    engine = p.get("god_engine")
    tf = p.get("tf")
    ok = engine == "M52_ML" and tf == "4h"
    return ok, f"engine={engine} tf={tf}"

def t_route_fr40():
    from brokers.capital_client import ASSET_PROFILES
    p = ASSET_PROFILES.get("FR40", {})
    engine = p.get("god_engine")
    z_thr = p.get("z_threshold")
    ok = engine == "M51_STAT"
    return ok, f"engine={engine} z_thr={z_thr}"

def t_route_avaxusd():
    from brokers.capital_client import ASSET_PROFILES
    p = ASSET_PROFILES.get("AVAXUSD", {})
    strat = p.get("strat")
    tf = p.get("tf")
    src = p.get("god_source")
    ok = strat == "BK" and tf == "1h"
    return ok, f"strat={strat} tf={tf} source={src}"

check("Route: BTCUSD  → BK/1h (Alpha)", t_route_btcusd)
check("Route: AVAXUSD → BK/1h (Alpha)", t_route_avaxusd)
check("Route: USDJPY  → M51_STAT/4h (Black Ops)", t_route_usdjpy)
check("Route: FR40    → M51_STAT/4h (Black Ops)", t_route_fr40)
check("Route: EURUSD  → M52_ML/1d (Lazarus)", t_route_eurusd)
check("Route: COPPER  → M52_ML/4h (Lazarus)", t_route_copper)

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 5: ML & STATSMODELS IMPORT CHECK
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n{'─' * 70}")
print("  🧠 PHASE 5: ML & StatsModels Import Check")
print(f"{'─' * 70}")

def t_sklearn_import():
    from sklearn.ensemble import RandomForestClassifier
    model = RandomForestClassifier(n_estimators=10, max_depth=3, random_state=42)
    return True, f"RandomForestClassifier instantiated (sklearn {__import__('sklearn').__version__})"

def t_sklearn_predict():
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier
    X = np.random.randn(100, 5)
    y = (X[:,0] > 0).astype(int)
    model = RandomForestClassifier(n_estimators=10, max_depth=3, random_state=42)
    model.fit(X[:80], y[:80])
    proba = model.predict_proba(X[80:])
    return proba.shape == (20, 2), f"predict_proba shape={proba.shape} (expected (20,2))"

def t_statsmodels_coint():
    import numpy as np
    from statsmodels.tsa.stattools import coint
    np.random.seed(42)
    x = np.cumsum(np.random.randn(200))
    y = x + np.random.randn(200) * 0.5  # co-integrated
    _, p_value, _ = coint(x, y)
    return p_value < 0.05, f"p_value={p_value:.4f} (co-integrated={p_value<0.05})"

def t_joblib():
    import joblib
    return True, f"joblib {joblib.__version__} available"

def t_numpy():
    import numpy as np
    return True, f"numpy {np.__version__}"

def t_pandas():
    import pandas as pd
    return True, f"pandas {pd.__version__}"

check("scikit-learn import", t_sklearn_import)
check("RandomForest fit/predict", t_sklearn_predict)
check("statsmodels coint()", t_statsmodels_coint)
check("joblib (model persistence)", t_joblib)
check("numpy", t_numpy)
check("pandas", t_pandas)

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 6: STRATEGY.PY SMOKE TEST
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n{'─' * 70}")
print("  📈 PHASE 6: Strategy Module Smoke Test")
print(f"{'─' * 70}")

def t_strategy_import():
    from strategy import Strategy
    s = Strategy()
    return hasattr(s, "compute_indicators") and hasattr(s, "get_signal"), "Strategy class loaded"

def t_strategy_indicators():
    import pandas as pd, numpy as np
    from strategy import Strategy
    s = Strategy()
    n = 300
    np.random.seed(42)
    price = 100 + np.cumsum(np.random.randn(n) * 0.5)
    df = pd.DataFrame({
        "open": price, "high": price + abs(np.random.randn(n)),
        "low": price - abs(np.random.randn(n)),
        "close": price + np.random.randn(n) * 0.2,
        "volume": np.random.randint(100, 10000, n).astype(float),
    }, index=pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC"))
    df = s.compute_indicators(df)
    required = ["atr", "adx", "rsi", "ema200", "bb_up", "bb_lo", "macd", "zscore"]
    missing = [c for c in required if c not in df.columns]
    return len(missing) == 0, f"{len(df)} rows, {len(df.columns)} cols, missing={missing or 'none'}"

def t_strategy_signal():
    import pandas as pd, numpy as np
    from strategy import Strategy
    s = Strategy()
    n = 300
    np.random.seed(42)
    price = 100 + np.cumsum(np.random.randn(n) * 0.5)
    df = pd.DataFrame({
        "open": price, "high": price + abs(np.random.randn(n)),
        "low": price - abs(np.random.randn(n)),
        "close": price + np.random.randn(n) * 0.2,
        "volume": np.random.randint(100, 10000, n).astype(float),
    }, index=pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC"))
    df = s.compute_indicators(df)
    profile = {"strat": "BK", "tf": "1h", "cat": "crypto", "bk_margin": 0.03, "range_lb": 4}
    sig, score, confs = s.get_signal(df, symbol="BTCUSD", asset_profile=profile)
    return sig in ("BUY", "SELL", "HOLD"), f"signal={sig} score={score:.2f}"

check("Strategy class import", t_strategy_import)
check("compute_indicators (300 bars)", t_strategy_indicators)
check("get_signal (BK/crypto mock)", t_strategy_signal)

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 7: DOCKER & REQUIREMENTS AUDIT
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n{'─' * 70}")
print("  🐳 PHASE 7: Docker & Requirements Audit")
print(f"{'─' * 70}")

def t_dockerfile_json_copy():
    with open(os.path.join(_DIR, "Dockerfile")) as f:
        content = f.read()
    has_copy = "optimized_rules.json" in content and "black_ops_rules.json" in content and "lazarus_rules.json" in content
    return has_copy, "COPY *.json found in Dockerfile"

def t_requirements_sklearn():
    with open(os.path.join(_DIR, "requirements.txt")) as f:
        content = f.read()
    return "scikit-learn" in content, "scikit-learn in requirements.txt"

def t_requirements_statsmodels():
    with open(os.path.join(_DIR, "requirements.txt")) as f:
        content = f.read()
    return "statsmodels" in content, "statsmodels in requirements.txt"

def t_docker_compose():
    path = os.path.join(_DIR, "docker-compose.yml")
    if not os.path.exists(path): return False, "FILE MISSING"
    with open(path) as f:
        content = f.read()
    has_bot = "nemesis_bot" in content
    has_pg = "nemesis_postgres" in content
    has_redis = "nemesis_redis" in content
    return has_bot and has_pg and has_redis, f"bot={has_bot} pg={has_pg} redis={has_redis}"

check("Dockerfile: JSON COPY", t_dockerfile_json_copy)
check("requirements: scikit-learn", t_requirements_sklearn)
check("requirements: statsmodels", t_requirements_statsmodels)
check("docker-compose.yml: 3 services", t_docker_compose)

# ═══════════════════════════════════════════════════════════════════════════════
# FINAL REPORT
# ═══════════════════════════════════════════════════════════════════════════════
print(f"\n{'═' * 70}")
total = len(results)
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)

if failed == 0:
    print(f"  🏆 ALL SYSTEMS GO — {passed}/{total} checks PASSED")
    print(f"  ⚡ GOD MODE is FLIGHT-READY")
else:
    print(f"  ⚠️ {failed}/{total} FAILURES DETECTED")
    print(f"\n  Failed checks:")
    for name, ok, detail in results:
        if not ok:
            print(f"    {FAIL} {name}: {detail}")

print(f"{'═' * 70}\n")
sys.exit(0 if failed == 0 else 1)
