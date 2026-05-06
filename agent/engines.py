from __future__ import annotations

import agent.store as store
from agent.analyzer import AnalyzerEngine
from agent.analyzer.base import Decision
from agent.executor import ExecutorEngine
from agent.executor.debounce import DebouncedExecutor
from agent.executor.deposit_earn import DepositEarnExecutor
from agent.polling import PollingEngine

# ---------------------------------------------------------------------------
# Executor wiring
# ---------------------------------------------------------------------------
# Each monitor type registers its own executor here. Wrap in DebouncedExecutor
# to control how often chains are allowed to fire — important for any executor
# that performs on-chain actions.

executor = ExecutorEngine()

executor.register_executor(
    "deposit_earn",
    DebouncedExecutor(
        DepositEarnExecutor(),
        consecutive_required=3,     # need 3 polls in a row agreeing before acting
        cooldown_seconds=600.0,     # ...and 10 minutes between chain runs
    ),
)


# ---------------------------------------------------------------------------
# Decision pipeline
# ---------------------------------------------------------------------------
# polling → analyzer → (store signal + hand to executor)

async def _on_decision(decision: Decision) -> None:
    store.push_signal(decision)
    await executor.handle(decision)


analyzer = AnalyzerEngine(on_decision=_on_decision)
polling = PollingEngine(on_data=analyzer.ingest)
