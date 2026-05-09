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

Restart behaviour
-----------------
_streak is intentionally NOT restored on restart — streaks are transient
debounce state; a clean slate after restart is safe and expected.

_last_run IS restored via restore_cooldown(), which reads the most recent
completed chain execution time from DB and reconstructs the monotonic
timestamp. This prevents a restart from immediately re-firing a chain that
is still within its cooldown window.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

import agent.db as db
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

    def restore_cooldown(self, monitor_id: str) -> None:
        """
        Reconstruct _last_run from the most recent completed chain execution
        stored in DB. If the cooldown has already expired, nothing is written
        (the default float(0) means "ran at epoch", i.e. cooldown long gone).
        """
        ts_str = db.get_last_completed_execution_time(monitor_id)
        if ts_str is None:
            return
        last_finished = datetime.fromisoformat(ts_str)
        elapsed = (datetime.now(timezone.utc) - last_finished).total_seconds()
        if elapsed < self._cooldown_seconds:
            self._last_run[monitor_id] = time.monotonic() - elapsed
            logger.info(
                "debounce monitor=%s cooldown restored: %.0fs elapsed of %.0fs",
                monitor_id, elapsed, self._cooldown_seconds,
            )

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
