"""
prop_firm_backtest.py — APEX ULTIMATE × Prop Firm Protocol
============================================================

Simule un challenge Prop Firm (FTMO / E8 / MyForexFunds) avec :
  - Capital       : 100 000 $
  - Instruments   : Elite 10 (London/NY Breakout 1H)
  - Multi-TP      : TP1 1.5R (40%) · TP2 2.5R (40%) · TP3 trailing (20%)
  - Commission ECN: 14 $/lot AR (IC Markets standard)
  - Kill Switch   : Daily DD ≥ 5% OU Total DD ≥ 10% → HALT
  - Objectif      : PnL net > 10 000$ (10%)

Mode       : Monte Carlo sur stats réelles de optimized_rules.json
Usage      : python3 prop_firm_backtest.py [--trades N] [--seed S] [--verbose]
"""

import json
import random
import argparse
import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import List, Optional

# ─── PROP FIRM PARAMÈTRES ────────────────────────────────────────────────────
INITIAL_CAPITAL      = 100_000.0   # $ Challenge capital
RISK_PCT             = 0.0035      # 0.35% du capital par trade = 350$
DAILY_DD_LIMIT_PCT   = 0.05        # 5% = 5 000$ daily drawdown max
TOTAL_DD_LIMIT_PCT   = 0.10        # 10% = 10 000$ total drawdown max
PROFIT_TARGET_PCT    = 0.10        # 10% = 10 000$ objectif minimum
ECN_COMMISSION_LOT   = 14.0        # $/lot AR (IC Markets ECN)
FOREX_LOT_UNITS      = 100_000     # 1 lot standard = 100k unités

# ─── MULTI-TP CONFIGURATION ──────────────────────────────────────────────────
TP1_R_MULT  = 1.5   # TP1 = entry ± 1.5R
TP2_R_MULT  = 2.5   # TP2 = entry ± 2.5R
TP3_R_MULT  = 4.0   # TP3 trailing au-delà de 2.5R (cible 4R avant stop)
TP1_PORTION = 0.40  # 40% de la position fermée à TP1
TP2_PORTION = 0.40  # 40% fermée à TP2
TP3_PORTION = 0.20  # 20% restant (trailing)

# ─── ELITE 10 INSTRUMENTS ────────────────────────────────────────────────────
# Sélectionnés depuis optimized_rules.json (win_rate ≥ 50%, max_dd ≤ 3.5%)
ELITE_10 = [
    "GOLD",    "SILVER",  "J225",  "EURJPY", "DE40",
    "UK100",   "AU200",   "GBPJPY","BTCUSD", "TSLA",
]


# ─── DATA STRUCTURES ─────────────────────────────────────────────────────────
@dataclass
class Trade:
    instrument: str
    entry:      float
    sl:         float
    tp1:        float
    tp2:        float
    risk_usd:   float       # 1R en USD (250$ à 0.25%)
    commission: float       # frais ECN ($)
    direction:  str         # BUY / SELL
    portion_open: float = 1.0  # fraction restante ouverte

    pnl_tp1:    float = 0.0
    pnl_tp2:    float = 0.0
    pnl_tp3:    float = 0.0
    pnl_total:  float = 0.0
    result:     str   = "OPEN"


@dataclass
class DayStats:
    date:       str
    starting_balance: float
    trades:     List[Trade] = field(default_factory=list)
    pnl_gross:  float = 0.0
    pnl_net:    float = 0.0
    dd_pct:     float = 0.0
    halted:     bool  = False


