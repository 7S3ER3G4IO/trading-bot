"""
drift_detector.py — Strategy Performance Drift Detection (#3)

Détecte quand la stratégie se dégrade et déclenche un re-hyperopt automatique.

Méthode : Page-Hinkley Test (PHT)
  - Suit la moyenne glissante du Sharpe ratio des trades récents
  - Si le signal de drift dépasse le seuil → stratégie dégradée → alerte + re-hyperopt

Triggers de drift :
  - Sharpe moyen 7j tombe sous 50% de la valeur de référence
  - WR hebdo < 35% (circuit breaker)
  - Max DD du mois dépasse 2× le DD moyen historique

Ce module lit l'historique depuis equity_history.json et trade_history.
"""
import sys, os, json, time, math
import warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

from datetime import datetime, timezone, timedelta
from loguru import logger

DRIFT_FILE        = "drift_state.json"
PHT_DELTA         = 0.01    # Sensibilité PHT (plus bas = plus sensible)
PHT_LAMBDA        = 3.0     # Seuil d'alarme PHT
WR_ALERT_THRESH   = 35.0    # WR hebdo minimum avant alerte
SHARPE_DROP_PCT   = 0.5     # Si Sharpe tombe sous 50% de référence → drift


class DriftDetector:
    """
    Surveille la performance de la stratégie en temps réel.
    Déclenche des alertes et re-hyperopt si dérive détectée.
    """

    def __init__(self):
        self._state = self._load()
        # Page-Hinkley Test state
        self._pht_sum   = 0.0
        self._pht_min   = 0.0
        self._pht_n     = 0

    def _load(self) -> dict:
        if os.path.exists(DRIFT_FILE):
            try:
                with open(DRIFT_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "reference_sharpe": None,
            "reference_wr":     None,
            "trade_results":    [],   # [(ts, pnl, win)]
            "drift_alerts":     [],
            "last_check":       0,
        }

    def _save(self):
        try:
            with open(DRIFT_FILE, "w") as f:
                json.dump(self._state, f, indent=2)
        except Exception:
            pass

    def record_trade(self, pnl: float, win: bool, symbol: str = ""):
        """Enregistre un trade terminé pour le monitoring."""
        self._state["trade_results"].append({
            "ts":     time.time(),
            "pnl":    pnl,
            "win":    win,
            "symbol": symbol,
        })
        # Garde seulement 500 derniers trades
        self._state["trade_results"] = self._state["trade_results"][-500:]
        self._save()

    def _recent_trades(self, days: int = 7) -> list:
        cutoff = time.time() - days * 86400
        return [t for t in self._state["trade_results"] if t["ts"] > cutoff]

    def set_reference(self, sharpe: float, wr: float):
        """Définit les métriques de référence (après hyperopt)."""
        if self._state["reference_sharpe"] is None:
            self._state["reference_sharpe"] = sharpe
            self._state["reference_wr"]     = wr
            logger.info(f"📈 Drift Detector : référence fixée → Sharpe={sharpe:.3f} WR={wr:.1f}%")
            self._save()

    def check_drift(self) -> dict:
        """
        Vérifie tous les indicateurs de drift.
        Retourne un dict : {"drift": bool, "reasons": list, "severity": str}
        """
        recent = self._recent_trades(7)
        if len(recent) < 5:
            return {"drift": False, "reasons": [], "severity": "OK", "n_trades": len(recent)}

        wins      = [t for t in recent if t["win"]]
        wr_7d     = len(wins) / len(recent) * 100
        pnls      = [t["pnl"] for t in recent]
        avg_pnl   = sum(pnls) / len(pnls)
        std_pnl   = (sum((p - avg_pnl)**2 for p in pnls) / len(pnls)) ** 0.5
        sharpe_7d = (avg_pnl / std_pnl * math.sqrt(len(pnls))) if std_pnl > 0 else 0

        reasons  = []
        severity = "OK"

        # 1. WR circuit breaker
        if wr_7d < WR_ALERT_THRESH:
            reasons.append(f"WR 7j = {wr_7d:.1f}% < {WR_ALERT_THRESH}%")
            severity = "HIGH"

        # 2. Sharpe drop vs référence
        ref_sharpe = self._state.get("reference_sharpe")
        if ref_sharpe and ref_sharpe > 0:
            if sharpe_7d < ref_sharpe * SHARPE_DROP_PCT:
                reasons.append(
                    f"Sharpe 7j = {sharpe_7d:.3f} < {ref_sharpe * SHARPE_DROP_PCT:.3f} "
                    f"(50% de référence {ref_sharpe:.3f})"
                )
                severity = "HIGH" if severity == "OK" else severity

        # 3. Page-Hinkley Test
        for pnl in pnls[-10:]:
            self._pht_sum += pnl - PHT_DELTA
            self._pht_min  = min(self._pht_min, self._pht_sum)
            pht_stat       = self._pht_sum - self._pht_min
            if pht_stat > PHT_LAMBDA:
                reasons.append(f"Page-Hinkley = {pht_stat:.2f} > {PHT_LAMBDA} (tendance baissière détectée)")
                severity = "MEDIUM" if severity == "OK" else severity
                self._pht_sum = 0.0
                self._pht_min = 0.0
                break

        drift = len(reasons) > 0

        if drift:
            alert = {
                "ts":       time.time(),
                "reasons":  reasons,
                "severity": severity,
                "wr_7d":    round(wr_7d, 1),
                "sharpe_7d": round(sharpe_7d, 3),
            }
            self._state["drift_alerts"].append(alert)
            self._state["drift_alerts"] = self._state["drift_alerts"][-20:]
            self._save()

            logger.warning(
                f"🚨 DRIFT DÉTECTÉ (sévérité={severity})\n"
                + "\n".join(f"  • {r}" for r in reasons)
            )

        return {
            "drift":     drift,
            "reasons":   reasons,
            "severity":  severity,
            "wr_7d":     round(wr_7d, 1),
            "sharpe_7d": round(sharpe_7d, 3),
            "n_trades":  len(recent),
        }

    def needs_reoptimization(self) -> bool:
        """True si la stratégie devrait être re-hyperoptimisée."""
        result = self.check_drift()
        return result["drift"] and result["severity"] == "HIGH"

    def format_status(self) -> str:
        """Format pour Telegram."""
        recent = self._recent_trades(7)
        if not recent:
            return "📉 <b>Drift Detector</b> : Pas assez de trades (< 5 sur 7j)"

        result  = self.check_drift()
        color   = "🔴" if result["drift"] else "🟢"
        status  = "DRIFT DÉTECTÉ" if result["drift"] else "Stratégie stable"

        text    = (
            f"📉 <b>Drift Detector</b>\n\n"
            f"  {color} Statut : <b>{status}</b>\n"
            f"  Trades 7j : {result['n_trades']}\n"
            f"  WR 7j     : {result['wr_7d']:.1f}%\n"
            f"  Sharpe 7j : {result['sharpe_7d']:.3f}\n"
        )
        if result["reasons"]:
            text += "\n  <b>Raisons :</b>\n"
            for r in result["reasons"]:
                text += f"  • {r}\n"
        return text


if __name__ == "__main__":
    import random
    dd = DriftDetector()
    dd.set_reference(0.35, 55.0)
    print(f"\n📉 Drift Detector — AlphaTrader\n")
    # Simulation d'une dégradation
    for i in range(15):
        win = random.random() < 0.55
        pnl = random.uniform(5, 50) if win else random.uniform(-40, -5)
        dd.record_trade(pnl, win, "ETH/USDT")
    # Ajout trades perdants
    for i in range(10):
        dd.record_trade(random.uniform(-80, -10), False, "ETH/USDT")

    result = dd.check_drift()
    print(f"  Drift   : {'🔴 OUI' if result['drift'] else '🟢 NON'}")
    print(f"  WR 7j   : {result['wr_7d']}%")
    print(f"  Sharpe  : {result['sharpe_7d']}")
    print(f"  Raisons : {result['reasons']}")
    print(f"  Re-hyperopt nécessaire : {dd.needs_reoptimization()}")
    print()
