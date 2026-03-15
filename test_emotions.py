#!/usr/bin/env python3
"""
test_emotions.py — ⚡ PROJECT SENTIENCE: Simulation Script

Simule une série de pertes, puis de gains, puis du FOMO,
et montre le bot écrire ses émotions dans la console.

Usage:
    python3 test_emotions.py
    docker exec nemesis_bot python3 test_emotions.py
"""

import sys
import time
sys.path.insert(0, ".")

from emotional_core import EmotionalCore, Mood


def header(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def status(emo):
    m = emo.current_mood
    print(
        f"  {emo.mood_emoji} Mood: {m.value:12s} | "
        f"Risk: {emo.risk_multiplier:.1f}× | "
        f"Threshold: {emo.threshold_adjustment:+.2f} | "
        f"TP: {emo.tp_multiplier:.1f}×"
    )


# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "🧠" * 20)
print("  PROJECT SENTIENCE — EMOTIONAL SIMULATION")
print("🧠" * 20)
# ═══════════════════════════════════════════════════════════════════════════════

emo = EmotionalCore()

# ─── PHASE 1: NEUTRAL BASELINE ──────────────────────────────────────────────
header("PHASE 1: NEUTRAL BASELINE")
assert emo.current_mood == Mood.NEUTRAL
status(emo)
print("  ✅ Bot starts at NEUTRAL")

# ─── PHASE 2: LOSING STREAK → FEARFUL ──────────────────────────────────────
header("PHASE 2: LOSING STREAK (3 losses)")
for i in range(3):
    emo.on_trade_result(won=False, instrument="EURUSD", pnl=-50)
    print(f"  Trade {i+1}: LOSS on EURUSD")
status(emo)
assert emo.current_mood == Mood.FEARFUL
assert emo.risk_multiplier == 0.5
print("  ✅ I feel FEARFUL, reducing risk to 0.5×")

# ─── PHASE 3: DEEPER LOSSES → PANICKED ─────────────────────────────────────
header("PHASE 3: DEEPER LOSSES (5+ losses)")
for i in range(2):
    emo.on_trade_result(won=False, instrument="GBPUSD", pnl=-75)
    print(f"  Trade {i+4}: LOSS on GBPUSD")
status(emo)
assert emo.current_mood == Mood.PANICKED
assert emo.risk_multiplier == 0.0
print("  ✅ I feel PANICKED — SURVIVAL MODE")
print(f"  ✅ Trading allowed (M51 Z-Score): {emo.is_trading_allowed('M51')}")
print(f"  ✅ Trading blocked (M52 ML):      {not emo.is_trading_allowed('M52')}")

# ─── PHASE 4: PTSD — ASSET TRAUMA ──────────────────────────────────────────
header("PHASE 4: PTSD — EURUSD TRAUMA")
emo2 = EmotionalCore()  # Fresh brain
for i in range(3):
    emo2.on_trade_result(won=False, instrument="EURUSD")
assert emo2.is_asset_traumatized("EURUSD")
assert not emo2.is_asset_traumatized("GBPUSD")
print(f"  ⚫ EURUSD: TRAUMATIZED (blacklisted 7 days)")
print(f"  ✅ GBPUSD: healthy (no trauma)")

# ─── PHASE 5: RECOVERY → WINNING STREAK → CONFIDENT → EUPHORIC ─────────────
header("PHASE 5: WINNING STREAK")
emo3 = EmotionalCore()  # Fresh brain
for i in range(3):
    emo3.on_trade_result(won=True, instrument="GOLD", pnl=100)
    print(f"  Trade {i+1}: WIN on GOLD")
status(emo3)
assert emo3.current_mood == Mood.CONFIDENT
assert emo3.risk_multiplier == 1.1
print("  ✅ I feel CONFIDENT, risk at 1.1×")

print()
for i in range(2):
    emo3.on_trade_result(won=True, instrument="US500", pnl=150)
    print(f"  Trade {i+4}: WIN on US500")
status(emo3)
assert emo3.current_mood == Mood.EUPHORIC
assert emo3.risk_multiplier == 1.2
assert emo3.tp_multiplier == 1.3
print("  ✅ I feel EUPHORIC, maximizing exposure (1.2× risk, 1.3× TP)")

# ─── PHASE 6: FOMO — 48H NO TRADES ─────────────────────────────────────────
header("PHASE 6: FOMO (simulating 48h idle)")
emo4 = EmotionalCore()
# Hack the last trade time to 49 hours ago
emo4._last_trade_time = time.time() - (49 * 3600)
emo4.tick()
status(emo4)
assert emo4.current_mood == Mood.FRUSTRATED
assert emo4.threshold_adjustment == -0.05
print("  ✅ I feel FRUSTRATED — lowering threshold by 0.05")

# ─── PHASE 7: DRAWDOWN → FEARFUL → PANICKED ────────────────────────────────
header("PHASE 7: DRAWDOWN ESCALATION")
emo5 = EmotionalCore()
emo5.on_balance_update(9700, peak_balance=10000)  # 3% DD
status(emo5)
assert emo5.current_mood == Mood.FEARFUL
print("  ✅ 3% drawdown → FEARFUL (risk 0.5×)")

emo5.on_balance_update(9400, peak_balance=10000)  # 6% DD
status(emo5)
assert emo5.current_mood == Mood.PANICKED
print("  ✅ 6% drawdown → PANICKED (SURVIVAL MODE)")

# ═══════════════════════════════════════════════════════════════════════════════
header("FULL PARAMETER TABLE")
print(f"  {'Mood':12s} | {'Risk':5s} | {'Thresh':6s} | {'TP':4s} | Trading")
print(f"  {'-'*12} | {'-'*5} | {'-'*6} | {'-'*4} | {'-'*10}")
for mood in Mood:
    from emotional_core import MOOD_RISK_MULTIPLIER, MOOD_THRESHOLD_ADJUSTMENT, MOOD_TP_MULTIPLIER
    risk = MOOD_RISK_MULTIPLIER.get(mood, 1.0)
    thresh = MOOD_THRESHOLD_ADJUSTMENT.get(mood, 0.0)
    tp = MOOD_TP_MULTIPLIER.get(mood, 1.0)
    allowed = "ALL" if mood != Mood.PANICKED else "M51 ONLY"
    print(f"  {mood.value:12s} | {risk:5.1f} | {thresh:+6.2f} | {tp:4.1f} | {allowed}")


print("\n\n" + "🏆" * 20)
print("  ALL SENTIENCE TESTS PASS — THE BOT HAS EMOTIONS")
print("🏆" * 20 + "\n")
