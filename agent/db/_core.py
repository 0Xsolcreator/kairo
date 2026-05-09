from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

_DB_PATH = "agent_data.db"


def _set_db_path(path: str) -> None:
    """Override the database file used by all subsequent _open() calls.
    Call this before creating engine singletons — e.g. pass ':memory:' in tests."""
    global _DB_PATH
    _DB_PATH = path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitors (
    id            TEXT    PRIMARY KEY,
    type          TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'active',
    scope_json    TEXT    NOT NULL,
    source_json   TEXT    NOT NULL,
    poll_interval INTEGER NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS deposit_earn_polls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id  TEXT    NOT NULL,
    polled_at   TEXT    NOT NULL,
    token       TEXT    NOT NULL,
    protocol    TEXT    NOT NULL,
    vault_addr  TEXT,
    apy_base    REAL,
    apy_rewards REAL,
    apy_net     REAL,
    tvl_usd     REAL,
    apy_7d      REAL,
    apy_30d     REAL,
    raw_json    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dep_monitor
    ON deposit_earn_polls(monitor_id, polled_at);

CREATE TABLE IF NOT EXISTS chain_executions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id      TEXT    NOT NULL,
    monitor_type    TEXT    NOT NULL,
    signal          TEXT    NOT NULL,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    status          TEXT    NOT NULL,
    steps_completed INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    state_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_chain_monitor
    ON chain_executions(monitor_id, started_at);

-- agent_master_seed: single row, BIP39 mnemonic.
CREATE TABLE IF NOT EXISTS agent_master_seed (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mnemonic    TEXT    NOT NULL,
    encrypted   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

-- holding_wallet: single row, the user's main wallet address.
CREATE TABLE IF NOT EXISTS holding_wallet (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    address     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);

-- monitor_wallets: one row per monitor. Public material only.
CREATE TABLE IF NOT EXISTS monitor_wallets (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id              TEXT    NOT NULL UNIQUE,
    derivation_index        INTEGER NOT NULL UNIQUE,
    operating_pubkey        TEXT    NOT NULL,
    umbra_meta_address      TEXT    NOT NULL,
    status                  TEXT    NOT NULL DEFAULT 'active',
    created_at              TEXT    NOT NULL,
    closed_at               TEXT,
    deposited_kamino_vault  TEXT
);
CREATE INDEX IF NOT EXISTS idx_monitor_wallets_monitor
    ON monitor_wallets(monitor_id);
"""


def _safe_json(obj: object) -> str:
    return json.dumps(obj, default=repr)


def _migrate(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(
            "ALTER TABLE monitor_wallets ADD COLUMN deposited_kamino_vault TEXT"
        )
    except sqlite3.OperationalError:
        pass  # column already exists


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()


def _open() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn
