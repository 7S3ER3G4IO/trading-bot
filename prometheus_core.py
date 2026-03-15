"""
prometheus_core.py — 🔥 PROJECT PROMETHEUS T3: Recursive Self-Improvement

Le Cerveau Agentique — boucle d'auto-réflexion nocturne.
Chaque nuit à 23h UTC:
  1. Analyse les trades perdants du jour
  2. Génère 5 mutations génétiques par actif en perte
  3. Backteste chaque mutation sur 14 jours via ShadowTester
  4. Si une mutation a un meilleur Sharpe → écrase la règle actuelle
  5. Le bot se réveille "évolué"

Usage:
    prometheus = PrometheusCore(capital, journal, shadow_tester, telegram_router)
    prometheus.start_nightly()  # Background thread, fires 23h UTC
    
    # Manual trigger
    mutations = prometheus.run_cycle()
"""

import os
import sys
import json
import time
import copy
import random
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from loguru import logger


# ─── Configuration ────────────────────────────────────────────────────────────
PROMETHEUS_HOUR     = 23      # 23h UTC nightly cycle
N_MUTATIONS         = 5       # Mutations per losing instrument
BACKTEST_DAYS       = 14      # Backtest window
MIN_SHARPE_IMPROVE  = 0.10    # Minimum Sharpe improvement to apply mutation
MUTATION_RANGE      = 0.20    # ±20% parameter variation
RULES_FILE          = "optimized_rules.json"
MUTATION_LOG_FILE   = Path(os.environ.get("PROMETHEUS_LOG", "/tmp/prometheus_mutations.json"))

# Parameters that can be mutated
MUTABLE_PARAMS = {
    "sl_buffer":   (0.05, 2.0),    # min, max
    "tp1":         (0.8, 4.0),
    "tp2":         (1.5, 6.0),
    "rsi_lo":      (15, 40),
    "rsi_hi":      (60, 85),
    "bk_margin":   (0.02, 0.30),
    "range_lb":    (3, 12),
    "adx_min":     (10, 30),
    "zscore_thresh": (1.5, 4.0),
}


