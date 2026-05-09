"""
Generic Solana on-chain query actions.
"""
from __future__ import annotations

import logging

from agent.clients._rpc import RpcConfigError, get_rpc_url
from agent.clients._solana import get_spl_token_balance
from agent.executor.base import Action, ActionContext, ChainAbort

logger = logging.getLogger(__name__)


class LoadATABalance(Action):
    """
    Read the operating wallet's public SPL token balance for the monitor's
    mint and store it (base units, int) in ctx.state[<into_key>].

    Captures ALL funds in the ATA regardless of source — Umbra withdrawals,
    protocol withdrawals, or direct user deposits. Returns 0 if no token
    account exists yet.

    Place this after all withdrawal actions so it sees the combined total,
    then gate the downstream deposit with RequireMinimum.
    """

    def __init__(self, into_key: str = "ata_balance") -> None:
        self._into_key = into_key

    async def run(self, ctx: ActionContext) -> None:
        from agent.engines import wallet
        from agent.store import get_monitor

        monitor = get_monitor(ctx.decision.monitor_id)
        if monitor is None:
            raise ChainAbort(f"monitor {ctx.decision.monitor_id} is not registered")

        mint = getattr(monitor.scope, "token_mint", None)
        if not mint:
            raise ChainAbort(f"monitor {ctx.decision.monitor_id} scope has no token_mint")

        kp = wallet.get_operating_keypair(ctx.decision.monitor_id)
        if kp is None:
            raise ChainAbort(
                f"no operating keypair for monitor {ctx.decision.monitor_id}"
            )

        try:
            rpc_url = await get_rpc_url()
        except RpcConfigError as e:
            raise ChainAbort(f"cannot resolve RPC URL: {e}") from e

        balance = await get_spl_token_balance(rpc_url, kp.address, mint)
        ctx.state[self._into_key] = balance
        logger.debug(
            "ATA balance — wallet=%s mint=%s rpc=%s → %d base units",
            kp.address, mint, rpc_url, balance,
        )
