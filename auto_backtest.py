"""
auto_backtest.py — Backtester Automatique (dimanche 23h UTC)

Utilise les données OHLCV en cache + les indicateurs de la stratégie
pour simuler les signaux sur les 30 dernières bougies et estimer les
performances. Rapport envoyé via Telegram.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict
from loguru import logger


class AutoBacktester:
    """
    Backtester vectorisé simple.
    - Simule les croisements EMA/RSI sur les 30 dernières bougies
    - Calcule Win Rate, RR moyen, PnL simulé
    - Exécute toutes les semaines (dimanche 23h UTC)
    """

    REPORT_WEEKDAY  = 6    # Dimanche (0=lundi)
    REPORT_HOUR_UTC = 23   # 23h00 UTC

    def __init__(self):
        self._last_report_week: int = -1
        self._stats_history: List[dict] = []

    def should_run(self) -> bool:
        now = datetime.now(timezone.utc)
        week = now.isocalendar()[1]
        return (
            now.weekday() == self.REPORT_WEEKDAY
            and now.hour == self.REPORT_HOUR_UTC
            and now.minute < 2
            and week != self._last_report_week
        )

    def run_backtest(self, instruments: list, ohlcv_cache, strategy) -> dict:
        """
        Lance un backtest vectorisé sur tous les instruments.
        Returns: dict avec les stats globales.
        """
        total_trades, total_wins, total_pnl = 0, 0, 0.0
        per_instrument: List[Dict] = []

        for instrument in instruments:
            try:
                df = ohlcv_cache.get(instrument, strategy=strategy)
                if df is None or len(df) < 30:
                    continue
                if "ema_fast" not in df.columns or "ema_slow" not in df.columns:
                    df = strategy.compute_indicators(df)

                wins, losses, pnl = 0, 0, 0.0
                # Simuler les N-30 dernières bougies
                _df = df.iloc[-30:].reset_index(drop=True)
                i = 0
                while i < len(_df) - 3:
                    row = _df.iloc[i]
                    nxt = _df.iloc[i + 1]
                    ema_fast = float(row.get("ema_fast", 0))
                    ema_slow = float(row.get("ema_slow", 0))
                    rsi      = float(row.get("rsi", 50))
                    atr      = float(row.get("atr", 0.001))
                    close    = float(row["close"])

                    # Signal BUY : EMA cross-up + RSI < 60
                    if ema_fast > ema_slow and rsi < 60 and atr > 0:
                        # TP = +1.5×ATR, SL = -1.0×ATR
                        tp   = close + 1.5 * atr
                        sl   = close - 1.0 * atr
                        # Scan les bougies suivantes pour voir si TP ou SL touché
                        hit = False
                        for j in range(i + 1, min(i + 5, len(_df))):
                            h = float(_df.iloc[j]["high"])
                            l = float(_df.iloc[j]["low"])
                            if h >= tp:
                                wins += 1
                                pnl += 1.5 * atr
                                hit = True; break
                            if l <= sl:
                                losses += 1
                                pnl -= 1.0 * atr
                                hit = True; break
                        if not hit:
                            i += 1; continue
                    i += 1

                n = wins + losses
                wr = wins / n if n > 0 else 0.0
                per_instrument.append({
                    "instrument": instrument,
                    "trades":     n,
                    "wins":       wins,
                    "wr_pct":     round(wr * 100, 1),
                    "pnl_sim":    round(pnl, 4),
                })
                total_trades += n
                total_wins   += wins
                total_pnl    += pnl
            except Exception as e:
                logger.debug(f"AutoBacktest {instrument}: {e}")

        global_wr = total_wins / total_trades if total_trades > 0 else 0.0
        result = {
            "week": datetime.now(timezone.utc).isocalendar()[1],
            "total_trades": total_trades,
            "total_wins":   total_wins,
            "global_wr_pct": round(global_wr * 100, 1),
            "pnl_sim":       round(total_pnl, 4),
            "per_instrument": sorted(per_instrument, key=lambda x: -x["wr_pct"])[:5],
        }
        self._stats_history.append(result)
        self._last_report_week = result["week"]
        logger.info(
            f"📊 AutoBacktest semaine {result['week']}: "
            f"{total_trades} trades simulés | WR={result['global_wr_pct']}%"
        )
        return result

    def build_report(self, result: dict) -> str:
        """Formate le rapport de backtest pour Telegram."""
        week = result.get("week", "?")
        n    = result.get("total_trades", 0)
        wr   = result.get("global_wr_pct", 0)
        pnl  = result.get("pnl_sim", 0)
        icon = "✅" if wr >= 50 else "⚠️" if wr >= 40 else "❌"

        lines = [
            f"📊 <b>Backtest Hebdomadaire — Semaine {week}</b>\n",
            f"  Trades simulés : <code>{n}</code>",
            f"  Win Rate       : {icon} <b>{wr:.1f}%</b>",
            f"  PnL simulé     : <code>{pnl:+.4f}</code> (en unitésATR)\n",
        ]

        top5 = result.get("per_instrument", [])
        if top5:
            lines.append("🏆 <b>Top 5 instruments :</b>")
            for inst in top5:
                icon_i = "🟢" if inst["wr_pct"] >= 50 else "🔴"
                lines.append(
                    f"  {icon_i} {inst['instrument']:<10} WR={inst['wr_pct']:.0f}%"
                    f"  ({inst['wins']}/{inst['trades']})"
                )

        strategy_note = (
            "\n✅ <i>Stratégie solide — continuer le trading</i>" if wr >= 50
            else "\n⚠️ <i>Performance limitée — revoir les paramètres</i>" if wr >= 40
            else "\n❌ <i>Stratégie sous-performante — review nécessaire</i>"
        )
        lines.append(strategy_note)
        return "\n".join(lines)
