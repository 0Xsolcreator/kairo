from __future__ import annotations

from collections import defaultdict, deque

from agent.analyzer.base import Decision
from agent.schemas.monitor import Monitor

_MAX_SIGNALS = 100

_signals: dict[str, deque[Decision]] = defaultdict(lambda: deque(maxlen=_MAX_SIGNALS))
_monitors: dict[str, Monitor] = {}


def push_signal(decision: Decision) -> None:
    _signals[decision.monitor_id].append(decision)


def get_signals(monitor_id: str, limit: int = 10) -> list[Decision]:
    return list(_signals[monitor_id])[-limit:]


def register_monitor(monitor: Monitor) -> None:
    _monitors[monitor.id] = monitor


def unregister_monitor(monitor_id: str) -> None:
    _monitors.pop(monitor_id, None)
    _signals.pop(monitor_id, None)


def get_monitor(monitor_id: str) -> Monitor | None:
    return _monitors.get(monitor_id)


def list_monitors() -> list[Monitor]:
    return list(_monitors.values())
