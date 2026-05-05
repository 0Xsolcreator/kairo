from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

_DB_PATH = "agent_memory.db"

_SCHEMA = """
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
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _open() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    _ensure_schema(conn)
    return conn


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
