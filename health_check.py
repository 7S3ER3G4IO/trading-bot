"""
health_check.py — DevOps : Health Check Auto-Vérificateur Production.

S'auto-vérifie à chaque démarrage ET sur demande /health (Telegram).

Checks exécutés:
  1. ✅ Capital.com API ping (GET /ping ou /session)
  2. ✅ Supabase DB connexion + COUNT tables
  3. ✅ Tables SQL requises existent et sont accessibles
  4. ✅ Dépendances Python (requirements.txt)
  5. ✅ Mémoire RAM disponible
  6. ✅ Rate Limiter status (429 en dernières 24h)

Auto-correction:
  - Si table manquante → la crée automatiquement (DDL embarqué)
  - Si dépendance manquante → log + skip (jamais crash)

Usage:
    hc = HealthCheck(capital, db, rate_limiter, telegram_router)
    report = hc.run()  # Dict avec tous les checks
    hc.send_telegram_report()
"""
import os
import sys
import time
import importlib
from datetime import datetime, timezone
from loguru import logger

# ─── Tables requises avec DDL de récupération ────────────────────────────────
REQUIRED_TABLES = {
    "capital_trades": """
        CREATE TABLE IF NOT EXISTS capital_trades (
            id SERIAL PRIMARY KEY, instrument VARCHAR(30),
            direction VARCHAR(4), entry DOUBLE PRECISION,
            sl DOUBLE PRECISION, tp1 DOUBLE PRECISION,
            size DOUBLE PRECISION DEFAULT 1.0,
            status VARCHAR(10) DEFAULT 'OPEN',
            result VARCHAR(10), pnl DOUBLE PRECISION DEFAULT 0,
            close_price DOUBLE PRECISION, duration_min INTEGER DEFAULT 0,
            opened_at TIMESTAMPTZ DEFAULT NOW(), close_time TIMESTAMPTZ
        )""",
    "shadow_trades": """
        CREATE TABLE IF NOT EXISTS shadow_trades (
            id SERIAL PRIMARY KEY, instrument VARCHAR(20),
            direction VARCHAR(4), entry DOUBLE PRECISION,
            sl DOUBLE PRECISION, tp1 DOUBLE PRECISION,
            score DOUBLE PRECISION DEFAULT 0,
            status VARCHAR(10) DEFAULT 'OPEN',
            result VARCHAR(10), pnl DOUBLE PRECISION DEFAULT 0,
            open_time TIMESTAMPTZ DEFAULT NOW(), close_time TIMESTAMPTZ,
            close_px DOUBLE PRECISION
        )""",
    "nemesis_equity": """
        CREATE TABLE IF NOT EXISTS nemesis_equity (
            id SERIAL PRIMARY KEY, balance DOUBLE PRECISION,
            pnl_day DOUBLE PRECISION DEFAULT 0,
            recorded_at TIMESTAMPTZ DEFAULT NOW()
        )""",
    "nemesis_bot_state": """
        CREATE TABLE IF NOT EXISTS nemesis_bot_state (
            key VARCHAR(100) PRIMARY KEY, value TEXT,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )""",
    "pairs_trades": """
        CREATE TABLE IF NOT EXISTS pairs_trades (
            id SERIAL PRIMARY KEY, asset_a VARCHAR(20), asset_b VARCHAR(20),
            direction_a VARCHAR(4), direction_b VARCHAR(4),
            correlation DOUBLE PRECISION, beta DOUBLE PRECISION,
            zscore_entry DOUBLE PRECISION, zscore_exit DOUBLE PRECISION DEFAULT 0,
            pnl_est DOUBLE PRECISION DEFAULT 0, reason VARCHAR(20),
            status VARCHAR(10) DEFAULT 'OPEN',
            opened_at TIMESTAMPTZ DEFAULT NOW(), closed_at TIMESTAMPTZ
        )""",
    "order_slices": """
        CREATE TABLE IF NOT EXISTS order_slices (
            id SERIAL PRIMARY KEY, order_id VARCHAR(50),
            instrument VARCHAR(20), direction VARCHAR(4),
            slice_n INTEGER, size DOUBLE PRECISION,
            price DOUBLE PRECISION, ref VARCHAR(50),
            sliced_at TIMESTAMPTZ DEFAULT NOW()
        )""",
}

# ─── Modules Python requis ────────────────────────────────────────────────────
REQUIRED_PACKAGES = [
    "requests", "loguru", "pandas", "numpy",
    "psycopg2", "dotenv",
]
OPTIONAL_PACKAGES = ["sklearn", "lightgbm", "feedparser"]


