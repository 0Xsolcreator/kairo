"""
JupiterClient — write operations against Jupiter Lend (Earn).

Read-only polling already happens via httpx in agent/polling/deposit_earn.py
(`https://api.jup.ag/lend/v1/earn/tokens`). This client handles the write
side: depositing to and withdrawing from Jupiter Lend positions.

Transaction flow
----------------
1. POST to /lend/v1/earn/deposit or /lend/v1/earn/withdraw with the token
   mint, amount (base units as string), and signer pubkey.
2. Jupiter returns a base64-encoded, unsigned transaction (one 64-byte zero
   placeholder for the wallet signature).
3. We sign the raw message bytes using PyNaCl ed25519 and submit via
   sendTransaction JSON-RPC.

API key
-------
Jupiter Lend requires an `x-api-key` header. The key is stored on the
monitor's scope (`DepositEarnScope.jup_api_key`) and passed into each
method by the action layer. The key is collected from the user when the
monitor is first configured.

RPC endpoint
------------
Resolved via agent.clients._rpc.get_rpc_url() — same source of truth as
KaminoClient.
"""
from __future__ import annotations

import logging

import httpx

from agent.clients._rpc import RpcConfigError, get_rpc_url
from agent.clients._solana import (
    TransactionExtractError,
    extract_transaction,
    send_and_confirm as _sol_send,
    sign_transaction as _sol_sign,
)
from agent.wallet.types import Keypair

logger = logging.getLogger(__name__)

_JUP_BASE = "https://api.jup.ag/lend/v1"


class JupiterError(RuntimeError):
    """API or RPC error from a Jupiter Lend operation."""


class JupiterNotImplemented(NotImplementedError):
    """Raised by stub methods that need real implementation before chains can run."""


class JupiterClient:
    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def get_position(
        self,
        operating: Keypair,
        token_mint: str,
        api_key: str,
    ) -> int:
        """
        Return the wallet's current Jupiter Lend position for `token_mint`
        in base units. Returns 0 if the wallet has no position.

        Queries GET /lend/v1/earn/positions?users=<wallet> and finds the
        entry matching `token_mint` by its `address` field. Returns the
        `balance` field (underlying token units) as an integer.
        """
        url = f"{_JUP_BASE}/earn/positions"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url,
                    params={"users": operating.address},
                    headers={"x-api-key": api_key},
                    timeout=15.0,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise JupiterError(f"Jupiter position GET failed: {e!r}") from e
        _raise_for_jup_error(resp)
        try:
            positions = resp.json()
        except ValueError as e:
            raise JupiterError(f"Jupiter returned non-JSON for position read: {resp.text!r}") from e

        for pos in positions or []:
            if pos.get("address") == token_mint:
                raw = pos.get("balance") or pos.get("shares") or "0"
                return int(raw)
        return 0

    async def deposit(
        self,
        operating: Keypair,
        token_mint: str,
        amount: int,
        api_key: str,
    ) -> str:
        """
        Deposit `amount` (base units) of `token_mint` into Jupiter Lend.
        Returns the confirmed transaction signature.
        """
        return await self._submit(
            path="/earn/deposit",
            operating=operating,
            token_mint=token_mint,
            amount=amount,
            api_key=api_key,
            label="deposit",
        )

    async def withdraw(
        self,
        operating: Keypair,
        token_mint: str,
        amount: int,
        api_key: str,
    ) -> str:
        """
        Withdraw `amount` (base units) of `token_mint` from Jupiter Lend.
        Returns the confirmed transaction signature.
        """
        return await self._submit(
            path="/earn/withdraw",
            operating=operating,
            token_mint=token_mint,
            amount=amount,
            api_key=api_key,
            label="withdraw",
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _submit(
        self,
        *,
        path: str,
        operating: Keypair,
        token_mint: str,
        amount: int,
        api_key: str,
        label: str,
    ) -> str:
        payload = {
            "asset": token_mint,
            "amount": str(amount),
            "signer": operating.address,
        }
        logger.debug("jupiter %s payload: %s", label, payload)
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{_JUP_BASE}{path}",
                    json=payload,
                    headers={"x-api-key": api_key, "Content-Type": "application/json"},
                    timeout=20.0,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise JupiterError(f"Jupiter {label} POST failed: {e!r}") from e
        _raise_for_jup_error(resp)
        try:
            tx_b64 = extract_transaction(resp.json(), label="jupiter")
        except (TransactionExtractError, ValueError) as e:
            raise JupiterError(str(e)) from e

        try:
            signed = _sol_sign(tx_b64, operating.private_key)
        except ValueError as e:
            raise JupiterError(f"Jupiter tx signing failed: {e}") from e

        try:
            rpc_url = await get_rpc_url()
        except RpcConfigError as e:
            raise JupiterError(str(e)) from e

        try:
            # Match Kamino: leave preflight on so simulation errors fail loud.
            return await _sol_send(signed, rpc_url)
        except RuntimeError as e:
            raise JupiterError(str(e)) from e


def _raise_for_jup_error(resp: httpx.Response) -> None:
    """Raise JupiterError with the API's message on 4xx/5xx responses."""
    if resp.is_success:
        return
    try:
        body = resp.json()
        msg = body.get("message") or body.get("error") or resp.text
    except Exception:
        msg = resp.text
    raise JupiterError(f"Jupiter API {resp.status_code}: {msg}")
