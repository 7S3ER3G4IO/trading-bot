"""
shadow_engine.py — Moteur 3 : Shadow Trading (R&D en Temps Réel).

Mode Fantôme: simule des trades avec une Stratégie B (paramètres modifiés)
pendant que le bot principal trade avec la Stratégie A.

- Aucun ordre réel: 100% simulation sur prix live
- Logs dans la table Supabase `shadow_trades`
- Rapport hebdomadaire: Stratégie A vs Stratégie B performance
- Tourne en daemon thread asynchrone

Paramètres Stratégie B (légèrement modifiés vs A):
    - TP multiplier × 1.3 (objectif plus ambitieux)
    - SL multiplier × 0.9 (stop plus serré)
    - Score minimum +0.05 (signal de qualité Plus élevé requis)

Usage:
    se = ShadowEngine(db, capital_client)
    # Enregistrer une entrée fantôme:
    se.on_signal(instrument, direction, entry, sl, tp1, score)
    # La fermeture est automatique (monitor thread)
    # Rapport:
    se.weekly_report() → dict
"""
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
from loguru import logger

# ─── Paramètres Stratégie B ──────────────────────────────────────────────────
_B_TP_MULT   = 1.30    # TP 30% plus loin
_B_SL_MULT   = 0.90    # SL 10% plus serré
_B_MIN_SCORE = 0.50    # Score minimum plus strict (vs 0.45 pour A)
_MONITOR_INTERVAL_S = 30  # Vérifie les trades fantômes toutes les 30s
_MAX_SHADOW_TRADES = 5    # Max positions simultanées en shadow mode


