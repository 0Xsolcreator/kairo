from __future__ import annotations

import json
import logging
from collections import defaultdict, deque

import agent.db as db
from agent.analyzer.base import Decision
from agent.schemas.monitor import Monitor, MonitorStatus

logger = logging.getLogger(__name__)

_MAX_SIGNALS = 100

_signals: dict[str, deque[Decision]] = defaultdict(lambda: deque(maxlen=_MAX_SIGNALS))
_monitors: dict[str, Monitor] = {}


def push_signal(decision: Decision) -> None:
    _signals[decision.monitor_id].append(decision)


def get_signals(monitor_id: str, limit: int = 10) -> list[Decision]:
    return list(_signals[monitor_id])[-limit:]


def register_monitor(monitor: Monitor) -> None:
    _monitors[monitor.id] = monitor
    db.upsert_monitor(
        id=monitor.id,
        type=monitor.type,
        status=monitor.status.value,
        scope_json=json.dumps(monitor.scope.model_dump()),
        poll_interval=monitor.poll_interval,
        created_at=monitor.created_at.isoformat(),
    )


def update_monitor_status(monitor_id: str, status: str) -> None:
    monitor = _monitors.get(monitor_id)
    if monitor:
        monitor.status = MonitorStatus(status)
    db.update_monitor_status(monitor_id, status)


def unregister_monitor(monitor_id: str) -> None:
    _monitors.pop(monitor_id, None)
    _signals.pop(monitor_id, None)
    db.delete_monitor(monitor_id)


def get_monitor(monitor_id: str) -> Monitor | None:
    return _monitors.get(monitor_id)


def list_monitors() -> list[Monitor]:
    return list(_monitors.values())


def load_from_db() -> list[Monitor]:
    """
    Populate in-memory state from persisted active monitors.
    Called once at agent startup before the event loop begins polling.
    Already-registered monitors are skipped (idempotent).
    """
    rows = db.load_active_monitors()
    restored: list[Monitor] = []
    for row in rows:
        if row["id"] in _monitors:
            continue
        try:
            monitor = Monitor(**row)
            _monitors[monitor.id] = monitor
            restored.append(monitor)
        except Exception:
            logger.warning("Skipping malformed monitor record id=%s", row.get("id"))
    return restored
