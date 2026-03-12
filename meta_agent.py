"""
meta_agent.py — Moteur 13 : Meta-Agent Ensemble (Le Cerveau du Cerveau).

Le Meta-Agent est l'arbitre suprême de notre architecture multi-cerveaux.
Il résout les conflits entre les 10 moteurs et réalloue le capital en temps réel.

Architecture:
  1. COLLECTEUR — agrège les signaux de tous les moteurs actifs
  2. ARBITRE — vote pondéré dynamique (pas de vote equal-weight)
  3. TRACKER — mesure la précision rolling de chaque moteur (fenêtre 50 trades)
  4. REBALANCEUR — coupe les budgets des moteurs sous-performants, amplifie les meilleurs

Sources de signaux intégrées:
  - ML Engine (Moteur 4): score prédictif 0→1
  - Alt-Data Sentiment (Moteur 5): score sentiment -1→+1
  - Pairs Trader (Moteur 6): signal actif/inactif
  - RL Agent (Moteur 8): action (BUY/HOLD/SELL) + confidence
  - VPIN Guard (Moteur 9): niveau de toxicité
  - HMM Portfolio (Moteur 10): régime + kelly mult
  - Indicateurs techniques natifs: RSI/ATR/Score de base

Décision finale:
  signal_final = Σ(signal_i × weight_i × accuracy_i)
  Si signal_final > THRESHOLD_BUY → BUY ✅
  Si signal_final < THRESHOLD_SELL → SELL ✅
  Sinon → HOLD (no trade)

Usage:
    meta = MetaAgent(db)
    decision = meta.decide(instrument, direction, signals_dict)
    if not decision.approved:
        return  # Meta-Agent bloque le signal
    size_mult = decision.size_multiplier
"""
import time
import threading
from typing import Dict, Optional, Tuple, Any
from collections import deque
from datetime import datetime, timezone
from loguru import logger

# ─── Seuils de consensus ──────────────────────────────────────────────────────
_THRESHOLD_BUY    =  0.35   # Score composite > 0.35 → BUY validé
_THRESHOLD_SELL   = -0.35   # Score composite < -0.35 → SELL validé
_MIN_ENGINES      =  2      # Min moteurs actifs pour prendre une décision
_ACCURACY_WINDOW  = 50      # Fenêtre rolling pour mesurer la précision de chaque moteur
_ACCURACY_FLOOR   = 0.30    # Un moteur sous 30% de précision → poids réduit à 10%
_REBALANCE_INTERVAL = 300   # Recalibration des poids toutes les 5 minutes

# ─── Poids initiaux par moteur ────────────────────────────────────────────────
# Ces valeurs sont la priorité a priori (prior bayésien).
# Ils sont ajustés dynamiquement par le tracker de précision.
INITIAL_WEIGHTS = {
    "technical":    0.35,   # Signaux RSI/ATR/Score natifs (backbones toujours actif)
    "ml":           0.20,   # Moteur 4: ML prédictif
    "rl":           0.15,   # Moteur 8: RL Agent
    "sentiment":    0.10,   # Moteur 5: Alt-Data
    "hmm":          0.15,   # Moteur 10: HMM régime
    "pairs":        0.05,   # Moteur 6: Stat-Arb (signal d'opportunité, pas de timing)
}


class Decision:
    """Résultat du Meta-Agent pour un signal donné."""

    def __init__(self, approved: bool, score: float, breakdown: dict,
                 size_multiplier: float = 1.0, reason: str = ""):
        self.approved        = approved
        self.score           = round(score, 4)
        self.breakdown       = breakdown
        self.size_multiplier = round(size_multiplier, 3)
        self.reason          = reason

    def __repr__(self):
        status = "✅ APPROVED" if self.approved else "❌ BLOCKED"
        return f"Decision({status} | score={self.score:.3f} | mult={self.size_multiplier}x | {self.reason})"


