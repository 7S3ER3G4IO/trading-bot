"""
trade_journal.py — 🧠 PROJECT PROMETHEUS T1: Cognitive Trade Journal

Enregistre un "Log de Contexte" ultra-détaillé à la clôture de chaque position.
Ce journal est la mémoire à long terme du bot — le fuel de l'auto-amélioration.

Chaque entrée contient:
  - PnL, durée, direction, taille
  - Raison d'entrée (confirmations, score, stratégie)
  - Raison de sortie (TP/SL/manual/hedge/expiry)
  - Contexte marché: ATR, ADX, RSI, régime HMM, spread
  - Contexte AI: sentiment FinBERT, L2 imbalance, mood émotionnel
  - Résultat: win/loss, R-multiple, slippage estimé

Usage:
    self.journal.log_close(instrument, trade_state, exit_reason, context)
    losers = self.journal.get_losers(period_days=1)
"""

import json
import time
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict
from loguru import logger


JOURNAL_DIR = Path(os.environ.get("JOURNAL_DIR", "/tmp/trade_journal"))
MAX_ENTRIES = 5000   # Rolling window


class TradeJournal:
    """
    Cognitive Trade Journal — mémoire à long terme du bot.
    Chaque trade fermé reçoit un enregistrement contextuel complet.
    """

    def __init__(self):
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        self._entries: list[dict] = []
        self._file = JOURNAL_DIR / "journal.json"
        self._load()

    def _load(self):
        """Load journal from disk."""
        if self._file.exists():
            try:
                with open(self._file) as f:
                    self._entries = json.load(f)
                logger.info(f"📓 Trade Journal: {len(self._entries)} entries loaded")
            except Exception as e:
                logger.debug(f"Journal load: {e}")
                self._entries = []

    def _save(self):
        """Persist journal to disk."""
        try:
            # Keep only last MAX_ENTRIES
            if len(self._entries) > MAX_ENTRIES:
                self._entries = self._entries[-MAX_ENTRIES:]
            with open(self._file, "w") as f:
                json.dump(self._entries, f, indent=2, default=str)
        except Exception as e:
            logger.debug(f"Journal save: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    #  LOG CLOSE — enregistre un trade fermé avec tout son contexte
    # ═══════════════════════════════════════════════════════════════════════

    def log_close(
        self,
        instrument: str,
        trade_state: dict,
        exit_reason: str,
        pnl: float,
        context: dict = None,
    ):
        """
        Log a closed trade with full cognitive context.

        Parameters:
            instrument: e.g. "EURUSD"
            trade_state: the full trade dict from capital_trades
            exit_reason: "TP1", "SL", "manual", "hedge", "trailing", "expiry"
            pnl: realized PnL in €
            context: optional dict with extra context {atr, rsi, adx, sentiment, l2, mood}
        """
        ctx = context or {}
        entry_price = trade_state.get("entry", 0)
        sl_price = trade_state.get("sl", 0)
        tp1_price = trade_state.get("tp1", 0)
        direction = trade_state.get("direction", "?")
        size = trade_state.get("size", 0)
        score = trade_state.get("score", 0)
        confirmations = trade_state.get("confirmations", [])
        open_time = trade_state.get("open_time")

        # Duration
        duration_min = 0
        if open_time:
            try:
                if isinstance(open_time, str):
                    open_time = datetime.fromisoformat(open_time)
                duration_min = (datetime.now(timezone.utc) - open_time).total_seconds() / 60
            except Exception:
                pass

        # R-multiple
        risk = abs(entry_price - sl_price) if sl_price else 0
        r_multiple = pnl / (risk * size) if risk > 0 and size > 0 else 0

        entry = {
            "id": f"{instrument}_{int(time.time())}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "instrument": instrument,
            "direction": direction,
            "size": size,
            "entry": entry_price,
            "sl": sl_price,
            "tp1": tp1_price,
            "exit_reason": exit_reason,
            "pnl": round(pnl, 2),
            "r_multiple": round(r_multiple, 2),
            "duration_min": round(duration_min, 1),
            "score": score,
            "confirmations": confirmations[:5],

            # Market context at entry
            "regime_at_entry": trade_state.get("regime", "?"),
            "adx_at_entry": trade_state.get("adx_at_entry", 0),
            "market_regime": trade_state.get("market_regime", "?"),
            "fear_greed": trade_state.get("fear_greed", 0),
            "in_overlap": trade_state.get("in_overlap", False),

            # Context at close
            "atr_at_close": ctx.get("atr", 0),
            "rsi_at_close": ctx.get("rsi", 0),
            "adx_at_close": ctx.get("adx", 0),
            "spread_at_close": ctx.get("spread", 0),

            # AI context
            "sentiment": ctx.get("sentiment", "neutral"),
            "sentiment_score": ctx.get("sentiment_score", 0),
            "l2_imbalance": ctx.get("l2_imbalance", 0),
            "mood": ctx.get("mood", "NEUTRAL"),

            # Meta
            "win": pnl > 0,
            "strategy": trade_state.get("confirmations", ["?"])[0] if confirmations else "?",
        }

        self._entries.append(entry)
        self._save()

        icon = "✅" if pnl > 0 else "❌"
        logger.info(
            f"📓 Journal {icon} {instrument} {direction} | "
            f"PnL={pnl:+.2f}€ R={r_multiple:+.1f}R | "
            f"Exit={exit_reason} | Duration={duration_min:.0f}min | "
            f"Score={score:.2f}"
        )

    # ═══════════════════════════════════════════════════════════════════════
    #  QUERIES — Prometheus uses these to analyze patterns
    # ═══════════════════════════════════════════════════════════════════════

    def get_losers(self, period_days: int = 1) -> list[dict]:
        """Get losing trades from the last N days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=period_days)).isoformat()
        return [
            e for e in self._entries
            if e.get("timestamp", "") >= cutoff and not e.get("win", True)
        ]

    def get_winners(self, period_days: int = 1) -> list[dict]:
        """Get winning trades from the last N days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=period_days)).isoformat()
        return [
            e for e in self._entries
            if e.get("timestamp", "") >= cutoff and e.get("win", False)
        ]

    def get_by_instrument(self, instrument: str, limit: int = 50) -> list[dict]:
        """Get last N trades for a specific instrument."""
        return [e for e in self._entries if e["instrument"] == instrument][-limit:]

    def get_stats(self, period_days: int = 7) -> dict:
        """Aggregate stats for Prometheus analysis."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=period_days)).isoformat()
        recent = [e for e in self._entries if e.get("timestamp", "") >= cutoff]

        if not recent:
            return {"total": 0}

        wins = [e for e in recent if e.get("win")]
        losses = [e for e in recent if not e.get("win")]
        pnl_total = sum(e.get("pnl", 0) for e in recent)

        # Per-instrument breakdown
        by_instrument = defaultdict(list)
        for e in recent:
            by_instrument[e["instrument"]].append(e)

        worst_instruments = sorted(
            by_instrument.items(),
            key=lambda x: sum(e.get("pnl", 0) for e in x[1])
        )[:5]

        # Common exit reasons for losses
        loss_reasons = defaultdict(int)
        for e in losses:
            loss_reasons[e.get("exit_reason", "?")] += 1

        return {
            "total": len(recent),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(recent) if recent else 0,
            "pnl_total": round(pnl_total, 2),
            "avg_r": round(sum(e.get("r_multiple", 0) for e in recent) / len(recent), 2),
            "avg_duration_min": round(sum(e.get("duration_min", 0) for e in recent) / len(recent), 1),
            "worst_instruments": [(i, round(sum(e.get("pnl", 0) for e in trades), 2)) for i, trades in worst_instruments],
            "loss_reasons": dict(loss_reasons),
        }

    @property
    def count(self) -> int:
        return len(self._entries)

    def format_status(self) -> str:
        return f"📓 <b>Trade Journal</b>: {len(self._entries)} entries"
