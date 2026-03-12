"""
asset_quarantine.py — Dynamic Blacklist: quarantaine automatique des actifs toxiques.

Logique:
- Analyse le win-rate par actif depuis Supabase (24h glissantes)
- Si win-rate < seuil OU N pertes consécutives → quarantaine temporaire
- Durée de quarantaine adaptative: plus l'actif performe mal, plus c'est long
- Réallocation automatique du capital vers les actifs les plus performants
- Notification Telegram à chaque mise en quarantaine / libération
"""
import json
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from loguru import logger


# ─── Configuration ────────────────────────────────────────────────────────────
_MIN_TRADES_TO_EVAL = 3        # Trades minimum pour évaluer un actif
_WR_QUARANTINE_THRESH = 0.30   # WR < 30% → quarantaine
_CONSEC_LOSS_THRESH = 3        # 3 pertes consécutives → quarantaine immédiate
_QUARANTINE_BASE_H = 4         # Durée base: 4h
_QUARANTINE_MAX_H = 48         # Max: 48h
_EVAL_WINDOW_H = 24            # Fenêtre d'analyse: 24h glissantes


class AssetQuarantine:
    """
    Gestionnaire de quarantaine dynamique par actif.

    Intégration:
        q = AssetQuarantine(db, telegram_router)
        if q.is_quarantined("GBPUSD"):
            return  # skip signal

        # A la fermeture d'un trade:
        q.record_result("GBPUSD", won=False)
    """

    def __init__(self, db=None, telegram_router=None):
        self._db = db
        self._tg = telegram_router
        self._lock = threading.Lock()

        # {instrument: {"until": timestamp, "reason": str, "count": int}}
        self._quarantine: Dict[str, dict] = {}

        # Compteur pertes consécutives en mémoire
        self._consec_losses: Dict[str, int] = {}

        # Cache win-rate Supabase (refresh toutes les 15 min)
        self._wr_cache: Dict[str, Tuple[float, float]] = {}  # {inst: (wr, ts)}
        self._cache_ttl = 900  # 15 min

    # ─── Public API ──────────────────────────────────────────────────────────

    def is_quarantined(self, instrument: str) -> bool:
        """Retourne True si l'actif est en quarantaine active."""
        with self._lock:
            entry = self._quarantine.get(instrument)
            if not entry:
                return False
            if time.time() > entry["until"]:
                # Quarantaine expirée → libération
                self._release(instrument)
                return False
            return True

    def record_result(self, instrument: str, won: bool):
        """
        Enregistre le résultat d'un trade. Met en quarantaine si nécessaire.
        Appelé depuis bot_monitor après fermeture d'un trade.
        """
        with self._lock:
            if won:
                self._consec_losses[instrument] = 0
            else:
                self._consec_losses[instrument] = self._consec_losses.get(instrument, 0) + 1

            losses = self._consec_losses.get(instrument, 0)
            if losses >= _CONSEC_LOSS_THRESH:
                self._quarantine_instrument(
                    instrument,
                    reason=f"{losses} pertes consécutives",
                    severity=losses - _CONSEC_LOSS_THRESH + 1
                )

    def refresh_from_db(self):
        """
        Analyse le win-rate 24h depuis Supabase pour tous les actifs.
        Lance la quarantaine sur les actifs sous-performants.
        Doit être appelé périodiquement (toutes les 15-30 min).
        """
        if not self._db or not self._db._pg:
            return  # Pas de Supabase → skip

        try:
            results = self._fetch_wr_from_db()
            for instrument, (wr, n_trades) in results.items():
                if n_trades < _MIN_TRADES_TO_EVAL:
                    continue
                if wr < _WR_QUARANTINE_THRESH:
                    with self._lock:
                        severity = max(1, int((_WR_QUARANTINE_THRESH - wr) / 0.10))
                        self._quarantine_instrument(
                            instrument,
                            reason=f"WR={wr:.0%} sur {n_trades} trades (24h)",
                            severity=severity,
                        )
        except Exception as e:
            logger.debug(f"AssetQuarantine.refresh_from_db: {e}")

    def get_quarantined(self) -> List[dict]:
        """Liste des actifs actuellement en quarantaine (pour le rapport EoD)."""
        now = time.time()
        with self._lock:
            active = []
            for inst, entry in list(self._quarantine.items()):
                if now < entry["until"]:
                    remaining_h = (entry["until"] - now) / 3600
                    active.append({
                        "instrument": inst,
                        "reason": entry["reason"],
                        "remaining_h": round(remaining_h, 1),
                        "quarantined_at": entry.get("quarantined_at", ""),
                    })
            return active

    def get_best_performers(self, n: int = 5) -> List[str]:
        """
        Retourne les N actifs avec le meilleur WR (24h) depuis le cache.
        Utilisé pour réallouer le capital.
        """
        try:
            results = self._fetch_wr_from_db()
            ranked = sorted(
                [(inst, wr) for inst, (wr, n_tr) in results.items()
                 if n_tr >= _MIN_TRADES_TO_EVAL and not self.is_quarantined(inst)],
                key=lambda x: x[1], reverse=True
            )
            return [inst for inst, _ in ranked[:n]]
        except Exception:
            return []

    def release_all(self):
        """Force la libération de toutes les quarantaines (ex: après reset)."""
        with self._lock:
            self._quarantine.clear()
            self._consec_losses.clear()
            logger.info("🟢 Toutes les quarantaines levées")

    def status_summary(self) -> str:
        """Résumé texte pour les logs et Telegram."""
        quarantined = self.get_quarantined()
        if not quarantined:
            return "✅ Aucun actif en quarantaine"
        lines = [f"🔴 {q['instrument']} — {q['reason']} ({q['remaining_h']}h restantes)"
                 for q in quarantined]
        return "\n".join(lines)

    # ─── Internals ───────────────────────────────────────────────────────────

    def _quarantine_instrument(self, instrument: str, reason: str, severity: int = 1):
        """Met un actif en quarantaine. severity [1-5] influence la durée."""
        if instrument in self._quarantine:
            old = self._quarantine[instrument]
            if time.time() < old["until"]:
                # Déjà en quarantaine — prolonger si gravité croissante
                extra_h = _QUARANTINE_BASE_H * severity
                new_until = min(
                    old["until"] + extra_h * 3600,
                    time.time() + _QUARANTINE_MAX_H * 3600
                )
                self._quarantine[instrument]["until"] = new_until
                self._quarantine[instrument]["count"] += 1
                logger.warning(
                    f"🔴 Quarantaine prolongée {instrument} "
                    f"+{extra_h}h | {reason}"
                )
                return

        duration_h = min(_QUARANTINE_BASE_H * severity, _QUARANTINE_MAX_H)
        until_ts = time.time() + duration_h * 3600
        self._quarantine[instrument] = {
            "until": until_ts,
            "reason": reason,
            "count": 1,
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.warning(
            f"🚫 QUARANTAINE {instrument} — {reason} — {duration_h}h"
        )

        # Notification Telegram
        if self._tg:
            try:
                self._tg.send_trade(
                    f"🚫 <b>Quarantaine Actif — {instrument}</b>\n\n"
                    f"  Raison : <b>{reason}</b>\n"
                    f"  Durée  : <b>{duration_h}h</b>\n"
                    f"  Fin    : {datetime.utcfromtimestamp(until_ts).strftime('%H:%M UTC')}\n\n"
                    f"Capital réalloué vers les meilleurs actifs. 🔄"
                )
            except Exception:
                pass

    def _release(self, instrument: str):
        """Libère un actif de quarantaine."""
        entry = self._quarantine.pop(instrument, None)
        self._consec_losses[instrument] = 0
        if entry:
            logger.info(f"🟢 Quarantaine levée — {instrument}")
            if self._tg:
                try:
                    self._tg.send_trade(
                        f"🟢 <b>Quarantaine Levée — {instrument}</b>\n"
                        f"L'actif est de nouveau éligible aux trades."
                    )
                except Exception:
                    pass

    def _fetch_wr_from_db(self) -> Dict[str, Tuple[float, float]]:
        """
        Récupère le win-rate par actif depuis Supabase (24h).
        Retourne {instrument: (win_rate, n_trades)}.
        """
        if not self._db:
            return {}

        try:
            sql = """
                SELECT instrument,
                       COUNT(*) as n_trades,
                       SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins
                FROM capital_trades
                WHERE status = 'CLOSED'
                AND opened_at >= NOW() - INTERVAL '24 hours'
                GROUP BY instrument
                HAVING COUNT(*) >= %s
            """ if self._db._pg else """
                SELECT instrument,
                       COUNT(*) as n_trades,
                       SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins
                FROM capital_trades
                WHERE status = 'CLOSED'
                AND datetime(opened_at) >= datetime('now', '-24 hours')
                GROUP BY instrument
                HAVING COUNT(*) >= ?
            """
            cur = self._db._execute(sql, (_MIN_TRADES_TO_EVAL,), fetch=True)
            rows = cur.fetchall()
            results = {}
            for row in rows:
                inst, n, wins = row[0], row[1], row[2] or 0
                wr = wins / n if n > 0 else 0.0
                results[inst] = (wr, n)
            return results
        except Exception as e:
            logger.debug(f"_fetch_wr_from_db: {e}")
            return {}
