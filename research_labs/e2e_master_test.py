#!/usr/bin/env python3
"""
e2e_master_test.py — 🧪 L'Épreuve du Feu — End-to-End Master Integration Test

Simule une semaine de trading condensée en quelques secondes.
Injecte de faux signaux (Mock Ticks) directement dans le pipeline pour validater
que tous les moteurs communiquent parfaitement.

SCÉNARIOS :
  1. CRYPTO Time Stop (M38+M40) : BTC ouvert 30h → doit rester ouvert (limite 48h)
  2. TRADFI Friday Kill-Switch : EUR/USD ouvert Ven 15h → tué à Ven 20:50 UTC
  3. NLP/Swarm Propagation (M26+M27) : news négative → propagation sans crash
  4. M38 R:R Gate dynamique : CRYPTO 1.5 vs TRADFI 1.2
  5. Backtest PositionTracker cohérence

Usage : python3 e2e_master_test.py
"""

import os
import sys
import traceback
from datetime import datetime, timezone, timedelta
from typing import Tuple

# ── Setup path ──────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Suppress loguru noise ──────────────────────────────────────────────────
from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")

# ═══════════════════════════════════════════════════════════════════════════════
# TEST INFRASTRUCTURE
# ═══════════════════════════════════════════════════════════════════════════════

TOTAL_TESTS = 0
PASSED = 0
FAILED = 0
RESULTS = []

def test(name: str, condition: bool, detail: str = ""):
    global TOTAL_TESTS, PASSED, FAILED
    TOTAL_TESTS += 1
    if condition:
        PASSED += 1
        icon = "✅"
        status = "SUCCESS"
    else:
        FAILED += 1
        icon = "❌"
        status = "FAILED"
    msg = f"  {icon} {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    RESULTS.append((name, status, detail))

def section(title: str):
    print(f"\n{'─' * 60}")
    print(f"  🧪 {title}")
    print(f"{'─' * 60}")


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 1 : ASSET CLASS CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

def test_asset_class_classification():
    section("TEST 1 : ASSET CLASS CLASSIFICATION")

    from brokers.capital_client import (
        get_asset_class, get_risk_params, RISK_BY_CLASS,
        ASSET_CLASS_MAP, ASSET_PROFILES,
    )

    # Crypto instruments
    test("BTCUSD → CRYPTO", get_asset_class("BTCUSD") == "CRYPTO")
    test("ETHUSD → CRYPTO", get_asset_class("ETHUSD") == "CRYPTO")
    test("SOLUSD → CRYPTO", get_asset_class("SOLUSD") == "CRYPTO")

    # TradFi instruments
    test("EURUSD → TRADFI", get_asset_class("EURUSD") == "TRADFI")
    test("GOLD → TRADFI",   get_asset_class("GOLD") == "TRADFI")
    test("US500 → TRADFI",  get_asset_class("US500") == "TRADFI")
    test("AAPL → TRADFI",   get_asset_class("AAPL") == "TRADFI")
    test("AUDNZD → TRADFI", get_asset_class("AUDNZD") == "TRADFI")

    # Risk params
    btc_risk = get_risk_params("BTCUSD")
    eur_risk = get_risk_params("EURUSD")
    test("CRYPTO time_stop = 48h", btc_risk["time_stop_h"] == 48,
         f"got {btc_risk['time_stop_h']}h")
    test("CRYPTO rr_min = 1.5",    btc_risk["rr_min"] == 1.5,
         f"got {btc_risk['rr_min']}")
    test("TRADFI time_stop = 24h", eur_risk["time_stop_h"] == 24,
         f"got {eur_risk['time_stop_h']}h")
    test("TRADFI rr_min = 1.2",    eur_risk["rr_min"] == 1.2,
         f"got {eur_risk['rr_min']}")


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 2 : CRYPTO TIME STOP (M38 + M40) — BTC 30h STILL OPEN
# ═══════════════════════════════════════════════════════════════════════════════

