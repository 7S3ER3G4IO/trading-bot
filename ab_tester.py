"""
ab_tester.py — Feature U : A/B Testing de stratégie.

Fait tourner 2 variantes de paramètres en parallèle :
  Variante A = paramètres actuels (stable)
  Variante B = paramètres explorateurs (+10% ADX, +5% BREAKOUT)

Après 20 trades par variante → compare le Profit Factor et garde le meilleur.
Rapport Telegram chaque dimanche avec winner et statistiques.

Usage
-----
tester = ABTester()
variant = tester.get_variant(instrument)   # 'A' ou 'B'
params  = tester.get_params(variant)       # dict de paramètres
tester.record_result(instrument, variant, pnl, won)
"""
import json
import os
import random
from typing import Dict, Tuple, Optional, List
from dataclasses import dataclass, field, asdict
from loguru import logger

STATE_PATH = os.path.join(os.path.dirname(__file__), ".ab_tester.json")
MIN_TRADES_PER_VARIANT = 20   # Nombre minimum de trades pour décider


@dataclass
class VariantStats:
    """Statistiques pour une variante de paramètres."""
    trades:    List[dict] = field(default_factory=list)  # [{pnl, won}]

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t["won"]) / len(self.trades)

    @property
    def profit_factor(self) -> float:
        gross_win  = sum(t["pnl"] for t in self.trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in self.trades if t["pnl"] <= 0))
        return gross_win / gross_loss if gross_loss > 0 else gross_win

    @property
    def total_pnl(self) -> float:
        return sum(t["pnl"] for t in self.trades)


# Paramètres des deux variantes
VARIANT_PARAMS = {
    "A": {
        "adx_min":         18,
        "breakout_margin": 0.10,   # 10% du range
        "min_score":       2,
        "rr_ratio":        2.0,
    },
    "B": {
        "adx_min":         22,     # +22% plus strict → signaux plus propres
        "breakout_margin": 0.12,   # +20% plus strict → moins de faux cassures
        "min_score":       2,
        "rr_ratio":        2.2,    # +10% objectif de profit
    },
}


