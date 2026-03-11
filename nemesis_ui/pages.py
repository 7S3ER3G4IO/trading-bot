"""
nemesis_ui.pages — Page Builder for Telegram Hub
Builds formatted pages for the command center.
"""
try:
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
except ImportError:
    InlineKeyboardMarkup = None
    InlineKeyboardButton = None

from .renderer import NemesisRenderer


def _build_kb(rows):
    """Build InlineKeyboardMarkup from rows, filtering None buttons."""
    if InlineKeyboardMarkup is None:
        return None
    clean = [[b for b in row if b] for row in rows]
    clean = [r for r in clean if r]
    return InlineKeyboardMarkup(clean) if clean else None


class PageBuilder:
    """Generates formatted text + keyboard pages for the Telegram Hub."""

    def __init__(self, bot_ref=None):
        self.bot = bot_ref

    # ── Hub main page ────────────────────────────────────────────────────
    def hub_main(self, balance=0, pnl_today=0, open_positions=0, **kw):
        """Returns (text, InlineKeyboardMarkup) for the main Hub."""
        R = NemesisRenderer
        trend = "📈" if pnl_today >= 0 else "📉"
        text = (
            f"{R.box_header('🏠 NEMESIS HUB')}\n\n"
            f"💰 Balance : <b>{balance:,.2f}€</b>\n"
            f"{trend} PnL Today : <b>{pnl_today:+.2f}€</b>\n"
            f"📋 Positions : <b>{open_positions}</b>\n"
        )
        kb = _build_kb([
            [_btn("📊 Dashboard", "hub_dashboard"),
             _btn("📋 Positions", "hub_positions")],
            [_btn("⚙️ Settings", "hub_settings"),
             _btn("🔄 Refresh", "hub_refresh")],
        ])
        return text, kb

    def dashboard_page(self, **kw):
        """Returns (text, kb) for the dashboard page."""
        R = NemesisRenderer
        text = f"{R.box_header('📊 DASHBOARD')}\n\n<i>Loading data...</i>"
        kb = _build_kb([[_btn("🏠 Back", "hub_main")]])
        return text, kb

    def positions_page(self, positions=None, **kw):
        """Returns (text, kb) for open positions."""
        R = NemesisRenderer
        positions = positions or []
        if not positions:
            body = "<i>No open positions</i>"
        else:
            lines = []
            for p in positions[:10]:
                sym = p.get("symbol", "?")
                pnl = p.get("pnl", 0)
                lines.append(f"  {'🟢' if pnl >= 0 else '🔴'} {sym} : {pnl:+.2f}€")
            body = "\n".join(lines)
        text = f"{R.box_header('📋 OPEN POSITIONS')}\n\n{body}"
        kb = _build_kb([[_btn("🏠 Back", "hub_main")]])
        return text, kb


def _btn(text, callback_data):
    """Safe InlineKeyboardButton creation."""
    if InlineKeyboardButton is None:
        return None
    return InlineKeyboardButton(text=text, callback_data=callback_data)
