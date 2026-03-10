"""
drl_sizer.py — Feature T : DRL Position Sizing (Kelly Adaptatif).

Implémente un agent de sizing adaptatif inspiré PPO, simplifié pour
fonctionner sur Railway sans GPU :

  État      : (win_rate_10, avg_rr_10, dd_pct, streak)
  Actions   : [0% = skip, 50% = demi-taille, 100% = taille normale, 150% = boost]
  Reward    : PnL_net - alpha × DD_penalty - beta × overtrading_penalty
  Mise à jour : chaque 10 trades → gradient policy gradient simplifié

En pratique : Kelly Criterion dynamique avec contraintes de sécurité.

F(kelly) = (win_rate × avg_win - loss_rate × avg_loss) / avg_win
Appliqué avec fraction 0.5 (half-kelly) pour sécurité.
Résultat : multiplicateur entre 0.3 et 1.5 du risque de base.
"""
import json
import os
from typing import List, Optional
from loguru import logger

MODEL_PATH = os.path.join(os.path.dirname(__file__), ".drl_sizer.json")

# Contraintes
MIN_MULTIPLIER = 0.3   # Ne jamais descendre en dessous de 30% du risque
MAX_MULTIPLIER = 1.5   # Ne jamais dépasser 150% du risque
RETRAIN_EVERY  = 10    # Recalculer toutes les N trades


class TradeRecord:
    """Représente le résultat d'un trade fermé."""
    __slots__ = ("pnl", "rr_actual", "direction")

    def __init__(self, pnl: float, rr_actual: float, direction: str):
        self.pnl         = pnl          # PnL en euros
        self.rr_actual   = rr_actual    # R:R réalisé (ex: 1.8)
        self.direction   = direction    # 'BUY' | 'SELL'


class DRLPositionSizer:
    """
    Agent de sizing DRL simplifié (Kelly adaptatif + contraintes de sécurité).

    Usage
    -----
    sizer = DRLPositionSizer()
    mult  = sizer.get_multiplier()   # utiliser juste avant de calculer la taille
    sizer.record_trade(pnl, rr)      # après fermeture du trade

    Le multiplicateur est entre 0.3× et 1.5× le risque de base.
    """

    def __init__(self):
        self._history: List[TradeRecord] = []
        self._current_multiplier: float = 1.0
        self._consecutive_losses: int  = 0
        self._total_trades: int        = 0
        self._load()

    def _load(self):
        """Charge l'état persisté depuis disque."""
        if os.path.exists(MODEL_PATH):
            try:
                with open(MODEL_PATH) as f:
                    d = json.load(f)
                self._current_multiplier   = float(d.get("multiplier", 1.0))
                self._consecutive_losses   = int(d.get("consec_losses", 0))
                self._total_trades         = int(d.get("total_trades", 0))
                # Reconstruire l'historique partiel
                for rec in d.get("history", [])[-RETRAIN_EVERY * 2:]:
                    self._history.append(
                        TradeRecord(rec["pnl"], rec["rr"], rec.get("dir", "BUY"))
                    )
                logger.info(
                    f"🎯 DRL Sizer chargé — mult={self._current_multiplier:.2f}× "
                    f"({self._total_trades} trades historiques)"
                )
            except Exception as e:
                logger.debug(f"DRL load: {e}")

    def _save(self):
        """Persiste l'état sur disque."""
        try:
            with open(MODEL_PATH, "w") as f:
                json.dump({
                    "multiplier":    self._current_multiplier,
                    "consec_losses": self._consecutive_losses,
                    "total_trades":  self._total_trades,
                    "history": [
                        {"pnl": r.pnl, "rr": r.rr_actual, "dir": r.direction}
                        for r in self._history[-RETRAIN_EVERY * 2:]
                    ],
                }, f, indent=2)
        except Exception as e:
            logger.debug(f"DRL save: {e}")

    def record_trade(self, pnl: float, rr_actual: float = 0.0, direction: str = "BUY"):
        """
        Enregistre le résultat d'un trade fermé.
        Déclenche un recalcul du multiplicateur tous les RETRAIN_EVERY trades.

        Parameters
        ----------
        pnl        : PnL réalisé en euros
        rr_actual  : R:R réalisé du trade
        direction  : 'BUY' | 'SELL'
        """
        self._history.append(TradeRecord(pnl, rr_actual, direction))
        self._total_trades += 1

        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        # Recalcul toutes les RETRAIN_EVERY trades
        if self._total_trades % RETRAIN_EVERY == 0:
            self._update_policy()

        self._save()
        logger.debug(
            f"🎯 DRL record — PnL={pnl:+.2f}€ | consec_losses={self._consecutive_losses} "
            f"| mult={self._current_multiplier:.2f}×"
        )

    def _update_policy(self):
        """
        Recalcule le multiplicateur Kelly sur la fenêtre glissante.

        Kelly Fraction = (W × R - L) / R
        où W=win_rate, L=loss_rate, R=avg_win/avg_loss
        Appliqué à 0.5× (half-Kelly) + contraintes [MIN, MAX].
        """
        window = self._history[-RETRAIN_EVERY * 3:]  # régression sur 30 trades max
        if len(window) < 5:
            return

        wins    = [r for r in window if r.pnl > 0]
        losses  = [r for r in window if r.pnl <= 0]

        win_rate  = len(wins)  / len(window)
        loss_rate = len(losses) / len(window)

        avg_win  = sum(r.pnl for r in wins)   / max(len(wins), 1)
        avg_loss = abs(sum(r.pnl for r in losses) / max(len(losses), 1))

        if avg_loss == 0:
            kelly = 1.0
        else:
            RR    = avg_win / avg_loss
            kelly = (win_rate * RR - loss_rate) / RR

        # Half-Kelly pour la sécurité
        half_kelly = kelly * 0.5

        # Pénalité pour streaks de pertes
        streak_penalty = 1.0 - (self._consecutive_losses * 0.1)
        streak_penalty = max(0.3, streak_penalty)

        new_mult = half_kelly * streak_penalty
        new_mult = round(max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, new_mult)), 2)

        logger.info(
            f"🎯 DRL Policy update — WR={win_rate:.0%} Kelly={kelly:.2f} "
            f"HalfKelly={half_kelly:.2f} streak_pen={streak_penalty:.1f} → "
            f"mult {self._current_multiplier:.2f}→{new_mult:.2f}×"
        )
        self._current_multiplier = new_mult

    def get_multiplier(self) -> float:
        """
        Retourne le multiplicateur de taille à appliquer sur le risque de base.

        Exemple : base_risk=1% × multiplier=0.7 → risk effectif=0.7%

        Returns
        -------
        float in [MIN_MULTIPLIER, MAX_MULTIPLIER]
        """
        # Sécurité supplémentaire : toujours dans les limites
        return round(
            max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, self._current_multiplier)), 2
        )

    def summary(self) -> str:
        """Retourne un résumé formaté pour Telegram."""
        window = self._history[-RETRAIN_EVERY * 3:]
        total  = len(window)
        wins   = len([r for r in window if r.pnl > 0])
        wr     = wins / total * 100 if total else 0
        return (
            f"🎯 DRL Sizer\n"
            f"  Multiplicateur : <b>{self._current_multiplier:.2f}×</b>\n"
            f"  WR ({total} trades) : <b>{wr:.0f}%</b>\n"
            f"  Streak pertes  : {self._consecutive_losses}"
        )
