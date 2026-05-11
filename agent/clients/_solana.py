"""
Shared Solana transaction signing and RPC submission helpers.

Used by KaminoClient and JupiterClient — neither depends on `solders`.
The only crypto dependencies are PyNaCl for ed25519 signing and
bip_utils for base58 decoding (blockhash only; pubkeys are passed as bytes).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import struct
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


async def get_sol_balance(rpc_url: str, pubkey: str) -> int:
    """Return the SOL balance of `pubkey` in lamports."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            rpc_url,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getBalance",
                "params": [pubkey, {"commitment": "confirmed"}],
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"getBalance failed: {body['error']}")
        return body["result"]["value"]


def _build_sol_transfer_tx(
    from_pubkey: bytes,
    to_pubkey: bytes,
    lamports: int,
    blockhash: bytes,
) -> str:
    """
    Build an unsigned legacy SOL transfer transaction. Returns base64.

    Layout:
      [01]           signature count = 1
      [64 × 00]      signature placeholder
      --- message ---
      [01 00 01]     header: 1 signer, 0 readonly-signed, 1 readonly-unsigned
      [03]           compact-u16: 3 account keys
      [32]           from_pubkey  (writable signer,   index 0)
      [32]           to_pubkey    (writable non-signer, index 1)
      [32]           SystemProgram (readonly,           index 2) = all-zeros
      [32]           recent blockhash
      [01]           compact-u16: 1 instruction
      [02]           program_id_index = 2 (SystemProgram)
      [02 00 01]     2 account indices: from=0, to=1
      [0c]           data length = 12
      [02 00 00 00]  SystemProgram::Transfer type
      [8 bytes LE]   lamports
    """
    ix_data = struct.pack("<I", 2) + struct.pack("<Q", lamports)
    message = (
        b"\x01\x00\x01"           # header
        + b"\x03"                 # 3 account keys
        + from_pubkey
        + to_pubkey
        + bytes(32)               # SystemProgram (all zeros)
        + blockhash
        + b"\x01"                 # 1 instruction
        + b"\x02"                 # program_id_index = SystemProgram
        + b"\x02\x00\x01"         # 2 account indices: from=0, to=1
        + b"\x0c"                 # data length = 12
        + ix_data
    )
    return base64.b64encode(b"\x01" + bytes(64) + message).decode()


async def transfer_sol(
    from_private_key: bytes,
    from_pubkey: bytes,
    to_pubkey: bytes,
    lamports: int,
    rpc_url: str,
) -> str:
    """
    Transfer `lamports` from `from_pubkey` to `to_pubkey`. Returns tx signature.

    `from_private_key` and both pubkeys are raw 32-byte ed25519 material.
    """
    from bip_utils import Base58Decoder

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            rpc_url,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getLatestBlockhash",
                "params": [{"commitment": "confirmed"}],
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"getLatestBlockhash failed: {body['error']}")
        blockhash_bytes = Base58Decoder.Decode(body["result"]["value"]["blockhash"])

    unsigned_tx = _build_sol_transfer_tx(from_pubkey, to_pubkey, lamports, blockhash_bytes)
    signed_tx   = sign_transaction(unsigned_tx, from_private_key)
    return await send_and_confirm(signed_tx, rpc_url)


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
