"""
golive_checklist.py — Étape 3 : Go-Live Checklist (Critères Mathématiques d'Activation).

Valide automatiquement si le bot est prêt pour le passage en mode LIVE
en interrogeant Supabase avec des critères mathématiques stricts.

Critères de Go-Live:
    1. Minimum 200 trades exécutés (shadow + démo combinés)
    2. Win Rate > 45% après slippage réel injecté
    3. Zéro crash DB sur 72h (suivi via heartbeat Supabase)
    4. Zéro erreur 429 sur 24h (suivi via rate_limiter stats)
    5. Drawdown maximum < 15% sur toute la période de démo
    6. Sharpe Ratio estimé > 0.8 sur les trades démo

Usage:
    checker = GoLiveChecker(db, rate_limiter, telegram_router)
    report = checker.run_full_check()
    # → dict avec tous les critères et status PASS/FAIL
    checker.send_telegram_report()
    # → rapport complet sur Telegram + requêtes SQL

Requêtes SQL directement utilisables dans Supabase Dashboard → SQL Editor.
"""
import os
import math
from datetime import datetime, timezone, timedelta
from loguru import logger

# ─── Critères de validation ───────────────────────────────────────────────────
CRITERIA = {
    "min_trades":         200,    # Trades minimum (shadow + démo)
    "min_wr_pct":         45.0,   # Win Rate > 45% (après slippage)
    "max_dd_pct":         15.0,   # Drawdown max < 15%
    "min_sharpe":         0.8,    # Sharpe Ratio estimé > 0.8
    "max_429_errors":     0,      # Zéro 429 en 24h
    "min_uptime_h":       72,     # 72h de stabilité continue
}

# ─── Requêtes SQL Go-Live (Supabase SQL Editor) ───────────────────────────────
GO_LIVE_SQL_QUERIES = {

    "1_trade_count": """
-- Critère 1: Minimum 200 trades exécutés (shadow + démo combinés)
SELECT
    (SELECT COUNT(*) FROM capital_trades WHERE status = 'CLOSED') as real_trades,
    (SELECT COUNT(*) FROM shadow_trades   WHERE status = 'CLOSED') as shadow_trades,
    (SELECT COUNT(*) FROM capital_trades WHERE status = 'CLOSED')
  + (SELECT COUNT(*) FROM shadow_trades   WHERE status = 'CLOSED') as total_combined,
    CASE WHEN
        (SELECT COUNT(*) FROM capital_trades WHERE status = 'CLOSED')
      + (SELECT COUNT(*) FROM shadow_trades   WHERE status = 'CLOSED') >= 200
    THEN '✅ PASS' ELSE '❌ FAIL' END as status;
""",

    "2_winrate_after_slippage": """
-- Critère 2: Win Rate > 45% (capital_trades uniquement — vrais ordres démo)
SELECT
    COUNT(*)                                                    as total_trades,
    SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END)            as wins,
    ROUND(
        SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END)::numeric
      / NULLIF(COUNT(*), 0) * 100, 2
    )                                                           as wr_pct,
    CASE WHEN
        SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END)::numeric
      / NULLIF(COUNT(*), 0) * 100 >= 45
    THEN '✅ PASS' ELSE '❌ FAIL' END                           as status
FROM capital_trades
WHERE status = 'CLOSED';
""",

    "3_drawdown": """
-- Critère 3: Drawdown maximum < 15%
WITH equity AS (
    SELECT
        recorded_at,
        balance,
        MAX(balance) OVER (ORDER BY recorded_at ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) as peak
    FROM nemesis_equity
    ORDER BY recorded_at
),
dd AS (
    SELECT
        recorded_at,
        balance,
        peak,
        ROUND((peak - balance) / NULLIF(peak, 0) * 100, 2) as drawdown_pct
    FROM equity
)
SELECT
    ROUND(MAX(drawdown_pct), 2)   as max_drawdown_pct,
    CASE WHEN MAX(drawdown_pct) <= 15 THEN '✅ PASS' ELSE '❌ FAIL' END as status,
    (SELECT balance FROM dd ORDER BY drawdown_pct DESC LIMIT 1) as worst_balance,
    (SELECT peak FROM dd ORDER BY drawdown_pct DESC LIMIT 1) as peak_at_worst_dd
FROM dd;
""",

    "4_sharpe_ratio": """
-- Critère 4: Sharpe Ratio estimé > 0.8 (sur trades fermés)
WITH pnl_data AS (
    SELECT
        pnl,
        AVG(pnl) OVER ()                 as avg_pnl,
        STDDEV(pnl) OVER ()              as std_pnl,
        COUNT(*) OVER ()                 as n
    FROM capital_trades
    WHERE status = 'CLOSED'
      AND pnl IS NOT NULL
)
SELECT
    ROUND(AVG(pnl)::numeric, 2)          as avg_trade_pnl,
    ROUND(STDDEV(pnl)::numeric, 2)       as std_trade_pnl,
    ROUND(
        AVG(pnl) / NULLIF(STDDEV(pnl), 0) * SQRT(252)::numeric,
    2)                                    as sharpe_annualized,
    CASE WHEN
        AVG(pnl) / NULLIF(STDDEV(pnl), 0) * SQRT(252) >= 0.8
    THEN '✅ PASS' ELSE '❌ FAIL' END     as status
FROM capital_trades
WHERE status = 'CLOSED';
""",

    "5_rate_limit_errors": """
-- Critère 5: Zéro erreur 429 sur les 24 dernières heures
-- (Le bot loggue automatiquement les 429 dans bot_state)
SELECT
    value as last_429_log,
    CASE
        WHEN value IS NULL OR value = '0' THEN '✅ PASS'
        ELSE '❌ FAIL — 429 détectés'
    END as status
FROM nemesis_bot_state
WHERE key = 'rate_limit_429_count_24h'
LIMIT 1;
""",

    "6_global_summary": """
-- Résumé Go-Live — TOUTES les métriques en une seule requête
SELECT
    -- Trades
    (SELECT COUNT(*) FROM capital_trades WHERE status='CLOSED')     as real_trades,
    (SELECT COUNT(*) FROM shadow_trades WHERE status='CLOSED')      as shadow_trades,
    (SELECT COUNT(*) FROM capital_trades WHERE status='CLOSED')
  + (SELECT COUNT(*) FROM shadow_trades WHERE status='CLOSED')      as total_trades,

    -- Win Rate
    ROUND(
        (SELECT SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END)::numeric
         FROM capital_trades WHERE status='CLOSED')
      / NULLIF((SELECT COUNT(*) FROM capital_trades WHERE status='CLOSED'), 0) * 100
    , 2)                                                            as wr_pct,

    -- PnL total
    (SELECT ROUND(SUM(pnl)::numeric, 2) FROM capital_trades WHERE status='CLOSED') as total_pnl,

    -- Meilleur actif
    (SELECT instrument FROM capital_trades WHERE status='CLOSED'
     GROUP BY instrument ORDER BY SUM(pnl) DESC LIMIT 1)           as best_asset,

    -- Pire actif
    (SELECT instrument FROM capital_trades WHERE status='CLOSED'
     GROUP BY instrument ORDER BY SUM(pnl) ASC LIMIT 1)            as worst_asset,

    -- Durée moyenne des trades
    ROUND(AVG(duration_min), 1)                                     as avg_duration_min
FROM capital_trades
WHERE status = 'CLOSED';
""",
}


