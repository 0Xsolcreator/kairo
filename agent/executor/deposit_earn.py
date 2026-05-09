"""
Action chains for the deposit_earn monitor type.

Signals:
  JUPITER  → withdraw from Kamino, deposit to Jupiter
  KAMINO   → withdraw from Jupiter, deposit to Kamino
  EQUAL    → no chain (filtered upstream by DebouncedExecutor.ignored_signals)
  ERROR    → no chain (filtered upstream by DebouncedExecutor.ignored_signals)

ATA sweep model
---------------
Each chain uses the operating wallet's ATA as a staging area:

  1. Withdraw from Umbra encrypted balance → ATA  (SeedFromEncrypted)
  2. Withdraw from source protocol → ATA           (skip_if_zero=True)
  3. Read total ATA balance                        (LoadATABalance)
  4. RequireMinimum — abort if nothing to deploy
  5. Deposit ATA total → target protocol

This handles every funding scenario in one pass:
  - First-time seed (only Umbra has funds)
  - Rebalance (only source protocol has funds)
  - Combined (both sources)
  - Stuck funds recovery (previous failed deposit left tokens in ATA)
  - Direct user deposit (user sent tokens straight to operating wallet)

Implementation status
---------------------
- The KAMINO chain (Jupiter → Kamino) is fully wired and runs end-to-end.
  LoadJupiterPosition returns underlying base units directly, so the
  amount plumbed into DepositToKamino is correctly scaled.

- The JUPITER chain (Kamino → Jupiter) is wired but blocked on
  KaminoClient.get_underlying_balance() — a stub that raises until the
  share→underlying conversion is implemented. Until then,
  LoadKaminoUnderlyingBalance ChainAborts cleanly. We deliberately don't
  fall back to LoadKaminoPosition (which returns *shares*) because piping
  shares into WithdrawFromKamino would mis-scale by orders of magnitude
  and burn funds.

Vault-binding gap (Kamino side)
-------------------------------
WithdrawFromKamino currently reads the vault address from
decision.metadata["kamino_vault"], which is the analyzer's *current* best
pick. If that pick rotates between deposit and withdraw, withdraws will
target the wrong vault. KaminoClient docstring tracks this gap; fixing it
is a separate schema-change PR (persist deposited-vault per monitor).
"""
from __future__ import annotations

from agent.executor.actions import (
    DepositToJupiter,
    DepositToKamino,
    EnsureUmbraUser,
    LoadATABalance,
    LoadJupiterPosition,
    LoadKaminoUnderlyingBalance,
    RequireMinimum,
    SeedFromEncrypted,
    WithdrawFromJupiter,
    WithdrawFromKamino,
)
from agent.executor.base import ActionChain, ChainBasedExecutor


class DepositEarnExecutor(ChainBasedExecutor):
    def __init__(self, dry_run: bool = False) -> None:
        super().__init__(
            chains={
                # ----------------------------------------------------------
                # JUPITER signal: seed from encrypted + rebalance Kamino → Jupiter
                # ----------------------------------------------------------
                # Phase 1 (seed): if the encrypted balance is non-zero,
                # withdraw it to the public ATA and deposit directly into
                # Jupiter Lend. Skipped cleanly when encrypted balance is 0.
                #
                # Phase 2 (rebalance): read the Kamino underlying balance,
                # withdraw, and deposit into Jupiter. Currently halts at
                # LoadKaminoUnderlyingBalance (stub); see module docstring.
                # ----------------------------------------------------------
                # JUPITER signal: move everything available → Jupiter Lend
                # ----------------------------------------------------------
                # Collects from all sources into the ATA, then deposits the
                # total. Handles stuck funds, direct deposits, and rebalances
                # in one sweep.
                #
                # Note: WithdrawFromKamino is blocked while
                # LoadKaminoUnderlyingBalance is still a stub — the chain
                # aborts there but any pre-existing ATA balance is still
                # picked up if the stub is removed.
                "JUPITER": ActionChain(actions=[
                    EnsureUmbraUser(),
                    SeedFromEncrypted(into_key="encrypted_seed"),            # Umbra → ATA
                    LoadKaminoUnderlyingBalance(into_key="kamino_amount"),
                    WithdrawFromKamino(amount_key="kamino_amount", skip_if_zero=True),  # Kamino → ATA
                    LoadATABalance(into_key="ata_balance"),
                    RequireMinimum(
                        "ata_balance", 1,
                        "no funds in Umbra, Kamino, or ATA to deploy to Jupiter",
                    ),
                    DepositToJupiter(amount_key="ata_balance"),
                ]),

                # ----------------------------------------------------------
                # KAMINO signal: move everything available → Kamino vault
                # ----------------------------------------------------------
                # Collects from all sources into the ATA, then deposits the
                # total. Handles stuck funds, direct deposits, and rebalances
                # in one sweep.
                "KAMINO": ActionChain(actions=[
                    EnsureUmbraUser(),
                    SeedFromEncrypted(into_key="encrypted_seed"),            # Umbra → ATA
                    LoadJupiterPosition(into_key="jupiter_amount"),
                    WithdrawFromJupiter(amount_key="jupiter_amount", skip_if_zero=True),  # Jupiter → ATA
                    LoadATABalance(into_key="ata_balance"),
                    RequireMinimum(
                        "ata_balance", 1,
                        "no funds in Umbra, Jupiter, or ATA to deploy to Kamino",
                    ),
                    DepositToKamino(amount_key="ata_balance"),
                ]),
            },
            dry_run=dry_run,
        )