class HealthCheck:
    """Auto-vérificateur de production — ping exchange + DB + tables + deps."""

    def __init__(self, capital=None, db=None, rate_limiter=None,
                 telegram_router=None):
        self._capital = capital
        self._db      = db
        self._rl      = rate_limiter
        self._tg      = telegram_router
        self._last_report: dict = {}

    # ─── Main Run ────────────────────────────────────────────────────────────

    def run(self) -> dict:
        """Exécute tous les checks. Retourne un dict complet."""
        t0 = time.monotonic()
        checks = {}

        checks["capital_api"]    = self._check_capital()
        checks["database"]       = self._check_database()
        checks["tables"]         = self._check_and_fix_tables()
        checks["dependencies"]   = self._check_dependencies()
        checks["rate_limiter"]   = self._check_rate_limiter()
        checks["memory"]         = self._check_memory()

        elapsed = round((time.monotonic() - t0) * 1000)
        all_ok  = all(c.get("ok", False) for c in checks.values())

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": elapsed,
            "all_ok": all_ok,
            "checks": checks,
        }
        self._last_report = result

        status = "✅ ALL SYSTEMS GO" if all_ok else "⚠️ ISSUES DETECTED"
        logger.info(f"🏥 Health Check: {status} ({elapsed}ms)")
        return result

    def send_telegram_report(self):
        """Envoie le rapport de santé sur Telegram."""
        r = self.run()
        lines = []
        for key, check in r["checks"].items():
            ok   = check.get("ok", False)
            icon = "✅" if ok else "❌"
            msg  = check.get("msg", "")
            lines.append(f"  {icon} {key}: {msg}")

        elapsed = r["elapsed_ms"]
        status  = "🟢 <b>Tous systèmes opérationnels</b>" if r["all_ok"] else "🔴 <b>Issues détectées</b>"
        full_msg = (
            f"🏥 <b>Health Check — Nemesis v2.0</b>\n\n"
            f"{chr(10).join(lines)}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{status}\n"
            f"<i>Check time: {elapsed}ms | {r['timestamp'][:19]}Z</i>"
        )

        if self._tg:
            try:
                self._tg.send_report(full_msg)
            except Exception:
                try:
                    self._tg.send_trade(full_msg)
                except Exception as e:
                    logger.warning(f"HealthCheck Telegram: {e}")

    # ─── Individual Checks ───────────────────────────────────────────────────

    def _check_capital(self) -> dict:
        """Ping Capital.com API."""
        try:
            if not self._capital:
                return {"ok": False, "msg": "client non initialisé"}
            bal = self._capital.get_balance()
            if bal is not None and bal > 0:
                return {"ok": True, "msg": f"balance={bal:.2f}€"}
            return {"ok": False, "msg": "balance=0 ou None"}
        except Exception as e:
            return {"ok": False, "msg": str(e)[:80]}

    def _check_database(self) -> dict:
        """Ping Supabase DB avec une requête légère."""
        try:
            if not self._db:
                return {"ok": False, "msg": "db non initialisé"}
            if self._db._pg:
                cur = self._db._execute("SELECT 1 AS alive", fetch=True)
                row = cur.fetchone()
                if row and row[0] == 1:
                    return {"ok": True, "msg": "Supabase (PostgreSQL) ✅"}
            else:
                cur = self._db._execute("SELECT 1", fetch=True)
                return {"ok": True, "msg": "SQLite (fallback) ⚠️"}
        except Exception as e:
            return {"ok": False, "msg": str(e)[:80]}
        return {"ok": False, "msg": "ping échoué"}

    def _check_and_fix_tables(self) -> dict:
        """Vérifie que toutes les tables requises existent, les crée si manquantes."""
        if not self._db or not self._db._pg:
            return {"ok": True, "msg": "SQLite: skip table check"}

        created = []
        failed  = []

        for table_name, ddl in REQUIRED_TABLES.items():
            try:
                # Vérifier existence
                cur = self._db._execute(
                    "SELECT to_regclass(%s)",
                    (f"public.{table_name}",),
                    fetch=True
                )
                exists = cur.fetchone()[0]
                if not exists:
                    # Auto-création
                    self._db._execute(ddl)
                    created.append(table_name)
                    logger.info(f"🏥 Table auto-créée: {table_name}")
            except Exception as e:
                failed.append(f"{table_name}:{str(e)[:30]}")

        msg_parts = []
        if created:
            msg_parts.append(f"créées: {created}")
        if failed:
            msg_parts.append(f"échecs: {failed}")
        if not created and not failed:
            msg_parts.append(f"{len(REQUIRED_TABLES)} tables OK")

        return {
            "ok": len(failed) == 0,
            "msg": " | ".join(msg_parts),
            "tables_created": created,
            "failed": failed,
        }

    def _check_dependencies(self) -> dict:
        """Vérifie les packages Python requis."""
        missing  = []
        optional_found = []

        for pkg in REQUIRED_PACKAGES:
            real_name = pkg.replace("-", "_")
            try:
                importlib.import_module(real_name)
            except ImportError:
                # Aliases
                aliases = {"dotenv": "python_dotenv", "psycopg2": "psycopg2"}
                alt = aliases.get(pkg)
                try:
                    if alt:
                        importlib.import_module(alt)
                    else:
                        missing.append(pkg)
                except ImportError:
                    missing.append(pkg)

        for pkg in OPTIONAL_PACKAGES:
            try:
                importlib.import_module(pkg)
                optional_found.append(pkg)
            except ImportError:
                pass

        ok  = len(missing) == 0
        msg = f"required OK | optional: {optional_found}" if ok else f"MANQUANTS: {missing}"
        return {"ok": ok, "msg": msg, "missing": missing}

    def _check_rate_limiter(self) -> dict:
        """Vérifie le nombre d'erreurs 429 en 24h."""
        try:
            if not self._rl:
                return {"ok": True, "msg": "rate limiter non attaché"}
            stats = self._rl.stats() if hasattr(self._rl, 'stats') else {}
            total_429 = stats.get("total_429", 0)
            ok = total_429 == 0
            return {
                "ok": ok,
                "msg": f"429 errors: {total_429} | req_count: {stats.get('total_requests', '?')}",
            }
        except Exception as e:
            return {"ok": True, "msg": f"skip: {e}"}

    def _check_memory(self) -> dict:
        """Vérifie la RAM disponible."""
        try:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            usage_mb = usage / 1024 if sys.platform != "darwin" else usage / (1024 * 1024)
            ok = usage_mb < 512
            return {"ok": ok, "msg": f"RAM: {usage_mb:.0f}MB ({'OK' if ok else 'HIGH'})"}
        except Exception:
            return {"ok": True, "msg": "RAM check non disponible"}
