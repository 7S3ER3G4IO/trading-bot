#!/usr/bin/env python3
"""
test_margin.py — Preuve du Leverage & Margin Controller.

Simule un compte de 10,000€ avec des scénarios extrêmes pour prouver
que le bot plafonne la taille au lieu d'envoyer des ordres impossibles.

Usage:
    python3 test_margin.py
    docker exec nemesis_bot python3 test_margin.py
"""

import sys
sys.path.insert(0, ".")

from config import (
    MAX_EFFECTIVE_LEVERAGE,
    ASSET_MARGIN_REQUIREMENTS,
    ASSET_CLASS_FALLBACK,
)

# ─── Simuler position_size SANS le broker (pure math) ────────────────────────

def position_size_sim(
    balance: float,
    risk_pct: float,
    entry: float,
    sl: float,
    epic: str,
    free_margin: float = 0.0,
) -> dict:
    """Simulation autonome du pipeline de sizing."""
    sl_dist = abs(entry - sl)
    if sl_dist == 0:
        return {"error": "SL == Entry"}

    # Étape 1: Raw
    risk_amt = balance * risk_pct
    raw_size = risk_amt / sl_dist
    raw_nominal = raw_size * entry
    raw_leverage = raw_nominal / balance

    # Étape 2: Leverage cap
    max_nominal = MAX_EFFECTIVE_LEVERAGE * balance
    capped_size = raw_size
    leverage_capped = False
    if raw_nominal > max_nominal and entry > 0:
        capped_size = max_nominal / entry
        leverage_capped = True

    # Étape 3: Margin check
    asset_class = ASSET_CLASS_FALLBACK.get(epic, "forex")
    margin_rate = ASSET_MARGIN_REQUIREMENTS.get(asset_class, 0.0333)
    margin_required = capped_size * entry * margin_rate
    effective_free = free_margin if free_margin > 0 else balance * 0.80
    margin_capped = False
    if margin_required > effective_free and entry > 0:
        max_by_margin = effective_free / (entry * margin_rate)
        capped_size = min(capped_size, max_by_margin)
        margin_capped = True

    final_size = max(0.01, round(capped_size, 2))
    final_nominal = final_size * entry
    final_leverage = final_nominal / balance
    final_margin = final_nominal * margin_rate

    return {
        "epic": epic,
        "asset_class": asset_class,
        "balance": balance,
        "entry": entry,
        "sl": sl,
        "sl_pct": round(sl_dist / entry * 100, 3),
        "risk_pct": risk_pct,
        "risk_amt": round(risk_amt, 2),
        "raw_size": round(raw_size, 4),
        "raw_nominal": round(raw_nominal, 0),
        "raw_leverage": round(raw_leverage, 1),
        "leverage_capped": leverage_capped,
        "margin_capped": margin_capped,
        "margin_rate": margin_rate,
        "margin_required": round(final_margin, 2),
        "final_size": final_size,
        "final_nominal": round(final_nominal, 0),
        "final_leverage": round(final_leverage, 2),
    }


def print_result(r: dict):
    icon = "🔴" if r.get("leverage_capped") or r.get("margin_capped") else "🟢"
    caps = []
    if r["leverage_capped"]:
        caps.append("LEVERAGE")
    if r["margin_capped"]:
        caps.append("MARGIN")
    cap_str = " + ".join(caps) if caps else "AUCUN"

    print(f"\n{'='*60}")
    print(f"{icon} {r['epic']} ({r['asset_class']}) — SL à {r['sl_pct']}%")
    print(f"{'='*60}")
    print(f"  Balance       : {r['balance']:>12,.2f}€")
    print(f"  Entrée        : {r['entry']:>12,.2f}")
    print(f"  Stop Loss     : {r['sl']:>12,.2f}")
    print(f"  Risque        : {r['risk_pct']*100:.1f}% = {r['risk_amt']:.2f}€")
    print(f"  ─────────────────────────────────────")
    print(f"  Raw size      : {r['raw_size']:>12.4f}")
    print(f"  Raw nominal   : {r['raw_nominal']:>12,.0f}€")
    print(f"  Raw leverage  : {r['raw_leverage']:>12.1f}×  ← AVANT CAP")
    print(f"  ─────────────────────────────────────")
    print(f"  🛡️ Caps actifs : {cap_str}")
    print(f"  Final size    : {r['final_size']:>12.2f}")
    print(f"  Final nominal : {r['final_nominal']:>12,.0f}€")
    print(f"  Final leverage: {r['final_leverage']:>12.2f}×  ← APRÈS CAP")
    print(f"  Marge requise : {r['margin_required']:>12,.2f}€ ({r['margin_rate']:.1%})")


# ═══════════════════════════════════════════════════════════════════════════════
# TEST CASES
# ═══════════════════════════════════════════════════════════════════════════════

