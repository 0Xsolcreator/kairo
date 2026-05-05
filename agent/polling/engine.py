from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from agent.polling.base import BasePoller
from agent.polling.deposit_earn import DepositEarnPoller
from agent.schemas.monitor import Monitor, MonitorStatus

logger = logging.getLogger(__name__)

OnDataCallback = Callable[[Monitor, dict], Awaitable[None]]


class PollingEngine:
    """Manages one async polling task per active monitor."""

    def __init__(self, on_data: OnDataCallback) -> None:
        self._on_data = on_data
        self._tasks: dict[str, asyncio.Task] = {}
        self._pollers: dict[str, BasePoller] = {
            "deposit_earn": DepositEarnPoller(),
        }

    def start(self, monitor: Monitor) -> None:
        if monitor.id in self._tasks:
            return
        self._tasks[monitor.id] = asyncio.create_task(
            self._run(monitor), name=f"poll-{monitor.id}"
        )
        logger.info("Started polling monitor %s (type=%s)", monitor.id, monitor.type)

    def stop(self, monitor_id: str) -> None:
        task = self._tasks.pop(monitor_id, None)
        if task:
            task.cancel()
            logger.info("Stopped polling monitor %s", monitor_id)

    async def stop_all(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    def register_poller(self, monitor_type: str, poller: BasePoller) -> None:
        self._pollers[monitor_type] = poller

    async def _run(self, monitor: Monitor) -> None:
        poller = self._pollers.get(monitor.type)
        if poller is None:
            logger.warning(
                "No poller for monitor type %r — skipping %s", monitor.type, monitor.id
            )
            return

        while monitor.status == MonitorStatus.active:
            try:
                data = await poller.poll(monitor)
                await self._on_data(monitor, data)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Poll failed for monitor %s", monitor.id)

            await asyncio.sleep(monitor.poll_interval)
