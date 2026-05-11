"""
Core building blocks for chain-based monitor executors.

Pattern
-------
- An `Action` is a single async unit of work (e.g. "check wallet balance",
  "deposit to Jupiter"). It reads from `ctx.state` and writes outputs back
  into it so later steps can consume them.
- An `ActionChain` is an ordered list of Actions plus an optional `when`
  predicate on the Decision. The chain walks its actions in order, stops
  on `ChainAbort` (clean) or any other exception (failure), and reports
  the outcome via `ChainResult`.
- A `ChainBasedExecutor` maps `decision.signal` → `ActionChain`. Subclass
  it per monitor type and pass in the chain map.

Failure model
-------------
- `ChainAbort` halts the chain cleanly (e.g. "balance too low — nothing to do").
  Logged at INFO, not ERROR.
- Any other exception halts the chain as a failure. Logged with stack trace.
- Per-action retry is governed by `Action.retry_policy` (default: no retry).
  The chain catches retryable exceptions and re-runs the same action with
  exponential backoff before giving up.
- There is intentionally no compensation/rollback mechanism. On-chain
  rollback is hard and protocol-specific; handle it explicitly inside
  individual actions when needed.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.analyzer.base import Decision
from agent.db import async_finish_execution, async_start_execution

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ChainAbort(Exception):
    """Raised inside an action to stop the chain cleanly (no error)."""


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetryPolicy:
    """
    Per-action retry configuration.

    By default actions do NOT retry. Opt in by overriding `retry_policy` on
    an Action subclass or by passing a custom policy where the action is
    constructed.

    Only exceptions matching `retryable` are retried; everything else
    propagates immediately. `ChainAbort` is never retried.
    """
    max_attempts: int = 1
    base_delay: float = 1.0          # seconds, exponential backoff base
    max_delay: float = 30.0
    retryable: tuple[type[Exception], ...] = ()


# ---------------------------------------------------------------------------
# Context shared between actions in one chain run
# ---------------------------------------------------------------------------

@dataclass
class ActionContext:
    decision: Decision
    state: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------

class Action(ABC):
    """One step in an ActionChain. Override `run`."""

    retry_policy: RetryPolicy = RetryPolicy()

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    async def run(self, ctx: ActionContext) -> None:
        """Execute the step. Mutate ctx.state to pass data downstream."""


# ---------------------------------------------------------------------------
# Chain result
# ---------------------------------------------------------------------------

@dataclass
class ChainResult:
    status: str               # "completed" | "aborted" | "failed" | "skipped"
    steps_completed: int
    error: str | None = None


# ---------------------------------------------------------------------------
# Action chain
# ---------------------------------------------------------------------------

WhenPredicate = Callable[[Decision], bool]


class ActionChain:
    """An ordered list of Actions, optionally gated by a predicate on Decision."""

    def __init__(
        self,
        actions: list[Action],
        when: WhenPredicate | None = None,
    ) -> None:
        self._actions = actions
        self._when = when

    def applies_to(self, decision: Decision) -> bool:
        return self._when is None or self._when(decision)

    async def execute(self, ctx: ActionContext) -> ChainResult:
        if not self.applies_to(ctx.decision):
            return ChainResult(status="skipped", steps_completed=0)

        steps_completed = 0
        for action in self._actions:
            try:
                await self._run_with_retry(action, ctx)
                steps_completed += 1
            except ChainAbort as e:
                logger.info(
                    "chain monitor=%s signal=%s aborted at %s: %s",
                    ctx.decision.monitor_id,
                    ctx.decision.signal,
                    action.name,
                    e,
                )
                return ChainResult(
                    status="aborted",
                    steps_completed=steps_completed,
                    error=str(e),
                )
            except Exception as e:
                logger.exception(
                    "chain monitor=%s signal=%s failed at %s",
                    ctx.decision.monitor_id,
                    ctx.decision.signal,
                    action.name,
                )
                return ChainResult(
                    status="failed",
                    steps_completed=steps_completed,
                    error=f"{type(e).__name__}: {e}",
                )
        return ChainResult(status="completed", steps_completed=steps_completed)

    async def _run_with_retry(self, action: Action, ctx: ActionContext) -> None:
        policy = action.retry_policy
        attempt = 0
        while True:
            attempt += 1
            try:
                await action.run(ctx)
                return
            except ChainAbort:
                raise
            except Exception as e:
                last_attempt = attempt >= policy.max_attempts
                retryable = bool(policy.retryable) and isinstance(e, policy.retryable)
                if last_attempt or not retryable:
                    raise
                delay = min(
                    policy.base_delay * (2 ** (attempt - 1)),
                    policy.max_delay,
                )
                logger.warning(
                    "action %s failed (%s); retry %d/%d in %.1fs",
                    action.name, type(e).__name__,
                    attempt, policy.max_attempts, delay,
                )
                await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Executor base
# ---------------------------------------------------------------------------

class BaseExecutor(ABC):
    """Routes a Decision to whatever should happen next for its monitor type."""

    @abstractmethod
    async def handle(self, decision: Decision) -> ChainResult | None: ...

    def restore_cooldown(self, monitor_id: str) -> None:
        """Reconstruct in-memory cooldown state from persistent storage.
        No-op by default; override in wrappers that track cooldown (e.g. DebouncedExecutor)."""


class ChainBasedExecutor(BaseExecutor):
    """
    An executor that picks an ActionChain by signal and runs it.

    Persists each non-dry-run chain execution to the `chain_executions`
    SQLite table via agent.db helpers.
    """

    def __init__(
        self,
        chains: dict[str, ActionChain],
        dry_run: bool = False,
    ) -> None:
        self._chains = chains
        self._dry_run = dry_run

    async def handle(self, decision: Decision) -> ChainResult | None:
        chain = self._chains.get(decision.signal)
        if chain is None:
            return None

        ctx = ActionContext(decision=decision, dry_run=self._dry_run)

        execution_id: int | None = None
        if not self._dry_run:
            execution_id = await async_start_execution(
                monitor_id=decision.monitor_id,
                monitor_type=decision.monitor_type,
                signal=decision.signal,
            )

        result = await chain.execute(ctx)

        if execution_id is not None:
            await async_finish_execution(
                execution_id,
                status=result.status,
                steps_completed=result.steps_completed,
                error=result.error,
                state=ctx.state,
            )

        return result