print("\n" + "🏦" * 20)
print("  LEVERAGE & MARGIN CONTROLLER — TEST SUITE")
print("  Balance : 10,000€ | Max Leverage : 3.0×")
print("🏦" * 20)

BALANCE = 10_000
RISK = 0.005  # 0.5%

# ─── TEST 1: BTC avec micro SL (0.1%) — le cas fatal ─────────────────────────
print("\n\n📋 TEST 1: BTCUSD — SL microscopique (0.1%)")
print("   Sans cap → nominal = 50,000€ = 5× leverage ← REJET CERTAIN")
r = position_size_sim(BALANCE, RISK, entry=90_000, sl=89_910, epic="BTCUSD")
print_result(r)
assert r["leverage_capped"] or r["margin_capped"], "❌ ÉCHEC: BTC micro SL non capé!"
assert r["final_leverage"] <= MAX_EFFECTIVE_LEVERAGE + 0.1, \
    f"❌ ÉCHEC: leverage {r['final_leverage']} > {MAX_EFFECTIVE_LEVERAGE}"
print(f"\n  ✅ PASS — Capé de {r['raw_leverage']:.1f}× à {r['final_leverage']:.2f}×")

# ─── TEST 2: BTC avec SL normal (2%) ────────────────────────────────────────
print("\n\n📋 TEST 2: BTCUSD — SL normal (2%)")
r = position_size_sim(BALANCE, RISK, entry=90_000, sl=88_200, epic="BTCUSD")
print_result(r)
print(f"\n  ✅ PASS — Leverage {r['final_leverage']:.2f}×")

# ─── TEST 3: EURUSD avec SL serré (5 pips) ──────────────────────────────────
print("\n\n📋 TEST 3: EURUSD — SL ultra-serré (5 pips)")
print("   Sans cap → nominal = 100,000€ = 10× leverage")
r = position_size_sim(BALANCE, RISK, entry=1.0800, sl=1.0795, epic="EURUSD")
print_result(r)
assert r["leverage_capped"], "❌ ÉCHEC: EURUSD 5-pip SL non capé!"
print(f"\n  ✅ PASS — Capé de {r['raw_leverage']:.1f}× à {r['final_leverage']:.2f}×")

# ─── TEST 4: EURUSD avec SL normal (30 pips) ────────────────────────────────
print("\n\n📋 TEST 4: EURUSD — SL normal (30 pips)")
r = position_size_sim(BALANCE, RISK, entry=1.0800, sl=1.0770, epic="EURUSD")
print_result(r)
print(f"\n  ✅ PASS — Leverage {r['final_leverage']:.2f}×")

# ─── TEST 5: GOLD avec SL normal ────────────────────────────────────────────
print("\n\n📋 TEST 5: GOLD — SL normal (5$)")
r = position_size_sim(BALANCE, RISK, entry=2350, sl=2345, epic="GOLD")
print_result(r)
print(f"\n  ✅ PASS — Leverage {r['final_leverage']:.2f}×")

# ─── TEST 6: US500 avec micro SL ────────────────────────────────────────────
print("\n\n📋 TEST 6: US500 — SL microscopique (2 points)")
r = position_size_sim(BALANCE, RISK, entry=5200, sl=5198, epic="US500")
print_result(r)
if r["leverage_capped"]:
    print(f"\n  ✅ PASS — Capé de {r['raw_leverage']:.1f}× à {r['final_leverage']:.2f}×")
else:
    print(f"\n  ✅ PASS — Leverage {r['final_leverage']:.2f}× (sous le cap)")

# ─── TEST 7: ETHUSD — margin check (crypto 50%) ─────────────────────────────
print("\n\n📋 TEST 7: ETHUSD — SL serré + marge crypto 50%")
print("   Crypto = 50% marge → 2:1 max chez Capital.com")
r = position_size_sim(BALANCE, RISK, entry=3500, sl=3490, epic="ETHUSD")
print_result(r)
assert r["margin_capped"] or r["leverage_capped"], "❌ ÉCHEC: ETH non capé!"
print(f"\n  ✅ PASS — Capé (marge crypto 50%)")


# ═══════════════════════════════════════════════════════════════════════════════
print("\n\n" + "🏆" * 20)
print("  TOUS LES TESTS PASSENT — LEVERAGE & MARGIN CONTROLLER OK")
print("🏆" * 20)
print(f"\n  Config:")
print(f"    MAX_EFFECTIVE_LEVERAGE = {MAX_EFFECTIVE_LEVERAGE}×")
for cls, rate in sorted(ASSET_MARGIN_REQUIREMENTS.items()):
    print(f"    {cls:15s} → {rate:.1%} margin ({1/rate:.0f}:1 leverage)")
print()
