#!/usr/bin/env python3
"""
silence_diagnostic.py — ⚡ THE SILENCE AUDIT

Force le calcul de signal sur les 40 actifs GOD MODE à l'instant T.
Trace CHAQUE kill-switch et affiche exactement OÙ la chaîne se brise.

Usage:
    docker exec nemesis_bot python3 silence_diagnostic.py
    docker exec nemesis_bot python3 silence_diagnostic.py EURUSD   # 1 actif détaillé
"""

import sys
import os
import json
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

sys.path.insert(0, ".")

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="DEBUG")  # Ultra-verbose


def header(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def load_god_mode_rules():
    rules = {}
    for f in ['optimized_rules.json', 'black_ops_rules.json', 'lazarus_rules.json']:
        if os.path.exists(f):
            with open(f) as fh:
                data = json.load(fh)
                rules.update(data)
    return rules


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN DIAGNOSTIC
# ═══════════════════════════════════════════════════════════════════════════════

def run_diagnostic(focus_instrument=None):
    print("\n" + "🔍" * 25)
    print("  THE SILENCE AUDIT — ZERO TRADES DIAGNOSTIC")
    print("🔍" * 25)

    now = datetime.now(timezone.utc)
    print(f"\n  📅 Time: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"  📅 Weekday: {now.strftime('%A')} | Hour: {now.hour}h UTC")

    # ─── T1: DATA FEED AUDIT ─────────────────────────────────────────────
    header("T1: DATA FEED AUDIT")

    try:
        from brokers.capital_client import CapitalClient
        capital = CapitalClient()
        print(f"  🔌 Capital.com API: {'✅ CONNECTED' if capital.available else '❌ DISCONNECTED'}")
        if capital.available:
            bal = capital.get_balance()
            print(f"  💰 Balance: {bal:,.2f}€")
        else:
            print("  ❌ CRITICAL: Broker not connected → NO DATA → NO TRADES")
            print("  💡 FIX: Check CAPITAL_API_KEY, CAPITAL_EMAIL, CAPITAL_PASSWORD in .env")
            return
    except Exception as e:
        print(f"  ❌ CapitalClient import failed: {e}")
        capital = None

    # ─── T2: GOD MODE & HARD BAN AUDIT ───────────────────────────────────
    header("T2: GOD MODE & HARD BAN AUDIT")

    rules = load_god_mode_rules()
    print(f"  📋 GOD_MODE_RULES: {len(rules)} instruments loaded")

    try:
        from god_mode import HARD_BAN
        print(f"  🚫 HARD_BAN: {len(HARD_BAN)} instruments banned: {sorted(HARD_BAN)}")
    except ImportError:
        HARD_BAN = set()
        print(f"  ⚠️  HARD_BAN: module not loaded (no bans)")

    active = [i for i in rules if i not in HARD_BAN]
    print(f"  ✅ Active instruments: {len(active)}")

    if len(active) == 0:
        print("  ❌ CRITICAL: ALL instruments are banned! Check HARD_BAN")
        return

    # Load CAPITAL_INSTRUMENTS
    try:
        from config import CAPITAL_INSTRUMENTS, MAX_OPEN_TRADES
        print(f"  📊 CAPITAL_INSTRUMENTS: {len(CAPITAL_INSTRUMENTS)}")
        print(f"  🔒 MAX_OPEN_TRADES: {MAX_OPEN_TRADES}")
    except Exception as e:
        print(f"  ⚠️  Config: {e}")
        CAPITAL_INSTRUMENTS = list(rules.keys())
        MAX_OPEN_TRADES = 10

    # ─── TIMEFRAME ANALYSIS ──────────────────────────────────────────────
    header("T4: TIMEFRAME & PROBABILITY ANALYSIS")

    tf_counts = defaultdict(list)
    for inst, cfg in rules.items():
        if inst in HARD_BAN:
            continue
        tf = cfg.get("tf", "1h")
        tf_counts[tf].append(inst)

    print(f"\n  Timeframe Distribution:")
    for tf, instruments in sorted(tf_counts.items()):
        pct = len(instruments) / len(active) * 100
        candle_hours = {"5m": 0.083, "15m": 0.25, "1h": 1, "4h": 4, "1d": 24, "1wk": 168}.get(tf, 1)
        trades_per_24h = 24 / candle_hours
        print(f"    {tf:6s}: {len(instruments):2d} instruments ({pct:.0f}%) | {trades_per_24h:.0f} candles/day")
        print(f"            {instruments[:5]}{'...' if len(instruments) > 5 else ''}")

    # Probability of a trade in first 2 hours
    print(f"\n  📐 Probability Analysis:")
    total_checks_per_2h = 0
    for tf, instruments in tf_counts.items():
        candle_hours = {"5m": 0.083, "15m": 0.25, "1h": 1, "4h": 4, "1d": 24, "1wk": 168}.get(tf, 1)
        candles_in_2h = max(1, 2 / candle_hours)
        total_checks_per_2h += len(instruments) * candles_in_2h
        for inst in instruments:
            cfg = rules[inst]
            win_rate = cfg.get("win_rate", 50)
            trades_hist = cfg.get("trades", 10)

    # Rough estimate: most strategies signal ~10% of candles
    signal_rate = 0.10
    expected_signals_2h = total_checks_per_2h * signal_rate
    print(f"    Total candle checks in 2h: ~{total_checks_per_2h:.0f}")
    print(f"    Estimated signal rate: ~{signal_rate:.0%}")
    print(f"    Expected raw signals in 2h: ~{expected_signals_2h:.1f}")
    print(f"    After 15+ kill-switches: ~{expected_signals_2h * 0.05:.2f} trades")
    print(f"    ⚠️  With 33% on 1D timeframe, those instruments only generate 1 candle/day!")
    print(f"    ⚠️  With R:R ≥ 3.0 (M38 Convexity), most signals are rejected.")

    # ─── T3: KILL-SWITCH-BY-KILL-SWITCH TRACE ────────────────────────────
    header("T3: INSTRUMENT-BY-INSTRUMENT SIGNAL AUDIT")

    if focus_instrument:
        instruments_to_check = [focus_instrument]
    else:
        instruments_to_check = active[:40]

    # Load strategy
    try:
        from strategy import Strategy
        strategy = Strategy()
    except Exception as e:
        print(f"  ❌ Strategy load failed: {e}")
        return

    # Load other modules
    try:
        from config import ASSET_PROFILES
    except ImportError:
        ASSET_PROFILES = {}

    try:
        from ohlcv_cache import OHLCVCache
        cache = OHLCVCache(capital_client=capital)
    except Exception as e:
        cache = None
        print(f"  ⚠️  OHLCVCache: {e}")

    # Kill-switch counters
    kills = defaultdict(int)  # kill_reason → count
    signals_generated = 0
    holds = 0

    for instrument in instruments_to_check:
        profile = ASSET_PROFILES.get(instrument, rules.get(instrument, {}))
        tf = profile.get("tf", "1h")
        strat = profile.get("strat", profile.get("engine", "BK"))
        verbose = (focus_instrument is not None) or len(instruments_to_check) <= 5

        if verbose:
            print(f"\n  {'─'*60}")
            print(f"  📊 {instrument} | Strategy: {strat} | TF: {tf}")
            print(f"  {'─'*60}")

        # KS1: HARD_BAN
        if instrument in HARD_BAN:
            kills["KS01_HARD_BAN"] += 1
            if verbose: print(f"    ❌ KS01: HARD_BAN → rejected")
            continue

        # KS2: Session check
        try:
            _cat = profile.get("cat", "forex")
            session_ok = strategy.is_session_ok_for(instrument, _cat)
            if not session_ok:
                kills["KS02_SESSION_CLOSED"] += 1
                if verbose: print(f"    ❌ KS02: Session closed for {_cat} → rejected")
                continue
            elif verbose:
                print(f"    ✅ KS02: Session OK ({_cat})")
        except Exception as e:
            if verbose: print(f"    ⚠️  KS02: Session check error: {e}")

        # KS3: OHLCV data
        df = None
        try:
            if cache:
                df = cache.get(instrument, strategy=strategy)
            if df is None or len(df) < 50:
                # Try direct fetch
                if capital and capital.available:
                    _count = {"5m": 300, "15m": 250, "1h": 200, "4h": 200, "1d": 200, "1wk": 100}.get(tf, 200)
                    df = capital.fetch_ohlcv(instrument, timeframe=tf, count=_count)
                    if df is not None and len(df) >= 50:
                        df = strategy.compute_indicators(df)
        except Exception as e:
            if verbose: print(f"    ⚠️  KS03: Data fetch error: {e}")

        if df is None or len(df) < 50:
            kills["KS03_NO_DATA"] += 1
            if verbose: print(f"    ❌ KS03: No OHLCV data ({len(df) if df is not None else 'None'} candles) → rejected")
            continue
        elif verbose:
            print(f"    ✅ KS03: OHLCV data OK ({len(df)} candles, TF={tf})")

        # Ensure indicators
        if "atr" not in df.columns:
            df = strategy.compute_indicators(df)

        # KS4: Signal generation
        try:
            sig, score, confirmations = strategy.get_signal(df, symbol=instrument, asset_profile=profile)
        except Exception as e:
            kills["KS04_SIGNAL_ERROR"] += 1
            if verbose: print(f"    ❌ KS04: Signal generation ERROR: {e}")
            continue

        if sig == "HOLD":
            kills["KS04_HOLD"] += 1
            holds += 1
            if verbose: print(f"    📊 KS04: HOLD | score={score:.3f} | {confirmations[:3] if confirmations else '∅'}")
            continue

        signals_generated += 1
        if verbose:
            print(f"    ⚡ KS04: SIGNAL = {sig} | score={score:.3f} | {confirmations[:3]}")

        # KS5: BK Retest gate (score < 0.60)
        if strat == "BK" and score < 0.60:
            kills["KS05_BK_RETEST_WAIT"] += 1
            if verbose: print(f"    ⏳ KS05: BK Retest wait (score {score:.2f} < 0.60) → pending, not executed")
            continue

        # KS6: Convexity R:R gate
        entry = float(df.iloc[-1]["close"])
        atr_val = strategy.get_atr(df)
        sl_buf = profile.get("sl_buffer", 0.10)

        if strat in ("MR", "TF"):
            sl_dist = atr_val * sl_buf if atr_val > 0 else 0
            if sl_dist <= 0:
                kills["KS06_SL_ZERO"] += 1
                if verbose: print(f"    ❌ KS06: SL distance = 0 → rejected")
                continue
            if sig == "BUY":
                sl = entry - sl_dist
                tp1 = entry + sl_dist * profile.get("tp1", 1.5)
            else:
                sl = entry + sl_dist
                tp1 = entry - sl_dist * profile.get("tp1", 1.5)
        else:
            sr = strategy.compute_session_range(df)
            rng = sr["size"]
            if rng <= 0 or sr["pct"] < 0.08:
                kills["KS06_RANGE_TOO_SMALL"] += 1
                if verbose:
                    print(f"    ❌ KS06: Range too small (size={rng:.5f}, pct={sr['pct']:.3f} < 0.08) → rejected")
                continue
            if sig == "BUY":
                sl = sr["low"] - rng * sl_buf
                tp1 = entry + rng * profile.get("tp1", 1.5)
            else:
                sl = sr["high"] + rng * sl_buf
                tp1 = entry - rng * profile.get("tp1", 1.5)

        # Check R:R
        tp_dist = abs(tp1 - entry)
        sl_dist_real = abs(sl - entry)
        rr = tp_dist / sl_dist_real if sl_dist_real > 0 else 0

        try:
            from convexity_gate import ConvexityGate
            cvx = ConvexityGate()
            rr_valid, actual_rr = cvx.validate_rr(entry, sl, tp1, instrument)
        except Exception:
            rr_valid = rr >= 3.0
            actual_rr = rr

        if not rr_valid:
            kills["KS07_CONVEXITY_RR"] += 1
            if verbose:
                print(f"    ❌ KS07: Convexity R:R = {actual_rr:.2f} < 3.0 → REJECTED")
                print(f"             Entry={entry:.5f} SL={sl:.5f} TP={tp1:.5f}")
            continue
        elif verbose:
            print(f"    ✅ KS07: R:R = {actual_rr:.2f} ≥ 3.0 OK")

        # KS8: Spread check
        if capital and capital.available:
            try:
                px = capital.get_current_price(instrument)
                if px:
                    spread = abs(px.get("ask", 0) - px.get("bid", 0))
                    tp_d = abs(tp1 - entry)
                    spread_ratio = spread / tp_d if tp_d > 0 else 0
                    if spread_ratio > 0.25:
                        kills["KS08_SPREAD_FILTER"] += 1
                        if verbose:
                            print(f"    ❌ KS08: Spread filter — spread={spread:.5f} / TP_dist={tp_d:.5f} = {spread_ratio:.0%} > 25%")
                        continue
                    elif verbose:
                        print(f"    ✅ KS08: Spread OK ({spread_ratio:.0%})")
            except Exception as e:
                if verbose: print(f"    ⚠️  KS08: {e}")

        # If we get here, the signal would have passed so far
        if verbose:
            print(f"    🟢 SIGNAL WOULD PASS (before ML/HMM/MetaAgent/OBGuard filters)")
            print(f"    📐 Entry={entry:.5f} | SL={sl:.5f} | TP1={tp1:.5f} | R:R={actual_rr:.2f}")

    # ─── SUMMARY ─────────────────────────────────────────────────────────
    header("DIAGNOSTIC SUMMARY")

    total = len(instruments_to_check)
    print(f"  📊 Instruments scanned: {total}")
    print(f"  ⚡ Signals generated:   {signals_generated}")
    print(f"  📊 HOLDs:               {holds}")
    print(f"  🚫 Killed by filters:   {total - signals_generated - holds}")
    print()

    if kills:
        print(f"  ─── Kill-Switch Breakdown ───")
        for ks, count in sorted(kills.items(), key=lambda x: -x[1]):
            pct = count / total * 100
            bar = "█" * int(pct / 2)
            label = {
                "KS01_HARD_BAN":        "HARD BAN (permanently banned asset)",
                "KS02_SESSION_CLOSED":  "SESSION CLOSED (market hours filter)",
                "KS03_NO_DATA":         "NO OHLCV DATA (cache empty, API fail)",
                "KS04_HOLD":            "HOLD (no technical signal)",
                "KS04_SIGNAL_ERROR":    "SIGNAL ERROR (strategy crash)",
                "KS05_BK_RETEST_WAIT":  "BK RETEST WAIT (score < 0.60)",
                "KS06_RANGE_TOO_SMALL": "RANGE < 0.08% (BK range too tight)",
                "KS06_SL_ZERO":         "SL DISTANCE = 0 (ATR issue)",
                "KS07_CONVEXITY_RR":    "R:R < 3.0 (M38 Convexity Gate)",
                "KS08_SPREAD_FILTER":   "SPREAD > 25% of TP (S-5 filter)",
            }.get(ks, ks)
            print(f"    {count:3d} ({pct:4.0f}%) {bar:25s} {ks}: {label}")

    # ─── BOTTLENECK DIAGNOSIS ────────────────────────────────────────────
    header("BOTTLENECK DIAGNOSIS")

    top_killer = max(kills.items(), key=lambda x: x[1]) if kills else ("none", 0)

    if top_killer[0] == "KS02_SESSION_CLOSED":
        print("  🎯 #1 BOTTLENECK: SESSION FILTER")
        print("  ─── The market is currently closed for most assets.")
        print(f"  ─── Current time: {now.hour}h UTC ({now.strftime('%A')})")
        print("  ─── Forex trades during London (8-16h UTC) & NY (13-21h UTC)")
        print("  💡 FIX: Wait for market open. This is normal outside trading hours.")

    elif top_killer[0] == "KS04_HOLD":
        print("  🎯 #1 BOTTLENECK: NO SIGNALS GENERATED")
        print("  ─── The strategy generates HOLD for most instruments.")
        print("  ─── This is NORMAL for conservative strategies on 4H/1D timeframes.")
        print("  💡 FIX: Lower thresholds in rules OR add more 1H/15m instruments.")

    elif top_killer[0] == "KS07_CONVEXITY_RR":
        print("  🎯 #1 BOTTLENECK: M38 CONVEXITY GATE (R:R ≥ 3.0)")
        print("  ─── Most signals are killed because Risk:Reward < 3.0")
        print("  ─── This is the MOST LIKELY cause of zero trades!")
        print("  💡 FIX: Lower ConvexityGate minimum R:R from 3.0 to 1.5 or 2.0")

    elif top_killer[0] == "KS05_BK_RETEST_WAIT":
        print("  🎯 #1 BOTTLENECK: BK RETEST WAIT")
        print("  ─── BK signals with score < 0.60 wait for a retest that never comes.")
        print("  💡 FIX: Lower BK retest threshold from 0.60 to 0.40")

    elif top_killer[0] == "KS03_NO_DATA":
        print("  🎯 #1 BOTTLENECK: MISSING DATA")
        print("  ─── Some instruments have no OHLCV data in cache.")
        print("  💡 FIX: Check if Capital.com API is returning data for these instruments")

    elif top_killer[0] == "KS06_RANGE_TOO_SMALL":
        print("  🎯 #1 BOTTLENECK: RANGE TOO SMALL (< 0.08%)")
        print("  ─── BK strategy requires range > 0.08%, but market is too tight.")
        print("  💡 FIX: Lower sr.pct threshold or use MR/TF for low-vol periods")

    elif top_killer[0] == "KS08_SPREAD_FILTER":
        print("  🎯 #1 BOTTLENECK: SPREAD FILTER (S-5)")
        print("  ─── Spread > 25% of TP1 distance — too expensive to trade.")
        print("  💡 FIX: Increase TP targets or trade during peak liquidity hours")

    else:
        print(f"  🎯 Top killer: {top_killer[0]} ({top_killer[1]} instruments)")

    print()


if __name__ == "__main__":
    focus = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        run_diagnostic(focus)
    except Exception as e:
        print(f"\n❌ FATAL: {e}")
        import traceback
        traceback.print_exc()
