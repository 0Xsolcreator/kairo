"""
Relayer action: top up a monitor's operating wallet with SOL so it can pay fees.

Configuration (env vars)
------------------------
QVAC_RELAYER_KEYPAIR      Path to a Solana keypair JSON file (64-byte array).
                          Required when the operating wallet needs funding.
QVAC_SOL_MIN_LAMPORTS     Top-up threshold (default: 5_000_000 = 0.005 SOL).
                          If the operating wallet balance is at or above this,
                          the action is a no-op.
QVAC_SOL_TOP_UP_LAMPORTS  Amount to send when a top-up is needed
                          (default: 10_000_000 = 0.01 SOL).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from agent.executor.base import Action, ActionContext, ChainAbort

logger = logging.getLogger(__name__)

_DEFAULT_MIN_LAMPORTS    = 10_000_000   # 0.01 SOL
_DEFAULT_TOP_UP_LAMPORTS = 10_000_000  # 0.01  SOL


class TopUpOperatingWallet(Action):
    """
    Ensure the monitor's operating wallet has enough SOL to pay on-chain fees.

    Checks the balance against QVAC_SOL_MIN_LAMPORTS. If underfunded, loads the
    relayer keypair from QVAC_RELAYER_KEYPAIR and transfers QVAC_SOL_TOP_UP_LAMPORTS
    to the operating wallet. ChainAborts with a clear message if funding is needed
    but no relayer is configured or the relayer itself is underfunded.
    """

    async def run(self, ctx: ActionContext) -> None:
        from agent.clients._rpc import get_rpc_url
        from agent.clients._solana import get_sol_balance, transfer_sol
        from agent.engines import wallet
        from bip_utils import Base58Encoder

        mid = ctx.decision.monitor_id
        kp  = wallet.get_operating_keypair(mid)
        if kp is None:
            raise ChainAbort(f"no operating keypair for monitor {mid}")

        min_lamports = int(os.environ.get("QVAC_SOL_MIN_LAMPORTS",    _DEFAULT_MIN_LAMPORTS))
        top_up       = int(os.environ.get("QVAC_SOL_TOP_UP_LAMPORTS", _DEFAULT_TOP_UP_LAMPORTS))
        rpc_url      = await get_rpc_url()

        balance = await get_sol_balance(rpc_url, kp.address)
        if balance >= min_lamports:
            logger.debug(
                "operating wallet %s has %d lamports — top-up not needed",
                kp.address, balance,
            )
            return

        logger.info(
            "operating wallet %s has %d lamports (min %d) — initiating top-up",
            kp.address, balance, min_lamports,
        )

        keypair_path = os.environ.get("QVAC_RELAYER_KEYPAIR")
        if not keypair_path:
            raise ChainAbort(
                f"operating wallet {kp.address} has only {balance} lamports "
                f"(need {min_lamports}). "
                "Set QVAC_RELAYER_KEYPAIR to the path of a funded Solana keypair "
                "to enable automatic SOL top-ups."
            )

        try:
            raw = bytes(json.loads(Path(keypair_path).read_text()))
        except Exception as exc:
            raise ChainAbort(f"failed to load relayer keypair from {keypair_path}: {exc}") from exc

        if len(raw) != 64:
            raise ChainAbort(
                f"relayer keypair at {keypair_path} is {len(raw)} bytes; expected 64"
            )

        relayer_priv = raw[:32]
        relayer_pub  = raw[32:]
        relayer_addr = Base58Encoder.Encode(relayer_pub)

        relayer_balance = await get_sol_balance(rpc_url, relayer_addr)
        if relayer_balance < top_up:
            raise ChainAbort(
                f"relayer wallet {relayer_addr} has only {relayer_balance} lamports "
                f"(need {top_up}). Fund the relayer and retry."
            )

        if ctx.dry_run:
            logger.info(
                "[dry_run] would transfer %d lamports from relayer %s to %s",
                top_up, relayer_addr, kp.address,
            )
            return

        sig = await transfer_sol(relayer_priv, relayer_pub, kp.public_key, top_up, rpc_url)
        logger.info(
            "topped up %s with %d lamports from relayer %s (tx %s)",
            kp.address, top_up, relayer_addr, sig,
        )