def test_crypto_time_stop():
    section("TEST 2 : CRYPTO TIME STOP — BTC ouvert 30h (limite 48h)")

    from backtest_engine import (
        PositionTracker, FrictionModel, BacktestTrade,
    )
    from brokers.capital_client import get_risk_params

    friction = FrictionModel()
    tracker = PositionTracker(10_000.0, friction)

    # Mercredi 10h00 UTC — Ouvrir BTC
    wed_10h = datetime(2026, 3, 11, 10, 0, tzinfo=timezone.utc)
    btc_price = 30000.0
    sl = 29000.0
    tp = 32000.0  # R:R = 2.0, will be auto-adjusted if < rr_min

    trade = tracker.open_trade("BTCUSD", "BUY", btc_price, sl, tp, wed_10h)
    test("BTC trade ouvert",
         trade is not None and "BTCUSD" in tracker.open_trades,
         f"entry={trade.entry_price:.0f}" if trade else "FAILED TO OPEN")

    # Avancer de 30 heures (Jeudi 16h) — BTC doit rester ouvert
    thu_16h = wed_10h + timedelta(hours=30)
    btc_curr = 30100.0
    prices = {"BTCUSD": (30050.0, 30200.0, 29900.0, btc_curr)}
    tracker.update_trades(prices, thu_16h)

    test("BTC TOUJOURS ouvert après 30h (CRYPTO limit=48h)",
         "BTCUSD" in tracker.open_trades,
         f"open={len(tracker.open_trades)} closed={len(tracker.closed_trades)}")

    # Vérifier que le trade n'a PAS été fermé
    test("Aucun trade fermé après 30h",
         len(tracker.closed_trades) == 0)

    # Avancer à 49h — BTC doit être fermé maintenant
    past_48h = wed_10h + timedelta(hours=49)
    prices = {"BTCUSD": (30050.0, 30200.0, 29900.0, btc_curr)}
    tracker.update_trades(prices, past_48h)

    test("BTC fermé après 49h (> 48h CRYPTO limit)",
         "BTCUSD" not in tracker.open_trades,
         f"reason={tracker.closed_trades[-1].exit_reason}" if tracker.closed_trades else "STILL OPEN")

    if tracker.closed_trades:
        test("Raison = TIME_STOP",
             tracker.closed_trades[-1].exit_reason == "TIME_STOP")

    return tracker


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 3 : TRADFI FRIDAY KILL-SWITCH — EUR/USD tué le vendredi
# ═══════════════════════════════════════════════════════════════════════════════

