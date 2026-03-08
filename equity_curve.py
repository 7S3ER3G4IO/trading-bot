"""
equity_curve.py — Equity Curve Automatique (#3)

Suit l'évolution de la balance au fil du temps.
Chaque heure : enregistre la balance.
Chaque jour  : génère un graphique + détecte si courbe sous SA MA20.

Fonctionnalités :
  - Graphique d'équité dark mode (PNG) envoyé sur Telegram
  - Auto-pause si équité < MA20 de l'équité (tendance baissière détectée)
  - Métriques : CAGR, Calmar, SQN, Benchmark vs BTC buy&hold
"""
import sys, io, json, os, time, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import datetime, timezone, timedelta
from loguru import logger

EQUITY_FILE = "equity_history.json"


class EquityCurve:

    def __init__(self, initial_balance: float):
        self.initial_balance = initial_balance
        self._history: list  = []   # [{"ts": float, "balance": float}]
        self._load()

    def _load(self):
        if os.path.exists(EQUITY_FILE):
            try:
                with open(EQUITY_FILE) as f:
                    data = json.load(f)
                self._history = data.get("history", [])
                logger.info(f"📈 Equity curve chargée : {len(self._history)} points")
            except Exception:
                self._history = []

    def _save(self):
        try:
            with open(EQUITY_FILE, "w") as f:
                json.dump({"history": self._history[-2000:]}, f)
        except Exception as e:
            logger.debug(f"Equity save: {e}")

    def record(self, balance: float):
        """Enregistre un point d'équité."""
        self._history.append({"ts": time.time(), "balance": balance})
        self._save()

    def _balances(self) -> np.ndarray:
        return np.array([h["balance"] for h in self._history]) if self._history else np.array([self.initial_balance])

    def is_below_ma(self, ma_period: int = 20) -> bool:
        """
        Détecte si la courbe d'équité est sous sa MA.
        Circuit breaker : auto-pause si True.
        """
        bals = self._balances()
        if len(bals) < ma_period:
            return False
        ma  = np.convolve(bals, np.ones(ma_period)/ma_period, mode="valid")
        return float(bals[-1]) < float(ma[-1])

    # ─── Métriques avancées ───────────────────────────────────────────────────

    def cagr(self) -> float:
        """CAGR — Compound Annual Growth Rate."""
        bals = self._balances()
        if len(bals) < 2:
            return 0.0
        start = float(bals[0])
        end   = float(bals[-1])
        days  = (self._history[-1]["ts"] - self._history[0]["ts"]) / 86400
        if days < 1 or start <= 0:
            return 0.0
        years = days / 365.25
        return ((end / start) ** (1 / years) - 1) * 100

    def max_drawdown(self) -> float:
        """Max Drawdown en %."""
        bals = self._balances()
        if len(bals) < 2:
            return 0.0
        peak = np.maximum.accumulate(bals)
        dd   = (peak - bals) / peak * 100
        return float(dd.max())

    def calmar(self) -> float:
        """Calmar Ratio = CAGR / Max DD."""
        dd = self.max_drawdown()
        return self.cagr() / dd if dd > 0 else 0.0

    def sqn(self) -> float:
        """
        SQN (System Quality Number) de Van Tharp.
        SQN = (Mean R / Std R) × sqrt(N_trades)
        > 2.0 = bon, > 3.0 = excellent, > 5.0 = exceptionnel
        """
        bals = self._balances()
        if len(bals) < 10:
            return 0.0
        returns = np.diff(bals) / bals[:-1]
        if returns.std() == 0:
            return 0.0
        return float(returns.mean() / returns.std() * np.sqrt(len(returns)))

    def total_pnl_pct(self) -> float:
        bals = self._balances()
        if len(bals) < 2:
            return 0.0
        return (float(bals[-1]) - float(bals[0])) / float(bals[0]) * 100

    def generate_chart(self, title: str = "AlphaTrader") -> bytes:
        """Génère le graphique d'équité en dark mode. Retourne bytes PNG."""
        bals = self._balances()
        if len(bals) < 2:
            return b""

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7),
                                        facecolor="#1a1a2e",
                                        gridspec_kw={"height_ratios": [3, 1]})
        fig.patch.set_facecolor("#1a1a2e")

        # Timestamps
        if self._history:
            ts = [datetime.fromtimestamp(h["ts"], tz=timezone.utc) for h in self._history]
        else:
            ts = list(range(len(bals)))

        # ─── Equity curve ────────────────────────────────────────────────────
        ax1.set_facecolor("#1a1a2e")
        color = "#00c896" if bals[-1] >= bals[0] else "#ff4560"
        ax1.plot(ts, bals, color=color, linewidth=2, label="Équité")
        ax1.fill_between(ts, bals, bals[0], alpha=0.15, color=color)

        # MA20
        if len(bals) >= 20:
            ma20 = np.convolve(bals, np.ones(20)/20, mode="valid")
            ax1.plot(ts[19:], ma20, color="#f0b429", linewidth=1,
                     linestyle="--", alpha=0.8, label="MA20")

        # Ligne de départ
        ax1.axhline(bals[0], color="#666688", linewidth=1, linestyle=":", alpha=0.5)

        ax1.set_title(f"⚡ {title} — Courbe d'Équité",
                      color="white", fontsize=12, fontweight="bold")
        ax1.tick_params(colors="#9999bb", labelsize=8)
        ax1.set_ylabel("Balance (USDT)", color="#9999bb", fontsize=9)
        ax1.legend(facecolor="#2d2d4e", labelcolor="white", fontsize=8)
        for spine in ax1.spines.values():
            spine.set_edgecolor("#2d2d4e")

        # ─── Drawdown ────────────────────────────────────────────────────────
        ax2.set_facecolor("#1a1a2e")
        peak = np.maximum.accumulate(bals)
        dd   = (peak - bals) / peak * 100
        ax2.fill_between(ts, -dd, 0, color="#ff4560", alpha=0.6)
        ax2.set_ylabel("Drawdown %", color="#9999bb", fontsize=9)
        ax2.tick_params(colors="#9999bb", labelsize=8)
        for spine in ax2.spines.values():
            spine.set_edgecolor("#2d2d4e")

        # ─── Annotations métriques ───────────────────────────────────────────
        metrics = (
            f"PnL: {self.total_pnl_pct():+.1f}%  |  "
            f"CAGR: {self.cagr():+.1f}%  |  "
            f"MaxDD: {self.max_drawdown():.1f}%  |  "
            f"Calmar: {self.calmar():.2f}  |  "
            f"SQN: {self.sqn():.2f}"
        )
        fig.text(0.5, 0.01, metrics, ha="center", fontsize=8,
                 color="#9999bb", fontfamily="monospace")

        fig.text(0.99, 0.01,
                 f"AlphaTrader v2.5 | {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC",
                 ha="right", va="bottom", fontsize=7, color="#666688")

        plt.tight_layout(rect=[0, 0.04, 1, 1])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130, facecolor="#1a1a2e", bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    def format_report(self) -> str:
        """Texte résumé style Station X pour Telegram."""
        bals    = self._balances()
        current = float(bals[-1]) if len(bals) else self.initial_balance
        below   = self.is_below_ma()
        status  = "⚠️ Sous la MA20" if below else "✅ Au-dessus de la MA20"

        return (
            f"📈 <b>EQUITY CURVE</b>\n"
            f"\n"
            f"💰 Capital actuel : <b>{current:,.2f} USDT</b>\n"
            f"📊 PnL total : <b>{self.total_pnl_pct():+.1f}%</b>\n"
            f"📉 Max Drawdown : <b>{self.max_drawdown():.1f}%</b>\n"
            f"\n"
            f"<b>Métriques avancées</b>\n"
            f"  • CAGR    : {self.cagr():+.1f}% / an\n"
            f"  • Calmar  : {self.calmar():.2f}\n"
            f"  • SQN     : {self.sqn():.2f}\n"
            f"\n"
            f"Tendance : {status}"
        )


if __name__ == "__main__":
    ec = EquityCurve(10_000)
    # Simulation test
    import random
    bal = 10_000.0
    for _ in range(100):
        bal += random.uniform(-50, 80)
        ec.record(bal)
    print(ec.format_report())
    chart = ec.generate_chart()
    with open("/tmp/equity_test.png", "wb") as f:
        f.write(chart)
    print(f"\n  Graphique → /tmp/equity_test.png")
    print(f"  Sous MA20 : {ec.is_below_ma()}")
