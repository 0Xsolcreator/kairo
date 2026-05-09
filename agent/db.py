from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

_DB_PATH = "agent_data.db"

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
    status          TEXT    NOT NULL,    -- 'running' | 'completed' | 'aborted' | 'failed' | 'skipped'
    steps_completed INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    state_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_chain_monitor
    ON chain_executions(monitor_id, started_at);

-- Wallet tables ------------------------------------------------------------
-- agent_master_seed: single row, BIP39 mnemonic. `encrypted` is a placeholder
-- so the encryption layer can be added without a schema migration.
CREATE TABLE IF NOT EXISTS agent_master_seed (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mnemonic    TEXT    NOT NULL,
    encrypted   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

-- holding_wallet: single row, the user's main wallet address. Funds flow
-- back here at monitor-close time. Address may be a regular Solana pubkey
-- or an Umbra meta-address depending on how the close-flow chain sends.
CREATE TABLE IF NOT EXISTS holding_wallet (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    address     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);

-- monitor_wallets: one row per monitor. Stores derivation_index +
-- public material only. Private keys are re-derived from the master
-- mnemonic on demand and never persisted.
CREATE TABLE IF NOT EXISTS monitor_wallets (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id              TEXT    NOT NULL UNIQUE,
    derivation_index        INTEGER NOT NULL UNIQUE,
    operating_pubkey        TEXT    NOT NULL,
    umbra_meta_address      TEXT    NOT NULL,
    status                  TEXT    NOT NULL DEFAULT 'active',  -- 'active' | 'closed'
    created_at              TEXT    NOT NULL,
    closed_at               TEXT,
    deposited_kamino_vault  TEXT                                -- vault we last deposited into
);
CREATE INDEX IF NOT EXISTS idx_monitor_wallets_monitor
    ON monitor_wallets(monitor_id);
"""


def _safe_json(obj: object) -> str:
    """Best-effort JSON dumps that never crashes on unserialisable objects."""
    return json.dumps(obj, default=repr)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply additive schema changes that can't be expressed as CREATE IF NOT EXISTS."""
    try:
        conn.execute(
            "ALTER TABLE monitor_wallets ADD COLUMN deposited_kamino_vault TEXT"
        )
    except sqlite3.OperationalError:
        pass  # column already exists


def _open() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    _ensure_schema(conn)
    return conn


def upsert_monitor(
    *,
    id: str,
    type: str,
    status: str,
    scope_json: str,
    source_json: str,
    poll_interval: int,
    created_at: str,
) -> None:
    conn = _open()
    try:
        conn.execute(
            """INSERT INTO monitors
               (id, type, status, scope_json, source_json, poll_interval, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status""",
            (id, type, status, scope_json, source_json, poll_interval, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def delete_monitor(monitor_id: str) -> None:
    conn = _open()
    try:
        conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
        conn.commit()
    finally:
        conn.close()


def load_active_monitors() -> list[dict]:
    """Return all monitors with status='active' as plain dicts."""
    conn = _open()
    try:
        rows = conn.execute(
            """SELECT id, type, status, scope_json, source_json, poll_interval, created_at
               FROM monitors WHERE status = 'active'"""
        ).fetchall()
        return [
            {
                "id": row[0],
                "type": row[1],
                "status": row[2],
                "scope": json.loads(row[3]),
                "source": json.loads(row[4]),
                "poll_interval": row[5],
                "created_at": row[6],
            }
            for row in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Poll data
# ---------------------------------------------------------------------------

def store_poll(
    *,
    monitor_id: str,
    token: str,
    protocol: str,
    vault_addr: str | None,
    apy_base: float | None,
    apy_rewards: float | None,
    apy_net: float | None,
    tvl_usd: float | None,
    apy_7d: float | None,
    apy_30d: float | None,
    raw: dict,
) -> None:
    """Synchronous insert — run via asyncio.to_thread from async code."""
    conn = _open()
    try:
        conn.execute(
            """INSERT INTO deposit_earn_polls
               (monitor_id, polled_at, token, protocol, vault_addr,
                apy_base, apy_rewards, apy_net, tvl_usd, apy_7d, apy_30d, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                monitor_id,
                datetime.now(timezone.utc).isoformat(),
                token,
                protocol,
                vault_addr,
                apy_base,
                apy_rewards,
                apy_net,
                tvl_usd,
                apy_7d,
                apy_30d,
                json.dumps(raw),
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def async_store_poll(**kwargs) -> None:
    await asyncio.to_thread(store_poll, **kwargs)


# ---------------------------------------------------------------------------
# Chain execution log
# ---------------------------------------------------------------------------

def start_execution(*, monitor_id: str, monitor_type: str, signal: str) -> int:
    """Synchronous insert — run via asyncio.to_thread from async code."""
    conn = _open()
    try:
        cur = conn.execute(
            """INSERT INTO chain_executions
               (monitor_id, monitor_type, signal, started_at, status, steps_completed)
               VALUES (?, ?, ?, ?, 'running', 0)""",
            (
                monitor_id,
                monitor_type,
                signal,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def finish_execution(
    execution_id: int,
    *,
    status: str,
    steps_completed: int,
    error: str | None = None,
    state: dict | None = None,
) -> None:
    """Synchronous update — run via asyncio.to_thread from async code."""
    conn = _open()
    try:
        conn.execute(
            """UPDATE chain_executions
               SET finished_at=?, status=?, steps_completed=?, error=?, state_json=?
               WHERE id=?""",
            (
                datetime.now(timezone.utc).isoformat(),
                status,
                steps_completed,
                error,
                _safe_json(state or {}),
                execution_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def async_start_execution(**kwargs) -> int:
    return await asyncio.to_thread(start_execution, **kwargs)


async def async_finish_execution(execution_id: int, **kwargs) -> None:
    await asyncio.to_thread(finish_execution, execution_id, **kwargs)


# ---------------------------------------------------------------------------
# Wallet — master seed
# ---------------------------------------------------------------------------

def get_master_mnemonic() -> str | None:
    conn = _open()
    try:
        row = conn.execute(
            "SELECT mnemonic FROM agent_master_seed ORDER BY id ASC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def put_master_mnemonic(mnemonic: str) -> None:
    """Insert if absent; never overwrite — only one master mnemonic ever exists."""
    conn = _open()
    try:
        existing = conn.execute(
            "SELECT 1 FROM agent_master_seed LIMIT 1"
        ).fetchone()
        if existing:
            return
        conn.execute(
            """INSERT INTO agent_master_seed (mnemonic, encrypted, created_at)
               VALUES (?, 0, ?)""",
            (mnemonic, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Wallet — holding wallet
# ---------------------------------------------------------------------------

def get_holding_address() -> str | None:
    conn = _open()
    try:
        row = conn.execute(
            "SELECT address FROM holding_wallet ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def put_holding_address(address: str) -> None:
    """Replace any existing holding address with the new one."""
    conn = _open()
    try:
        conn.execute("DELETE FROM holding_wallet")
        conn.execute(
            "INSERT INTO holding_wallet (address, created_at) VALUES (?, ?)",
            (address, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Wallet — per-monitor
# ---------------------------------------------------------------------------

def next_monitor_derivation_index() -> int:
    """Return MAX(derivation_index) + 1, or 0 if the table is empty."""
    conn = _open()
    try:
        row = conn.execute(
            "SELECT MAX(derivation_index) FROM monitor_wallets"
        ).fetchone()
        return 0 if row[0] is None else int(row[0]) + 1
    finally:
        conn.close()


def store_monitor_wallet(
    *,
    monitor_id: str,
    derivation_index: int,
    operating_pubkey: str,
    umbra_meta_address: str,
) -> None:
    conn = _open()
    try:
        conn.execute(
            """INSERT INTO monitor_wallets
               (monitor_id, derivation_index, operating_pubkey,
                umbra_meta_address, status, created_at)
               VALUES (?, ?, ?, ?, 'active', ?)""",
            (
                monitor_id,
                derivation_index,
                operating_pubkey,
                umbra_meta_address,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_monitor_wallet(monitor_id: str) -> dict | None:
    conn = _open()
    try:
        row = conn.execute(
            """SELECT derivation_index, operating_pubkey, umbra_meta_address,
                      status, created_at, closed_at
               FROM monitor_wallets WHERE monitor_id = ?""",
            (monitor_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "derivation_index": int(row[0]),
            "operating_pubkey": row[1],
            "umbra_meta_address": row[2],
            "status": row[3],
            "created_at": row[4],
            "closed_at": row[5],
        }
    finally:
        conn.close()


def mark_monitor_wallet_closed(monitor_id: str) -> None:
    conn = _open()
    try:
        conn.execute(
            """UPDATE monitor_wallets
               SET status='closed', closed_at=?
               WHERE monitor_id=? AND status='active'""",
            (datetime.now(timezone.utc).isoformat(), monitor_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_kamino_deposited_vault(monitor_id: str, vault_address: str) -> None:
    """Record which Kamino vault funds were most recently deposited into."""
    conn = _open()
    try:
        conn.execute(
            "UPDATE monitor_wallets SET deposited_kamino_vault=? WHERE monitor_id=?",
            (vault_address, monitor_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_kamino_deposited_vault(monitor_id: str) -> str | None:
    """Return the Kamino vault last deposited into for this monitor, or None."""
    conn = _open()
    try:
        row = conn.execute(
            "SELECT deposited_kamino_vault FROM monitor_wallets WHERE monitor_id=?",
            (monitor_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()