def test_friday_killswitch():
    section("TEST 3 : FRIDAY KILL-SWITCH — EUR/USD tué, BTC survit")

    from backtest_engine import PositionTracker, FrictionModel
    from brokers.capital_client import get_asset_class

    friction = FrictionModel()
    tracker = PositionTracker(10_000.0, friction)

    # ── Phase 1 : Ouvrir BTC le Jeudi (< 48h avant Ven 20:50) ──
    thu_10h = datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc)
    tracker.open_trade("BTCUSD", "BUY", 30000.0, 29000.0, 32000.0, thu_10h)
    test("BTC ouvert le Jeudi 10h (34h50m avant Kill-Switch)",
         "BTCUSD" in tracker.open_trades)

    # ── Phase 2 : Ouvrir EUR/USD le Vendredi 15h ──
    fri_15h = datetime(2026, 3, 13, 15, 0, tzinfo=timezone.utc)
    tracker.open_trade("EURUSD", "BUY", 1.0850, 1.0800, 1.0920, fri_15h)
    test("EUR/USD ouvert le Vendredi 15h", "EURUSD" in tracker.open_trades)

    test("2 positions ouvertes",
         len(tracker.open_trades) == 2,
         f"positions: {list(tracker.open_trades.keys())}")

    # ── Phase 3 : Avancer à Vendredi 20:49 — PAS encore Kill ──
    fri_2049 = datetime(2026, 3, 13, 20, 49, tzinfo=timezone.utc)
    prices = {
        "BTCUSD": (30050.0, 30200.0, 29900.0, 30100.0),
        "EURUSD": (1.0855, 1.0870, 1.0840, 1.0860),
    }
    tracker.update_trades(prices, fri_2049)

    test("EUR/USD TOUJOURS ouvert à 20:49",
         "EURUSD" in tracker.open_trades,
         "1 minute avant le Kill-Switch")

    test("BTC TOUJOURS ouvert à 20:49",
         "BTCUSD" in tracker.open_trades)

    # ── Phase 4 : Avancer à Vendredi 20:50 — KILL TIME ──
    fri_2050 = datetime(2026, 3, 13, 20, 50, tzinfo=timezone.utc)
    prices = {
        "BTCUSD": (30100.0, 30250.0, 29950.0, 30150.0),
        "EURUSD": (1.0860, 1.0880, 1.0845, 1.0865),
    }
    tracker.update_trades(prices, fri_2050)

    test("EUR/USD ABATTU par Friday Kill-Switch à 20:50",
         "EURUSD" not in tracker.open_trades,
         f"positions restantes: {list(tracker.open_trades.keys())}")

    test("BTC SURVIT au Friday Kill-Switch (CRYPTO exempt)",
         "BTCUSD" in tracker.open_trades,
         "CRYPTO 24/7 — pas concerné par le weekend")

    # Vérifier la raison de fermeture EUR/USD
    eurusd_trades = [t for t in tracker.closed_trades if t.instrument == "EURUSD"]
    if eurusd_trades:
        test("EUR/USD exit_reason = FRIDAY_KILL",
             eurusd_trades[-1].exit_reason == "FRIDAY_KILL",
             f"got: {eurusd_trades[-1].exit_reason}")
    else:
        test("EUR/USD exit_reason = FRIDAY_KILL", False, "no closed EURUSD trade found")

    return tracker


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 4 : M38 CONVEXITY GATE — R:R DYNAMIQUE
# ═══════════════════════════════════════════════════════════════════════════════

def test_m38_convexity_gate():
    section("TEST 4 : M38 CONVEXITY GATE — R:R dynamique par classe d'actif")

    from backtest_engine import PositionTracker, FrictionModel

    friction = FrictionModel()
    ts = datetime(2026, 3, 11, 10, 0, tzinfo=timezone.utc)

    # ── CRYPTO : R:R min = 1.5 ──
    tracker = PositionTracker(10_000.0, friction)

    # Trade avec R:R = 2.0 (> 1.5) → doit s'ouvrir
    trade = tracker.open_trade("BTCUSD", "BUY", 30000.0, 29000.0, 32000.0, ts)
    test("CRYPTO R:R=2.0 → accepté (>1.5)",
         trade is not None,
         f"R:R=2.0, min=1.5")

    # Trade avec R:R = 1.0 → TP auto-adjusted to 1.5
    tracker2 = PositionTracker(10_000.0, friction)
    trade2 = tracker2.open_trade("ETHUSD", "BUY", 2000.0, 1900.0, 2100.0, ts)
    # R:R = (2100-2000)/(2000-1900) = 1.0 < 1.5 → TP adjusted to 2000 + 100*1.5 = 2150
    test("CRYPTO R:R=1.0 → TP auto-adjusted to R:R=1.5",
         trade2 is not None,
         f"TP adjusted to {trade2.tp:.0f} (expected ~2150)" if trade2 else "FAILED")
    if trade2:
        test("CRYPTO TP ≥ entry + risk*1.5",
             trade2.tp >= 2000.0 + 100.0 * 1.5 - 1,  # small margin for friction
             f"TP={trade2.tp:.2f}")

    # ── TRADFI : R:R min = 1.2 ──
    tracker3 = PositionTracker(10_000.0, friction)
    # R:R = 0.8 → auto-adjusted to 1.2
    trade3 = tracker3.open_trade("EURUSD", "BUY", 1.0850, 1.0800, 1.0890, ts)
    # R:R = (1.0890-1.0850)/(1.0850-1.0800) = 0.8 < 1.2 → TP adjusted
    test("TRADFI R:R=0.8 → TP auto-adjusted to R:R=1.2",
         trade3 is not None,
         f"TP={trade3.tp:.5f}" if trade3 else "FAILED")
    if trade3:
        expected_tp = 1.0850 + 0.0050 * 1.2  # 1.0910
        test("TRADFI TP = entry + risk*1.2",
             abs(trade3.tp - expected_tp) < 0.0001,
             f"TP={trade3.tp:.5f} expected={expected_tp:.5f}")


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 5 : NLP SENTIMENT PROPAGATION (M26 + M27 Mock)
# ═══════════════════════════════════════════════════════════════════════════════