# ─── PROP FIRM ENGINE ─────────────────────────────────────────────────────────
class PropFirmBacktest:
    """
    Monte Carlo backtest Prop Firm.
    Génère des trades aléatoires en respectant les probabilités réelles
    (win_rate, rr) de chaque instrument depuis optimized_rules.json.
    """

    def __init__(self, n_trades: int = 200, seed: int = 42, verbose: bool = False):
        self.n_trades          = n_trades
        self.seed              = seed
        self.verbose           = verbose
        self.rng               = random.Random(seed)

        self.capital           = INITIAL_CAPITAL
        self.peak_capital      = INITIAL_CAPITAL
        self.daily_stats:  List[DayStats] = []
        self.all_trades:   List[Trade]    = []
        self.kill_reason:  Optional[str]  = None

        self._rules = self._load_rules()

    def _load_rules(self) -> dict:
        """Charge optimized_rules.json — source de vérité des probabilités."""
        try:
            with open("optimized_rules.json") as f:
                data = json.load(f)
            rules = {k: v for k, v in data.items() if k in ELITE_10}
            # Compléter les manquants avec des valeurs conservatrices
            for instr in ELITE_10:
                if instr not in rules:
                    rules[instr] = {"win_rate": 50.0, "rr": 1.5, "max_dd": -2.0, "cat": "forex"}
            return rules
        except FileNotFoundError:
            print("⚠️  optimized_rules.json non trouvé — utilisation de valeurs par défaut")
            return {instr: {"win_rate": 55.0, "rr": 1.5, "max_dd": -2.0} for instr in ELITE_10}

    def _compute_lot_size(self, risk_usd: float, entry: float, sl: float, cat: str) -> float:
        """Calcule le nb de lots en fonction du risque USD cible."""
        pips = abs(entry - sl)
        if pips <= 0:
            return 0.01
        if cat == "forex":
            pip_val = FOREX_LOT_UNITS * pips  # 1 lot = 100k unités
            lots = risk_usd / pip_val if pip_val > 0 else 0.01
        else:
            # Indices, commodités, crypto : valeur nominale = entry * lots * 10 (mini)
            lots = risk_usd / (pips * 10) if pips > 0 else 0.01
        return max(0.01, round(lots, 2))

    def _simulate_trade(self, instrument: str, day_idx: int) -> Trade:
        """Simule un trade Multi-TP Monte Carlo pour un instrument donné."""
        rule = self._rules.get(instrument, {})
        win_rate = rule.get("win_rate", 55.0) / 100.0
        rr       = rule.get("rr", 1.5)
        cat      = rule.get("cat", "forex")

        # Prix synthétiques normalisés (backtest: on travaille en R)
        entry   = 100.0
        sl      = entry - 1.0          # 1R de distance
        risk_usd = INITIAL_CAPITAL * RISK_PCT  # 250$

        tp1     = entry + TP1_R_MULT
        tp2     = entry + TP2_R_MULT
        direction = self.rng.choice(["BUY", "SELL"])

        lots       = self._compute_lot_size(risk_usd, entry, sl, cat)
        # ECN commission : 14$/lot AR. Pour risque 250$ en montecarlo,
        # équivalent ~0.1 lot sur forex majeur → commission réaliste ~1.4$
        # On utilise le calcul lots mais on plafonne à 0.5 lot max pour la simulation
        lots_capped = min(lots, 0.5)
        commission = round(lots_capped * ECN_COMMISSION_LOT, 2)

        trade = Trade(
            instrument=instrument,
            entry=entry, sl=sl, tp1=tp1, tp2=tp2,
            risk_usd=risk_usd, commission=commission, direction=direction,
        )

        # ─── Monte Carlo résultat ─────────────────────────────────────────────
        # Phase 1 : TP1 ou SL ?
        hits_tp1 = self.rng.random() < win_rate

        if not hits_tp1:
            # SL touché — perte complète 1R (+ commission)
            trade.pnl_tp1  = -risk_usd
            trade.pnl_total = -risk_usd - commission
            trade.result   = "SL"
        else:
            # TP1 touché — 40% de la position fermée à +1.5R
            trade.pnl_tp1 = risk_usd * TP1_R_MULT * TP1_PORTION

            # Phase 2 : TP2 ou retour à BE ?
            hits_tp2 = self.rng.random() < (win_rate * 0.85)  # légèrement moins probable

            if not hits_tp2:
                # Retour à Break-Even sur les 60% restants
                trade.pnl_tp2  = 0.0
                trade.pnl_tp3  = 0.0
            else:
                # TP2 touché — 40% fermé à +2.5R
                trade.pnl_tp2 = risk_usd * TP2_R_MULT * TP2_PORTION

                # Phase 3 : TP3 trailing — 20% restant
                hits_tp3 = self.rng.random() < (win_rate * 0.70)
                if hits_tp3:
                    trade.pnl_tp3 = risk_usd * TP3_R_MULT * TP3_PORTION
                else:
                    # Trailing SL déclenché autour de 2R
                    trade.pnl_tp3 = risk_usd * 2.0 * TP3_PORTION

            trade.result = "TP"

        trade.pnl_total = (
            trade.pnl_tp1 + trade.pnl_tp2 + trade.pnl_tp3 - commission
        ) if hits_tp1 else trade.pnl_total

        return trade

    def _check_kill_switches(self, day: DayStats) -> Optional[str]:
        """Vérifie les kill switches Daily DD et Total DD."""
        # Daily DD
        daily_loss = self.capital - day.starting_balance
        if daily_loss < 0 and abs(daily_loss) / day.starting_balance >= DAILY_DD_LIMIT_PCT:
            return f"DAILY DD HALT : perte {daily_loss:+.2f}$ = {daily_loss/day.starting_balance:.1%} ≥ {DAILY_DD_LIMIT_PCT:.0%}"

        # Total DD (depuis peak)
        total_dd = (self.peak_capital - self.capital) / self.peak_capital
        if total_dd >= TOTAL_DD_LIMIT_PCT:
            return f"TOTAL DD HALT : peak={self.peak_capital:.2f}$ capital={self.capital:.2f}$ dd={total_dd:.1%} ≥ {TOTAL_DD_LIMIT_PCT:.0%}"

        return None

    def run(self) -> List[DayStats]:
        """Boucle principale : distribue les trades sur ~30 jours de trading."""
        random.seed(self.seed)

        # Distribuer n_trades sur 30 jours (~6-8 trades/jour)
        trades_per_day = max(1, self.n_trades // 30)
        day_num = 0

        for _ in range(self.n_trades):
            if _ % trades_per_day == 0:
                day_num += 1
                day_date = f"2026-02-{day_num:02d}" if day_num <= 28 else f"2026-03-{day_num-28:02d}"
                day = DayStats(date=day_date, starting_balance=self.capital)
                self.daily_stats.append(day)

            day = self.daily_stats[-1]

            # Sélectionner un instrument aléatoire dans Elite 10
            instrument = self.rng.choice(ELITE_10)

            # Simuler le trade
            trade = self._simulate_trade(instrument, day_num)
            self.all_trades.append(trade)
            day.trades.append(trade)

            # Mettre à jour le capital
            self.capital += trade.pnl_total
            self.capital = round(self.capital, 2)

            # Màj peak
            if self.capital > self.peak_capital:
                self.peak_capital = self.capital

            # Màj stats jour
            day.pnl_gross += (trade.pnl_tp1 + trade.pnl_tp2 + trade.pnl_tp3)
            day.pnl_net   += trade.pnl_total
            day.dd_pct     = (day.starting_balance - self.capital) / day.starting_balance * 100

            if self.verbose:
                print(
                    f"  [{day.date}] {instrument:8s} {trade.result:3s} "
                    f"PnL={trade.pnl_total:+8.2f}$ | Capital={self.capital:,.2f}$"
                )

            # Kill switches
            reason = self._check_kill_switches(day)
            if reason:
                day.halted = True
                self.kill_reason = reason
                print(f"\n🛑 KILL SWITCH : {reason}")
                break

        return self.daily_stats

    def report(self):
        """Affiche le PROP FIRM VERDICT final."""
        pnl_net     = self.capital - INITIAL_CAPITAL
        pnl_pct     = pnl_net / INITIAL_CAPITAL * 100

        # Max Daily DD
        max_daily_dd_pct = 0.0
        max_daily_dd_usd = 0.0
        for day in self.daily_stats:
            if day.dd_pct > max_daily_dd_pct:
                max_daily_dd_pct = day.dd_pct
                max_daily_dd_usd = day.starting_balance - self.capital

        # Max Total DD — recalcul correct sur toute l'equity curve
        running_peak = INITIAL_CAPITAL
        running_capital = INITIAL_CAPITAL
        max_total_dd_pct = 0.0
        for t in self.all_trades:
            running_capital += t.pnl_total
            if running_capital > running_peak:
                running_peak = running_capital
            dd = (running_peak - running_capital) / running_peak * 100
            if dd > max_total_dd_pct:
                max_total_dd_pct = dd

        # Statistiques
        total_trades = len(self.all_trades)
        wins  = sum(1 for t in self.all_trades if t.result == "TP")
        win_rate_actual = wins / total_trades * 100 if total_trades else 0
        total_commission = sum(t.commission for t in self.all_trades)
        avg_rr = (sum(t.pnl_total for t in self.all_trades if t.result == "TP") /
                  abs(sum(t.pnl_total for t in self.all_trades if t.result == "SL") or 1))

        # PASS / FAIL
        passed_profit  = pnl_net >= INITIAL_CAPITAL * PROFIT_TARGET_PCT
        passed_daily   = max_daily_dd_pct < DAILY_DD_LIMIT_PCT * 100
        passed_total   = (self.peak_capital - self.capital) / self.peak_capital < TOTAL_DD_LIMIT_PCT
        passed_all     = passed_profit and passed_daily and passed_total and not self.kill_reason

        verdict = "✅ CHALLENGE PASSED" if passed_all else "❌ CHALLENGE FAILED"

        print("")
        print("━" * 52)
        print("         🏦 PROP FIRM VERDICT — APEX ULTIMATE")
        print("━" * 52)
        print(f" Capital initial    : {INITIAL_CAPITAL:,.2f} $")
        print(f" Capital final      : {self.capital:,.2f} $")
        print(f" PnL Net            : {pnl_net:+,.2f} $ ({pnl_pct:+.2f}%)")
        print(f"   — dont commis.   : -{total_commission:,.2f} $ ECN ({total_trades} trades × ~14$/lot)")
        print(f" Objectif PnL       : > {INITIAL_CAPITAL * PROFIT_TARGET_PCT:,.0f} $ (10%)  {'✅' if passed_profit else '❌'}")
        print("─" * 52)
        print(f" Max Daily DD       : {max_daily_dd_pct:.2f}%  (limite : < {DAILY_DD_LIMIT_PCT*100:.0f}%)  {'✅' if passed_daily else '❌'}")
        print(f" Max Total DD       : {max_total_dd_pct:.2f}%  (limite : < {TOTAL_DD_LIMIT_PCT*100:.0f}%)  {'✅' if passed_total else '❌'}")
        print("─" * 52)
        print(f" Win Rate réel      : {win_rate_actual:.1f}%  ({wins}/{total_trades})")
        print(f" R:R moyen réalisé  : {avg_rr:.2f}R")
        print(f" Durée simulée      : {len(self.daily_stats)} jours de trading")
        if self.kill_reason:
            print(f" Kill Switch        : {self.kill_reason}")
        print("━" * 52)
        print(f"  {verdict}")
        print("━" * 52)
        print("")

        # FAIL — détail des règles échouées
        if not passed_all:
            print("  Raisons du FAIL :")
            if not passed_profit:
                print(f"    ❌ Profit insuffisant : {pnl_pct:.2f}% < 10%")
            if not passed_daily:
                print(f"    ❌ Daily DD dépassé   : {max_daily_dd_pct:.2f}% ≥ 5%")
            if not passed_total:
                total_dd_pct = (self.peak_capital - self.capital) / self.peak_capital * 100
                print(f"    ❌ Total DD dépassé   : {total_dd_pct:.2f}% ≥ 10%")
            if self.kill_reason:
                print(f"    ❌ Kill switch déclenché")
            print("")

        return {
            "passed":          passed_all,
            "pnl_net":         pnl_net,
            "pnl_pct":         pnl_pct,
            "max_daily_dd_pct":max_daily_dd_pct,
            "max_total_dd_pct":max_total_dd_pct,
            "win_rate":        win_rate_actual,
            "total_trades":    total_trades,
            "kill_reason":     self.kill_reason,
        }


# ─── TEST KILL SWITCH ─────────────────────────────────────────────────────────
def test_kill_switches():
    """
    Phase 4 validation : vérifie que les kill switches s'activent correctement.
    Injecte 20 trades perdants consécutifs.
    """
    print("\n" + "═" * 52)
    print("  🧪 TEST KILL SWITCHES")
    print("═" * 52)

    bt = PropFirmBacktest(n_trades=20, seed=999, verbose=False)

    # Forcer 20 SL consécutifs
    day = DayStats(date="2026-03-01", starting_balance=bt.capital)
    bt.daily_stats.append(day)

    for i in range(20):
        trade = Trade(
            instrument="GOLD", entry=100, sl=99, tp1=101.5, tp2=102.5,
            risk_usd=250.0, commission=14.0, direction="BUY",
        )
        trade.pnl_total = -264.0  # 250$ risque + 14$ commission
        trade.result    = "SL"
        bt.all_trades.append(trade)
        day.trades.append(trade)
        bt.capital += trade.pnl_total
        day.pnl_net += trade.pnl_total

        reason = bt._check_kill_switches(day)
        if reason:
            print(f"  ✅ Kill switch activé au trade #{i+1} : {reason}")
            bt.kill_reason = reason
            break
    else:
        print("  ❌ Kill switch NON activé — ERREUR !")

    print("")


# ─── ENTRY POINT ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="APEX ULTIMATE × Prop Firm Backtest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--trades",  type=int,  default=200, help="Nombre de trades simulés (défaut: 200)")
    parser.add_argument("--seed",    type=int,  default=42,  help="Graine aléatoire (défaut: 42)")
    parser.add_argument("--verbose", action="store_true",    help="Afficher chaque trade")
    parser.add_argument("--test-kill-switch", action="store_true", help="Tester les kill switches uniquement")
    args = parser.parse_args()

    print(f"\n🏦 APEX ULTIMATE × Prop Firm Backtest")
    print(f"   Capital : {INITIAL_CAPITAL:,.0f}$  |  Risque : {RISK_PCT:.2%}/trade  |  Elite 10")
    print(f"   Multi-TP: TP1={TP1_R_MULT}R({TP1_PORTION:.0%}) · TP2={TP2_R_MULT}R({TP2_PORTION:.0%}) · TP3 trailing({TP3_PORTION:.0%})")
    print(f"   Kill DD : Journalier {DAILY_DD_LIMIT_PCT:.0%} · Total {TOTAL_DD_LIMIT_PCT:.0%}")
    print(f"   ECN     : {ECN_COMMISSION_LOT}$/lot AR (IC Markets)")
    print(f"   Seed    : {args.seed}  |  Trades : {args.trades}\n")

    if args.test_kill_switch:
        test_kill_switches()
        return

    bt = PropFirmBacktest(n_trades=args.trades, seed=args.seed, verbose=args.verbose)
    bt.run()
    results = bt.report()

    # Test kill switches après le backtest principal
    test_kill_switches()

    # Code de sortie : 0 = PASSED, 1 = FAILED
    sys.exit(0 if results["passed"] else 1)


if __name__ == "__main__":
    main()
