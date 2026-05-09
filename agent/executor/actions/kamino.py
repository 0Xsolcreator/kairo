"""
Kamino KVault chain actions.

Each action delegates to KaminoClient (agent/clients/kamino.py).

Vault resolution
----------------
Kamino has many vaults per token; the polling/analyzer pipeline picks
the best one and surfaces it as `decision.metadata["kamino_vault"]`.
Actions read that key for the *target* vault on a deposit. For withdraws,
the persisted vault address (the one we deposited into earlier) should
override the analyzer's current pick — see KaminoClient docstring on the
deposit-vault binding. Until that persistence layer lands, withdraws use
the metadata field too; flag this as a known gap to fix before live use.

Token decimals
--------------
Resolved from the monitor's mint via
agent.schemas.monitor.get_token_decimals() and passed through to the
client per call. The KTX API takes amounts in decimal token format, so a
wrong decimals value silently mis-scales by 10^n. Resolving from the
scope keeps the same client instance correct across USDC/USDT (6) and
wSOL (9).
"""
from __future__ import annotations

import logging

from agent.clients.kamino import KaminoClient, KaminoError, KaminoNotImplemented
from agent.executor.base import Action, ActionContext, ChainAbort
from agent.schemas.monitor import get_token_decimals

logger = logging.getLogger(__name__)

_client = KaminoClient()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_scope(ctx: ActionContext):
    from agent.store import get_monitor
    monitor = get_monitor(ctx.decision.monitor_id)
    if monitor is None:
        raise ChainAbort(f"monitor {ctx.decision.monitor_id} is not registered")
    return monitor.scope


def _resolve_vault(ctx: ActionContext) -> str:
    """Pull the kamino vault address from the analyzer's decision metadata."""
    vault = ctx.decision.metadata.get("kamino_vault")
    if not vault:
        raise ChainAbort(
            "no kamino_vault in decision.metadata — run the polling+analyzer "
            "pipeline at least once before invoking a Kamino chain"
        )
    return vault


def _resolve_decimals(ctx: ActionContext) -> int:
    mint = getattr(_resolve_scope(ctx), "token_mint", None)
    if not mint:
        raise ChainAbort(
            f"monitor {ctx.decision.monitor_id} scope has no token_mint"
        )
    try:
        return get_token_decimals(mint)
    except ValueError as e:
        raise ChainAbort(str(e)) from e


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

class LoadKaminoPosition(Action):
    """
    Read the current Kamino position in the analyzer-chosen vault and
    store it under `ctx.state[<into_key>]`.

    UNITS: returns share units, not underlying token base units. Suitable
    for presence checks via RequireMinimum (zero is zero in either unit
    system); MUST NOT be piped directly into `WithdrawFromKamino` as the
    amount — that path expects underlying base units. Use
    `LoadKaminoUnderlyingBalance` when you need an amount to withdraw.
    """

    def __init__(self, into_key: str = "kamino_position_shares") -> None:
        self._into_key = into_key

    async def run(self, ctx: ActionContext) -> None:
        vault = _resolve_vault(ctx)
        try:
            ctx.state[self._into_key] = await _client.get_position(
                _operating_keypair(ctx), vault,
            )
        except (KaminoNotImplemented, KaminoError) as e:
            raise ChainAbort(str(e)) from e


class LoadKaminoUnderlyingBalance(Action):
    """
    Read the current Kamino position expressed in underlying token base
    units and store it under `ctx.state[<into_key>]` (default
    'kamino_underlying'). This is the value that should be passed to
    `WithdrawFromKamino`'s `amount_key`.

    Backed by `KaminoClient.get_underlying_balance()`. Raises ChainAbort
    cleanly while that client method is still a stub — the rebalance chain
    halts with a clear "not implemented yet" instead of mis-scaling funds.
    """

    def __init__(self, into_key: str = "kamino_underlying") -> None:
        self._into_key = into_key

    async def run(self, ctx: ActionContext) -> None:
        vault = _resolve_vault(ctx)
        try:
            ctx.state[self._into_key] = await _client.get_underlying_balance(
                _operating_keypair(ctx), vault,
            )
        except (KaminoNotImplemented, KaminoError) as e:
            raise ChainAbort(str(e)) from e


class DepositToKamino(Action):
    """
    Deposit `amount` (base units) into the analyzer-chosen vault.

    Reads amount from ctx.state[<amount_key>]. Stores the tx signature at
    ctx.state[<sig_key>] (default 'kamino_deposit_sig').

    Set `skip_if_zero=True` when the amount may legitimately be 0 and the
    deposit should be a no-op rather than a ChainAbort (e.g. after
    SeedFromEncrypted when no encrypted balance was available).
    """

    def __init__(
        self,
        amount_key: str = "amount",
        sig_key: str = "kamino_deposit_sig",
        skip_if_zero: bool = False,
    ) -> None:
        self._amount_key = amount_key
        self._sig_key = sig_key
        self._skip_if_zero = skip_if_zero

    async def run(self, ctx: ActionContext) -> None:
        vault = _resolve_vault(ctx)
        value = ctx.state.get(self._amount_key)
        if self._skip_if_zero and (not isinstance(value, int) or value == 0):
            logger.debug("skipping kamino deposit: %s is zero", self._amount_key)
            return
        amount = _read_amount(ctx, self._amount_key)
        decimals = _resolve_decimals(ctx)
        if ctx.dry_run:
            logger.info("[dry_run] would kamino deposit %d into %s (decimals=%d)",
                        amount, vault, decimals)
            return
        try:
            sig = await _client.deposit(
                _operating_keypair(ctx), vault, amount, decimals,
            )
            ctx.state[self._sig_key] = sig
        except (KaminoNotImplemented, KaminoError) as e:
            raise ChainAbort(str(e)) from e


class WithdrawFromKamino(Action):
    """
    Withdraw `amount` (base units) from the kamino vault back to the
    operating wallet's public balance.

    KNOWN GAP: vault address is currently read from
    decision.metadata["kamino_vault"], which reflects the analyzer's
    *current* best pick. The vault we deposited into may differ if the
    analyzer rotated between deposit and withdraw. Persist the
    deposited-vault address per monitor and override here once that
    persistence layer lands (see KaminoClient module docstring).

    Reads amount from ctx.state[<amount_key>]. Stores tx signature at
    ctx.state[<sig_key>] (default 'kamino_withdraw_sig').

    Set `skip_if_zero=True` to silently skip when amount is 0 instead of
    aborting — useful when Kamino may or may not have a position.
    """

    def __init__(
        self,
        amount_key: str = "amount",
        sig_key: str = "kamino_withdraw_sig",
        skip_if_zero: bool = False,
    ) -> None:
        self._amount_key = amount_key
        self._sig_key = sig_key
        self._skip_if_zero = skip_if_zero

    async def run(self, ctx: ActionContext) -> None:
        vault = _resolve_vault(ctx)
        value = ctx.state.get(self._amount_key)
        if self._skip_if_zero and (not isinstance(value, int) or value == 0):
            logger.debug("skipping kamino withdraw: %s is zero", self._amount_key)
            return
        amount = _read_amount(ctx, self._amount_key)
        decimals = _resolve_decimals(ctx)
        if ctx.dry_run:
            logger.info("[dry_run] would kamino withdraw %d from %s (decimals=%d)",
                        amount, vault, decimals)
            return
        try:
            sig = await _client.withdraw(
                _operating_keypair(ctx), vault, amount, decimals,
            )
            ctx.state[self._sig_key] = sig
        except (KaminoNotImplemented, KaminoError) as e:
            raise ChainAbort(str(e)) from e
