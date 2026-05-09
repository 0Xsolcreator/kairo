from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from agent.db._core import _open, _safe_json


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


def get_last_completed_execution_time(monitor_id: str) -> str | None:
    """
    Return the finished_at timestamp (UTC ISO string) of the most recent
    chain execution with status='completed' for this monitor, or None.
    """
    conn = _open()
    try:
        row = conn.execute(
            """SELECT MAX(finished_at) FROM chain_executions
               WHERE monitor_id = ? AND status = 'completed'""",
            (monitor_id,),
        ).fetchone()
        return row[0] if row and row[0] else None
    finally:
        conn.close()


async def async_start_execution(**kwargs) -> int:
    return await asyncio.to_thread(start_execution, **kwargs)


async def async_finish_execution(execution_id: int, **kwargs) -> None:
    await asyncio.to_thread(finish_execution, execution_id, **kwargs)
