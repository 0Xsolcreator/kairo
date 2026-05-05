from __future__ import annotations

import agent.store as store
from agent.analyzer import AnalyzerEngine
from agent.analyzer.base import Decision
from agent.polling import PollingEngine


async def _on_decision(decision: Decision) -> None:
    store.push_signal(decision)


analyzer = AnalyzerEngine(on_decision=_on_decision)
polling = PollingEngine(on_data=analyzer.ingest)
