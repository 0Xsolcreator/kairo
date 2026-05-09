"""
Shared Solana transaction signing and RPC submission helpers.

Used by KaminoClient and JupiterClient — neither depends on `solders`.
The only crypto dependency is PyNaCl for ed25519 signing.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time

import httpx
import nacl.signing

logger = logging.getLogger(__name__)

_CONFIRM_TIMEOUT_S = 60
_CONFIRM_POLL_S = 2
_SEND_RETRIES = 3  # initial + 2 retries on transient send failures
_SEND_RETRY_DELAY_S = 1.5


class TransactionExtractError(ValueError):
    """The API response did not contain a recognizable transaction blob."""


def extract_transaction(body: object, *, label: str) -> str:
    """
    Pull the base64 transaction string from a protocol API response.

    Both Jupiter and Kamino return the unsigned tx under `transaction`; some
    older shapes use `tx`. Both clients route through this helper so any
    future API quirk is fixed in one place. `label` is included in the
    error message so the caller's protocol name shows up in logs.
    """
    if isinstance(body, dict):
        tx = body.get("transaction") or body.get("tx")
        if isinstance(tx, str) and tx:
            return tx
    raise TransactionExtractError(f"no transaction field in {label} response: {body!r}")


def sign_transaction(tx_b64: str, private_key: bytes) -> str:
    """
    Sign a Solana transaction returned as a base64-encoded blob.

    Both Kamino and Jupiter return unsigned transactions with one 64-byte
    zero placeholder at offset 1 (byte 0 is the compact-u16 signature count,
    which is 0x01 for single-signer transactions). We sign the message bytes
    (everything after the signature array) and write the signature back in.
    """
    raw = bytearray(base64.b64decode(tx_b64))
    num_sigs = raw[0]
    if num_sigs == 0 or num_sigs > 8:
        raise ValueError(
            f"unexpected signature count byte {num_sigs:#x} in transaction"
        )
    sig_start = 1
    message = bytes(raw[sig_start + num_sigs * 64:])
    signature = nacl.signing.SigningKey(private_key).sign(message).signature
    raw[sig_start: sig_start + 64] = signature
    return base64.b64encode(bytes(raw)).decode()


async def send_and_confirm(
    signed_tx_b64: str,
    rpc_url: str,
    *,
    skip_preflight: bool = False,
    timeout_s: int = _CONFIRM_TIMEOUT_S,
    poll_s: float = _CONFIRM_POLL_S,
) -> str:
    """
    Submit a signed base64 transaction via sendTransaction and poll
    getSignatureStatuses until confirmed or the timeout expires.

    Returns the transaction signature string on success.
    Raises RuntimeError on RPC error, on-chain failure, or timeout.
    """
    send_opts: dict = {"encoding": "base64"}
    if skip_preflight:
        send_opts["skipPreflight"] = True
    else:
        send_opts["preflightCommitment"] = "confirmed"

    async with httpx.AsyncClient() as client:
        sig = await _send_with_retry(client, rpc_url, signed_tx_b64, send_opts)
        logger.info("tx submitted: %s", sig)

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_s)
            sr = await client.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0", "id": 2,
                    "method": "getSignatureStatuses",
                    "params": [[sig], {"searchTransactionHistory": True}],
                },
                timeout=15.0,
            )
            sr.raise_for_status()
            value = (sr.json().get("result") or {}).get("value") or [None]
            status = value[0]
            if status is None:
                continue
            if status.get("err"):
                raise RuntimeError(f"on-chain tx failed: {status['err']}")
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                logger.info("tx confirmed: %s", sig)
                return sig

    raise RuntimeError(f"tx {sig} not confirmed within {timeout_s}s")


async def get_spl_token_balance(
    rpc_url: str,
    wallet_pubkey: str,
    mint: str,
) -> int:
    """
    Return the raw (base-unit) SPL token balance held by `wallet_pubkey`
    for `mint`, summed across all token accounts the wallet owns for that
    mint (normally just one ATA).

    Returns 0 if no token account exists — never raises on a missing account.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            rpc_url,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    wallet_pubkey,
                    {"mint": mint},
                    {"encoding": "jsonParsed"},
                ],
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"getTokenAccountsByOwner failed: {body['error']}")
        accounts = (body.get("result") or {}).get("value") or []
        total = 0
        for acct in accounts:
            raw = (
                acct.get("account", {})
                    .get("data", {})
                    .get("parsed", {})
                    .get("info", {})
                    .get("tokenAmount", {})
                    .get("amount", "0")
            )
            try:
                total += int(raw)
            except ValueError:
                pass
        return total


async def _send_with_retry(
    client: httpx.AsyncClient,
    rpc_url: str,
    signed_tx_b64: str,
    send_opts: dict,
) -> str:
    """
    Submit a signed transaction with a small retry loop around transient
    network/RPC errors. Hard RPC errors (preflight failures, on-chain
    rejections) raise immediately — retrying those would just resubmit the
    same broken tx.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _SEND_RETRIES + 1):
        try:
            resp = await client.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "sendTransaction",
                    "params": [signed_tx_b64, send_opts],
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            body = resp.json()
            if "error" in body:
                # Logical RPC error — don't retry, the tx is broken.
                raise RuntimeError(f"sendTransaction failed: {body['error']}")
            return body["result"]
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as e:
            last_exc = e
            if attempt == _SEND_RETRIES:
                break
            logger.warning(
                "sendTransaction attempt %d/%d failed (%s); retrying in %.1fs",
                attempt, _SEND_RETRIES, e.__class__.__name__, _SEND_RETRY_DELAY_S,
            )
            await asyncio.sleep(_SEND_RETRY_DELAY_S)
    raise RuntimeError(f"sendTransaction exhausted retries: {last_exc!r}") from last_exc