class ABTester:
    """
    Gestionnaire A/B Testing pour la stratégie NEMESIS.

    Alterne les instruments entre variante A (stable) et B (explorateur).
    Compare après MIN_TRADES_PER_VARIANT et promouvra automatiquement le winner.
    """

    def __init__(self):
        # {instrument: {"variant": "A"|"B", "stats": {A: VariantStats, B: VariantStats}}}
        self._state: Dict[str, dict] = {}
        self._current_winner: str = "A"   # winner global
        self._load()

    def _load(self):
        """Restaure l'état depuis disque."""
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH) as f:
                    raw = json.load(f)
                self._current_winner = raw.get("winner", "A")
                for inst, data in raw.get("instruments", {}).items():
                    self._state[inst] = {
                        "variant": data["variant"],
                        "stats": {
                            "A": VariantStats(trades=data["stats"].get("A", {}).get("trades", [])),
                            "B": VariantStats(trades=data["stats"].get("B", {}).get("trades", [])),
                        }
                    }
                logger.info(
                    f"📊 ABTester chargé — winner={self._current_winner} "
                    f"({len(self._state)} instruments)"
                )
            except Exception as e:
                logger.debug(f"ABTester load: {e}")

    def _save(self):
        """Persiste l'état sur disque."""
        try:
            instruments = {}
            for inst, data in self._state.items():
                instruments[inst] = {
                    "variant": data["variant"],
                    "stats": {
                        v: {"trades": data["stats"][v].trades}
                        for v in ("A", "B")
                    }
                }
            with open(STATE_PATH, "w") as f:
                json.dump({
                    "winner":      self._current_winner,
                    "instruments": instruments,
                }, f, indent=2)
        except Exception as e:
            logger.debug(f"ABTester save: {e}")

    def _init_instrument(self, instrument: str):
        """Initialise un instrument s'il n'existe pas encore."""
        if instrument not in self._state:
            # Moitié des instruments en A, moitié en B
            variant = "A" if random.random() < 0.5 else "B"
            self._state[instrument] = {
                "variant": variant,
                "stats": {"A": VariantStats(), "B": VariantStats()},
            }

    def get_variant(self, instrument: str) -> str:
        """Retourne la variante active ('A' ou 'B') pour un instrument."""
        self._init_instrument(instrument)
        return self._state[instrument]["variant"]

    def get_params(self, variant: str = "A") -> dict:
        """Retourne les paramètres pour une variante donnée."""
        return dict(VARIANT_PARAMS.get(variant, VARIANT_PARAMS["A"]))

    def record_result(self, instrument: str, variant: str, pnl: float, won: bool):
        """
        Enregistre le résultat d'un trade pour un instrument/variante.
        Déclenche une comparaison si les deux variantes ont assez de trades.

        Parameters
        ----------
        instrument : épique (ex: 'GOLD')
        variant    : 'A' ou 'B'
        pnl        : PnL du trade en euros
        won        : True si trade gagnant
        """
        self._init_instrument(instrument)
        self._state[instrument]["stats"][variant].trades.append({
            "pnl": round(pnl, 4),
            "won": won,
        })

        # Vérifier si on peut décider
        stats = self._state[instrument]["stats"]
        if stats["A"].n >= MIN_TRADES_PER_VARIANT and stats["B"].n >= MIN_TRADES_PER_VARIANT:
            self._evaluate(instrument)

        self._save()

    def _evaluate(self, instrument: str):
        """Compare les deux variantes et bascule vers le meilleur."""
        stats = self._state[instrument]["stats"]
        pf_a = stats["A"].profit_factor
        pf_b = stats["B"].profit_factor

        winner = "B" if pf_b > pf_a * 1.05 else "A"  # B doit être 5% meilleur pour gagner
        old_variant = self._state[instrument]["variant"]

        if winner != old_variant:
            self._state[instrument]["variant"] = winner
            logger.info(
                f"🏆 AB Test {instrument} : variante {winner} promotionnée "
                f"(PF_A={pf_a:.2f} vs PF_B={pf_b:.2f})"
            )
        else:
            logger.debug(f"AB Test {instrument} : variante {old_variant} maintenue (PF_A={pf_a:.2f} PF_B={pf_b:.2f})")

        # Réinitialiser les stats pour le prochain cycle
        stats["A"].trades.clear()
        stats["B"].trades.clear()

    def weekly_report(self) -> str:
        """Génère un rapport hebdomadaire A/B pour Telegram."""
        lines = ["📊 <b>Rapport A/B Testing</b>", ""]
        for inst, data in sorted(self._state.items()):
            v    = data["variant"]
            sa   = data["stats"]["A"]
            sb   = data["stats"]["B"]
            n_a  = sa.n
            n_b  = sb.n
            pf_a = f"{sa.profit_factor:.2f}" if n_a else "—"
            pf_b = f"{sb.profit_factor:.2f}" if n_b else "—"
            lines.append(
                f"  {inst}: ★{v}  A={pf_a}({n_a}tr) B={pf_b}({n_b}tr)"
            )
        return "\n".join(lines)

    def global_winner(self) -> str:
        """Retourne la variante globale dominante."""
        votes = {"A": 0, "B": 0}
        for data in self._state.values():
            votes[data["variant"]] += 1
        return "A" if votes["A"] >= votes["B"] else "B"

    def get_all_stats(self) -> dict:
        """Retourne toutes les stats pour debugging."""
        return {
            inst: {
                "variant": data["variant"],
                "A": {"n": data["stats"]["A"].n, "pf": data["stats"]["A"].profit_factor,
                      "wr": data["stats"]["A"].win_rate, "pnl": data["stats"]["A"].total_pnl},
                "B": {"n": data["stats"]["B"].n, "pf": data["stats"]["B"].profit_factor,
                      "wr": data["stats"]["B"].win_rate, "pnl": data["stats"]["B"].total_pnl},
            }
            for inst, data in self._state.items()
        }
