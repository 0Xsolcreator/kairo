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
v0.2.5+ supports a --user <name> flag on all ETA and register commands.
Each call carries its own user context, so commands can run concurrently
without a shared lock or global active-user state. The old _CLI_LOCK /
user_use serialization pattern is no longer needed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from typing import Sequence

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

logger = logging.getLogger(__name__)


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
    for line in _ANSI_RE.sub("", output).splitlines():
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
    other than the cached "binary verified" flag.
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
        """Run `umbra <args>`. Returns stdout."""
        self.ensure_installed()
        timeout = float(os.environ.get("QVAC_UMBRA_TIMEOUT_S", "120"))
        logger.debug("umbra %s", " ".join(args))
        proc = await asyncio.create_subprocess_exec(
            self._binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise UmbraCommandFailed(
                args, -1, "",
                f"timed out after {timeout:.0f}s — try setting QVAC_UMBRA_TIMEOUT_S",
            )
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
        """Set the global active user. Prefer passing --user where supported."""
        await self._run("user", "use", name)

    async def user_list_raw(self) -> str:
        """Returns raw stdout from `umbra user list` for callers to parse."""
        return await self._run("user", "list")

    async def user_exists(self, name: str) -> bool:
        output = await self.user_list_raw()
        for line in output.splitlines():
            tokens = line.strip().split()
            if tokens and tokens[0] == name:
                return True
        return False

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    async def register(
        self,
        *,
        user: str | None = None,
        confidential: bool = True,
    ) -> None:
        """
        Publish the on-chain Umbra account for the given user.

        `confidential=True` (default) registers the X25519 key required for
        shared-mode encrypted balances — without it, `eta withdraw` fails
        preflight because no shared-mode account exists on-chain.
        Pass `user` to target a specific user without changing the active user.
        """
        args = ["register"]
        if confidential:
            args += ["--confidential"]
        if user:
            args += ["--user", user]
        await self._run(*args)

    # ------------------------------------------------------------------
    # Encrypted token account (ETA)
    # ------------------------------------------------------------------

    async def eta_balance_raw(self, mint: str, *, user: str | None = None) -> str:
        """Returns raw stdout from `umbra eta balance <mint>`."""
        args = ["eta", "balance", mint]
        if user:
            args += ["--user", user]
        return (await self._run(*args)).strip()

    async def eta_balance(self, mint: str, *, user: str | None = None) -> int:
        """
        Return the encrypted balance for `mint` in base units.
        Returns 0 if the CLI output contains no numeric balance row.
        """
        return _parse_eta_balance_output(await self.eta_balance_raw(mint, user=user))

    async def eta_deposit(
        self,
        mint: str,
        amount: int,
        *,
        recipient: str | None = None,
        user: str | None = None,
    ) -> None:
        """
        Move `amount` (base units) of `mint` from the user's public wallet
        into an encrypted ETA.

        Pass `recipient` to deposit into someone else's encrypted ETA.
        Pass `user` to act as a specific user without changing the active user.
        """
        args = ["eta", "deposit", mint, str(amount)]
        if recipient:
            args += ["--recipient", recipient]
        if user:
            args += ["--user", user]
        await self._run(*args)

    async def eta_convert(
        self,
        mint: str | None = None,
        *,
        all_tokens: bool = False,
        user: str | None = None,
    ) -> None:
        """
        Convert MXE-encrypted ETA balance(s) to shared mode so they can be
        read by eta_balance and withdrawn via eta_withdraw.

        Pass a specific `mint` address, or set `all_tokens=True` to convert
        every token supported by the relayer.
        Pass `user` to act as a specific user without changing the active user.
        """
        args = ["eta", "convert"]
        if all_tokens:
            args += ["--all"]
        elif mint:
            args += [mint]
        if user:
            args += ["--user", user]
        await self._run(*args)

    async def eta_withdraw(
        self,
        mint: str,
        amount: int,
        *,
        user: str | None = None,
    ) -> None:
        """
        Move `amount` (base units) of `mint` from encrypted → public balance.
        Pass `user` to act as a specific user without changing the active user.
        """
        args = ["eta", "withdraw", mint, str(amount)]
        if user:
            args += ["--user", user]
        await self._run(*args)
