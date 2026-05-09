"""
Kamino KVault chain actions.

Each action delegates to KaminoClient (agent/clients/kamino.py).

Vault resolution
----------------
Two helpers handle vault address resolution:

  _resolve_deposit_vault  — reads decision.metadata["kamino_vault"], the
    current analyzer pick. Used only by DepositToKamino (we deposit where
    the analyzer says is best right now).

  _resolve_deposited_vault — reads the vault address we previously wrote
    to DB after a successful deposit. Used by WithdrawFromKamino and
    LoadKaminoUnderlyingBalance. This is the vault our funds are actually
    in, regardless of what the analyzer currently recommends.

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

import asyncio
import logging

import agent.db as db
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


def _resolve_deposit_vault(ctx: ActionContext) -> str:
    """For deposits: the current analyzer pick from decision metadata."""
    vault = ctx.decision.metadata.get("kamino_vault")
    if not vault:
        raise ChainAbort(
            "no kamino_vault in decision.metadata — run the polling+analyzer "
            "pipeline at least once before invoking a Kamino chain"
        )
    return vault


def _resolve_deposited_vault(ctx: ActionContext) -> str:
    """
    For withdraws/reads: the vault we actually deposited into, from DB.
    This is authoritative — it won't rotate even if the analyzer's current
    best vault has changed since the deposit.
    """
    vault = db.get_kamino_deposited_vault(ctx.decision.monitor_id)
    if not vault:
        raise ChainAbort(
            "no Kamino deposit on record for this monitor — "
            "nothing to withdraw (has a KAMINO signal completed first?)"
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
    Read the current Kamino position in the vault we last deposited into
    and store it under `ctx.state[<into_key>]`.

    UNITS: returns share units, not underlying token base units. Suitable
    for presence checks via RequireMinimum (zero is zero in either unit
    system); MUST NOT be piped directly into `WithdrawFromKamino` as the
    amount — that path expects underlying base units. Use
    `LoadKaminoUnderlyingBalance` when you need an amount to withdraw.
    """

    def __init__(self, into_key: str = "kamino_position_shares") -> None:
        self._into_key = into_key

    async def run(self, ctx: ActionContext) -> None:
        vault = _resolve_deposited_vault(ctx)
        try:
            ctx.state[self._into_key] = await _client.get_position(
                _operating_keypair(ctx), vault,
            )
        except (KaminoNotImplemented, KaminoError) as e:
            raise ChainAbort(str(e)) from e


class LoadKaminoUnderlyingBalance(Action):
    """
    Read the Kamino position in the vault we last deposited into, expressed
    in underlying token base units, and store it under `ctx.state[<into_key>]`
    (default 'kamino_underlying'). This is the value that should be passed to
    `WithdrawFromKamino`'s `amount_key`.

    Reads from the persisted deposited vault — not the analyzer's current pick
    — so it stays correct even if the best vault has rotated since the deposit.

    Raises ChainAbort cleanly while KaminoClient.get_underlying_balance() is
    still a stub.
    """

    def __init__(self, into_key: str = "kamino_underlying") -> None:
        self._into_key = into_key

    async def run(self, ctx: ActionContext) -> None:
        vault = _resolve_deposited_vault(ctx)
        try:
            ctx.state[self._into_key] = await _client.get_underlying_balance(
                _operating_keypair(ctx), vault,
            )
        except (KaminoNotImplemented, KaminoError) as e:
            raise ChainAbort(str(e)) from e


class DepositToKamino(Action):
    """
    Deposit `amount` (base units) into the analyzer-chosen vault.

    Reads amount from ctx.state[<amount_key>]. After a successful on-chain
    deposit, persists the vault address to DB so subsequent withdrawals
    target the correct vault even if the analyzer's recommendation rotates.
    Stores the tx signature at ctx.state[<sig_key>] (default 'kamino_deposit_sig').

    Set `skip_if_zero=True` when the amount may legitimately be 0 and the
    deposit should be a no-op rather than a ChainAbort.
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
        vault = _resolve_deposit_vault(ctx)
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
        # Persist the vault so future withdrawals target the right address.
        await asyncio.to_thread(db.set_kamino_deposited_vault, ctx.decision.monitor_id, vault)


class WithdrawFromKamino(Action):
    """
    Withdraw `amount` (base units) from the vault we previously deposited
    into, back to the operating wallet's public balance.

    Vault address is read from DB (set by DepositToKamino on a prior chain
    run) — not from decision.metadata. This is intentional: the analyzer's
    current best vault may have rotated since the deposit, so using metadata
    would target the wrong vault and move zero funds.

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
        vault = _resolve_deposited_vault(ctx)
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
