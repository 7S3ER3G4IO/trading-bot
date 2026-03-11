"""
bot.py — TradingBot final : combine tous les mixins
"""
from .bot_init import BotInitMixin
from .bot_tick import BotTickMixin
from .bot_signals import BotSignalsMixin
from .bot_monitor import BotMonitorMixin
from .bot_reports import BotReportsMixin
from .bot_commands import BotCommandsMixin


class TradingBot(
    BotInitMixin,
    BotTickMixin,
    BotSignalsMixin,
    BotMonitorMixin,
    BotReportsMixin,
    BotCommandsMixin,
):
    """
    ⚡ NEMESIS v2.0 — Capital.com CFD | London/NY Breakout
    Classe composée via mixins pour modularité.
    """
    pass