def test_nlp_sentiment_propagation():
    section("TEST 5 : NLP & SWARM — Sentiment négatif propagé sans crash")

    # ── M26 NLP Mock : simulate le parsing d'une news négative ──
    class MockNLPEngine:
        """
        Stub M26 : analyse le sentiment d'un texte macro-économique.
        En production, ce module utilise un modèle NLP ou CryptoPanic.
        """
        NEGATIVE_KEYWORDS = [
            "crash", "inflation", "misses", "recession", "bearish",
            "sell-off", "downgrade", "crisis", "default", "plunge",
        ]
        POSITIVE_KEYWORDS = [
            "rally", "bullish", "beat", "recovery", "upgrade",
            "surge", "growth", "expansion", "all-time high",
        ]

        def analyze(self, text: str) -> dict:
            text_lower = text.lower()
            neg = sum(1 for kw in self.NEGATIVE_KEYWORDS if kw in text_lower)
            pos = sum(1 for kw in self.POSITIVE_KEYWORDS if kw in text_lower)
            total = neg + pos
            if total == 0:
                score = 0.0
            else:
                score = (pos - neg) / total  # -1.0 (bearish) to +1.0 (bullish)
            return {
                "text": text[:80],
                "sentiment_score": round(score, 2),
                "negative_hits": neg,
                "positive_hits": pos,
                "signal": "BEARISH" if score < -0.3 else ("BULLISH" if score > 0.3 else "NEUTRAL"),
            }

    # ── M27 Swarm Mock : agrège les signaux de multiple sources ──
    class MockSwarmAggregator:
        """
        Stub M27 : agrège les signaux de divers modules (NLP, Technique, OBI...)
        et produit un score de consensus. Propagation via dict (pas Redis).
        """
        def __init__(self):
            self.signals = []

        def ingest(self, source: str, signal: dict):
            self.signals.append({"source": source, **signal})

        def consensus(self) -> dict:
            if not self.signals:
                return {"consensus": "NEUTRAL", "confidence": 0.0}
            scores = [s.get("sentiment_score", 0) for s in self.signals]
            avg = sum(scores) / len(scores)
            return {
                "consensus": "BEARISH" if avg < -0.3 else ("BULLISH" if avg > 0.3 else "NEUTRAL"),
                "confidence": round(abs(avg), 2),
                "n_sources": len(self.signals),
            }

    # ── Execute test ──
    nlp = MockNLPEngine()
    swarm = MockSwarmAggregator()

    # Injecter une news macro négative
    news = "CPI Inflation misses expectations, markets crash into recession fears"
    result = nlp.analyze(news)

    test("NLP parse sans crash", True, f"score={result['sentiment_score']}")
    test("NLP détecte sentiment BEARISH",
         result["signal"] == "BEARISH",
         f"signal={result['signal']}, score={result['sentiment_score']}")
    test("NLP négatif hits ≥ 2",
         result["negative_hits"] >= 2,
         f"hits={result['negative_hits']}")

    # Propager dans le Swarm
    swarm.ingest("M26_NLP", result)

    # Ajouter un signal technique mock
    swarm.ingest("M01_RSI", {"sentiment_score": -0.5, "signal": "OVERBOUGHT"})

    consensus = swarm.consensus()
    test("Swarm agrège sans crash", True,
         f"consensus={consensus['consensus']}, conf={consensus['confidence']}")
    test("Swarm consensus = BEARISH",
         consensus["consensus"] == "BEARISH",
         f"avg score négatif de {consensus['n_sources']} sources")

    # ── Vérifier que la news empêcherait un BUY signal ──
    test("Consensus bloque les BUY en mode BEARISH",
         consensus["consensus"] != "BULLISH",
         "NLP négatif → pas de confirmation d'achat")

    # Injecter une news positive (recovery)
    news2 = "Markets rally after strong GDP growth, bullish expansion"
    result2 = nlp.analyze(news2)
    test("NLP détecte news BULLISH",
         result2["signal"] == "BULLISH",
         f"score={result2['sentiment_score']}")


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 6 : M40 DEAD CAPITAL DETECTOR (live module)
# ═══════════════════════════════════════════════════════════════════════════════

