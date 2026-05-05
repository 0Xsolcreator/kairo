from __future__ import annotations

from abc import ABC, abstractmethod

from agent.schemas.monitor import Monitor


class BasePoller(ABC):
    @abstractmethod
    async def poll(self, monitor: Monitor) -> dict:
        """Fetch one snapshot of data for the given monitor. Must be overridden."""
        ...
