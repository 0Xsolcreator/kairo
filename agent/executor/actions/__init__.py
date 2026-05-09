"""
Reusable Action building blocks shared across monitor-type executors.

Modules:
  control.py  — generic chain-flow helpers (RequireMinimum, Copy)
  solana.py   — generic Solana on-chain queries (ATA balance)
  umbra.py    — wraps the umbraprivacy-cli for the encrypted-balance flow
  jupiter.py  — Jupiter Lend deposit/withdraw/position
  kamino.py   — Kamino KVault deposit/withdraw/position
"""
from agent.executor.actions.control import Copy, RequireMinimum
from agent.executor.actions.solana import LoadATABalance
from agent.executor.actions.jupiter import (
    DepositToJupiter,
    LoadJupiterPosition,
    WithdrawFromJupiter,
)
from agent.executor.actions.kamino import (
    DepositToKamino,
    LoadKaminoPosition,
    LoadKaminoUnderlyingBalance,
    WithdrawFromKamino,
)
from agent.executor.actions.umbra import (
    DepositToEncrypted,
    EnsureUmbraUser,
    ReadEncryptedBalance,
    SeedFromEncrypted,
    SendToHolding,
    WithdrawFromEncrypted,
)

__all__ = [
    # Generic flow control
    "Copy",
    "RequireMinimum",
    # Solana on-chain queries
    "LoadATABalance",
    # Umbra
    "DepositToEncrypted",
    "EnsureUmbraUser",
    "ReadEncryptedBalance",
    "SeedFromEncrypted",
    "SendToHolding",
    "WithdrawFromEncrypted",
    # Jupiter Lend
    "DepositToJupiter",
    "LoadJupiterPosition",
    "WithdrawFromJupiter",
    # Kamino KVaults
    "DepositToKamino",
    "LoadKaminoPosition",
    "LoadKaminoUnderlyingBalance",
    "WithdrawFromKamino",
]