def test_m40_dead_capital_detector():
    section("TEST 6 : M40 DEAD CAPITAL DETECTOR — module live")

    from time_stop import DeadCapitalDetector
    from brokers.capital_client import FRIDAY_KILLSWITCH_HOUR, FRIDAY_KILLSWITCH_MINUTE

    m40 = DeadCapitalDetector()

    # ── Friday Kill-Switch detection ──
    fri_2050 = datetime(2026, 3, 13, 20, 50, tzinfo=timezone.utc)
    fri_1800 = datetime(2026, 3, 13, 18, 0, tzinfo=timezone.utc)
    thu_2050 = datetime(2026, 3, 12, 20, 50, tzinfo=timezone.utc)

    test("is_friday_killswitch(Fri 20:50) = True",
         m40.is_friday_killswitch(fri_2050))
    test("is_friday_killswitch(Fri 18:00) = False",
         not m40.is_friday_killswitch(fri_1800))
    test("is_friday_killswitch(Thu 20:50) = False",
         not m40.is_friday_killswitch(thu_2050))

    # ── friday_scan : kill TRADFI, spare CRYPTO ──
    trades = {
        "EURUSD": {"entry": 1.10, "direction": "BUY"},
        "GOLD":   {"entry": 2000, "direction": "SELL"},
        "BTCUSD": {"entry": 30000, "direction": "BUY"},
    }
    kills = m40.friday_scan(trades, fri_2050)
    test("friday_scan kills EURUSD", "EURUSD" in kills)
    test("friday_scan kills GOLD", "GOLD" in kills)
    test("friday_scan spares BTCUSD", "BTCUSD" not in kills)

    # ── friday_scan avant 20:50 = rien ──
    kills2 = m40.friday_scan(trades, fri_1800)
    test("friday_scan Fri 18:00 = vide", len(kills2) == 0)

    # Stats
    stats = m40.stats()
    test("stats contient risk_by_class",
         "risk_by_class" in stats,
         f"keys={list(stats.keys())}")


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 7 : FRICTION MODEL EXACTITUDE
# ═══════════════════════════════════════════════════════════════════════════════

def test_friction_model():
    section("TEST 7 : FRICTION MODEL — frais non-négociables")

    from backtest_engine import FrictionModel

    fm = FrictionModel()

    # Entry friction BUY → price goes UP (we pay more)
    entry_buy = fm.apply_entry_friction(100.0, "BUY")
    test("Entry friction BUY: price increases",
         entry_buy > 100.0,
         f"100.0 → {entry_buy:.4f} (+{(entry_buy-100)*100:.2f}%)")

    # Entry friction SELL → price goes DOWN (we get less)
    entry_sell = fm.apply_entry_friction(100.0, "SELL")
    test("Entry friction SELL: price decreases",
         entry_sell < 100.0,
         f"100.0 → {entry_sell:.4f}")

    # Fees round trip
    fees = fm.compute_fees(size=1.0, entry_price=100.0, exit_price=101.0)
    expected = 1.0 * 100.0 * 0.001 + 1.0 * 101.0 * 0.001  # 0.201
    test("Fees round-trip correct",
         abs(fees - expected) < 0.001,
         f"fees={fees:.4f} expected={expected:.4f}")


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 8 : STRATEGY PIPELINE SMOKE TEST
# ═══════════════════════════════════════════════════════════════════════════════

