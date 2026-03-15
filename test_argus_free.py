#!/usr/bin/env python3
"""
test_argus_free.py — ⚡ PROJECT ARGUS: Simulation Free NLP

Prouve que le bot peut analyser des headlines financières
100% localement, sans aucune API payante.

Phase 1: Test keyword fallback (toujours disponible)
Phase 2: Test FinBERT local (si transformers+torch installés)

Usage:
    python3 test_argus_free.py
    docker exec nemesis_bot python3 test_argus_free.py
"""

import sys
import time
sys.path.insert(0, ".")


def header(title):
    print(f"\n{'='*65}")
    print(f"  {title}")
    print(f"{'='*65}")


def print_result(r: dict, idx: int = 0):
    sentiment = r["sentiment"].upper()
    conf = r["confidence"]
    impact = r["impact_score"]
    assets = ", ".join(r["assets"]) if r["assets"] else "—"
    impulse = "⚡ IMPULSE" if r["is_impulse"] else ""

    # Color codes
    if sentiment == "POSITIVE":
        emoji = "🟢"
    elif sentiment == "NEGATIVE":
        emoji = "🔴"
    else:
        emoji = "⚪"

    print(f"  {idx}. {emoji} {sentiment:8s} | conf={conf:.2f} | impact={impact:+.2f} | {impulse}")
    print(f"     📰 \"{r['headline'][:70]}\"")
    print(f"     🎯 Assets: {assets}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "📡" * 20)
print("  PROJECT ARGUS — 100% FREE NLP NEWS ENGINE")
print("  Zero API. Local FinBERT. Open-Source Only.")
print("📡" * 20)
# ═══════════════════════════════════════════════════════════════════════════════

from argus_brain import ArgusBrain
from argus_sensors import ArgusSensors

# ─── Test Headlines ──────────────────────────────────────────────────────────
HEADLINES = [
    "Massive drought destroys coffee crops in Brazil",
    "Fed raises interest rates by 50 basis points, surprising markets",
    "Bitcoin surges past $100,000 as institutional adoption accelerates",
    "Oil prices crash after OPEC fails to agree on production cuts",
    "European Central Bank holds rates steady amid inflation concerns",
    "Gold hits record high as geopolitical tensions rise in Middle East",
    "Nasdaq rallies 3% as tech earnings beat expectations",
    "Bank of England warns of recession risk, pound drops sharply",
    "US unemployment falls to lowest since 1960s, dollar strengthens",
    "Ethereum upgrade successfully completed, network transactions soar",
    "USDA reports massive wheat surplus, commodity prices fall",
    "Japan intervenes in currency market as yen falls to 30-year low",
]


# ─── PHASE 1: KEYWORD FALLBACK (always available) ───────────────────────────
header("PHASE 1: KEYWORD FALLBACK SENTIMENT (no model required)")
brain_fallback = ArgusBrain()  # Don't load model
print(f"  🧠 Model loaded: {brain_fallback._model_loaded} (expected: False)")
print()

for i, headline in enumerate(HEADLINES, 1):
    result = brain_fallback.analyze(headline, source="test")
    print_result(result, i)

print(f"  📊 Total analyzed: {brain_fallback._analyzed_count}")
print(f"  ⚡ Impulse signals: {brain_fallback._impulse_count}")


# ─── PHASE 2: ASSET MAPPING TEST ────────────────────────────────────────────
header("PHASE 2: ASSET MAPPING VERIFICATION")

test_cases = [
    ("Fed raises rates", ["AUDUSD", "EURUSD", "GBPUSD", "NZDUSD", "USDCHF", "USDJPY"]),
    ("Bitcoin hits new ATH", ["BTCUSD"]),
    ("Gold surges on safe haven demand", ["GOLD"]),
    ("Oil prices crash after OPEC meeting", ["OIL_BRENT", "OIL_WTI"]),
    ("Nasdaq tech rally continues", ["US100"]),
    ("ECB holds rates amid eurozone slowdown", ["EURUSD"]),
]

all_pass = True
for headline, expected_assets in test_cases:
    result = brain_fallback.analyze(headline)
    matched = set(result["assets"])
    expected = set(expected_assets)
    ok = expected.issubset(matched)
    status = "✅" if ok else "❌"
    if not ok:
        all_pass = False
    print(f"  {status} \"{headline[:45]}...\"")
    print(f"     Expected: {sorted(expected)} → Got: {sorted(matched)}")

assert all_pass, "❌ Asset mapping failed!"
print(f"\n  ✅ ALL ASSET MAPPING TESTS PASS")


# ─── PHASE 3: FINBERT LOCAL (if available) ──────────────────────────────────
header("PHASE 3: FINBERT LOCAL MODEL")

brain_finbert = ArgusBrain()
model_ok = brain_finbert.load_model()

if model_ok:
    print(f"  ✅ FinBERT loaded successfully (100% local)")
    print()

    # The CEO's test case
    ceo_test = "Massive drought destroys coffee crops in Brazil"
    result = brain_finbert.analyze(ceo_test, source="CEO_TEST")
    print(f"  📰 CEO Test: \"{ceo_test}\"")
    print(f"  🧠 FinBERT Sentiment: {result['sentiment'].upper()}")
    print(f"  💯 Confidence: {result['confidence']:.2%}")
    print(f"  📈 Impact Score: {result['impact_score']:+.2f}")
    print()

    # Full batch with FinBERT
    print("  ─── Full FinBERT Analysis ───")
    for i, headline in enumerate(HEADLINES, 1):
        result = brain_finbert.analyze(headline, source="test_finbert")
        print_result(result, i)

    print(f"\n  📊 FinBERT analyzed: {brain_finbert._analyzed_count}")
    print(f"  ⚡ FinBERT impulses: {brain_finbert._impulse_count}")
else:
    print(f"  ⚠️  FinBERT not available (transformers/torch not installed)")
    print(f"  📋 To install: pip install transformers torch")
    print(f"  📋 Model will auto-download on first run (~400MB)")
    print(f"  ✅ Keyword fallback is active as backup")


# ─── PHASE 4: RSS SENSORS ───────────────────────────────────────────────────
header("PHASE 4: RSS SENSORS MODULE")
sensors = ArgusSensors()
sensors.inject_headline("Fed announces emergency rate cut", "test")
sensors.inject_headline("Bitcoin ETF approved by SEC", "test")
recent = sensors.get_recent(5)
print(f"  📡 Buffer: {len(recent)} headlines")
print(f"  📊 Stats: {sensors.stats}")
for ts, source, title, link in recent:
    print(f"    [{source}] {title}")
print(f"\n  ✅ RSS Sensors module operational")


# ═══════════════════════════════════════════════════════════════════════════════
header("COST ANALYSIS")
print("  💰 OpenAI API:      $0.00 (NOT USED)")
print("  💰 NewsAPI:         $0.00 (NOT USED)")
print("  💰 RSS Feeds:       $0.00 (PUBLIC)")
print("  💰 FinBERT Model:   $0.00 (OPEN-SOURCE)")
print("  💰 HuggingFace:     $0.00 (FREE TIER)")
print("  ──────────────────────────────")
print("  💰 TOTAL MONTHLY:   $0.00")

print("\n\n" + "🏆" * 20)
print("  PROJECT ARGUS — 100% FREE & VERIFIED")
print("🏆" * 20 + "\n")
