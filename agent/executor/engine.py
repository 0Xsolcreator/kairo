"""
ExecutorEngine — routes analyzer Decisions to the executor registered
for their monitor type.

Mirrors PollingEngine and AnalyzerEngine: same register-by-type pattern,
same place in the data flow (one step downstream of the analyzer).

Concurrency: each monitor gets an asyncio.Lock. If a chain is already
running for that monitor when a new Decision arrives, the new Decision
is dropped with a log line — important because on-chain actions cost
gas and must not overlap. Use the debounce wrapper to control how often
chains fire in the first place.
"""
from __future__ import annotations

import asyncio
import logging

from agent.analyzer.base import Decision
from agent.executor.base import BaseExecutor

logger = logging.getLogger(__name__)


class ExecutorEngine:
    def __init__(self) -> None:
        self._executors: dict[str, BaseExecutor] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def register_executor(self, monitor_type: str, executor: BaseExecutor) -> None:
        self._executors[monitor_type] = executor

    def restore_cooldowns(self, monitor_id: str, monitor_type: str) -> None:
        """Delegate cooldown restoration to the executor registered for this monitor type."""
        executor = self._executors.get(monitor_type)
        if executor is not None:
            executor.restore_cooldown(monitor_id)

    async def handle(self, decision: Decision) -> None:
        executor = self._executors.get(decision.monitor_type)
        if executor is None:
            return

        lock = self._locks.setdefault(decision.monitor_id, asyncio.Lock())
        if lock.locked():
            logger.info(
                "executor monitor=%s busy — dropping signal=%s",
                decision.monitor_id,
                decision.signal,
            )
            return

        async with lock:
            try:
                await executor.handle(decision)
            except Exception:
                logger.exception(
                    "executor monitor=%s signal=%s crashed",
                    decision.monitor_id,
                    decision.signal,
                )