class ShadowEngine:
    """
    Moteur de simulation parallèle (Mode Fantôme).
    Tourne en background, aucun impact sur le bot principal.
    """

    def __init__(self, db=None, capital_client=None):
        self._db      = db
        self._capital = capital_client
        self._lock    = threading.Lock()

        # {instrument: shadow_trade_dict}
        self._open_shadows: Dict[str, dict] = {}

        # Stats live
        self._stats = {
            "A_trades": 0, "A_wins": 0, "A_pnl": 0.0,
            "B_trades": 0, "B_wins": 0, "B_pnl": 0.0,
        }

        # Démarrer le monitor thread
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="shadow_monitor",
        )
        self._monitor_thread.start()
        self._ensure_table()
        logger.info("👻 Shadow Engine démarré (Mode Fantôme actif)")

    # ─── Public API ──────────────────────────────────────────────────────────

    def on_signal(self, instrument: str, direction: str, entry: float,
                   sl: float, tp1: float, score: float,
                   strat_a_size: float = 1.0):
        """
        Enregistre un trade fantôme si le signal passe le filtre Stratégie B.
        Appelé depuis bot_signals après chaque signal validé.
        """
        # Ne pas surcharger la shadow avec trop de positions
        with self._lock:
            if len(self._open_shadows) >= _MAX_SHADOW_TRADES:
                return
            if instrument in self._open_shadows:
                return

        # Filtre Stratégie B: score plus strict
        if score < _B_MIN_SCORE:
            return

        # Calcul SL/TP Stratégie B
        sl_dist = abs(entry - sl)
        tp_dist = abs(tp1 - entry)

        if direction == "BUY":
            b_sl  = round(entry - sl_dist * _B_SL_MULT, 5)
            b_tp1 = round(entry + tp_dist * _B_TP_MULT, 5)
        else:
            b_sl  = round(entry + sl_dist * _B_SL_MULT, 5)
            b_tp1 = round(entry - tp_dist * _B_TP_MULT, 5)

        shadow_trade = {
            "instrument": instrument,
            "direction":  direction,
            "entry":      entry,
            "sl":         b_sl,
            "tp1":        b_tp1,
            "score":      score,
            "open_time":  datetime.now(timezone.utc),
            "status":     "OPEN",
        }

        with self._lock:
            self._open_shadows[instrument] = shadow_trade

        # Sauvegarder en DB async
        self._save_shadow_open_async(shadow_trade)
        logger.debug(
            f"👻 Shadow {instrument} {direction} @ {entry:.5f} "
            f"| B-SL={b_sl:.5f} B-TP={b_tp1:.5f} (score={score:.2f})"
        )

    def on_real_trade_closed(self, instrument: str, real_pnl: float,
                              real_result: str):
        """
        Synchronise la fermeture réelle avec le shadow.
        Appelé depuis bot_monitor quand un vrai trade est fermé.
        """
        self._stats["A_trades"] += 1
        self._stats["A_wins"]   += 1 if real_result == "WIN" else 0
        self._stats["A_pnl"]    += real_pnl

    def weekly_report(self) -> dict:
        """Rapport de performance Stratégie A vs Stratégie B."""
        a = self._stats
        a_wr = a["A_wins"] / a["A_trades"] if a["A_trades"] > 0 else 0
        b_wr = a["B_wins"] / a["B_trades"] if a["B_trades"] > 0 else 0

        return {
            "strategy_A": {
                "trades": a["A_trades"], "wr": f"{a_wr:.1%}",
                "pnl": round(a["A_pnl"], 2),
            },
            "strategy_B": {
                "trades": a["B_trades"], "wr": f"{b_wr:.1%}",
                "pnl": round(a["B_pnl"], 2),
            },
            "winner": "A" if a["A_pnl"] >= a["B_pnl"] else "B",
            "B_edge": round(a["B_pnl"] - a["A_pnl"], 2),
        }

    def format_telegram_report(self) -> str:
        r = self.weekly_report()
        a, b = r["strategy_A"], r["strategy_B"]
        winner = "🏆 Stratégie A" if r["winner"] == "A" else "🏆 Stratégie B (candidat!)"
        edge = r["B_edge"]
        edge_str = f"{edge:+.2f}€"
        return (
            f"👻 <b>Shadow Trading — Rapport Hebdo</b>\n\n"
            f"  🔵 Stratégie A : {a['trades']} trades | WR {a['wr']} | PnL {a['pnl']:+.2f}€\n"
            f"  🟣 Stratégie B : {b['trades']} trades | WR {b['wr']} | PnL {b['pnl']:+.2f}€\n\n"
            f"  {winner}\n"
            f"  Edge B vs A : <b>{edge_str}</b>\n\n"
            f"  <i>Paramètres B: TP×{_B_TP_MULT} | SL×{_B_SL_MULT} | Score>{_B_MIN_SCORE}</i>"
        )

    def stop(self):
        self._running = False

    # ─── Monitor Loop ─────────────────────────────────────────────────────────

    def _monitor_loop(self):
        """Thread daemon: vérifie les prix des trades fantômes toutes les 30s."""
        while self._running:
            try:
                self._check_shadow_trades()
            except Exception as e:
                logger.debug(f"Shadow monitor: {e}")
            time.sleep(_MONITOR_INTERVAL_S)

    def _check_shadow_trades(self):
        """Vérifie si des trades fantômes ont atteint leur SL ou TP."""
        if not self._capital:
            return

        with self._lock:
            open_shadows = dict(self._open_shadows)

        for instrument, trade in open_shadows.items():
            try:
                px = self._capital.get_current_price(instrument)
                if not px:
                    continue

                mid = px.get("mid", 0)
                direction = trade["direction"]
                sl  = trade["sl"]
                tp1 = trade["tp1"]
                entry = trade["entry"]

                # Vérification TP/SL
                hit_tp = (direction == "BUY"  and mid >= tp1) or \
                         (direction == "SELL" and mid <= tp1)
                hit_sl = (direction == "BUY"  and mid <= sl) or \
                         (direction == "SELL" and mid >= sl)

                # Time-stop: 24h max
                age_h = (datetime.now(timezone.utc) - trade["open_time"]).total_seconds() / 3600
                hit_time = age_h >= 24

                if hit_tp or hit_sl or hit_time:
                    result  = "WIN" if hit_tp else ("LOSS" if hit_sl else "TIME")
                    pnl_dir = 1 if direction == "BUY" else -1
                    pnl_est = round((mid - entry) * pnl_dir, 4)

                    self._close_shadow(instrument, result, mid, pnl_est)

            except Exception as e:
                logger.debug(f"Shadow check {instrument}: {e}")

    def _close_shadow(self, instrument: str, result: str,
                       close_px: float, pnl: float):
        """Ferme un trade fantôme et met à jour les stats."""
        with self._lock:
            trade = self._open_shadows.pop(instrument, None)
        if not trade:
            return

        self._stats["B_trades"] += 1
        self._stats["B_wins"]   += 1 if result == "WIN" else 0
        self._stats["B_pnl"]    += pnl

        logger.debug(
            f"👻 Shadow CLOSE {instrument} | {result} | PnL={pnl:+.4f} | close={close_px:.5f}"
        )
        self._save_shadow_close_async(trade, result, close_px, pnl)

    # ─── DB Persistence ───────────────────────────────────────────────────────

    def _ensure_table(self):
        """Crée la table shadow_trades si elle n'existe pas."""
        if not self._db:
            return
        try:
            if self._db._pg:
                self._db._execute("""
                    CREATE TABLE IF NOT EXISTS shadow_trades (
                        id         SERIAL PRIMARY KEY,
                        instrument VARCHAR(20) NOT NULL,
                        direction  VARCHAR(4),
                        entry      DOUBLE PRECISION,
                        sl         DOUBLE PRECISION,
                        tp1        DOUBLE PRECISION,
                        score      DOUBLE PRECISION DEFAULT 0,
                        open_time  TIMESTAMPTZ DEFAULT NOW(),
                        close_time TIMESTAMPTZ,
                        close_px   DOUBLE PRECISION,
                        pnl        DOUBLE PRECISION DEFAULT 0,
                        result     VARCHAR(10),
                        status     VARCHAR(10) DEFAULT 'OPEN'
                    )
                """)
            else:
                self._db._execute("""
                    CREATE TABLE IF NOT EXISTS shadow_trades (
                        id        INTEGER PRIMARY KEY AUTOINCREMENT,
                        instrument TEXT NOT NULL,
                        direction TEXT,
                        entry     REAL,
                        sl        REAL,
                        tp1       REAL,
                        score     REAL DEFAULT 0,
                        open_time TEXT,
                        close_time TEXT,
                        close_px  REAL,
                        pnl       REAL DEFAULT 0,
                        result    TEXT,
                        status    TEXT DEFAULT 'OPEN'
                    )
                """)
        except Exception as e:
            logger.debug(f"Shadow table: {e}")

    def _save_shadow_open_async(self, trade: dict):
        if not self._db:
            return
        self._db.async_write(self._save_shadow_open_sync, trade)

    def _save_shadow_open_sync(self, trade: dict):
        try:
            ph = "%s" if self._db._pg else "?"
            self._db._execute(
                f"""INSERT INTO shadow_trades
                    (instrument, direction, entry, sl, tp1, score, open_time, status)
                    VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},'OPEN')""",
                (trade["instrument"], trade["direction"], trade["entry"],
                 trade["sl"], trade["tp1"], trade["score"],
                 trade["open_time"].isoformat())
            )
        except Exception as e:
            logger.debug(f"Shadow save open: {e}")

    def _save_shadow_close_async(self, trade: dict, result: str,
                                   close_px: float, pnl: float):
        if not self._db:
            return
        self._db.async_write(
            self._save_shadow_close_sync, trade, result, close_px, pnl
        )

    def _save_shadow_close_sync(self, trade: dict, result: str,
                                  close_px: float, pnl: float):
        try:
            ph = "%s" if self._db._pg else "?"
            self._db._execute(
                f"""UPDATE shadow_trades SET
                    status='CLOSED', result={ph}, close_px={ph},
                    pnl={ph}, close_time={ph}
                    WHERE instrument={ph} AND status='OPEN'""",
                (result, close_px, pnl,
                 datetime.now(timezone.utc).isoformat(),
                 trade["instrument"])
            )
        except Exception as e:
            logger.debug(f"Shadow save close: {e}")
