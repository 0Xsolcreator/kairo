"""
Generic chain-control actions — no protocol or wallet dependency.

These exist so chains can express simple flow logic (skip if condition,
plumb a value through under a different name) without dropping into
custom one-off Action classes.
"""
from __future__ import annotations

from agent.executor.base import Action, ActionContext, ChainAbort


class RequireMinimum(Action):
    """
    ChainAbort if `ctx.state[state_key]` is missing or below `min_value`.

    Use as a guard between steps — e.g. "skip deposit if balance is zero":

        ActionChain([
            LoadKaminoUnderlyingBalance(into_key="kamino_amount"),
            RequireMinimum("kamino_amount", 1, "already in Jupiter"),
            WithdrawFromKamino(amount_key="kamino_amount"),
            ...
        ])
    """

    def __init__(
        self,
        state_key: str,
        min_value: int = 1,
        message: str | None = None,
    ) -> None:
        self._state_key = state_key
        self._min_value = min_value
        self._message = message

    async def run(self, ctx: ActionContext) -> None:
        value = ctx.state.get(self._state_key)
        if not isinstance(value, int):
            raise ChainAbort(
                self._message
                or f"ctx.state[{self._state_key!r}] is missing or not an int (got {value!r})"
            )
        if value < self._min_value:
            raise ChainAbort(
                self._message
                or f"ctx.state[{self._state_key!r}]={value} below minimum {self._min_value}"
            )


class Copy(Action):
    """
    Copy ctx.state[src_key] to ctx.state[dst_key].

    Useful for plumbing a position amount into the slot a downstream action
    expects (e.g. `Copy("rebalance_amount", "amount")` before `DepositToJupiter`).
    Aborts the chain if the source key is missing.
    """

    def __init__(self, src_key: str, dst_key: str) -> None:
        self._src_key = src_key
        self._dst_key = dst_key

    async def run(self, ctx: ActionContext) -> None:
        if self._src_key not in ctx.state:
            raise ChainAbort(
                f"Copy: source key {self._src_key!r} not in ctx.state"
            )
        ctx.state[self._dst_key] = ctx.state[self._src_key]
