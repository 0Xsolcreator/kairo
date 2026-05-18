from __future__ import annotations

import json

from agent.db._core import _open


def upsert_monitor(
    *,
    id: str,
    type: str,
    status: str,
    scope_json: str,
    poll_interval: int,
    created_at: str,
) -> None:
    conn = _open()
    try:
        conn.execute(
            """INSERT INTO monitors
               (id, type, status, scope_json, poll_interval, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status""",
            (id, type, status, scope_json, poll_interval, created_at),
        )
        conn.commit()
    finally:
        conn.close()

def update_monitor_status(monitor_id: str, status: str) -> None:
    conn = _open()
    try:
        conn.execute(
            "UPDATE monitors SET status = ? WHERE id = ?",
            (status, monitor_id),
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
            """SELECT id, type, status, scope_json, poll_interval, created_at
               FROM monitors WHERE status = 'active'"""
        ).fetchall()
        return [
            {
                "id": row[0],
                "type": row[1],
                "status": row[2],
                "scope": json.loads(row[3]),
                "poll_interval": row[4],
                "created_at": row[5],
            }
            for row in rows
        ]
    finally:
        conn.close()
