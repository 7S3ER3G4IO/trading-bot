-- Nemesis Local DB init — tables minima créées au premier démarrage
-- Le bot créera les tables manquantes automatiquement au boot (ensure_table)

-- FIX: table nemesis_equity était utilisée dans database.py mais jamais créée ici
CREATE TABLE IF NOT EXISTS nemesis_equity (
    id          SERIAL PRIMARY KEY,
    balance     DOUBLE PRECISION,
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS positions (
    id           SERIAL PRIMARY KEY,
    instrument   VARCHAR(20) UNIQUE,
    direction    VARCHAR(4),
    entry        DOUBLE PRECISION,
    sl           DOUBLE PRECISION,
    tp1          DOUBLE PRECISION,
    tp2          DOUBLE PRECISION,
    tp3          DOUBLE PRECISION,
    size         DOUBLE PRECISION,
    score        DOUBLE PRECISION DEFAULT 0,
    pnl          DOUBLE PRECISION DEFAULT 0,
    duration_min DOUBLE PRECISION DEFAULT 0,
    close_price  DOUBLE PRECISION DEFAULT 0,
    result       VARCHAR(10),
    status       VARCHAR(20) DEFAULT 'OPEN',
    ref1         VARCHAR(60),
    ref2         VARCHAR(60),
    ref3         VARCHAR(60),
    regime       VARCHAR(20),
    ab_variant   VARCHAR(4)  DEFAULT 'A',
    in_overlap   INTEGER     DEFAULT 0,
    tp1_hit      INTEGER     DEFAULT 0,
    tp2_hit      INTEGER     DEFAULT 0,
    close_time   TIMESTAMPTZ,
    opened_at    TIMESTAMPTZ DEFAULT NOW(),
    closed_at    TIMESTAMPTZ
);

-- Migrations: add any missing columns to existing deployments
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='positions' AND column_name='close_price') THEN
    ALTER TABLE positions ADD COLUMN close_price DOUBLE PRECISION DEFAULT 0;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='positions' AND column_name='duration_min') THEN
    ALTER TABLE positions ADD COLUMN duration_min DOUBLE PRECISION DEFAULT 0;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='positions' AND column_name='close_time') THEN
    ALTER TABLE positions ADD COLUMN close_time TIMESTAMPTZ;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='positions' AND column_name='pnl') THEN
    ALTER TABLE positions ADD COLUMN pnl DOUBLE PRECISION DEFAULT 0;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS state_snapshots (
    id          SERIAL PRIMARY KEY,
    open_trades INTEGER,
    snapshot_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cluster_workers (
    worker_id    VARCHAR(60) PRIMARY KEY,
    role         VARCHAR(20),
    state        VARCHAR(20),
    host         VARCHAR(100),
    heartbeat_at TIMESTAMPTZ DEFAULT NOW()
);

-- FIX MINEUR: index manquants sur positions (WHERE status='OPEN' très fréquent)
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_opened ON positions(opened_at);

-- Table alerts (utilisée par bot_tick.py pour circuit breaker et DD alerts)
CREATE TABLE IF NOT EXISTS alerts (
    id         SERIAL PRIMARY KEY,
    type       VARCHAR(40),
    message    TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index manquants sur trades (table principale — WHERE status='OPEN' très fréquent)
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at);
