"""
database.py — Persistance PostgreSQL via Supabase (remplace SQLite).
Les données survivent aux redémarrages Docker (volume persistant).

Améliorations production:
- Reconnexion automatique si la connexion est perdue (keepalive)
- Écriture async non-bloquante (ThreadPoolExecutor) pour ne pas bloquer la loop
- Compatible PGBouncer Transaction Pooler (pas de prepared statements)
- SSL forcé
"""
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional, List
from loguru import logger

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.extensions
    HAS_PG = True
except ImportError:
    HAS_PG = False

import sqlite3

# ─── URL de connexion ────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")
_SQLITE_PATH = "logs/nemesis.db"

# Thread pool for non-blocking async writes (never blocks trading loop)
_WRITE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="db_write")


class _FetchResult:
    """Wrapper thread-safe pour remplacer le cursor brut retourné hors du lock."""
    def __init__(self, rows, description):
        self._rows = rows
        self.description = description

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class Database:
    """
    Base de données unifiée : PostgreSQL (Supabase) si DATABASE_URL est défini,
    SQLite local sinon (développement / fallback).

    Self-healing: reconnexion automatique si la connexion PostgreSQL est perdue.
    Non-blocking: les écritures peuvent être envoyées en arrière-plan.
    """

    def __init__(self):
        os.makedirs("logs", exist_ok=True)
        self._pg = DATABASE_URL and HAS_PG
        self._lock = threading.Lock()
        self._conn = None
        self._connect()
        self._create_tables()

    # ─── Connexion & Reconnexion ──────────────────────────────────────────────

    def _connect(self):
        """Établit la connexion. Compatible PGBouncer Transaction Pooler."""
        if self._pg:
            try:
                _is_local = os.environ.get("DEPLOYMENT_ENV", "cloud") == "local"
                # Si DATABASE_URL contient sslmode=, on ne le double pas
                if "sslmode=" in (DATABASE_URL or ""):
                    _ssl_kw = {}
                else:
                    _ssl_kw = {"sslmode": "disable" if _is_local else "require"}
                self._conn = psycopg2.connect(
                    DATABASE_URL,
                    connect_timeout=10,
                    keepalives=1,
                    keepalives_idle=30,
                    keepalives_interval=10,
                    keepalives_count=5,
                    # PGBouncer Transaction Pooler : pas de prepared statements
                    options="-c statement_timeout=30000",
                    **_ssl_kw,
                )
                self._conn.autocommit = True   # Requis PGBouncer transaction mode
                logger.info(f"🗄️  PostgreSQL connecté ✅ (ssl={'disabled' if _is_local else 'required'})")
            except Exception as e:
                logger.warning(f"⚠️  PostgreSQL indisponible ({e}) — fallback SQLite")
                self._pg = False
                self._conn = sqlite3.connect(_SQLITE_PATH, check_same_thread=False)
        else:
            self._conn = sqlite3.connect(_SQLITE_PATH, check_same_thread=False)
            if not DATABASE_URL:
                logger.warning("⚠️  DATABASE_URL non défini — SQLite local (données non persistantes)")
            else:
                logger.warning("⚠️  psycopg2 absent — SQLite local utilisé")

    def _ping(self) -> bool:
        """Vérifie si la connexion est vivante. Reconnecte si nécessaire."""
        if not self._pg:
            return True
        try:
            self._conn.cursor().execute("SELECT 1")
            return True
        except Exception:
            logger.warning("🔄 DB: connexion perdue — tentative de reconnexion...")
            _is_local = os.environ.get("DEPLOYMENT_ENV", "cloud") == "local"
            if "sslmode=" in (DATABASE_URL or ""):
                _ssl_kw = {}
            else:
                _ssl_kw = {"sslmode": "disable" if _is_local else "require"}
            for attempt in range(1, 4):
                try:
                    time.sleep(attempt * 2)
                    self._conn = psycopg2.connect(
                        DATABASE_URL,
                        connect_timeout=10,
                        keepalives=1,
                        keepalives_idle=30,
                        keepalives_interval=10,
                        keepalives_count=5,
                        options="-c statement_timeout=30000",
                        **_ssl_kw,
                    )
                    self._conn.autocommit = True
                    logger.info(f"✅ DB: reconnecté (tentative {attempt})")
                    return True
                except Exception as re:
                    logger.warning(f"⚠️  DB reconnexion {attempt}/3: {re}")
            logger.error("❌ DB: impossible de se reconnecter après 3 tentatives")
            return False

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _execute(self, sql: str, params=(), fetch=False):
        """Exécute une requête compatible SQLite/PostgreSQL (thread-safe, self-healing)."""
        if not self._pg:
            sql = sql.replace("%s", "?")
        with self._lock:
            self._ping()
            try:
                cur = self._conn.cursor()
                cur.execute(sql, params)
                if not self._pg and not self._conn.isolation_level:
                    pass  # autocommit
                elif not self._pg:
                    self._conn.commit()
                if fetch:
                    # FIX: retourner les données DANS le lock, pas le cursor brut
                    rows = cur.fetchall()
                    desc = cur.description
                    return _FetchResult(rows, desc)
            except Exception as e:
                # CRITICAL: rollback to prevent "current transaction is aborted"
                # cascade that blocks ALL subsequent queries
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                logger.error(f"❌ DB execute error: {e} | SQL: {sql[:80]}")
                raise
        return cur

    def async_write(self, fn, *args, **kwargs):
        """Lance une écriture DB en arrière-plan (ne bloque jamais la loop)."""
        _WRITE_POOL.submit(fn, *args, **kwargs)

    def _create_tables(self):
        serial = "SERIAL" if self._pg else "INTEGER"
        text   = "TEXT"
        real   = "REAL" if not self._pg else "DOUBLE PRECISION"
        int_   = "INTEGER"
        ts     = "TIMESTAMPTZ DEFAULT NOW()" if self._pg else "TEXT"

        self._execute(f"""
        CREATE TABLE IF NOT EXISTS trades (
            id           {serial} PRIMARY KEY,
            symbol       {text}    NOT NULL,
            side         {text}    NOT NULL,
            entry        {real},
            exit_price   {real},
            amount       {real},
            sl           {real},
            tp1          {real},
            tp2          {real},
            tp3          {real},
            be           {real},
            current_sl   {real},
            remaining    {real},
            tp1_hit      {int_}    DEFAULT 0,
            tp2_hit      {int_}    DEFAULT 0,
            be_active    {int_}    DEFAULT 0,
            total_pnl    {real}    DEFAULT 0,
            status       {text}    DEFAULT 'OPEN',
            result       {text},
            fees         {real}    DEFAULT 0,
            opened_at    {'TIMESTAMPTZ DEFAULT NOW()' if self._pg else 'TEXT'},
            closed_at    {'TIMESTAMPTZ' if self._pg else 'TEXT'},
            sl_order_id  {text},
            tp1_order_id {text},
            tp2_order_id {text}
        )
        """)

        # Indexes on trades (critical for performance)
        if self._pg:
            self._execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
            self._execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
            self._execute("CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at)")

        self._execute(f"""
        CREATE TABLE IF NOT EXISTS daily_stats (
            id          {serial} PRIMARY KEY,
            date        {text}   UNIQUE,
            trades_won  {int_}   DEFAULT 0,
            trades_lost {int_}   DEFAULT 0,
            pnl_gross   {real}   DEFAULT 0,
            pnl_fees    {real}   DEFAULT 0,
            balance_end {real}   DEFAULT 0
        )
        """)

        self._execute(f"""
        CREATE TABLE IF NOT EXISTS weekly_stats (
            id           {serial} PRIMARY KEY,
            week_start   {text}   UNIQUE,
            trades_tot   {int_}   DEFAULT 0,
            winrate      {real}   DEFAULT 0,
            pnl_net      {real}   DEFAULT 0,
            best_trade   {real}   DEFAULT 0,
            worst_trade  {real}   DEFAULT 0,
            max_drawdown {real}   DEFAULT 0
        )
        """)

        # Capital.com trades (persistance CFD après redémarrage Docker)
        self._execute(f"""
        CREATE TABLE IF NOT EXISTS positions (
            id          {serial} PRIMARY KEY,
            instrument  {text}   NOT NULL UNIQUE,
            direction   {text},
            entry       {real},
            sl          {real},
            tp1         {real},
            tp2         {real},
            tp3         {real},
            ref1        {text},
            ref2        {text},
            ref3        {text},
            score       {real}   DEFAULT 0,
            regime      {text},
            ab_variant  {text}   DEFAULT 'A',
            in_overlap  {int_}   DEFAULT 0,
            tp1_hit     {int_}   DEFAULT 0,
            tp2_hit     {int_}   DEFAULT 0,
            opened_at   {text},
            status      {text}   DEFAULT 'OPEN',
            strategy    {text}   DEFAULT 'BK',
            broker      {text}   DEFAULT 'capital'
        )
        """)
        # Performance attribution — upgrade silencieux si table déjà existante
        for col, col_type in [("strategy", text), ("broker", text)]:
            try:
                if self._pg:
                    self._execute(
                        f"ALTER TABLE positions ADD COLUMN IF NOT EXISTS "
                        f"{col} TEXT DEFAULT '{col}'"
                    )
                else:
                    # SQLite doesn't support IF NOT EXISTS on ALTER TABLE
                    cur = self._execute(
                        "PRAGMA table_info(positions)", fetch=True
                    )
                    cols = [row[1] for row in cur.fetchall()]
                    if col not in cols:
                        self._execute(
                            f"ALTER TABLE positions ADD COLUMN {col} TEXT DEFAULT ''"
                        )
            except Exception:
                pass


        # Bot state persistence (dd_paused, daily_start_balance, etc.)
        self._execute(f"""
        CREATE TABLE IF NOT EXISTS bot_state (
            key    {text} PRIMARY KEY,
            value  {text}
        )
        """)

        # Equity history — persistance des équity points pour Chart.js et GoLive checker
        # CRITIQUE FIX: table était utilisée dans save_equity() mais jamais créée
        self._execute(f"""
        CREATE TABLE IF NOT EXISTS nemesis_equity (
            id          {serial} PRIMARY KEY,
            balance     {real},
            recorded_at {ts}
        )
        """)


        # ─── Schema migrations (safe: run on every start, idempotent) ────────────
        # PostgreSQL: use DO block to add missing columns without error
        if self._pg:
            _capital_migrations = [
                ("close_price",  "DOUBLE PRECISION DEFAULT 0"),
                ("duration_min", "DOUBLE PRECISION DEFAULT 0"),
                ("close_time",   "TIMESTAMPTZ"),
                ("pnl",          "DOUBLE PRECISION DEFAULT 0"),
                ("tp2",          "DOUBLE PRECISION DEFAULT 0"),
                ("tp3",          "DOUBLE PRECISION DEFAULT 0"),
                ("ref2",         "TEXT"),
                ("ref3",         "TEXT"),
                ("regime",       "TEXT"),
                ("ab_variant",   "TEXT DEFAULT 'A'"),
                ("in_overlap",   "INTEGER DEFAULT 0"),
                ("tp1_hit",      "INTEGER DEFAULT 0"),
                ("tp2_hit",      "INTEGER DEFAULT 0"),
            ]
            for col_name, col_def in _capital_migrations:
                try:
                    self._execute(
                        f"ALTER TABLE positions ADD COLUMN IF NOT EXISTS {col_name} {col_def}"
                    )
                except Exception:
                    pass

        # SQLite migration
        if not self._pg:
            for col in ["sl_order_id TEXT", "tp1_order_id TEXT", "tp2_order_id TEXT",
                        "close_price REAL", "duration_min REAL", "pnl REAL",
                        "tp2 REAL", "tp3 REAL", "ref2 TEXT", "ref3 TEXT",
                        "regime TEXT", "ab_variant TEXT", "in_overlap INTEGER",
                        "tp1_hit INTEGER DEFAULT 0", "tp2_hit INTEGER DEFAULT 0"]:
                try:
                    self._execute(f"ALTER TABLE trades ADD COLUMN {col}")
                except Exception:
                    pass
                try:
                    self._execute(f"ALTER TABLE positions ADD COLUMN {col}")
                except Exception:
                    pass

    # ─── Trades Binance ───────────────────────────────────────────────────────

    def save_trade_open(self, trade) -> int:
        ph = "%s" if self._pg else "?"
        sql = f"""
            INSERT INTO trades
            (symbol, side, entry, amount, sl, tp1, tp2, tp3, be,
             current_sl, remaining, opened_at)
            VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})
        """
        if self._pg:
            sql += " RETURNING id"
        cur = self._execute(sql, (
            trade.symbol, trade.side, trade.entry, trade.total_amount,
            trade.initial_sl, trade.tp1, trade.tp2, trade.tp3, trade.be,
            trade.current_sl, trade.remaining,
            datetime.now(timezone.utc).isoformat()
        ))
        if self._pg:
            trade_id = cur.fetchone()[0]
        else:
            trade_id = cur.lastrowid
        logger.debug(f"🗄️  Trade sauvegardé ID={trade_id}")
        return trade_id

    def update_trade(self, trade_id: int, **kwargs):
        if not kwargs:
            return
        ph = "%s" if self._pg else "?"
        cols = ", ".join(f"{k}={ph}" for k in kwargs)
        vals = list(kwargs.values()) + [trade_id]
        self._execute(f"UPDATE trades SET {cols} WHERE id={ph}", vals)

    def close_trade(self, trade_id: int, exit_price: float, result: str,
                    total_pnl: float, fees: float):
        ph = "%s" if self._pg else "?"
        self._execute(f"""
            UPDATE trades SET
                exit_price={ph}, result={ph}, total_pnl={ph}, fees={ph},
                status='CLOSED', closed_at={ph}
            WHERE id={ph}
        """, (exit_price, result, total_pnl, fees,
              datetime.now(timezone.utc).isoformat(), trade_id))

    def load_open_trades(self) -> List[dict]:
        cur = self._execute(
            "SELECT * FROM trades WHERE status='OPEN'",
            fetch=True
        )
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        if rows:
            logger.info(f"🔄 {len(rows)} trades ouverts restaurés depuis la BDD")
        return rows

    # ─── Stats ───────────────────────────────────────────────────────────────

    def get_closed_trades(self, days: int = 7) -> List[dict]:
        ph = "%s" if self._pg else "?"
        if self._pg:
            sql = f"""
                SELECT * FROM trades
                WHERE status='CLOSED'
                AND closed_at::date >= (CURRENT_DATE - INTERVAL '{days} days')
                ORDER BY closed_at DESC
            """
        else:
            sql = f"""
                SELECT * FROM trades WHERE status='CLOSED'
                AND date(closed_at) >= date('now', '-{days} days')
                ORDER BY closed_at DESC
            """
        cur = self._execute(sql, fetch=True)
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_weekly_pnl(self) -> float:
        if self._pg:
            sql = """
                SELECT COALESCE(SUM(total_pnl - fees), 0) FROM trades
                WHERE status='CLOSED'
                AND closed_at::date >= (CURRENT_DATE - INTERVAL '7 days')
            """
        else:
            sql = """
                SELECT COALESCE(SUM(total_pnl - fees), 0) FROM trades
                WHERE status='CLOSED'
                AND date(closed_at) >= date('now', '-7 days')
            """
        return self._execute(sql, fetch=True).fetchone()[0] or 0.0

    def get_total_fees(self, days: int = 30) -> float:
        if self._pg:
            sql = f"""
                SELECT COALESCE(SUM(fees), 0) FROM trades
                WHERE status='CLOSED'
                AND closed_at::date >= (CURRENT_DATE - INTERVAL '{days} days')
            """
        else:
            sql = f"""
                SELECT COALESCE(SUM(fees), 0) FROM trades
                WHERE status='CLOSED'
                AND date(closed_at) >= date('now', '-{days} days')
            """
        return self._execute(sql, fetch=True).fetchone()[0] or 0.0

    # ─── Capital.com trades ───────────────────────────────────────────────────

    def save_position(self, instrument: str, state: dict):
        """
        Sauvegarde (UPSERT) un trade Capital.com ouvert.
        Appelé à l'ouverture et lors des mises à jour tp1_hit.
        """
        ph = "%s" if self._pg else "?"
        refs = state.get("refs", [None, None, None])
        strategy = state.get("strategy", state.get("strat", "BK"))
        broker = "mt5" if state.get("broker_mt5") else "capital"
        try:
            if self._pg:
                sql = f"""
                    INSERT INTO positions
                        (instrument, direction, entry, sl, tp1, tp2, tp3,
                         ref1, ref2, ref3, score, regime, ab_variant, in_overlap,
                         tp1_hit, tp2_hit, opened_at, status, strategy, broker)
                    VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},
                            {ph},{ph},{ph},{ph},{ph},{ph},{ph},'OPEN',{ph},{ph})
                    ON CONFLICT (instrument) DO UPDATE SET
                        direction=EXCLUDED.direction, entry=EXCLUDED.entry,
                        sl=EXCLUDED.sl, tp1=EXCLUDED.tp1, tp2=EXCLUDED.tp2,
                        tp3=EXCLUDED.tp3, ref1=EXCLUDED.ref1,
                        score=EXCLUDED.score, regime=EXCLUDED.regime,
                        tp1_hit=EXCLUDED.tp1_hit, tp2_hit=EXCLUDED.tp2_hit,
                        strategy=EXCLUDED.strategy, broker=EXCLUDED.broker,
                        status='OPEN'
                """
            else:
                sql = f"""
                    INSERT OR REPLACE INTO positions
                        (instrument, direction, entry, sl, tp1, tp2, tp3,
                         ref1, ref2, ref3, score, regime, ab_variant, in_overlap,
                         tp1_hit, tp2_hit, opened_at, status, strategy, broker)
                    VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},
                            {ph},{ph},{ph},{ph},{ph},{ph},{ph},'OPEN',{ph},{ph})
                """
            self._execute(sql, (
                instrument,
                state.get("direction", ""),
                state.get("entry", 0.0),
                state.get("sl", 0.0),
                state.get("tp1", 0.0),
                state.get("tp2", 0.0),
                state.get("tp3", 0.0),
                refs[0] if len(refs) > 0 else None,
                refs[1] if len(refs) > 1 else None,
                refs[2] if len(refs) > 2 else None,
                float(state.get("score", 0)),
                state.get("regime", ""),
                state.get("ab_variant", "A"),
                int(state.get("in_overlap", False)),
                int(state.get("tp1_hit", False)),
                int(state.get("tp2_hit", False)),
                datetime.now(timezone.utc).isoformat(),
                strategy,
                broker,
            ))
        except Exception as e:
            logger.error(f"DB save_position {instrument}: {e}")

    def save_position_async(self, instrument: str, state: dict):
        """Sauvegarde non-bloquante — n'interrompt jamais la loop principale."""
        self.async_write(self.save_position, instrument, state)

    def load_open_positions(self) -> List[dict]:
        """Charge les trades Capital.com ouverts (reprise après redémarrage)."""
        try:
            cur = self._execute(
                "SELECT * FROM positions WHERE status='OPEN'",
                fetch=True
            )
            if not cur or not cur.description:
                return []
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            if rows:
                logger.info(f"🔄 {len(rows)} trades Capital.com restaurés depuis la BDD")
            return rows
        except Exception as e:
            logger.error(f"❌ DB load_positions: {e}")
            return []

    def close_position(self, instrument: str, pnl: float = 0.0,
                             result: str = "UNKNOWN", close_price: float = 0.0,
                             duration_min: int = 0):
        """Marque un trade Capital.com comme fermé avec PnL."""
        ph = "%s" if self._pg else "?"
        try:
            if self._pg:
                self._execute(
                    f"""UPDATE positions SET status='CLOSED',
                        pnl={ph}, result={ph}, close_price={ph},
                        duration_min={ph}, close_time=NOW()
                        WHERE instrument={ph}""",
                    (pnl, result, close_price, duration_min, instrument)
                )
            else:
                self._execute(
                    f"""UPDATE positions SET status='CLOSED',
                        pnl={ph}, result={ph}
                        WHERE instrument={ph}""",
                    (pnl, result, instrument)
                )
        except Exception as e:
            logger.error(f"❌ DB close_position {instrument}: {e}")

    def close_position_async(self, instrument: str, **kwargs):
        """Fermeture non-bloquante."""
        self.async_write(self.close_position, instrument, **kwargs)

    def save_equity(self, balance: float):
        """Enregistre le solde actuel dans nemesis_equity."""
        ph = "%s" if self._pg else "?"
        try:
            if self._pg:
                self._execute(
                    f"INSERT INTO nemesis_equity (balance, recorded_at) VALUES ({ph}, NOW())",
                    (balance,)
                )
            else:
                self._execute(
                    f"INSERT INTO nemesis_equity (balance) VALUES ({ph})",
                    (balance,)
                )
        except Exception:
            pass  # Non-critique

    # ─── C-4: Bot state persistence ──────────────────────────────────────────

    def save_bot_state(self, key: str, value: str):
        ph = "%s" if self._pg else "?"
        try:
            if self._pg:
                self._execute(
                    f"INSERT INTO bot_state (key, value) VALUES ({ph}, {ph}) "
                    f"ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                    (key, value)
                )
            else:
                self._execute(
                    f"INSERT OR REPLACE INTO bot_state (key, value) VALUES ({ph}, {ph})",
                    (key, value)
                )
        except Exception as e:
            logger.debug(f"save_bot_state {key}: {e}")

    def load_bot_state(self, key: str, default: str = "") -> str:
        ph = "%s" if self._pg else "?"
        try:
            cur = self._execute(
                f"SELECT value FROM bot_state WHERE key={ph}",
                (key,), fetch=True
            )
            row = cur.fetchone()
            return row[0] if row else default
        except Exception as e:
            logger.debug(f"load_bot_state {key}: {e}")
            return default



# Singleton instance
_db_instance = None
_db_lock = threading.Lock()

def get_db() -> Database:
    """Returns singleton Database instance (thread-safe)."""
    global _db_instance
    with _db_lock:
        if _db_instance is None:
            _db_instance = Database()
    return _db_instance