class PrometheusCore:
    """
    Recursive Self-Improvement Engine.
    
    Darwinian evolution: mutate → test → select → apply.
    The bot rewrites its own rules based on evidence.
    """

    def __init__(self, capital_client=None, journal=None,
                 shadow_tester=None, telegram_router=None):
        self._capital = capital_client
        self._journal = journal
        self._shadow = shadow_tester
        self._router = telegram_router
        self._thread = None
        self._running = False

        # Stats
        self._cycles = 0
        self._mutations_applied = 0
        self._mutations_rejected = 0
        self._mutation_history: list[dict] = []

        self._load_history()

    def _load_history(self):
        """Load mutation history from disk."""
        if MUTATION_LOG_FILE.exists():
            try:
                with open(MUTATION_LOG_FILE) as f:
                    self._mutation_history = json.load(f)
            except Exception:
                self._mutation_history = []

    def _save_history(self):
        """Persist mutation history."""
        try:
            MUTATION_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(MUTATION_LOG_FILE, "w") as f:
                json.dump(self._mutation_history[-200:], f, indent=2, default=str)
        except Exception as e:
            logger.debug(f"Prometheus save: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    #  SCHEDULER
    # ═══════════════════════════════════════════════════════════════════════

    def start_nightly(self):
        """Start background thread for nightly cycle at 23h UTC."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._schedule_loop, daemon=True, name="prometheus"
        )
        self._thread.start()
        logger.info(
            f"🔥 Prometheus: nightly cycle scheduled at {PROMETHEUS_HOUR}h UTC"
        )

    def stop(self):
        self._running = False

    def _schedule_loop(self):
        last_day = ""
        while self._running:
            now = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")

            if now.hour == PROMETHEUS_HOUR and today != last_day:
                last_day = today
                logger.info("🔥 Prometheus: nightly cycle starting...")
                try:
                    self.run_cycle()
                except Exception as e:
                    logger.error(f"Prometheus cycle error: {e}")
                    self._send_alert(
                        f"❌ <b>Prometheus Error</b>\n\n{str(e)[:200]}"
                    )

            time.sleep(300)  # Check every 5 minutes

    # ═══════════════════════════════════════════════════════════════════════
    #  MAIN CYCLE: Reflect → Mutate → Test → Apply
    # ═══════════════════════════════════════════════════════════════════════

    def run_cycle(self, losers: list[dict] = None) -> dict:
        """
        Full Prometheus cycle.

        1. Get today's losers from journal
        2. For each losing instrument:
           a. Load current rules
           b. Generate N mutations
           c. Backtest each mutation
           d. If sharpe improves → apply mutation
        3. Report
        """
        t0 = time.time()
        self._cycles += 1

        # Step 1: Get losers
        if losers is None and self._journal:
            losers = self._journal.get_losers(period_days=1)

        if not losers:
            logger.info("🔥 Prometheus: no losing trades today — nothing to optimize")
            return {"status": "no_losers", "mutations": 0}

        # Group losers by instrument
        by_instrument = {}
        for trade in losers:
            inst = trade.get("instrument", "?")
            by_instrument.setdefault(inst, []).append(trade)

        logger.info(
            f"🔥 Prometheus: analyzing {len(losers)} losers across "
            f"{len(by_instrument)} instruments"
        )

        # Step 2: Load current rules
        current_rules = self._load_rules()

        applied = []
        rejected = []

        for instrument, inst_losers in by_instrument.items():
            try:
                result = self._optimize_instrument(
                    instrument, inst_losers, current_rules
                )
                if result.get("applied"):
                    applied.append(result)
                    self._mutations_applied += 1
                else:
                    rejected.append(result)
                    self._mutations_rejected += 1
            except Exception as e:
                logger.error(f"Prometheus {instrument}: {e}")
                rejected.append({"instrument": instrument, "error": str(e)})

        # Step 3: Save updated rules
        if applied:
            self._save_rules(current_rules)

        elapsed = time.time() - t0

        # Report
        report = self._build_report(applied, rejected, elapsed)
        self._send_alert(report)

        return {
            "status": "complete",
            "losers_analyzed": len(losers),
            "instruments": len(by_instrument),
            "mutations_applied": len(applied),
            "mutations_rejected": len(rejected),
            "elapsed_s": round(elapsed, 1),
            "details": applied + rejected,
        }

    def _optimize_instrument(self, instrument: str,
                              losers: list[dict],
                              current_rules: dict) -> dict:
        """
        Optimize a single instrument:
        - Analyze WHY it lost
        - Generate mutations
        - Backtest mutations
        - Apply if better
        """
        current_params = current_rules.get(instrument, {})
        strat = current_params.get("strat", current_params.get("engine", "BK"))

        # ─── Step 1: Diagnose the loss ────────────────────────────────────
        diagnosis = self._diagnose(instrument, losers, current_params)
        logger.info(f"🧠 Prometheus {instrument}: {diagnosis['hypothesis']}")

        # ─── Step 2: Get backtest data ────────────────────────────────────
        df = None
        if self._capital and self._capital.available:
            tf = current_params.get("tf", "1h")
            count = {"5m": 2000, "15m": 1500, "1h": 500, "4h": 300, "1d": 200}.get(tf, 500)
            try:
                df = self._capital.fetch_ohlcv(instrument, timeframe=tf, count=count)
            except Exception:
                pass

        if df is None or len(df) < 50:
            return {"instrument": instrument, "applied": False,
                    "reason": "no data"}

        # Compute indicators
        try:
            from strategy import Strategy
            strategy = Strategy()
            df = strategy.compute_indicators(df)
        except Exception:
            return {"instrument": instrument, "applied": False,
                    "reason": "indicator error"}

        # ─── Step 3: Baseline backtest ────────────────────────────────────
        baseline_params = {
            "strat": strat,
            "sl_buffer": current_params.get("sl_buffer", 0.10),
            "tp1": current_params.get("tp1", 1.5),
            "rsi_lo": current_params.get("rsi_lo", 25),
            "rsi_hi": current_params.get("rsi_hi", 75),
            "bk_margin": current_params.get("bk_margin", 0.10),
            "range_lb": current_params.get("range_lb", 6),
            "adx_min": current_params.get("adx_min", 15),
        }

        baseline = self._shadow.backtest(df, baseline_params)
        baseline_sharpe = baseline["sharpe"]
        logger.info(
            f"📊 Baseline {instrument}: {baseline['total_trades']} trades, "
            f"WR={baseline['win_rate']:.0%}, Sharpe={baseline_sharpe:.2f}"
        )

        # ─── Step 4: Generate & test mutations ────────────────────────────
        mutations = self._generate_mutations(
            baseline_params, diagnosis["focus_params"]
        )

        best_mutation = None
        best_sharpe = baseline_sharpe

        for i, mutation_params in enumerate(mutations):
            result = self._shadow.backtest(df, mutation_params)
            sharpe = result["sharpe"]

            logger.debug(
                f"  Mutation #{i+1}: {self._diff_params(baseline_params, mutation_params)} "
                f"→ Sharpe={sharpe:.2f} (vs baseline {baseline_sharpe:.2f})"
            )

            if sharpe > best_sharpe + MIN_SHARPE_IMPROVE:
                best_sharpe = sharpe
                best_mutation = mutation_params
                best_result = result

        # ─── Step 5: Apply if improved ────────────────────────────────────
        if best_mutation:
            diff = self._diff_params(baseline_params, best_mutation)
            log_entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "instrument": instrument,
                "diagnosis": diagnosis["hypothesis"],
                "changes": diff,
                "baseline_sharpe": baseline_sharpe,
                "new_sharpe": best_sharpe,
                "improvement": round(best_sharpe - baseline_sharpe, 3),
                "new_win_rate": best_result["win_rate"],
            }

            # Apply mutation to current rules
            for key, value in best_mutation.items():
                if key in MUTABLE_PARAMS:
                    current_rules.setdefault(instrument, {})[key] = value

            # Add Prometheus tag
            current_rules.setdefault(instrument, {})["prometheus_mutation"] = (
                f"Applied {datetime.now(timezone.utc).strftime('%Y-%m-%d')}: "
                f"{diff} | Sharpe {baseline_sharpe:.2f}→{best_sharpe:.2f}"
            )

            self._mutation_history.append(log_entry)
            self._save_history()

            logger.info(
                f"🔥 PROMETHEUS MUTATION APPLIED: {instrument}\n"
                f"  {diff}\n"
                f"  Sharpe: {baseline_sharpe:.2f} → {best_sharpe:.2f} "
                f"(+{best_sharpe - baseline_sharpe:.2f})"
            )

            return {
                "instrument": instrument,
                "applied": True,
                "changes": diff,
                "baseline_sharpe": baseline_sharpe,
                "new_sharpe": best_sharpe,
                "log": log_entry,
            }

        return {
            "instrument": instrument,
            "applied": False,
            "reason": "no mutation improved Sharpe",
            "baseline_sharpe": baseline_sharpe,
        }

    # ═══════════════════════════════════════════════════════════════════════
    #  DIAGNOSIS: Understand WHY a trade lost
    # ═══════════════════════════════════════════════════════════════════════

    def _diagnose(self, instrument: str, losers: list[dict],
                   params: dict) -> dict:
        """
        Analyze losing trades and form a hypothesis.

        Returns:
            {"hypothesis": str, "focus_params": list[str]}
        """
        # Average R-multiple of losses
        avg_r = sum(t.get("r_multiple", -1) for t in losers) / max(1, len(losers))

        # Common exit reason
        exit_reasons = {}
        for t in losers:
            r = t.get("exit_reason", "SL")
            exit_reasons[r] = exit_reasons.get(r, 0) + 1
        top_exit = max(exit_reasons, key=exit_reasons.get) if exit_reasons else "SL"

        # Average duration
        avg_dur = sum(t.get("duration_min", 0) for t in losers) / max(1, len(losers))

        # Hypothesis
        focus = []

        if top_exit == "SL" and avg_r < -0.8:
            hypothesis = (
                f"SL trop serré (avg R={avg_r:.1f}, {len(losers)} stops). "
                f"Augmenter sl_buffer pourrait donner plus de marge."
            )
            focus = ["sl_buffer", "tp1"]

        elif avg_dur < 30:
            hypothesis = (
                f"Trades trop courts (avg {avg_dur:.0f}min). "
                f"Le marché n'a pas le temps d'atteindre le TP."
            )
            focus = ["tp1", "sl_buffer"]

        elif avg_dur > 1440:  # >24h
            hypothesis = (
                f"Trades trop longs (avg {avg_dur:.0f}min). "
                f"Le signal s'épuise avant le TP."
            )
            focus = ["tp1", "tp2"]

        else:
            hypothesis = (
                f"Pertes mixtes (avg R={avg_r:.1f}, {top_exit} dominant). "
                f"Optimisation globale des niveaux SL/TP."
            )
            focus = ["sl_buffer", "tp1", "rsi_lo", "rsi_hi"]

        return {"hypothesis": hypothesis, "focus_params": focus}

    # ═══════════════════════════════════════════════════════════════════════
    #  MUTATION: Genetic parameter variation
    # ═══════════════════════════════════════════════════════════════════════

    def _generate_mutations(self, base_params: dict,
                             focus_params: list[str]) -> list[dict]:
        """
        Generate N_MUTATIONS variations of the base parameters.
        Focus on diagnostically-relevant params but allow global variation.
        """
        mutations = []

        for _ in range(N_MUTATIONS):
            mutated = copy.deepcopy(base_params)

            # Mutate 1-3 parameters per mutation
            n_changes = random.randint(1, min(3, len(focus_params) + 1))

            # Prioritize focus params (70%) + random others (30%)
            params_to_mutate = []
            for _ in range(n_changes):
                if focus_params and random.random() < 0.7:
                    p = random.choice(focus_params)
                else:
                    p = random.choice(list(MUTABLE_PARAMS.keys()))
                if p not in params_to_mutate:
                    params_to_mutate.append(p)

            for param in params_to_mutate:
                if param not in MUTABLE_PARAMS:
                    continue
                min_val, max_val = MUTABLE_PARAMS[param]
                current = base_params.get(param, (min_val + max_val) / 2)

                # ±20% variation
                delta = current * MUTATION_RANGE
                new_val = current + random.uniform(-delta, delta)
                new_val = max(min_val, min(max_val, new_val))

                # Round appropriately
                if isinstance(current, int) or param in ("range_lb", "rsi_lo", "rsi_hi"):
                    new_val = int(round(new_val))
                else:
                    new_val = round(new_val, 3)

                mutated[param] = new_val

            mutations.append(mutated)

        return mutations

    # ═══════════════════════════════════════════════════════════════════════
    #  HELPERS
    # ═══════════════════════════════════════════════════════════════════════

    def _diff_params(self, old: dict, new: dict) -> str:
        """Human-readable diff between two param sets."""
        changes = []
        for key in MUTABLE_PARAMS:
            if key in new and new.get(key) != old.get(key):
                changes.append(f"{key}: {old.get(key)} → {new[key]}")
        return " | ".join(changes) if changes else "no changes"

    def _load_rules(self) -> dict:
        """Load current rules from disk."""
        rules = {}
        for f in [RULES_FILE, "black_ops_rules.json", "lazarus_rules.json"]:
            if os.path.exists(f):
                try:
                    with open(f) as fh:
                        rules.update(json.load(fh))
                except Exception:
                    pass
        return rules

    def _save_rules(self, rules: dict):
        """Save updated rules to disk."""
        try:
            with open(RULES_FILE, "w") as f:
                json.dump(rules, f, indent=2)
            logger.info(f"📝 Prometheus: {RULES_FILE} updated")
        except Exception as e:
            logger.error(f"Prometheus save rules: {e}")

    def _build_report(self, applied: list, rejected: list,
                       elapsed: float) -> str:
        report = (
            f"🔥 <b>PROMETHEUS — Nightly Evolution</b>\n\n"
            f"⏱ Cycle #{self._cycles} ({elapsed:.0f}s)\n"
            f"✅ Mutations applied: {len(applied)}\n"
            f"⏭ Rejected: {len(rejected)}\n\n"
        )
        for m in applied:
            report += (
                f"  🧬 <b>{m['instrument']}</b>: "
                f"Sharpe {m['baseline_sharpe']:.2f} → <b>{m['new_sharpe']:.2f}</b>\n"
                f"    {m['changes']}\n"
            )
        return report

    def _send_alert(self, text: str):
        if self._router:
            try:
                self._router.send_to("risk", text)
            except Exception:
                pass

    def format_status(self) -> str:
        return (
            f"🔥 <b>Prometheus</b>\n"
            f"  🔄 Cycles: {self._cycles}\n"
            f"  ✅ Mutations applied: {self._mutations_applied}\n"
            f"  ❌ Rejected: {self._mutations_rejected}\n"
            f"  📓 History: {len(self._mutation_history)}"
        )

    @property
    def stats(self) -> dict:
        return {
            "cycles": self._cycles,
            "mutations_applied": self._mutations_applied,
            "mutations_rejected": self._mutations_rejected,
            "history_size": len(self._mutation_history),
            "running": self._running,
        }
