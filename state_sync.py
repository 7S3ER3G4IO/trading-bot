"""
state_sync.py — ⚡ Tâche 1: Orphan Position Detector & State Reconciliation

Toutes les 5 minutes, compare les positions chez Capital.com avec l'état interne.
Si une position fantôme est détectée (chez le broker mais pas en mémoire):
  → L'importe dans capital_trades avec un Safety SL (2× ATR ou 2% fallback)
  → Alerte Risk Telegram

Si une position locale n'existe plus chez le broker:
  → La marque comme fermée en mémoire

Usage:
    sync = StateSync(capital, db, telegram_router)
    sync.reconcile(capital_trades)  # Appelé toutes les 5 min depuis bot_tick
"""

from datetime import datetime, timezone
from loguru import logger


# Safety SL fallback: 2% du prix d'entrée
SAFETY_SL_PCT = 0.02


class StateSync:
    """Reconciliation broker ↔ état interne toutes les 5 minutes."""

    def __init__(self, capital=None, db=None, telegram_router=None):
        self._capital = capital
        self._db = db
        self._router = telegram_router
        self._last_sync = datetime.now(timezone.utc)
        self._orphan_count = 0
        self._ghost_count = 0

    def reconcile(self, capital_trades: dict) -> dict:
        """
        Réconcilie les positions broker vs état local.

        Parameters
        ----------
        capital_trades : dict
            {instrument: state_dict_or_None} — état interne du bot

        Returns
        -------
        dict with orphans_imported, ghosts_closed, broker_positions
        """
        if not self._capital or not self._capital.available:
            return {"status": "skip", "reason": "broker unavailable"}

        try:
            broker_positions = self._capital.get_open_positions()
        except Exception as e:
            logger.error(f"StateSync: get_open_positions failed: {e}")
            return {"status": "error", "reason": str(e)}

        # ─── Build broker state ───────────────────────────────────────────
        broker_epics = {}  # epic → {deal_id, direction, entry, sl, size}
        for pos in broker_positions:
            p = pos.get("position", {})
            m = pos.get("market", {})
            epic = m.get("epic", "")
            if not epic:
                continue

            deal_id   = p.get("dealId", "")
            direction = p.get("direction", "BUY")
            entry     = float(p.get("level", 0))
            sl        = float(p.get("stopLevel", 0))
            tp        = float(p.get("limitLevel", 0))
            size      = float(p.get("size", 0))

            # Keep first position per epic (Capital.com can have multiples)
            if epic not in broker_epics:
                broker_epics[epic] = {
                    "deal_id": deal_id,
                    "direction": direction,
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "size": size,
                }

        orphans_imported = []
        ghosts_closed = []

        # ─── Detect ORPHANS: at broker but NOT in memory ─────────────────
        for epic, broker_state in broker_epics.items():
            local_state = capital_trades.get(epic)
            if local_state is not None:
                continue  # Known position — OK

            # ORPHAN DETECTED
            logger.warning(
                f"👻 ORPHAN DETECTED: {epic} {broker_state['direction']} "
                f"@ {broker_state['entry']} — NOT in bot memory!"
            )

            # Compute safety SL if broker has none
            entry = broker_state["entry"]
            sl = broker_state["sl"]
            direction = broker_state["direction"]

            if sl == 0 and entry > 0:
                if direction == "BUY":
                    sl = round(entry * (1 - SAFETY_SL_PCT), 5)
                else:
                    sl = round(entry * (1 + SAFETY_SL_PCT), 5)
                logger.warning(
                    f"🛡️ Safety SL attached to orphan {epic}: {sl} "
                    f"({SAFETY_SL_PCT:.0%} from entry)"
                )
                # Attach SL at broker
                self._attach_safety_sl(epic, broker_state["deal_id"], sl)

            # Import into bot memory
            tp = broker_state.get("tp", 0) or entry
            capital_trades[epic] = {
                "refs":          [broker_state["deal_id"], None, None],
                "entry":         entry,
                "sl":            sl,
                "tp1":           tp,
                "tp2":           tp,
                "tp3":           tp,
                "direction":     direction,
                "tp1_hit":       False,
                "tp2_hit":       False,
                "score":         0,
                "confirmations": ["ORPHAN_IMPORT"],
                "regime":        "UNKNOWN",
                "fear_greed":    None,
                "in_overlap":    False,
                "adx_at_entry":  0,
                "open_time":     datetime.now(timezone.utc),
                "ab_variant":    "A",
                "_orphan":       True,
            }

            orphans_imported.append(epic)
            self._orphan_count += 1

            # Alert
            self._send_alert(
                f"👻 <b>ORPHAN IMPORTÉ</b>\n\n"
                f"📊 {epic} | {direction}\n"
                f"💰 Entry: <code>{entry}</code>\n"
                f"🛑 Safety SL: <code>{sl}</code>\n"
                f"📦 Size: {broker_state['size']}\n\n"
                f"⚠️ Position inconnue importée en mémoire avec SL de sécurité."
            )

        # ─── Detect GHOSTS: in memory but NOT at broker ──────────────────
        for epic, local_state in capital_trades.items():
            if local_state is None:
                continue
            if epic not in broker_epics:
                # Ghost: local thinks it's open, but broker closed it
                logger.warning(
                    f"💀 GHOST DETECTED: {epic} {local_state.get('direction')} "
                    f"@ {local_state.get('entry')} — CLOSED at broker but OPEN in memory!"
                )
                ghosts_closed.append(epic)
                self._ghost_count += 1

                self._send_alert(
                    f"💀 <b>GHOST NETTOYÉ</b>\n\n"
                    f"📊 {epic} | {local_state.get('direction')}\n"
                    f"💰 Entry: <code>{local_state.get('entry')}</code>\n\n"
                    f"⚠️ Position fermée chez le broker mais encore en mémoire → nettoyée."
                )

        # Clean ghosts from memory
        for epic in ghosts_closed:
            capital_trades[epic] = None

        self._last_sync = datetime.now(timezone.utc)

        result = {
            "status": "ok",
            "broker_count": len(broker_epics),
            "local_count": sum(1 for v in capital_trades.values() if v is not None),
            "orphans_imported": orphans_imported,
            "ghosts_closed": ghosts_closed,
        }

        if orphans_imported or ghosts_closed:
            logger.warning(
                f"🔄 StateSync: {len(orphans_imported)} orphan(s) imported, "
                f"{len(ghosts_closed)} ghost(s) cleaned"
            )
        else:
            logger.debug(
                f"✅ StateSync: {result['broker_count']} broker / "
                f"{result['local_count']} local — synced"
            )

        return result

    def _attach_safety_sl(self, epic: str, deal_id: str, sl: float):
        """Attach a safety SL to an orphan position at the broker."""
        if not self._capital:
            return
        try:
            self._capital.amend_position(deal_id, stop_level=sl)
            logger.info(f"🛡️ Safety SL set on {epic} deal={deal_id}: {sl}")
        except Exception as e:
            logger.error(f"❌ Failed to attach safety SL on {epic}: {e}")

    def _send_alert(self, text: str):
        if self._router:
            try:
                self._router.send_to("risk", text)
            except Exception as e:
                logger.error(f"StateSync Telegram: {e}")

    @property
    def stats(self) -> dict:
        return {
            "last_sync": self._last_sync.isoformat(),
            "total_orphans": self._orphan_count,
            "total_ghosts": self._ghost_count,
        }