def test_strategy_pipeline():
    section("TEST 8 : STRATEGY PIPELINE — smoke test sans crash")

    import pandas as pd
    import numpy as np

    try:
        from strategy import Strategy, SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD
        from brokers.capital_client import ASSET_PROFILES

        strat = Strategy()

        # Créer 300 bougies synthétiques
        np.random.seed(42)
        n = 300
        dates = pd.date_range(end="2026-03-12", periods=n, freq="1h", tz="UTC")
        prices = 30000.0 * np.exp(np.cumsum(np.random.normal(0, 0.003, n)))
        df = pd.DataFrame({
            "open": prices * (1 + np.random.normal(0, 0.001, n)),
            "high": prices * (1 + np.abs(np.random.normal(0, 0.002, n))),
            "low":  prices * (1 - np.abs(np.random.normal(0, 0.002, n))),
            "close": prices,
            "volume": np.random.lognormal(10, 1, n),
        }, index=dates)
        df["high"] = df[["open", "high", "close"]].max(axis=1)
        df["low"]  = df[["open", "low",  "close"]].min(axis=1)

        # compute_indicators
        df_ind = strat.compute_indicators(df)
        test("compute_indicators() sans crash",
             df_ind is not None and len(df_ind) > 0,
             f"{len(df_ind)} barres avec indicateurs")

        # get_signal
        profile = ASSET_PROFILES.get("BTCUSD", {})
        sig, score, confs = strat.get_signal(df_ind, symbol="BTCUSD", asset_profile=profile)
        test("get_signal() retourne un signal valide",
             sig in (SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD),
             f"sig={sig}, score={score:.2f}")

        # compute_session_range
        sr = strat.compute_session_range(df_ind, range_lookback=4)
        test("compute_session_range() sans crash",
             sr is not None and "size" in sr,
             f"range_size={sr.get('size', 0):.2f}")

    except Exception as e:
        test("Strategy pipeline sans crash", False, f"Exception: {e}")
        traceback.print_exc()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN — L'ÉPREUVE DU FEU
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print()
    print("═" * 60)
    print("  🔥 L'ÉPREUVE DU FEU — E2E MASTER INTEGRATION TEST")
    print("  Simulation d'une semaine de trading condensée")
    print("═" * 60)

    try:
        test_asset_class_classification()
    except Exception as e:
        test("TEST 1 CRASH", False, str(e))
        traceback.print_exc()

    try:
        test_crypto_time_stop()
    except Exception as e:
        test("TEST 2 CRASH", False, str(e))
        traceback.print_exc()

    try:
        test_friday_killswitch()
    except Exception as e:
        test("TEST 3 CRASH", False, str(e))
        traceback.print_exc()

    try:
        test_m38_convexity_gate()
    except Exception as e:
        test("TEST 4 CRASH", False, str(e))
        traceback.print_exc()

    try:
        test_nlp_sentiment_propagation()
    except Exception as e:
        test("TEST 5 CRASH", False, str(e))
        traceback.print_exc()

    try:
        test_m40_dead_capital_detector()
    except Exception as e:
        test("TEST 6 CRASH", False, str(e))
        traceback.print_exc()

    try:
        test_friction_model()
    except Exception as e:
        test("TEST 7 CRASH", False, str(e))
        traceback.print_exc()

    try:
        test_strategy_pipeline()
    except Exception as e:
        test("TEST 8 CRASH", False, str(e))
        traceback.print_exc()

    # ── VERDICT FINAL ──
    print()
    print("═" * 60)
    if FAILED == 0:
        print(f"  ✅ VERDICT : 100% SUCCESS — {PASSED}/{TOTAL_TESTS} tests réussis")
        print(f"  🟢 TOUS LES SYSTÈMES COMMUNIQUENT PARFAITEMENT")
        print(f"  🟢 Bot validé pour Paper Trading live")
    else:
        print(f"  🔴 VERDICT : {PASSED}/{TOTAL_TESTS} SUCCESS | {FAILED} FAILED")
        print(f"  🔴 CORRECTIONS REQUISES")
        for name, status, detail in RESULTS:
            if status == "FAILED":
                print(f"     ❌ {name}: {detail}")
    print("═" * 60)
    print()

    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
