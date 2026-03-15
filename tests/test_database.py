"""
tests/test_database.py — Tests unitaires Database (SQLite fallback)
Lance sans PostgreSQL : utilise le backend SQLite automatique.
"""
import os, sys, time, tempfile, pytest

os.environ.pop("DATABASE_URL", None)
os.environ["DEPLOYMENT_ENV"] = "test"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from database import Database


@pytest.fixture
def db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    d = Database()
    yield d
    try:
        if d._conn:
            d._conn.close()
    except Exception:
        pass
    try:
        os.unlink(db_path)
    except Exception:
        pass


# ─── Tables ──────────────────────────────────────────────────────

class TestTableCreation:
    def test_positions_table_exists(self, db):
        result = db._execute("SELECT name FROM sqlite_master WHERE type='table'", fetch=True)
        tables = [r[0] for r in result.fetchall()]
        assert "positions" in tables, f"positions manquante — tables: {tables}"

    def test_trades_table_exists(self, db):
        result = db._execute("SELECT name FROM sqlite_master WHERE type='table'", fetch=True)
        tables = [r[0] for r in result.fetchall()]
        assert "trades" in tables

    def test_daily_stats_table_exists(self, db):
        result = db._execute("SELECT name FROM sqlite_master WHERE type='table'", fetch=True)
        tables = [r[0] for r in result.fetchall()]
        assert "daily_stats" in tables


# ─── Positions CRUD ───────────────────────────────────────────────

class TestPositions:
    def test_save_and_load_open_position(self, db):
        state = {"direction": "BUY", "entry": 1.0850, "sl": 1.0800, "tp1": 1.0920, "size": 0.1}
        db.save_position("EURUSD", state)
        positions = db.load_open_positions()
        assert len(positions) >= 1
        instruments = [p["instrument"] for p in positions]
        assert "EURUSD" in instruments

    def test_save_multiple_positions(self, db):
        db.save_position("EURUSD", {"direction": "BUY", "entry": 1.08, "sl": 1.07, "size": 0.1})
        db.save_position("GBPUSD", {"direction": "SELL", "entry": 1.26, "sl": 1.27, "size": 0.1})
        positions = db.load_open_positions()
        instruments = [p["instrument"] for p in positions]
        assert "EURUSD" in instruments
        assert "GBPUSD" in instruments

    def test_upsert_same_instrument(self, db):
        """save_position deux fois → pas de doublon."""
        db.save_position("USDCAD", {"direction": "BUY", "entry": 1.38, "sl": 1.37, "size": 0.1})
        db.save_position("USDCAD", {"direction": "BUY", "entry": 1.38, "sl": 1.36, "size": 0.2})
        positions = db.load_open_positions()
        usd_positions = [p for p in positions if p["instrument"] == "USDCAD"]
        assert len(usd_positions) == 1  # pas de doublon


# ─── bot_state ────────────────────────────────────────────────────

class TestBotState:
    def test_save_and_load_state(self, db):
        db.save_bot_state("mode", "LIVE")
        val = db.load_bot_state("mode")
        assert val == "LIVE"

    def test_default_value(self, db):
        val = db.load_bot_state("nonexistent_key", default="FALLBACK")
        assert val == "FALLBACK"

    def test_overwrite_state(self, db):
        db.save_bot_state("mode", "LIVE")
        db.save_bot_state("mode", "PAUSED")
        assert db.load_bot_state("mode") == "PAUSED"


# ─── equity ───────────────────────────────────────────────────────

class TestEquity:
    def test_save_equity(self, db):
        """save_equity ne doit pas lever d'exception."""
        db.save_equity(100_500.0)
        db.save_equity(101_200.0)


# ─── Async write ──────────────────────────────────────────────────

class TestAsyncWrite:
    def test_async_write_does_not_block(self, db):
        state = {"direction": "BUY", "entry": 1.08, "sl": 1.07, "size": 0.1}
        db.save_position_async("EURUSD", state)
        time.sleep(0.3)
        positions = db.load_open_positions()
        instruments = [p["instrument"] for p in positions]
        assert "EURUSD" in instruments
