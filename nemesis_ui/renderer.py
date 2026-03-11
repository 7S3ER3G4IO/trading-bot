"""
renderer.py — Nemesis Design System
Primitives visuelles unifiées pour tous les messages Telegram.
"""
from datetime import datetime, timezone


class NemesisRenderer:
    """Design system : boxes, sparklines, barres, formatters."""

    # ── Box Header ────────────────────────────────────────────────────────────
    @staticmethod
    def box_header(title: str) -> str:
        """Encadré premium pour les headers de section."""
        width = max(len(title) + 4, 29)
        top = "┌" + "─" * width + "┐"
        bot = "└" + "─" * width + "┘"
        inner = "│  " + title.ljust(width - 3) + "│"
        return f"{top}\n{inner}\n{bot}"

    # ── Sparkline ─────────────────────────────────────────────────────────────
    @staticmethod
    def sparkline(values: list, width: int = 8) -> str:
        """Mini-graphique Unicode. Ex: ▁▂▃▅▇█▇▅"""
        if not values or len(values) < 2:
            return "—"
        bars = "▁▂▃▄▅▆▇█"
        mn = min(values)
        mx = max(values)
        rng = mx - mn
        if rng == 0:
            return bars[4] * min(len(values), width)
        # Resample to width
        if len(values) > width:
            step = len(values) / width
            sampled = [values[int(i * step)] for i in range(width)]
        else:
            sampled = values
        return "".join(bars[min(int((v - mn) / rng * 7), 7)] for v in sampled)

    # ── Progress Bar (SL → TP) ────────────────────────────────────────────────
    @staticmethod
    def progress_bar(current: float, sl: float, tp: float, width: int = 10) -> str:
        """Barre de progression SL→TP. Ex: SL░░░●▓▓▓TP"""
        try:
            total = abs(tp - sl)
            if total == 0:
                return ""
            progress = abs(current - sl) / total
            progress = max(0.0, min(1.0, progress))
            pos = int(progress * width)
            bar = "░" * pos + "●" + "▓" * (width - pos)
            return f"SL{bar}TP"
        except Exception:
            return ""

    # ── Score Bar ─────────────────────────────────────────────────────────────
    @staticmethod
    def score_bar(score: int, max_score: int = 3) -> str:
        """Score visuel. Ex: ▰▰▱ pour 2/3"""
        filled = min(score, max_score)
        return "▰" * filled + "▱" * (max_score - filled)

    # ── Win Rate Bar ──────────────────────────────────────────────────────────
    @staticmethod
    def wr_bar(value: float, max_val: float = 100, width: int = 10) -> str:
        """Barre de progression remplie. Ex: ████░░░░░░ 65%"""
        filled = int((value / max_val) * width) if max_val > 0 else 0
        filled = min(filled, width)
        return "█" * filled + "░" * (width - filled)

    # ── PnL Format ────────────────────────────────────────────────────────────
    @staticmethod
    def format_pnl(amount: float, currency: str = "€") -> str:
        """Formate le PnL avec signe. Ex: +42.50€ ou -15.00€"""
        return f"{amount:+.2f}{currency}"

    # ── R:R Format ────────────────────────────────────────────────────────────
    @staticmethod
    def format_rr(entry: float, sl: float, tp: float) -> str:
        """Calcule et formate le Risk:Reward. Ex: R:R 2.4x"""
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk == 0:
            return "R:R —"
        rr = reward / risk
        return f"R:R {rr:.1f}x"

    # ── Session Progress ──────────────────────────────────────────────────────
    @staticmethod
    def session_progress(start_h: int, end_h: int, width: int = 10) -> str:
        """Barre de progression de la session. Ex: ██████░░░░"""
        h = datetime.now(timezone.utc).hour
        if h < start_h or h >= end_h:
            return "░" * width
        total = end_h - start_h
        elapsed = h - start_h
        filled = int((elapsed / total) * width)
        filled = min(filled, width)
        return "█" * filled + "░" * (width - filled)

    # ── Streak Format ─────────────────────────────────────────────────────────
    @staticmethod
    def format_streak(win_streak: int) -> str:
        """Affiche le streak. Ex: 🔥 5 wins"""
        if win_streak <= 0:
            return ""
        fire = "🔥" * min(win_streak, 3)
        return f"{fire} {win_streak} wins"

    # ── Timestamp ─────────────────────────────────────────────────────────────
    @staticmethod
    def utc_time() -> str:
        return datetime.now(timezone.utc).strftime("%H:%M UTC")

    @staticmethod
    def date_label() -> str:
        months = ["Jan", "Fév", "Mar", "Avr", "Mai", "Juin",
                  "Juil", "Août", "Sep", "Oct", "Nov", "Déc"]
        d = datetime.now(timezone.utc)
        return f"{d.day} {months[d.month - 1]}"

    @staticmethod
    def session_name() -> str:
        h = datetime.now(timezone.utc).hour
        if 7 <= h < 11:
            return "London 🇬🇧"
        if 13 <= h < 17:
            return "New York 🗽"
        return "Hors session 🌙"

    # ── Separator ─────────────────────────────────────────────────────────────
    SEP = "━━━━━━━━━━━━━━━━━━━━━━━"
