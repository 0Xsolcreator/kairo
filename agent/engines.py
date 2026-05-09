from __future__ import annotations

import logging
from dataclasses import dataclass

import agent.store as store
from agent.analyzer import AnalyzerEngine
from agent.analyzer.base import Decision
from agent.executor import ExecutorEngine
from agent.executor.debounce import DebouncedExecutor
from agent.executor.deposit_earn import DepositEarnExecutor
from agent.polling import PollingEngine
from agent.wallet import WalletService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Engines container
# ---------------------------------------------------------------------------

@dataclass
class Engines:
    """All runtime singletons, fully wired and ready to use."""
    wallet:   WalletService
    executor: ExecutorEngine
    analyzer: AnalyzerEngine
    polling:  PollingEngine

    async def restore_active_monitors(self) -> None:
        """
        Reload monitors from DB and restart their polling tasks.
        Also restores executor cooldown state so a restart doesn't bypass the
        cooldown window on chains that fired shortly before the agent stopped.
        """
        monitors = store.load_from_db()
        for monitor in monitors:
            self.polling.start(monitor)
            self.executor.restore_cooldowns(monitor.id, monitor.type)
        if monitors:
            logger.info("Restored %d monitor(s) from DB", len(monitors))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_engines(db_path: str = "agent_data.db") -> Engines:
    """
    Create and wire all runtime singletons.

    Pass db_path=':memory:' in tests to get a fully isolated instance
    without touching the on-disk database.
    """
    from agent.db._core import _set_db_path
    _set_db_path(db_path)

    _wallet = WalletService()

    _executor = ExecutorEngine()
    _executor.register_executor(
        "deposit_earn",
        DebouncedExecutor(
            DepositEarnExecutor(),
            consecutive_required=3,
            cooldown_seconds=600.0,
        ),
    )

    async def _on_decision(decision: Decision) -> None:
        store.push_signal(decision)
        await _executor.handle(decision)

    _analyzer = AnalyzerEngine(on_decision=_on_decision)
    _polling  = PollingEngine(on_data=_analyzer.ingest)

    return Engines(
        wallet=_wallet,
        executor=_executor,
        analyzer=_analyzer,
        polling=_polling,
    )


# ---------------------------------------------------------------------------
# Application-level singletons
# ---------------------------------------------------------------------------
# Existing callers (graph.py, services/, action files, main.py) all reference
# these module-level names directly and continue to work unchanged.

_default = create_engines()

wallet   = _default.wallet
executor = _default.executor
analyzer = _default.analyzer
polling  = _default.polling


async def restore_active_monitors() -> None:
    """Module-level convenience wrapper used by main.py at startup."""
    await _default.restore_active_monitors()
