"""
Shared Solana RPC URL resolution.

Both KaminoClient and JupiterClient need the same RPC URL. We use the
Umbra CLI as the single source of truth (`umbra config get rpc`) so the
agent and the user-facing CLI never disagree on which cluster they're
talking to.

The result is cached at module level — `umbra config get rpc` is an
external subprocess and we don't want to pay for it on every call.
"""
from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

_cached_url: str | None = None
_lock = asyncio.Lock()


class RpcConfigError(RuntimeError):
    """Raised when the Umbra CLI doesn't return a usable RPC URL."""


async def get_rpc_url() -> str:
    """
    Return the Solana RPC URL configured in Umbra, caching the result for
    subsequent calls in the same process.

    The lookup is `umbra config get rpc` by default; override the binary
    name with the `UMBRA_BIN` env var (useful in tests).
    """
    global _cached_url
    if _cached_url is not None:
        return _cached_url

    async with _lock:
        # Re-check inside the lock — a concurrent caller may have populated it.
        if _cached_url is not None:
            return _cached_url

        binary = os.environ.get("UMBRA_BIN", "umbra")
        proc = await asyncio.create_subprocess_exec(
            binary, "config", "get", "rpcUrl",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        output = stdout_b.decode("utf-8", errors="replace").strip()

        # Output may be a bare URL or `rpc: <url>` — pick the http token.
        url = next((t for t in output.split() if t.startswith("http")), None)
        if not url:
            stderr_msg = stderr_b.decode("utf-8", errors="replace").strip()
            raise RpcConfigError(
                f"`{binary} config get rpc` returned no URL — "
                f"stdout={output!r} stderr={stderr_msg!r}"
            )
        _cached_url = url
        logger.debug("resolved umbra rpc url: %s", url)
        return url


def _reset_cache_for_tests() -> None:
    """Test-only hook to drop the cached URL between cases."""
    global _cached_url
    _cached_url = None
