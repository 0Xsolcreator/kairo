"""
KaminoClient — write operations against Kamino KVaults.

Read-only polling already happens via httpx in agent/polling/deposit_earn.py
(`https://api.kamino.finance/kvaults/...`). This client handles the write
side: depositing to and withdrawing from a specific Kamino vault.

Transaction flow
----------------
1. POST to /ktx/kvault/deposit or /ktx/kvault/withdraw with the wallet,
   vault address, and decimal amount.
2. Kamino returns a base64-encoded, unsigned transaction (one 64-byte zero
   placeholder for the wallet signature).
3. We sign the raw message bytes using PyNaCl ed25519 and write the
   signature back into the placeholder slot.
4. Submit via sendTransaction JSON-RPC and poll for confirmation.

Configuration
-------------
RPC endpoint is resolved via agent.clients._rpc.get_rpc_url() — same source
of truth as JupiterClient. The lookup is cached process-wide.

Token decimals
--------------
The KTX API takes amounts in decimal token format ("1.234567"), not base
units. Decimals are passed *per call* (resolved from the monitor's mint by
the action layer) so the same client instance works across USDC (6), USDT
(6), and wSOL (9) without scaling bugs.

Vault selection
---------------
Kamino has many vaults per token. The polling/analyzer pipeline picks the
best one and surfaces it as `decision.metadata["kamino_vault"]`. Actions
read that key when they need to know which vault to interact with.

Position-vault binding: the vault you deposited into is the only vault you
can withdraw from. If the analyzer's "best vault" rotates between deposit
and withdraw, withdrawing from the new vault would move zero funds. Persist
the chosen vault address per monitor (extend monitor_wallets or add a
monitor_positions table) so `withdraw` can use the correct vault even after
the recommendation rotates. KNOWN GAP — see action layer for current behavior.
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

_KAMINO_BASE = "https://api.kamino.finance"


class KaminoError(RuntimeError):
    """API or RPC error from a Kamino operation."""


class KaminoNotImplemented(NotImplementedError):
    """Raised by stub methods that need real implementation before chains can run."""


class KaminoClient:
    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def get_position(
        self,
        operating: Keypair,
        vault_address: str,
    ) -> int:
        """
        Return the wallet's current Kamino position in `vault_address`,
        expressed as **share units** (totalShares × 10^9), or 0 if there is
        no position.

        IMPORTANT — these are *not* underlying token base units. They are
        suitable for presence checks (RequireMinimum >= 1, "do we have a
        position?") but MUST NOT be piped directly into `withdraw(amount=...)`
        — the API expects underlying-token decimals there. Converting shares
        → underlying requires the vault's current `sharePrice`, which this
        endpoint doesn't return on its own; resolve it via the kvault read
        endpoint (`/kvaults/{vault}`) when needed.

        TODO(#kamino-position-units): return underlying base units once the
        analyzer pipeline persists `share_price` into decision.metadata, so
        the conversion can happen here without an extra round-trip.
        """
        url = f"{_KAMINO_BASE}/kvaults/users/{operating.address}/positions/{vault_address}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=15.0)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise KaminoError(f"Kamino position GET failed: {e!r}") from e
        if resp.status_code == 404:
            return 0
        _raise_for_kamino_error(resp)
        try:
            data = resp.json()
        except ValueError as e:
            raise KaminoError(f"Kamino returned non-JSON for position read: {resp.text!r}") from e
        total_shares = float(data.get("totalShares") or "0")
        return int(total_shares * 10**9)

    async def get_underlying_balance(
        self,
        operating: Keypair,
        vault_address: str,
    ) -> int:
        """
        Return the wallet's Kamino position in `vault_address` expressed in
        **underlying token base units** (e.g. for a 6-decimal token, 1 token
        = 1_000_000). Returns 0 if there is no position.

        This is the value that should be plumbed into `withdraw(amount=...)`
        — the KTX API expects underlying decimals, not shares.

        Implementation guidance
        -----------------------
        Implement by combining two reads:
          1. `GET /kvaults/users/{wallet}/positions/{vault}` → `totalShares`
          2. `GET /kvaults/vaults/{vault}/metrics`           → share price
             or total underlying assets + total shares for the conversion.

        Then: underlying_base_units = round(shares * price * 10^decimals).

        For now this is a stub so the action layer can compose chains
        against a stable surface. Wiring it up is a focused follow-up: pick
        the metrics field once you confirm its name, do the conversion, and
        replace the raise below with the real read.
        """
        raise KaminoNotImplemented(
            "KaminoClient.get_underlying_balance is not implemented yet — "
            "this is what blocks the JUPITER (Kamino → Jupiter) rebalance "
            "chain from running. See module docstring for the read pattern."
        )

    async def deposit(
        self,
        operating: Keypair,
        vault_address: str,
        amount: int,
        token_decimals: int,
    ) -> str:
        """
        Deposit `amount` (token base units) into the kvault at
        `vault_address`. `token_decimals` is the on-chain decimals of the
        underlying token — used to format the amount as the decimal string
        the KTX API expects. Returns the confirmed transaction signature.
        """
        return await self._submit(
            path="/ktx/kvault/deposit",
            operating=operating,
            vault_address=vault_address,
            amount=amount,
            token_decimals=token_decimals,
            label="deposit",
        )

    async def withdraw(
        self,
        operating: Keypair,
        vault_address: str,
        amount: int,
        token_decimals: int,
    ) -> str:
        """
        Withdraw `amount` (token base units) from the kvault at
        `vault_address`. Returns the confirmed transaction signature.

        `vault_address` must be the vault we previously deposited into for
        this monitor — caller is responsible for resolving that. Don't assume
        it matches the analyzer's current "best vault".
        """
        return await self._submit(
            path="/ktx/kvault/withdraw",
            operating=operating,
            vault_address=vault_address,
            amount=amount,
            token_decimals=token_decimals,
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
        vault_address: str,
        amount: int,
        token_decimals: int,
        label: str,
    ) -> str:
        payload = {
            "wallet": operating.address,
            "kvault": vault_address,
            "amount": _to_decimal(amount, token_decimals),
        }
        logger.debug("kamino %s payload: %s", label, payload)
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{_KAMINO_BASE}{path}",
                    json=payload,
                    timeout=20.0,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise KaminoError(f"Kamino {label} POST failed: {e!r}") from e
        _raise_for_kamino_error(resp)
        try:
            tx_b64 = extract_transaction(resp.json(), label="kamino")
        except (TransactionExtractError, ValueError) as e:
            raise KaminoError(str(e)) from e

        try:
            signed_b64 = _sol_sign(tx_b64, operating.private_key)
        except ValueError as e:
            raise KaminoError(f"Kamino tx signing failed: {e}") from e

        try:
            rpc_url = await get_rpc_url()
        except RpcConfigError as e:
            raise KaminoError(str(e)) from e

        try:
            return await _sol_send(signed_b64, rpc_url)
        except RuntimeError as e:
            raise KaminoError(str(e)) from e


def _to_decimal(amount: int, decimals: int) -> str:
    """Convert base units to the decimal string the KTX API expects."""
    factor = 10 ** decimals
    whole = amount // factor
    frac = amount % factor
    return f"{whole}.{frac:0{decimals}d}"


def _raise_for_kamino_error(resp: httpx.Response) -> None:
    """Raise KaminoError with the API's error message on 4xx/5xx responses."""
    if resp.is_success:
        return
    try:
        body = resp.json()
        msg = body.get("message") or body.get("error") or resp.text
    except Exception:
        msg = resp.text
    raise KaminoError(f"Kamino API {resp.status_code}: {msg}")
