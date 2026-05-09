"""
Umbra-CLI-backed actions for chain composition.

Conventions
-----------
- Every chain that touches Umbra must run `EnsureUmbraUser` first. It writes
  the per-monitor keypair to disk in Solana JSON format, registers it with
  the CLI under a stable name (`qvac-<monitor_id>`), runs `umbra register`,
  and leaves the active user set to that name. The other actions in this
  module assume the active user is already correct.

- Actions that read/write encrypted balances pull the token mint from the
  monitor's stored scope (`agent.store.get_monitor(...).scope.token_mint`).
  No need to plumb it through ctx.state.

- Amounts are in base units (lamports for SOL, raw integer for SPL tokens —
  for a 6-decimal token, 1_000_000 = 1 token). Actions read them from
  `ctx.state[<amount_key>]`; raise ChainAbort if the value is missing or
  not a positive int.

Dependencies
------------
- The `umbra` CLI must be installed and `umbra config set rpc <url>` must
  have been run once. EnsureUmbraUser will ChainAbort with a clear message
  if the binary is missing.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from agent.clients.umbra import (
    UmbraClient,
    UmbraCommandFailed,
    UmbraNotInstalled,
)
from agent.executor.base import Action, ActionContext, ChainAbort

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KEYPAIR_DIR = Path(
    os.environ.get(
        "QVAC_KEYPAIR_DIR",
        os.path.join(os.path.expanduser("~"), ".qvac", "keypairs"),
    )
)


def _qvac_user_name(monitor_id: str) -> str:
    """Stable umbra user name for a given monitor."""
    return f"qvac-{monitor_id}"


def _write_solana_keypair(secret_64_bytes: bytes, path: Path) -> None:
    """Write a 64-byte (priv || pub) blob in Solana's keypair JSON-array format."""
    if len(secret_64_bytes) != 64:
        raise ValueError(
            f"Solana keypair secret must be 64 bytes, got {len(secret_64_bytes)}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(secret_64_bytes)))
    path.chmod(0o600)


def _resolve_token_mint(ctx: ActionContext) -> str:
    """Pull the token mint from the monitor's stored scope."""
    from agent.store import get_monitor
    monitor = get_monitor(ctx.decision.monitor_id)
    if monitor is None:
        raise ChainAbort(f"monitor {ctx.decision.monitor_id} is not registered")
    mint = getattr(monitor.scope, "token_mint", None)
    if not mint:
        raise ChainAbort(
            f"monitor {ctx.decision.monitor_id} scope has no token_mint"
        )
    return mint


def _read_amount(ctx: ActionContext, key: str) -> int:
    value = ctx.state.get(key)
    if not isinstance(value, int) or value <= 0:
        raise ChainAbort(
            f"expected positive int at ctx.state[{key!r}], got {value!r}"
        )
    return value


# Single shared client instance — UmbraClient holds no per-instance state
# that we need separated, and the global CLI lock lives at module level.
_client = UmbraClient()


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

class EnsureUmbraUser(Action):
    """
    Idempotently add and register the monitor's umbra signer user.

    On first run for a monitor:
      1. Write the operating keypair (priv || pub, 64 bytes) to
         `$QVAC_KEYPAIR_DIR/qvac-<monitor_id>.json` (0600).
      2. `umbra user add qvac-<monitor_id> --backend local --keypair <path>`
      3. `umbra user use qvac-<monitor_id>`
      4. `umbra register`

    On subsequent runs: just `umbra user use qvac-<monitor_id>`.

    Writes:
      ctx.state["umbra_user"] = "qvac-<monitor_id>"
    """

    async def run(self, ctx: ActionContext) -> None:
        from agent.engines import wallet

        # Preflight: surface "umbra not installed" as a clean ChainAbort
        # rather than letting it propagate from the first _run call.
        try:
            _client.ensure_installed()
        except UmbraNotInstalled as e:
            raise ChainAbort(str(e)) from e

        mid = ctx.decision.monitor_id
        user_name = _qvac_user_name(mid)
        ctx.state["umbra_user"] = user_name

        if ctx.dry_run:
            logger.info("[dry_run] would ensure umbra user %s", user_name)
            return

        if await _client.user_exists(user_name):
            await _client.user_use(user_name)
            return

        keypair = wallet.get_operating_keypair(mid)
        if keypair is None:
            raise ChainAbort(
                f"no operating keypair derivable for monitor {mid}; "
                "did create_monitor_wallet run?"
            )

        keypair_path = _KEYPAIR_DIR / f"{user_name}.json"
        _write_solana_keypair(keypair.to_solana_secret(), keypair_path)
        logger.info("wrote keypair for %s to %s", user_name, keypair_path)

        try:
            await _client.user_add(user_name, keypair_path=str(keypair_path))
            await _client.user_use(user_name)
            await _client.register()
        except UmbraCommandFailed as e:
            # Surface the CLI's stderr verbatim — debugging Umbra issues
            # without it is miserable.
            raise ChainAbort(f"umbra setup failed: {e}") from e


class ReadEncryptedBalance(Action):
    """
    Read the active user's encrypted balance for the monitor's token mint.

    Stores the parsed integer balance (base units) in
    `ctx.state[<into_key>]`. Returns 0 if there is no encrypted balance
    for this mint, so downstream actions can branch on `>= some_minimum`
    without special-casing missing rows.
    """

    def __init__(self, into_key: str = "encrypted_balance") -> None:
        self._into_key = into_key

    async def run(self, ctx: ActionContext) -> None:
        mint = _resolve_token_mint(ctx)
        ctx.state[self._into_key] = await _client.eta_balance(mint)


class WithdrawFromEncrypted(Action):
    """
    Move funds from the active user's encrypted balance back into their
    public wallet (so subsequent on-chain DeFi actions can spend them).

    Reads the amount (base units, positive int) from
    `ctx.state[<amount_key>]`.
    """

    def __init__(self, amount_key: str = "amount") -> None:
        self._amount_key = amount_key

    async def run(self, ctx: ActionContext) -> None:
        mint = _resolve_token_mint(ctx)
        amount = _read_amount(ctx, self._amount_key)
        if ctx.dry_run:
            logger.info("[dry_run] would eta withdraw %d of %s", amount, mint)
            return
        try:
            await _client.eta_withdraw(mint, amount)
        except UmbraCommandFailed as e:
            raise ChainAbort(f"eta withdraw failed: {e}") from e


class DepositToEncrypted(Action):
    """
    Move funds from the active user's public wallet into their own encrypted
    balance.

    Reads the amount (base units, positive int) from
    `ctx.state[<amount_key>]`.
    """

    def __init__(self, amount_key: str = "amount") -> None:
        self._amount_key = amount_key

    async def run(self, ctx: ActionContext) -> None:
        mint = _resolve_token_mint(ctx)
        amount = _read_amount(ctx, self._amount_key)
        if ctx.dry_run:
            logger.info("[dry_run] would eta deposit %d of %s", amount, mint)
            return
        try:
            await _client.eta_deposit(mint, amount)
        except UmbraCommandFailed as e:
            raise ChainAbort(f"eta deposit failed: {e}") from e


class SeedFromEncrypted(Action):
    """
    Best-effort seed: read the active user's encrypted balance and, if it
    is at or above `min_amount`, withdraw the full amount to the public ATA
    so a downstream deposit action can spend it.

    Stores the withdrawn amount (base units) in `ctx.state[<into_key>]`.
    Stores 0 and continues cleanly — no ChainAbort — if the balance is
    absent or below `min_amount`. This lets rebalance steps that follow
    still run normally when there is nothing to seed.

    Place immediately after EnsureUmbraUser; follow with a deposit action
    that has `skip_if_zero=True` so the deposit is skipped when no seed
    was available.
    """

    def __init__(
        self,
        into_key: str = "encrypted_seed",
        min_amount: int = 1,
    ) -> None:
        self._into_key = into_key
        self._min_amount = min_amount

    async def run(self, ctx: ActionContext) -> None:
        mint = _resolve_token_mint(ctx)
        balance = await _client.eta_balance(mint)
        if balance < self._min_amount:
            ctx.state[self._into_key] = 0
            logger.debug(
                "encrypted balance %d below minimum %d — skipping seed withdrawal",
                balance, self._min_amount,
            )
            return
        ctx.state[self._into_key] = balance
        if ctx.dry_run:
            logger.info("[dry_run] would seed: eta withdraw %d of %s", balance, mint)
            return
        try:
            await _client.eta_withdraw(mint, balance)
        except UmbraCommandFailed as e:
            raise ChainAbort(f"seed: eta withdraw failed: {e}") from e


class SendToHolding(Action):
    """
    Close-flow action: send funds from the active user's public wallet
    directly into the configured holding wallet's encrypted ETA via
    `umbra eta deposit <mint> <amount> --recipient <holding_address>`.

    Only the deposit transaction is on-chain visible — the encrypted
    balance under the holding address stays private. The owner of the
    holding wallet later runs `umbra eta withdraw` (off-agent) to unshield.

    Reads the amount (base units, positive int) from
    `ctx.state[<amount_key>]`. Aborts the chain cleanly if no holding
    address has been configured.
    """

    def __init__(self, amount_key: str = "amount") -> None:
        self._amount_key = amount_key

    async def run(self, ctx: ActionContext) -> None:
        from agent.engines import wallet
        from agent.wallet.service import HoldingWalletNotSet

        mint = _resolve_token_mint(ctx)
        amount = _read_amount(ctx, self._amount_key)

        try:
            holding = wallet.require_holding_address()
        except HoldingWalletNotSet as e:
            raise ChainAbort(str(e)) from e

        if ctx.dry_run:
            logger.info(
                "[dry_run] would eta deposit %d of %s --recipient %s",
                amount, mint, holding,
            )
            return
        try:
            await _client.eta_deposit(mint, amount, recipient=holding)
        except UmbraCommandFailed as e:
            raise ChainAbort(f"send-to-holding failed: {e}") from e
