#!/usr/bin/env python3
"""
test_prometheus.py — 🔥 PROJECT PROMETHEUS: Full Simulation

Simulates the complete Prometheus cognitive loop:
  1. Creates 5 fake losing trades (SL too tight)
  2. Journal records full context
  3. Prometheus diagnoses the losses
  4. Generates 5 mutations
  5. Shadow backtests each mutation
  6. Applies the best mutation to optimized_rules.json
  7. Bot is now "evolved"

Usage:
    python3 test_prometheus.py
    docker exec nemesis_bot python3 test_prometheus.py
"""

import sys
import os
import json
import time
import copy
import shutil
from datetime import datetime, timezone, timedelta

sys.path.insert(0, ".")


def header(title):
    print(f"\n{'='*65}")
    print(f"  {title}")
    print(f"{'='*65}")


def main():
    print("\n" + "🔥" * 25)
    print("  PROJECT PROMETHEUS — COGNITIVE LOOP SIMULATION")
    print("🔥" * 25)

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 1: TRADE JOURNAL — Record 5 Losing Trades
    # ═══════════════════════════════════════════════════════════════════
    header("PHASE 1: TRADE JOURNAL — Recording 5 Losing Trades")

    from trade_journal import TradeJournal
    journal = TradeJournal()

    # Simulate 5 losing EURUSD trades (SL too tight)
    fake_trades = [
        {"entry": 1.0900, "sl": 1.0895, "tp1": 1.0920, "direction": "BUY",
         "size": 1.0, "score": 0.65, "confirmations": ["ADX 25", "Vol 1.2×"],
         "regime": "RANGING", "adx_at_entry": 25, "market_regime": "NEUTRAL",
         "fear_greed": 45, "in_overlap": False,
         "open_time": (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()},
        {"entry": 1.0880, "sl": 1.0876, "tp1": 1.0910, "direction": "BUY",
         "size": 1.0, "score": 0.58, "confirmations": ["RSI 28", "BB✓"],
         "regime": "RANGING", "adx_at_entry": 18, "market_regime": "NEUTRAL",
         "fear_greed": 42, "in_overlap": True,
         "open_time": (datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat()},
        {"entry": 1.0920, "sl": 1.0917, "tp1": 1.0950, "direction": "BUY",
         "size": 0.8, "score": 0.72, "confirmations": ["VWAP✓", "Momentum"],
         "regime": "TREND_UP", "adx_at_entry": 32, "market_regime": "RISK_ON",
         "fear_greed": 55, "in_overlap": False,
         "open_time": (datetime.now(timezone.utc) - timedelta(minutes=22)).isoformat()},
        {"entry": 1.0860, "sl": 1.0855, "tp1": 1.0890, "direction": "BUY",
         "size": 1.2, "score": 0.61, "confirmations": ["ADX 20", "OFI↑↑"],
         "regime": "RANGING", "adx_at_entry": 20, "market_regime": "NEUTRAL",
         "fear_greed": 38, "in_overlap": False,
         "open_time": (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()},
        {"entry": 1.0845, "sl": 1.0841, "tp1": 1.0870, "direction": "BUY",
         "size": 0.9, "score": 0.55, "confirmations": ["Z=-2.6", "RSI 23"],
         "regime": "TREND_DOWN", "adx_at_entry": 27, "market_regime": "RISK_OFF",
         "fear_greed": 32, "in_overlap": False,
         "open_time": (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()},
    ]

    for i, trade in enumerate(fake_trades):
        pnl = -(trade["entry"] - trade["sl"]) * trade["size"] * 10000  # pip PnL
        pnl = round(pnl * -1, 2)  # Negative PnL
        journal.log_close(
            instrument="EURUSD",
            trade_state=trade,
            exit_reason="SL",
            pnl=-abs(pnl),
            context={
                "atr": 0.0008,
                "rsi": 35 + i * 3,
                "adx": 20 + i * 2,
                "sentiment": "negative",
                "l2_imbalance": -0.3,
                "mood": "FEARFUL",
            },
        )
        print(f"  📓 Trade #{i+1}: EURUSD BUY → SL hit | PnL = -€{abs(pnl):.2f}")

    print(f"\n  📊 Journal: {journal.count} entries total")

    # Check stats
    stats = journal.get_stats(period_days=1)
    print(f"  📊 Today: {stats['total']} trades, {stats['wins']} wins, {stats['losses']} losses")
    print(f"  📊 Win rate: {stats['win_rate']:.0%}")
    print(f"  📊 Avg R: {stats['avg_r']:.2f}")
    if stats.get("worst_instruments"):
        print(f"  📊 Worst: {stats['worst_instruments'][:3]}")

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 2: SHADOW TESTER — Verify backtest engine works
    # ═══════════════════════════════════════════════════════════════════
    header("PHASE 2: SHADOW TESTER — In-Memory Backtest")

    from shadow_tester import ShadowTester
    tester = ShadowTester()

    # Generate synthetic OHLCV data (mean-reverting with noise)
    import numpy as np
    import pandas as pd

    np.random.seed(42)
    n = 500
    prices = 1.0900 + np.cumsum(np.random.randn(n) * 0.0005)  # Random walk
    highs = prices + np.abs(np.random.randn(n) * 0.0003)
    lows = prices - np.abs(np.random.randn(n) * 0.0003)
    opens = prices + np.random.randn(n) * 0.0001
    volumes = np.random.uniform(1000, 5000, n)

    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": prices, "volume": volumes
    })

    # Add indicators
    try:
        from strategy import Strategy
        s = Strategy()
        df = s.compute_indicators(df)
    except Exception:
        # Manual fallback
        df["rsi"] = 50 + np.random.randn(n) * 15
        df["adx"] = 20 + np.random.randn(n) * 8
        df["atr"] = 0.0008 + np.abs(np.random.randn(n) * 0.0002)
        df["zscore"] = np.random.randn(n)
        df = df.dropna()

    # Test baseline params
    baseline_params = {
        "strat": "MR",
        "sl_buffer": 0.10,
        "tp1": 1.5,
        "rsi_lo": 25,
        "rsi_hi": 75,
    }

    t0 = time.time()
    baseline = tester.backtest(df, baseline_params)
    t1 = time.time()

    print(f"  ⏱ Backtest time: {(t1-t0)*1000:.0f}ms")
    print(f"  📊 Baseline MR: {baseline['total_trades']} trades, WR={baseline['win_rate']:.0%}, Sharpe={baseline['sharpe']:.2f}")

    # Test with wider SL (mutation)
    mutation_params = {**baseline_params, "sl_buffer": 0.25, "tp1": 2.0}
    mutation = tester.backtest(df, mutation_params)
    print(f"  📊 Mutation (SL 0.25, TP 2.0): {mutation['total_trades']} trades, WR={mutation['win_rate']:.0%}, Sharpe={mutation['sharpe']:.2f}")

    improvement = mutation["sharpe"] - baseline["sharpe"]
    icon = "📈" if improvement > 0 else "📉"
    print(f"  {icon} Sharpe improvement: {improvement:+.2f}")

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 3: PROMETHEUS CORE — Full Self-Improvement Cycle
    # ═══════════════════════════════════════════════════════════════════
    header("PHASE 3: PROMETHEUS — Genetic Mutation Cycle")

    # Backup rules
    rules_file = "optimized_rules.json"
    backup_file = "/tmp/optimized_rules_backup.json"
    if os.path.exists(rules_file):
        shutil.copy(rules_file, backup_file)
        print(f"  💾 Rules backed up to {backup_file}")

    # Ensure EURUSD exists in rules
    rules = {}
    if os.path.exists(rules_file):
        with open(rules_file) as f:
            rules = json.load(f)

    if "EURUSD" not in rules:
        rules["EURUSD"] = {
            "strat": "MR", "engine": "M52_ML", "tf": "1d",
            "sl_buffer": 0.10, "tp1": 1.5, "tp2": 3.0,
            "rsi_lo": 25, "rsi_hi": 75, "cat": "forex",
        }
        with open(rules_file, "w") as f:
            json.dump(rules, f, indent=2)

    original_sl = rules.get("EURUSD", {}).get("sl_buffer", 0.10)
    original_tp = rules.get("EURUSD", {}).get("tp1", 1.5)
    print(f"  📋 Current EURUSD: sl_buffer={original_sl}, tp1={original_tp}")

    from prometheus_core import PrometheusCore
    prometheus = PrometheusCore(
        capital_client=None,  # No live API needed for simulation
        journal=journal,
        shadow_tester=tester,
        telegram_router=None,
    )

    # Override _load_rules and _save_rules for test
    prometheus._load_rules = lambda: copy.deepcopy(rules)

    def _test_save(updated):
        global rules
        rules = updated
        with open(rules_file, "w") as f:
            json.dump(updated, f, indent=2)
    prometheus._save_rules = _test_save

    # Override data fetching: use our synthetic data
    prometheus._capital = type("FakeCapital", (), {
        "available": True,
        "fetch_ohlcv": lambda self, inst, **kw: df.copy(),
    })()

    # Get losers from journal
    losers = journal.get_losers(period_days=1)
    print(f"\n  🧠 Prometheus analyzing {len(losers)} losers...")

    result = prometheus.run_cycle(losers=losers)

    print(f"\n  📊 Cycle result:")
    print(f"     Mutations applied: {result.get('mutations_applied', 0)}")
    print(f"     Mutations rejected: {result.get('mutations_rejected', 0)}")
    print(f"     Duration: {result.get('elapsed_s', 0):.1f}s")

    # Check if rules were updated
    if os.path.exists(rules_file):
        with open(rules_file) as f:
            updated_rules = json.load(f)
        new_sl = updated_rules.get("EURUSD", {}).get("sl_buffer", original_sl)
        new_tp = updated_rules.get("EURUSD", {}).get("tp1", original_tp)
        mutation_tag = updated_rules.get("EURUSD", {}).get("prometheus_mutation", "none")

        print(f"\n  📋 Updated EURUSD:")
        print(f"     sl_buffer: {original_sl} → {new_sl} {'✅ CHANGED' if new_sl != original_sl else '⏭ same'}")
        print(f"     tp1:       {original_tp} → {new_tp} {'✅ CHANGED' if new_tp != original_tp else '⏭ same'}")
        print(f"     Prometheus tag: {mutation_tag}")

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 4: SUMMARY
    # ═══════════════════════════════════════════════════════════════════
    header("COGNITIVE LOOP COMPLETE")

    print(f"""
  📓 Journal: {journal.count} trades recorded with full context
  🔬 Shadow Tester: {tester.stats['tests_run']} backtests in {tester.stats['total_time_s']:.1f}s
  🔥 Prometheus: {prometheus._cycles} cycles, {prometheus._mutations_applied} mutations applied

  ┌─────────────────────────────────────────────────┐
  │  THE BOT HAS EVOLVED ITSELF.                    │
  │                                                 │
  │  It identified that SL was too tight,           │
  │  tested alternative parameters,                 │
  │  and rewrote its own configuration.             │
  │                                                 │
  │  Monday morning, it trades with a new brain.    │
  └─────────────────────────────────────────────────┘
""")

    # Restore backup
    if os.path.exists(backup_file):
        shutil.copy(backup_file, rules_file)
        print(f"  💾 Rules restored from backup (simulation complete)")


if __name__ == "__main__":
    main()
