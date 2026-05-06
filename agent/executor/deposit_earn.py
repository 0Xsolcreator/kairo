"""
Action chains for the deposit_earn monitor type.

Signals:
  JUPITER  → withdraw from Kamino, deposit to Jupiter
  KAMINO   → withdraw from Jupiter, deposit to Kamino
  EQUAL    → no chain (filtered upstream by DebouncedExecutor.ignored_signals)
  ERROR    → no chain (filtered upstream by DebouncedExecutor.ignored_signals)

The chains are intentionally empty for now. Reusable Actions live under
agent/executor/actions/ once they exist; protocol clients live under
agent/clients/.
"""
from __future__ import annotations

from agent.executor.base import ActionChain, ChainBasedExecutor


class DepositEarnExecutor(ChainBasedExecutor):
    def __init__(self, dry_run: bool = False) -> None:
        super().__init__(
            chains={
                "JUPITER": ActionChain(actions=[
                    # TODO: Kamino → Jupiter rebalance
                ]),
                "KAMINO": ActionChain(actions=[
                    # TODO: Jupiter → Kamino rebalance
                ]),
            },
            dry_run=dry_run,
        )
