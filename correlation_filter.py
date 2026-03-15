"""
correlation_filter.py — Filtre de Corrélation entre Instruments
Bloque l'ouverture d'une nouvelle position si un instrument corrélé est déjà ouvert.

Corrélations fixes (empiriques) :
- EURUSD ↔ GBPUSD ↔ AUDUSD ↔ NZDUSD  (long USD side)
- USDJPY ↔ USDCHF ↔ USDCAD             (short USD side)
- GOLD ↔ SILVER                         (precious metals)
- US500 ↔ US100 ↔ US30                  (US indices)

Usage dans bot_signals.py :
    from correlation_filter import CorrelationFilter
    cf = CorrelationFilter(max_correlated=2)
    can_open, reason = cf.can_open(instrument, open_positions)
"""
from loguru import logger

# ─── Groupes de corrélation (± 0.7 historique) ───────────────────────────────

CORRELATION_GROUPS = {
    # Forex USD longs (même exposition USD courte)
    "usd_long": {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "EURJPY", "GBPJPY", "AUDJPY"},
    # Forex USD courts (même exposition USD longue)
    "usd_short": {"USDJPY", "USDCHF", "USDCAD"},
    # Métaux précieux
    "metals": {"GOLD", "SILVER", "XAUUSD", "XAGUSD"},
    # Indices US
    "us_indices": {"US500", "US100", "US30"},
    # Indices EUR
    "eur_indices": {"DE40", "FR40", "UK100"},
}

# Poids maximum de corrélation (1 = un seul trade par groupe)
DEFAULT_MAX_PER_GROUP = 2  # max 2 trades corrélés simultanément


def _get_groups(instrument: str) -> list:
    """Retourne les groupes de corrélation d'un instrument."""
    sym = instrument.upper()
    groups = []
    for group_name, members in CORRELATION_GROUPS.items():
        if sym in members:
            groups.append(group_name)
    return groups


class CorrelationFilter:
    """Limite l'exposition à des instruments corrélés."""

    def __init__(self, max_per_group: int = DEFAULT_MAX_PER_GROUP):
        self.max_per_group = max_per_group

    def can_open(self, instrument: str, positions: dict) -> tuple[bool, str]:
        """
        Vérifie si on peut ouvrir une position sur `instrument` 
        compte tenu des positions actuellement ouvertes.
        
        Returns:
            (True, "") si OK
            (False, reason) si bloqué par corrélation
        """
        groups = _get_groups(instrument)
        if not groups:
            return True, ""  # Instrument non corrélé → libre

        # Instruments actuellement ouverts
        open_instruments = {
            sym.upper()
            for sym, state in positions.items()
            if state is not None
        }

        for group_name in groups:
            group_members = CORRELATION_GROUPS[group_name]
            # Nombre d'instruments de ce groupe déjà ouverts
            open_in_group = group_members & open_instruments
            count = len(open_in_group)

            if count >= self.max_per_group:
                blocking = ", ".join(sorted(open_in_group))
                reason = (
                    f"Corrélation [{group_name}]: "
                    f"{count}/{self.max_per_group} déjà ouverts ({blocking})"
                )
                logger.debug(f"🚫 CorrelationFilter {instrument}: {reason}")
                return False, reason

        return True, ""

    def same_direction_check(
        self,
        instrument: str,
        direction: str,
        positions: dict,
        max_same_direction: int = 3,
    ) -> tuple[bool, str]:
        """
        Bloque si trop de trades dans la même direction (directionnel bias).
        Ex: max 3 BUY simultanés sur l'ensemble du portfolio.
        """
        open_directions = [
            state.get("direction", "")
            for state in positions.values()
            if state is not None
        ]
        same_count = open_directions.count(direction.upper())
        if same_count >= max_same_direction:
            reason = f"Trop de {direction} simultanés ({same_count}/{max_same_direction})"
            logger.debug(f"🚫 CorrelationFilter {instrument}: {reason}")
            return False, reason
        return True, ""

    def currency_exposure(
        self,
        instrument: str,
        direction: str,
        positions: dict,
        max_currency_exposure: int = 3,
    ) -> tuple[bool, str]:
        """
        Compte l'exposition nette à une devise.
        Ex: limit EUR exposure (EURUSD BUY + EURCHF BUY + EURJPY BUY = 3 EUR longs)
        """
        sym = instrument.upper()
        # Extraire les 2 devises du symbole
        if len(sym) < 6:
            return True, ""

        base_ccy = sym[:3]   # ex: EUR dans EURUSD
        quote_ccy = sym[3:6] # ex: USD dans EURUSD

        target_ccy = base_ccy if direction.upper() == "BUY" else quote_ccy

        # Compter l'exposition existante
        exposure = 0
        for sym_open, state in positions.items():
            if state is None:
                continue
            s = sym_open.upper()
            if len(s) < 6:
                continue
            d = state.get("direction", "")
            b, q = s[:3], s[3:6]
            if (b == target_ccy and d == "BUY") or (q == target_ccy and d == "SELL"):
                exposure += 1
            elif (q == target_ccy and d == "BUY") or (b == target_ccy and d == "SELL"):
                exposure += 1  # comptage simplifié

        if exposure >= max_currency_exposure:
            reason = f"Exposition {target_ccy} trop élevée ({exposure}/{max_currency_exposure})"
            logger.debug(f"🚫 CorrelationFilter {instrument}: {reason}")
            return False, reason

        return True, ""

    def format_status(self, positions: dict) -> str:
        """Résumé des groupes de corrélation actuels."""
        lines = ["🔗 Corrélations actives:"]
        for group_name, members in CORRELATION_GROUPS.items():
            open_in = [s for s in positions if positions[s] and s.upper() in members]
            if open_in:
                lines.append(f"  {group_name}: {', '.join(open_in)}")
        return "\n".join(lines) if len(lines) > 1 else "🔗 Aucune corrélation active"
