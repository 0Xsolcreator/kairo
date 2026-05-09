"""
Jupiter Lend chain actions.

Each action delegates to JupiterClient (agent/clients/jupiter.py) for the
actual on-chain work. The action surface is stable; until the client's
methods are implemented, chains that include these will ChainAbort with a
clear "not implemented" message rather than silently no-op.

All amounts are token base units (positive int). Token mint comes from
the monitor's stored scope (DepositEarnScope.token_mint) — no need to
plumb it through ctx.state.
"""
from __future__ import annotations

import logging

from agent.clients.jupiter import JupiterClient, JupiterError, JupiterNotImplemented
from agent.executor.base import Action, ActionContext, ChainAbort

logger = logging.getLogger(__name__)

# Shared client — no per-instance state worth separating.
_client = JupiterClient()


# ---------------------------------------------------------------------------
# Helpers (small, kept local — same shape as the umbra action helpers)
# ---------------------------------------------------------------------------

def _resolve_scope(ctx: ActionContext):
    from agent.store import get_monitor
    monitor = get_monitor(ctx.decision.monitor_id)
    if monitor is None:
        raise ChainAbort(f"monitor {ctx.decision.monitor_id} is not registered")
    return monitor.scope


def _resolve_token_mint(ctx: ActionContext) -> str:
    mint = getattr(_resolve_scope(ctx), "token_mint", None)
    if not mint:
        raise ChainAbort(f"monitor {ctx.decision.monitor_id} scope has no token_mint")
    return mint


def _resolve_api_key(ctx: ActionContext) -> str:
    key = getattr(_resolve_scope(ctx), "jup_api_key", None)
    if not key:
        raise ChainAbort(
            f"monitor {ctx.decision.monitor_id} has no jup_api_key — "
            "set it in the monitor scope when configuring the monitor"
        )
    return key


def _read_amount(ctx: ActionContext, key: str) -> int:
    value = ctx.state.get(key)
    if not isinstance(value, int) or value <= 0:
        raise ChainAbort(
            f"expected positive int at ctx.state[{key!r}], got {value!r}"
        )
    return value


def _operating_keypair(ctx: ActionContext):
    from agent.engines import wallet
    kp = wallet.get_operating_keypair(ctx.decision.monitor_id)
    if kp is None:
        raise ChainAbort(
            f"no operating keypair derivable for monitor {ctx.decision.monitor_id}"
        )
    return kp


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

class LoadJupiterPosition(Action):
    """
    Read the current Jupiter Lend position size for the monitor's token
    and store it (base units, int) under `ctx.state[<into_key>]`.

    Returns 0 if there's no position, so callers can guard with
    RequireMinimum.
    """

    def __init__(self, into_key: str = "jupiter_position") -> None:
        self._into_key = into_key

    async def run(self, ctx: ActionContext) -> None:
        mint = _resolve_token_mint(ctx)
        api_key = _resolve_api_key(ctx)
        try:
            ctx.state[self._into_key] = await _client.get_position(
                _operating_keypair(ctx), mint, api_key,
            )
        except (JupiterNotImplemented, JupiterError) as e:
            raise ChainAbort(str(e)) from e


class DepositToJupiter(Action):
    """
    Deposit `amount` (base units) from the operating wallet's public balance
    into Jupiter Lend.

    Reads amount from ctx.state[<amount_key>]. Stores the resulting tx
    signature at ctx.state[<sig_key>] (default 'jupiter_deposit_sig').

    Set `skip_if_zero=True` when the amount may legitimately be 0 and the
    deposit should be a no-op rather than a ChainAbort (e.g. after
    SeedFromEncrypted when no encrypted balance was available).
    """

    def __init__(
        self,
        amount_key: str = "amount",
        sig_key: str = "jupiter_deposit_sig",
        skip_if_zero: bool = False,
    ) -> None:
        self._amount_key = amount_key
        self._sig_key = sig_key
        self._skip_if_zero = skip_if_zero

    async def run(self, ctx: ActionContext) -> None:
        mint = _resolve_token_mint(ctx)
        value = ctx.state.get(self._amount_key)
        if self._skip_if_zero and (not isinstance(value, int) or value == 0):
            logger.debug("skipping jupiter deposit: %s is zero", self._amount_key)
            return
        amount = _read_amount(ctx, self._amount_key)
        api_key = _resolve_api_key(ctx)
        if ctx.dry_run:
            logger.info("[dry_run] would jupiter deposit %d of %s", amount, mint)
            return
        try:
            sig = await _client.deposit(_operating_keypair(ctx), mint, amount, api_key)
            ctx.state[self._sig_key] = sig
        except (JupiterNotImplemented, JupiterError) as e:
            raise ChainAbort(str(e)) from e


class WithdrawFromJupiter(Action):
    """
    Withdraw `amount` (base units) from Jupiter Lend back to the operating
    wallet's public balance.

    Reads amount from ctx.state[<amount_key>]. Stores tx signature at
    ctx.state[<sig_key>] (default 'jupiter_withdraw_sig').

    Set `skip_if_zero=True` to silently skip when amount is 0 instead of
    aborting — useful when Jupiter may or may not have a position.
    """

    def __init__(
        self,
        amount_key: str = "amount",
        sig_key: str = "jupiter_withdraw_sig",
        skip_if_zero: bool = False,
    ) -> None:
        self._amount_key = amount_key
        self._sig_key = sig_key
        self._skip_if_zero = skip_if_zero

    async def run(self, ctx: ActionContext) -> None:
        mint = _resolve_token_mint(ctx)
        value = ctx.state.get(self._amount_key)
        if self._skip_if_zero and (not isinstance(value, int) or value == 0):
            logger.debug("skipping jupiter withdraw: %s is zero", self._amount_key)
            return
        amount = _read_amount(ctx, self._amount_key)
        api_key = _resolve_api_key(ctx)
        if ctx.dry_run:
            logger.info("[dry_run] would jupiter withdraw %d of %s", amount, mint)
            return
        try:
            sig = await _client.withdraw(_operating_keypair(ctx), mint, amount, api_key)
            ctx.state[self._sig_key] = sig
        except (JupiterNotImplemented, JupiterError) as e:
            raise ChainAbort(str(e)) from e