class EngineTracker:
    """Suit la précision rolling d'un moteur sur les N derniers trades."""

    def __init__(self, name: str, window: int = _ACCURACY_WINDOW):
        self.name    = name
        self._window = window
        self._calls  = deque(maxlen=window)   # 1 = correct, 0 = incorrect

    def record(self, predicted_direction: str, actual_win: bool,
                trade_direction: str):
        """Enregistre si la prédiction du moteur était correcte."""
        correct = (
            (predicted_direction == "BUY" and trade_direction == "BUY" and actual_win) or
            (predicted_direction == "SELL" and trade_direction == "SELL" and actual_win)
        )
        self._calls.append(1 if correct else 0)

    @property
    def accuracy(self) -> float:
        if not self._calls:
            return 0.55  # Prior neutre
        return sum(self._calls) / len(self._calls)

    @property
    def n(self) -> int:
        return len(self._calls)


class MetaAgent:
    """
    Cerveau des cerveaux: consensus dynamique + auto-rebalancement des poids.
    Thread-safe, daemon thread de rebalancement toutes les 5min.
    """

    def __init__(self, db=None, telegram_router=None):
        self._db  = db
        self._tg  = telegram_router
        self._lock    = threading.Lock()

        # Poids courants (copiés depuis INITIAL_WEIGHTS)
        self._weights: Dict[str, float] = dict(INITIAL_WEIGHTS)

        # Trackers de précision par moteur
        self._trackers: Dict[str, EngineTracker] = {
            k: EngineTracker(k) for k in INITIAL_WEIGHTS
        }

        # Stats
        self._total_decisions    = 0
        self._approved_count     = 0
        self._blocked_count      = 0
        self._override_count     = 0
        self._rebalance_count    = 0

        # Historique des décisions (pour le TX Telegram)
        self._decision_history   = deque(maxlen=100)

        self._last_rebalance     = time.monotonic()

        # Daemon rebalancement
        self._running = True
        self._thread  = threading.Thread(
            target=self._rebalance_loop, daemon=True, name="meta_agent"
        )
        self._thread.start()

        logger.info("🧬 Meta-Agent Ensemble initialisé")

    # ─── Public API ──────────────────────────────────────────────────────────

    def decide(self, instrument: str, direction: str,
               signals: dict) -> Decision:
        """
        Décision finale après consensus pondéré.

        signals dict attendu:
          {
            "technical_score": float,   # 0→1 (score natif du bot)
            "ml_score": float,          # 0→1 (ML Engine)
            "rl_action": int,           # 0=SELL,1=HOLD,2=BUY (RL Agent)
            "rl_confidence": float,     # 0→1
            "sentiment_score": float,   # -1→+1 (AltData)
            "hmm_kelly_mult": float,    # multiplicateur Kelly HMM
            "hmm_regime": str,          # régime de marché
            "vpin_score": float,        # 0→1 (déjà vérifié avant, pas bloquant ici)
          }
        """
        self._total_decisions += 1

        with self._lock:
            weights = dict(self._weights)
            accs    = {k: t.accuracy for k, t in self._trackers.items()}

        breakdown   = {}
        total_score = 0.0
        total_w     = 0.0

        # === 1. Signal technique (backbone) ===
        tech_score = float(signals.get("technical_score", 0.5))
        # Normalise en -1→+1 selon la direction demandée
        tech_contrib = (tech_score - 0.5) * 2 if direction == "BUY" else (0.5 - tech_score) * 2
        eff_w = weights.get("technical", 0.35) * self._acc_factor(accs.get("technical", 0.55))
        breakdown["technical"] = round(tech_contrib * eff_w, 4)
        total_score += tech_contrib * eff_w
        total_w     += eff_w

        # === 2. ML Score ===
        ml_score = float(signals.get("ml_score", 0.5))
        ml_contrib = (ml_score - 0.5) * 2 if direction == "BUY" else (0.5 - ml_score) * 2
        eff_w = weights.get("ml", 0.20) * self._acc_factor(accs.get("ml", 0.55))
        breakdown["ml"] = round(ml_contrib * eff_w, 4)
        total_score += ml_contrib * eff_w
        total_w     += eff_w

        # === 3. RL Agent ===
        rl_action = int(signals.get("rl_action", 1))   # 1 = HOLD
        rl_conf   = float(signals.get("rl_confidence", 0.5))
        rl_raw    = (rl_action - 1) * rl_conf   # -conf, 0, +conf
        if direction == "SELL":
            rl_raw = -rl_raw
        eff_w = weights.get("rl", 0.15) * self._acc_factor(accs.get("rl", 0.55))
        breakdown["rl"] = round(rl_raw * eff_w, 4)
        total_score += rl_raw * eff_w
        total_w     += eff_w

        # === 4. AltData Sentiment ===
        sent = float(signals.get("sentiment_score", 0.0))
        sent_contrib = sent if direction == "BUY" else -sent
        eff_w = weights.get("sentiment", 0.10) * self._acc_factor(accs.get("sentiment", 0.55))
        breakdown["sentiment"] = round(sent_contrib * eff_w, 4)
        total_score += sent_contrib * eff_w
        total_w     += eff_w

        # === 5. HMM Régime ===
        hmm_mult   = float(signals.get("hmm_kelly_mult", 1.0))
        hmm_regime = signals.get("hmm_regime", "RANGE_MID_VOL")
        hmm_contrib = (hmm_mult - 1.0)   # +0.2 en bull, -0.7 en crisis
        eff_w = weights.get("hmm", 0.15) * self._acc_factor(accs.get("hmm", 0.55))
        breakdown["hmm"] = round(hmm_contrib * eff_w, 4)
        total_score += hmm_contrib * eff_w
        total_w     += eff_w

        # Score final normalisé
        final_score = total_score / total_w if total_w > 0 else 0.0

        # Vérifier nombre de moteurs actifs
        active_engines = sum(1 for v in breakdown.values() if abs(v) > 0.0001)

        # Décision
        if active_engines < _MIN_ENGINES:
            approved = False
            reason   = f"only {active_engines} active engines (min {_MIN_ENGINES})"
        elif direction == "BUY" and final_score > _THRESHOLD_BUY:
            approved = True
            reason   = f"BUY consensus score={final_score:.3f}"
        elif direction == "SELL" and final_score < _THRESHOLD_SELL:
            approved = True
            reason   = f"SELL consensus score={final_score:.3f}"
        elif direction == "BUY" and final_score < -_THRESHOLD_BUY * 0.5:
            # Contre-signal fort → block
            approved = False
            reason   = f"BUY contradicted by ensemble score={final_score:.3f}"
        elif direction == "SELL" and final_score > _THRESHOLD_SELL * -0.5:
            approved = False
            reason   = f"SELL contradicted by ensemble score={final_score:.3f}"
        else:
            # Zone grise → on suit le signal technique si présent
            approved = abs(tech_contrib) > 0.3
            reason   = f"zone grise → tech fallback ({tech_contrib:.2f})"

        # Multiplicateur de taille basé sur la conviction du score
        conviction = min(abs(final_score) / max(_THRESHOLD_BUY, 0.01), 1.5)
        size_mult  = max(0.5, min(1.5, conviction)) if approved else 0.0

        dec = Decision(
            approved=approved,
            score=final_score,
            breakdown=breakdown,
            size_multiplier=size_mult,
            reason=reason,
        )

        if approved:
            self._approved_count += 1
        else:
            self._blocked_count += 1

        self._decision_history.append({
            "instrument": instrument,
            "direction": direction,
            "approved": approved,
            "score": final_score,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

        logger.debug(f"🧬 MetaAgent {instrument} {direction}: {dec}")
        return dec

    def record_outcome(self, engine_name: str, predicted_dir: str,
                        actual_win: bool, trade_dir: str):
        """Appelé après la fermeture d'un trade pour mettre à jour les trackers."""
        with self._lock:
            if engine_name in self._trackers:
                self._trackers[engine_name].record(predicted_dir, actual_win, trade_dir)

    def format_report(self) -> str:
        with self._lock:
            weights = dict(self._weights)
            accs    = {k: t.accuracy for k, t in self._trackers.items()}

        lines = []
        for eng, w in sorted(weights.items(), key=lambda x: -x[1]):
            acc = accs.get(eng, 0.55)
            bar = "█" * int(w * 20) + "░" * (20 - int(w * 20))
            lines.append(f"  {eng:<12} {bar} {w:.1%} acc={acc:.0%}")

        return (
            f"🧬 <b>Meta-Agent Ensemble</b>\n\n"
            f"{'chr(10)'.join(lines)}\n\n"
            f"  Décisions: {self._total_decisions} | "
            f"✅ {self._approved_count} | ❌ {self._blocked_count}\n"
            f"  Rebalancement: #{self._rebalance_count}"
        ).replace("'chr(10)'", "\n")

    def stats(self) -> dict:
        with self._lock:
            return {
                "total": self._total_decisions,
                "approved": self._approved_count,
                "blocked": self._blocked_count,
                "rebalances": self._rebalance_count,
                "weights": dict(self._weights),
            }

    # ─── Auto-Rebalancing ─────────────────────────────────────────────────────

    def _rebalance_loop(self):
        while self._running:
            time.sleep(_REBALANCE_INTERVAL)
            try:
                self._rebalance_weights()
            except Exception as e:
                logger.debug(f"MetaAgent rebalance: {e}")

    def _rebalance_weights(self):
        """
        Réajuste les poids selon les précisions observées.
        Plus un moteur est précis, plus son poids augmente.
        Un moteur sous 30% de précision est réduit à 10% de son poids initial.
        """
        with self._lock:
            accs = {k: t.accuracy for k, t in self._trackers.items()}

        # Score proportionnel à la précision
        scores = {}
        for eng, acc in accs.items():
            if acc < _ACCURACY_FLOOR:
                scores[eng] = INITIAL_WEIGHTS.get(eng, 0.1) * 0.1   # pénalité
            else:
                scores[eng] = INITIAL_WEIGHTS.get(eng, 0.1) * (acc - 0.4) * 2

        total = sum(scores.values())
        if total <= 0:
            return

        new_weights = {k: v / total for k, v in scores.items()}

        # Lissage exponentiel: pas de changement brutal
        with self._lock:
            old_w = dict(self._weights)
            for k in new_weights:
                self._weights[k] = 0.7 * old_w.get(k, new_weights[k]) + 0.3 * new_weights[k]
            self._rebalance_count += 1

        # Log les changements significatifs
        changes = []
        for k in new_weights:
            delta = new_weights[k] - old_w.get(k, 0)
            if abs(delta) > 0.02:
                changes.append(f"{k}: {old_w.get(k,0):.1%}→{new_weights[k]:.1%}")

        if changes:
            logger.info(f"🧬 MetaAgent rebalanced: {' | '.join(changes)}")
            if self._tg:
                try:
                    self._tg.send_report(
                        f"🧬 <b>Meta-Agent Rebalancement #{self._rebalance_count}</b>\n\n"
                        + "\n".join(f"  → {c}" for c in changes)
                    )
                except Exception:
                    pass

        self._save_weights_async()

    # ─── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _acc_factor(acc: float) -> float:
        """Facteur d'amplification/atténuation basé sur la précision."""
        # acc=0.5 → factor=1.0 (neutre), acc=0.7 → 1.4, acc=0.3 → 0.6
        return max(0.1, min(2.0, acc * 2))

    # ─── Persistence ─────────────────────────────────────────────────────────

    def _save_weights_async(self):
        if not self._db:
            return
        self._db.async_write(self._save_weights_sync)

    def _save_weights_sync(self):
        try:
            if not self._db._pg:
                return
            with self._lock:
                w = dict(self._weights)
            ph = "%s"
            self._db._execute(
                f"INSERT INTO meta_agent_weights "
                f"(technical,ml,rl,sentiment,hmm,pairs,recorded_at) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},NOW())",
                (w.get("technical"), w.get("ml"), w.get("rl"),
                 w.get("sentiment"), w.get("hmm"), w.get("pairs"))
            )
        except Exception as e:
            logger.debug(f"meta weights save: {e}")

    def ensure_table(self):
        if not self._db or not self._db._pg:
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS meta_agent_weights (
                    id          SERIAL PRIMARY KEY,
                    technical   DOUBLE PRECISION,
                    ml          DOUBLE PRECISION,
                    rl          DOUBLE PRECISION,
                    sentiment   DOUBLE PRECISION,
                    hmm         DOUBLE PRECISION,
                    pairs       DOUBLE PRECISION,
                    recorded_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"meta_agent_weights table: {e}")

    def stop(self):
        self._running = False