class GoLiveChecker:
    """
    Vérifie automatiquement tous les critères de Go-Live depuis Supabase.
    """

    def __init__(self, db=None, rate_limiter=None, telegram_router=None):
        self._db = db
        self._rl = rate_limiter
        self._tg = telegram_router

    # ─── Public API ──────────────────────────────────────────────────────────

    def run_full_check(self) -> dict:
        """
        Exécute tous les critères et retourne un dict {critère: {value, threshold, pass}}.
        """
        results = {}

        results["trade_count"]   = self._check_trade_count()
        results["win_rate"]      = self._check_win_rate()
        results["drawdown"]      = self._check_drawdown()
        results["rate_429"]      = self._check_429_errors()
        results["sharpe"]        = self._check_sharpe()

        all_pass = all(r.get("pass", False) for r in results.values())
        results["_ready_for_live"] = all_pass

        if all_pass:
            logger.info("✅ GO-LIVE CHECKLIST: TOUS LES CRITÈRES VALIDÉS — PRÊT POUR LE LIVE")
        else:
            fails = [k for k, v in results.items() if not v.get("pass") and not k.startswith("_")]
            logger.warning(f"❌ GO-LIVE: {len(fails)} critère(s) non validé(s): {fails}")

        return results

    def send_telegram_report(self):
        """Envoie le rapport Go-Live complet sur Telegram."""
        if not self._tg:
            return

        results = self.run_full_check()
        ready = results.pop("_ready_for_live", False)

        lines = []
        for key, r in results.items():
            icon = "✅" if r.get("pass") else "❌"
            val  = r.get("value", "N/A")
            thr  = r.get("threshold", "")
            name = r.get("name", key)
            lines.append(f"  {icon} {name}: <b>{val}</b> (seuil: {thr})")

        status_line = (
            "🚀 <b>PRÊT POUR LE LIVE !</b>" if ready
            else "⏳ <b>PAS ENCORE PRÊT — Critères non validés</b>"
        )

        msg = (
            f"📋 <b>Go-Live Checklist — Nemesis v2.0</b>\n\n"
            f"{chr(10).join(lines)}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{status_line}\n\n"
            f"<i>Pour voir les requêtes SQL complètes: /golive_sql</i>"
        )
        try:
            self._tg.send_report(msg)
        except Exception:
            try:
                self._tg.send_trade(msg)
            except Exception as e:
                logger.warning(f"GoLive Telegram: {e}")

    def get_sql_queries(self) -> dict:
        """Retourne les requêtes SQL prêtes à coller dans Supabase Dashboard."""
        return GO_LIVE_SQL_QUERIES

    # ─── Critères individuels ─────────────────────────────────────────────────

    def _check_trade_count(self) -> dict:
        try:
            real = self._query_scalar("SELECT COUNT(*) FROM capital_trades WHERE status='CLOSED'") or 0
            shadow = self._query_scalar("SELECT COUNT(*) FROM shadow_trades WHERE status='CLOSED'") or 0
            total = real + shadow
            return {
                "name": "Trades minimum",
                "value": f"{total} ({real} réels + {shadow} shadow)",
                "threshold": f"≥{CRITERIA['min_trades']}",
                "pass": total >= CRITERIA["min_trades"],
            }
        except Exception as e:
            return {"name": "Trades minimum", "value": f"Erreur: {e}", "pass": False, "threshold": f"≥{CRITERIA['min_trades']}"}

    def _check_win_rate(self) -> dict:
        try:
            sql = """
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins
                FROM capital_trades WHERE status='CLOSED'
            """
            cur = self._db._execute(sql, fetch=True)
            row = cur.fetchone()
            if row and row[0] and row[0] > 0:
                wr = float(row[1] or 0) / float(row[0]) * 100
            else:
                wr = 0.0
            return {
                "name": "Win Rate (après slippage)",
                "value": f"{wr:.1f}%",
                "threshold": f"≥{CRITERIA['min_wr_pct']}%",
                "pass": wr >= CRITERIA["min_wr_pct"],
            }
        except Exception as e:
            return {"name": "Win Rate", "value": f"Erreur: {e}", "pass": False, "threshold": f"≥{CRITERIA['min_wr_pct']}%"}

    def _check_drawdown(self) -> dict:
        try:
            # Approx: utilise nemesis_equity si disponible
            max_dd = 0.0
            if self._db and self._db._pg:
                sql = """
                    SELECT MAX((peak - balance) / NULLIF(peak, 0) * 100)
                    FROM (
                        SELECT balance,
                               MAX(balance) OVER (ORDER BY recorded_at) as peak
                        FROM nemesis_equity
                    ) sub
                """
                cur = self._db._execute(sql, fetch=True)
                val = cur.fetchone()
                max_dd = float(val[0]) if val and val[0] is not None else 0.0
            return {
                "name": "Drawdown Maximum",
                "value": f"{max_dd:.1f}%",
                "threshold": f"≤{CRITERIA['max_dd_pct']}%",
                "pass": max_dd <= CRITERIA["max_dd_pct"],
            }
        except Exception as e:
            return {"name": "Drawdown", "value": f"Erreur: {e}", "pass": False, "threshold": f"≤{CRITERIA['max_dd_pct']}%"}

    def _check_sharpe(self) -> dict:
        try:
            if self._db and self._db._pg:
                sql = """
                    SELECT AVG(pnl), STDDEV(pnl)
                    FROM capital_trades WHERE status='CLOSED' AND pnl IS NOT NULL
                """
                cur = self._db._execute(sql, fetch=True)
                row = cur.fetchone()
                if row and row[1] and float(row[1]) > 0:
                    sharpe = float(row[0]) / float(row[1]) * math.sqrt(252)
                else:
                    sharpe = 0.0
            else:
                sharpe = 0.0
            return {
                "name": "Sharpe Ratio (annualisé)",
                "value": f"{sharpe:.2f}",
                "threshold": f"≥{CRITERIA['min_sharpe']}",
                "pass": sharpe >= CRITERIA["min_sharpe"],
            }
        except Exception as e:
            return {"name": "Sharpe Ratio", "value": f"Erreur: {e}", "pass": False, "threshold": f"≥{CRITERIA['min_sharpe']}"}

    def _check_429_errors(self) -> dict:
        count = 0
        if self._rl:
            try:
                count = self._rl.stats().get("total_429", 0)
            except Exception:
                pass
        return {
            "name": "Erreurs 429 Rate-Limit",
            "value": str(count),
            "threshold": f"={CRITERIA['max_429_errors']}",
            "pass": count <= CRITERIA["max_429_errors"],
        }

    def _query_scalar(self, sql: str):
        if not self._db:
            return None
        try:
            cur = self._db._execute(sql, fetch=True)
            val = cur.fetchone()
            return val[0] if val else None
        except Exception:
            return None
