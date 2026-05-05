from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from agent.analyzer.base import BaseAnalyzer, Decision
from agent.analyzer.deposit_earn import DepositEarnAnalyzer
from agent.schemas.monitor import Monitor

logger = logging.getLogger(__name__)

OnDecisionCallback = Callable[[Decision], Awaitable[None]]


class AnalyzerEngine:
    """
    Sits between PollingEngine and the signal store.

    Wire up:
        analyzer = AnalyzerEngine(on_decision=my_callback)
        polling  = PollingEngine(on_data=analyzer.ingest)
    """

    def __init__(self, on_decision: OnDecisionCallback) -> None:
        self._on_decision = on_decision
        self._analyzers: dict[str, BaseAnalyzer] = {
            "deposit_earn": DepositEarnAnalyzer(),
        }

    def register_analyzer(self, monitor_type: str, analyzer: BaseAnalyzer) -> None:
        self._analyzers[monitor_type] = analyzer

    async def ingest(self, monitor: Monitor, raw: dict) -> None:
        analyzer = self._analyzers.get(monitor.type)
        if analyzer is None:
            logger.warning("No analyzer for monitor type %r", monitor.type)
            return

        try:
            decision = analyzer.analyze(monitor, raw)
            logger.debug(
                "monitor=%s signal=%s reason=%s",
                decision.monitor_id,
                decision.signal,
                decision.reason,
            )
            await self._on_decision(decision)
        except Exception:
            logger.exception("Analyzer error for monitor %s", monitor.id)
