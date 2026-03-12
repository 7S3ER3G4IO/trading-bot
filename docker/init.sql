-- Nemesis Local DB init — tables minima créées au premier démarrage
-- Le bot créera les tables manquantes automatiquement au boot (ensure_table)

CREATE TABLE IF NOT EXISTS capital_trades (
    id           SERIAL PRIMARY KEY,
    instrument   VARCHAR(20),
    direction    VARCHAR(4),
    entry        DOUBLE PRECISION,
    sl           DOUBLE PRECISION,
    tp1          DOUBLE PRECISION,
    size         DOUBLE PRECISION,
    score        DOUBLE PRECISION,
    pnl          DOUBLE PRECISION,
    duration_min DOUBLE PRECISION,
    result       VARCHAR(10),
    status       VARCHAR(20) DEFAULT 'OPEN',
    ref          VARCHAR(60),
    opened_at    TIMESTAMPTZ DEFAULT NOW(),
    closed_at    TIMESTAMPTZ
);

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

