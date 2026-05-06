"""
DebouncedExecutor — wraps another BaseExecutor and enforces three
guards before letting a Decision through to the inner executor:

  1. consecutive_required: the same actionable signal must repeat N
     times in a row before the chain is allowed to fire. Prevents
     reacting to a single noisy poll.

  2. cooldown_seconds: after a successful pass-through, no further
     chains run for the same monitor for M seconds. Prevents
     gas-burning ping-pong when two protocols are near parity.

  3. ignored_signals: signals listed here (e.g. EQUAL, ERROR) never
     pass through and reset the consecutive-streak counter. Useful
     so a brief ERROR blip doesn't carry over into the streak math.

State is per-monitor and lives in memory; if the agent restarts the
streak resets. Cooldowns also reset on restart — accept this for now,
or persist to db.py if it becomes important.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict

from agent.analyzer.base import Decision
from agent.executor.base import BaseExecutor

logger = logging.getLogger(__name__)


class DebouncedExecutor(BaseExecutor):
    def __init__(
        self,
        inner: BaseExecutor,
        *,
        consecutive_required: int = 3,
        cooldown_seconds: float = 600.0,
        ignored_signals: tuple[str, ...] = ("EQUAL", "ERROR"),
    ) -> None:
        self._inner = inner
        self._consecutive_required = consecutive_required
        self._cooldown_seconds = cooldown_seconds
        self._ignored = set(ignored_signals)

        # per-monitor state
        self._streak: dict[str, tuple[str, int]] = {}      # monitor_id → (signal, count)
        self._last_run: dict[str, float] = defaultdict(float)

    async def handle(self, decision: Decision) -> None:
        mid = decision.monitor_id
        sig = decision.signal

        if sig in self._ignored:
            # Reset streak — a brief no-op signal shouldn't preserve a half-built streak.
            self._streak.pop(mid, None)
            return

        # Streak accounting
        last_signal, count = self._streak.get(mid, (None, 0))
        count = count + 1 if last_signal == sig else 1
        self._streak[mid] = (sig, count)

        if count < self._consecutive_required:
            logger.debug(
                "debounce monitor=%s signal=%s streak=%d/%d — waiting",
                mid, sig, count, self._consecutive_required,
            )
            return

        # Cooldown check
        now = time.monotonic()
        elapsed = now - self._last_run[mid]
        if elapsed < self._cooldown_seconds:
            logger.info(
                "debounce monitor=%s signal=%s in cooldown (%.0fs / %.0fs)",
                mid, sig, elapsed, self._cooldown_seconds,
            )
            return

        # Pass through
        self._last_run[mid] = now
        # Reset streak so the next chain requires another N consecutive signals.
        self._streak.pop(mid, None)
        await self._inner.handle(decision)
