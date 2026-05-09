"""
Async wrapper around the `umbra` CLI (npm: umbraprivacy-cli).

Install
-------
    npm install --global umbraprivacy-cli

Set the binary path with `UMBRA_BIN` if it's not on PATH (default: `umbra`).

Set the Solana RPC once globally before running the agent:
    umbra config set rpc <url>

The agent does not manage that config — it's CLI-wide state and
duplicating it in agent config invites two-source-of-truth bugs.

Concurrency
-----------
The CLI keeps an "active user" as process-global state, set with
`umbra user use <name>`. To avoid two concurrent chains clobbering
each other's active user, every command run goes through a single
module-level `asyncio.Lock` — slower but correct.

If you confirm the CLI accepts a per-call `--user <name>` flag,
drop the lock and inject the flag in `_run` instead.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Sequence

logger = logging.getLogger(__name__)

# Single global lock guarding every umbra invocation. See module docstring.
_CLI_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class UmbraNotInstalled(RuntimeError):
    """Raised when the umbra binary cannot be found on PATH."""


class UmbraCommandFailed(RuntimeError):
    """Raised when an umbra command exits with a non-zero status."""

    def __init__(
        self,
        args: Sequence[str],
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.args = list(args)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"umbra {' '.join(args)} exited {exit_code}\n"
            f"--- stdout ---\n{stdout}\n"
            f"--- stderr ---\n{stderr}"
        )


# ---------------------------------------------------------------------------
# Output parsers
# ---------------------------------------------------------------------------

def _parse_eta_balance_output(output: str) -> int:
    """
    Parse `umbra eta balance <mint>` output for a single-mint query.

    Example output:
        ✓ Encrypted balances
          So11…1112   9988

    Strategy: scan lines for the first one whose last whitespace-token
    parses as a non-negative integer (after stripping commas/underscores
    that some CLIs use as thousands separators). The header line's last
    token is "balances" so it's skipped naturally.

    Returns 0 if no numeric row is present — assumed to mean "no encrypted
    balance for this mint." If the CLI ever emits an explicit "no balance"
    text line, this still does the right thing because it parses to 0.
    """
    for line in output.splitlines():
        tokens = line.strip().split()
        if not tokens:
            continue
        last = tokens[-1].replace(",", "").replace("_", "")
        try:
            value = int(last)
        except ValueError:
            continue
        if value >= 0:
            return value
    return 0


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class UmbraClient:
    """
    One instance is fine for the whole agent — there's no per-instance state
    other than the cached "binary verified" flag, and the active-user lock
    is module-level anyway.
    """

    def __init__(self, binary: str | None = None) -> None:
        self._binary = binary or os.environ.get("UMBRA_BIN", "umbra")
        self._verified: bool = False

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------

    def ensure_installed(self) -> None:
        """Raise UmbraNotInstalled if the binary isn't reachable."""
        if self._verified:
            return
        if shutil.which(self._binary) is None:
            raise UmbraNotInstalled(
                f"umbra CLI not found on PATH (looked for '{self._binary}'). "
                "Install with `npm install --global umbraprivacy-cli`."
            )
        self._verified = True

    # ------------------------------------------------------------------
    # Internal command runner
    # ------------------------------------------------------------------

    async def _run(self, *args: str) -> str:
        """Run `umbra <args>` under the global lock. Returns stdout."""
        self.ensure_installed()
        async with _CLI_LOCK:
            logger.debug("umbra %s", " ".join(args))
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await proc.communicate()
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                raise UmbraCommandFailed(args, proc.returncode or -1, stdout, stderr)
            return stdout

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def user_add(self, name: str, *, keypair_path: str) -> None:
        """Add a signer user backed by a local Solana keypair file."""
        await self._run(
            "user", "add", name,
            "--backend", "local",
            "--param", f"keypair={keypair_path}",
        )

    async def user_use(self, name: str) -> None:
        await self._run("user", "use", name)

    async def user_list_raw(self) -> str:
        """Returns raw stdout from `umbra user list` for callers to parse."""
        return await self._run("user", "list")

    async def user_exists(self, name: str) -> bool:
        """
        Best-effort existence check by scanning `user list` output.

        The CLI's exact format isn't documented here — we look for a line
        whose first whitespace-delimited token equals `name`. Adjust if the
        list output is decorated (TTY colors, table borders, etc.).
        """
        output = await self.user_list_raw()
        for line in output.splitlines():
            tokens = line.strip().split()
            if tokens and tokens[0] == name:
                return True
        return False

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    async def register(self) -> None:
        """
        Publish the on-chain Umbra account for the active user.

        Re-runs are intended to be safe per the SDK docs (it handles key
        rotation), but each call submits an on-chain tx with SOL cost,
        so callers should gate with a "is registered?" check if they
        can determine it cheaply.
        """
        await self._run("register")

    # ------------------------------------------------------------------
    # Encrypted token account (ETA)
    # ------------------------------------------------------------------

    async def eta_balance_raw(self, mint: str) -> str:
        """Returns raw stdout from `umbra eta balance <mint>` (single-mint query)."""
        return (await self._run("eta", "balance", mint)).strip()

    async def eta_balance(self, mint: str) -> int:
        """
        Return the active user's encrypted balance for `mint` in base units.

        Returns 0 if the CLI output contains no numeric balance row — that's
        treated as "no encrypted balance for this mint" rather than an error.

        Caller passes a single mint, so the CLI emits exactly one data row
        on success. If we ever pass `--all` or multiple mints, swap to a
        dict-returning variant.
        """
        return _parse_eta_balance_output(await self.eta_balance_raw(mint))

    async def eta_deposit(
        self,
        mint: str,
        amount: int,
        *,
        recipient: str | None = None,
    ) -> None:
        """
        Move `amount` (base units) of `mint` from the active user's public
        wallet into an encrypted ETA.

        Defaults to depositing into the active user's own encrypted balance.
        Pass `recipient` (a Solana pubkey) to deposit directly into someone
        else's encrypted ETA — used by the close-flow to shield funds back
        to the user's holding wallet without an intermediate step.
        """
        args = ["eta", "deposit", mint, str(amount)]
        if recipient:
            args += ["--recipient", recipient]
        await self._run(*args)

    async def eta_withdraw(self, mint: str, amount: int) -> None:
        """Move `amount` (base units) of `mint` from encrypted → public balance."""
        await self._run("eta", "withdraw", mint, str(amount))
