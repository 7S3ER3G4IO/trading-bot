"""
order_guardian.py — ⚡ Phase 1.2: Order Confirmation + Orphan Position Detector

1. Order Confirmation Loop: After place_order(), verify position exists in broker within 5s
2. Orphan Detector: Periodic scan of broker positions vs internal state — alert on desync
3. Slippage Logger: Record entry price vs fill price for every trade
4. Margin Check: Verify available margin before placing order

Usage:
    guardian = OrderGuardian(capital_client, telegram_router)
    guardian.confirm_order(instrument, deal_ref)
    guardian.scan_orphans(internal_trades)
    guardian.check_margin(required_margin)
"""

import time
from datetime import datetime, timezone
from loguru import logger


class OrderGuardian:
    """Post-trade verification + orphan detection + slippage logging."""

    def __init__(self, capital=None, telegram_router=None, db=None):
        self._capital = capital
        self._router = telegram_router
        self._db = db
        self._slippage_log: list = []  # [{instrument, requested, filled, slip_pips, ts}]

    # ═══════════════════════════════════════════════════════════════════════════
    # 1. ORDER CONFIRMATION LOOP
    # ═══════════════════════════════════════════════════════════════════════════

    def confirm_order(self, instrument: str, deal_ref: str,
                      expected_direction: str = "", timeout: float = 5.0) -> bool:
        """
        After place_order(), verify the position actually exists at the broker.
        Retries for `timeout` seconds. Returns True if confirmed, False if phantom.
        """
        if not self._capital or not self._capital.available:
            return True  # Skip if no broker

        t0 = time.monotonic()
        attempt = 0
        while time.monotonic() - t0 < timeout:
            attempt += 1
            try:
                positions = self._capital.get_open_positions()
                for pos in positions:
                    p = pos.get("position", {})
                    if p.get("dealId") == deal_ref:
                        # Confirmed — log slippage if possible
                        fill_price = p.get("level", 0)
                        if fill_price:
                            self._log_fill(instrument, fill_price, expected_direction)
                        logger.info(
                            f"✅ Order confirmed: {instrument} ref={deal_ref} "
                            f"(attempt {attempt}, {time.monotonic()-t0:.1f}s)"
                        )
                        return True
            except Exception as e:
                logger.debug(f"Confirm order {instrument}: {e}")
            time.sleep(1.0)

        # Not confirmed after timeout
        logger.warning(
            f"⚠️ PHANTOM ORDER: {instrument} ref={deal_ref} — "
            f"not found in broker after {timeout}s"
        )
        self._send_alert(
            "⚠️ <b>PHANTOM ORDER DETECTED</b>\n\n"
            f"📋 Instrument : <b>{instrument}</b>\n"
            f"🔑 DealRef : <code>{deal_ref}</code>\n"
            f"⏱ Timeout : {timeout}s ({attempt} checks)\n\n"
            "L'ordre a été envoyé mais n'apparaît pas chez le broker."
        )
        return False

    # ═══════════════════════════════════════════════════════════════════════════
    # 2. ORPHAN POSITION DETECTOR
    # ═══════════════════════════════════════════════════════════════════════════

    def scan_orphans(self, internal_trades: dict) -> list:
        """
        Compare broker positions vs internal state.
        Returns list of orphan deal_ids found at broker but not tracked internally.
        """
        if not self._capital or not self._capital.available:
            return []

        try:
            broker_positions = self._capital.get_open_positions()
        except Exception as e:
            logger.debug(f"Orphan scan: {e}")
            return []

        # Build set of deal_ids we know about
        known_refs = set()
        for instr, state in internal_trades.items():
            if state is None:
                continue
            for ref in state.get("refs", []):
                if ref:
                    known_refs.add(ref)

        # Find orphans
        orphans = []
        for pos in broker_positions:
            p = pos.get("position", {})
            deal_id = p.get("dealId", "")
            if deal_id and deal_id not in known_refs:
                orphans.append({
                    "dealId": deal_id,
                    "instrument": pos.get("market", {}).get("epic", "?"),
                    "direction": p.get("direction", "?"),
                    "size": p.get("size", 0),
                    "level": p.get("level", 0),
                    "pnl": p.get("upl", 0),
                })

        if orphans:
            instr_list = ", ".join(o["instrument"] for o in orphans)
            logger.warning(
                f"🔍 ORPHAN POSITIONS DETECTED: {len(orphans)} — {instr_list}"
            )
            self._send_alert(
                f"🔍 <b>ORPHAN POSITIONS</b> — {len(orphans)} trouvées\n\n"
                + "\n".join(
                    f"  • <b>{o['instrument']}</b> {o['direction']} "
                    f"size={o['size']} pnl={o.get('pnl', 0):+.2f}€"
                    for o in orphans[:5]
                )
                + "\n\nCes positions existent chez le broker mais ne sont pas "
                "trackées par le bot."
            )

        return orphans

    # ═══════════════════════════════════════════════════════════════════════════
    # 3. SLIPPAGE LOGGER
    # ═══════════════════════════════════════════════════════════════════════════

    def _log_fill(self, instrument: str, fill_price: float,
                  direction: str = ""):
        """Record fill price for slippage analysis."""
        entry = {
            "instrument": instrument,
            "filled": fill_price,
            "direction": direction,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._slippage_log.append(entry)

        # Keep last 500 entries
        if len(self._slippage_log) > 500:
            self._slippage_log = self._slippage_log[-500:]

    def log_slippage(self, instrument: str, requested: float, filled: float):
        """Explicit slippage logging: called with both requested and filled price."""
        from brokers.capital_client import PIP_FACTOR
        pip = PIP_FACTOR.get(instrument, 0.0001)
        slip_pips = round(abs(filled - requested) / pip, 1)

        entry = {
            "instrument": instrument,
            "requested": requested,
            "filled": filled,
            "slip_pips": slip_pips,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._slippage_log.append(entry)

        if slip_pips > 5:
            logger.warning(f"⚠️ SLIPPAGE {instrument}: {slip_pips} pips "
                         f"(req={requested:.5f} fill={filled:.5f})")

        # Keep last 500
        if len(self._slippage_log) > 500:
            self._slippage_log = self._slippage_log[-500:]

    def get_slippage_stats(self) -> dict:
        """Return slippage statistics."""
        if not self._slippage_log:
            return {"count": 0, "avg_pips": 0, "max_pips": 0}
        entries_with_slip = [e for e in self._slippage_log if "slip_pips" in e]
        if not entries_with_slip:
            return {"count": len(self._slippage_log), "avg_pips": 0, "max_pips": 0}
        slips = [e["slip_pips"] for e in entries_with_slip]
        return {
            "count": len(entries_with_slip),
            "avg_pips": round(sum(slips) / len(slips), 2),
            "max_pips": max(slips),
        }

    # ═══════════════════════════════════════════════════════════════════════════
    # 4. MARGIN CHECK PRE-TRADE
    # ═══════════════════════════════════════════════════════════════════════════

    def check_margin(self, instrument: str, size: float,
                     min_margin_pct: float = 0.20) -> bool:
        """
        Verify sufficient margin before placing order.
        min_margin_pct = 20% means we need at least 20% of balance as free margin.
        Returns True if OK to trade.
        """
        if not self._capital or not self._capital.available:
            return True

        try:
            balance = self._capital.get_balance()
            if not balance or balance <= 0:
                return False

            # Get account info for available margin
            try:
                acct = self._capital._session_data
                if acct:
                    deposit = float(acct.get("balance", {}).get("deposit", balance))
                    available = float(acct.get("balance", {}).get("available", balance))
                    pnl = float(acct.get("balance", {}).get("profitLoss", 0))
                    margin_used_pct = 1 - (available / deposit) if deposit > 0 else 0

                    if available / deposit < min_margin_pct:
                        logger.warning(
                            f"⚠️ MARGIN LOW: {instrument} — "
                            f"available={available:.2f}€ ({available/deposit:.0%}) "
                            f"< {min_margin_pct:.0%} minimum"
                        )
                        self._send_alert(
                            f"⚠️ <b>MARGIN INSUFFISANT</b>\n\n"
                            f"📋 {instrument} rejeté pré-trade\n"
                            f"💰 Disponible : <b>{available:.2f}€</b> "
                            f"({available/deposit:.0%})\n"
                            f"🛑 Minimum : {min_margin_pct:.0%}"
                        )
                        return False
            except Exception:
                pass  # Fallback: allow trade if margin check fails

            return True

        except Exception as e:
            logger.debug(f"Margin check {instrument}: {e}")
            return True  # Allow on error (don't block trading)

    # ─── Telegram ─────────────────────────────────────────────────────────────

    def _send_alert(self, text: str):
        if self._router:
            try:
                self._router.send_to("risk", text)
            except Exception as e:
                logger.error(f"Guardian Telegram: {e}")
