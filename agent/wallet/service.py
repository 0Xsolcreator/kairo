"""
WalletService — single point of access for everything wallet-related.

Wired as a module-level singleton in agent.engines, so chain actions
reach it via `from agent.engines import wallet`.

Persistence model
-----------------
Stored:
  - agent master mnemonic (plaintext for now; encryption to follow)
  - holding-wallet address (one row, the user's main wallet)
  - per-monitor row: derivation_index, operating pubkey, umbra meta address

Re-derived on demand:
  - all private keys (from the master mnemonic + stored derivation_index)

This keeps the secret surface to one row: lose the mnemonic, lose access
to every monitor wallet — so back it up. Persisting only public material
per monitor means a DB compromise alone can't move funds.
"""
from __future__ import annotations

import logging
import os

import agent.db as db
from agent.wallet.derive import (
    derive_monitor_wallet,
    generate_mnemonic,
    mnemonic_to_seed,
    validate_solana_address,
)
from agent.wallet.types import Keypair, MonitorWallet, UmbraMetaKeys

logger = logging.getLogger(__name__)


class WalletNotConfigured(RuntimeError):
    """Raised when something asks for derived material before the seed exists."""


class HoldingWalletNotSet(RuntimeError):
    """Raised when a flow needs a holding address and none is configured."""


class WalletService:
    def __init__(self) -> None:
        self._seed: bytes | None = None  # cached after first derivation
        # shared mode: all monitors derive from index 0 (same wallet).
        # Useful for testing — set QVAC_WALLET_MODE=shared to enable.
        self._shared = os.environ.get("QVAC_WALLET_MODE", "hd").lower() == "shared"
        if self._shared:
            logger.warning(
                "QVAC_WALLET_MODE=shared — all monitors will use the same "
                "wallet. Do not use this in production."
            )

    # ------------------------------------------------------------------
    # Master seed
    # ------------------------------------------------------------------

    def _ensure_seed(self) -> bytes:
        if self._seed is not None:
            return self._seed
        mnemonic = db.get_master_mnemonic()
        if mnemonic is None:
            mnemonic = generate_mnemonic()
            db.put_master_mnemonic(mnemonic)
            logger.warning(
                "Generated a new agent master mnemonic. BACK THIS UP — losing "
                "it means losing access to every monitor wallet:\n  %s",
                mnemonic,
            )
        self._seed = mnemonic_to_seed(mnemonic)
        return self._seed

    def reveal_master_mnemonic(self) -> str:
        """For backup/export flows. Treat the return value as a secret."""
        mnemonic = db.get_master_mnemonic()
        if mnemonic is None:
            raise WalletNotConfigured("no master mnemonic has been generated yet")
        return mnemonic

    # ------------------------------------------------------------------
    # Holding wallet
    # ------------------------------------------------------------------

    def get_holding_address(self) -> str | None:
        return db.get_holding_address()

    def has_holding_address(self) -> bool:
        return db.get_holding_address() is not None

    def set_holding_address(self, address: str) -> None:
        address = address.strip()
        validate_solana_address(address)  # raises ValueError on bad input
        db.put_holding_address(address)

    def require_holding_address(self) -> str:
        addr = self.get_holding_address()
        if addr is None:
            raise HoldingWalletNotSet("holding wallet address has not been configured")
        return addr

    # ------------------------------------------------------------------
    # Per-monitor wallets
    # ------------------------------------------------------------------

    def create_monitor_wallet(self, monitor_id: str) -> MonitorWallet:
        """
        Allocate the next derivation index, derive the keys, and persist
        the public-only record. Idempotent: returns the existing wallet
        if one already exists for `monitor_id`.

        In shared mode (QVAC_WALLET_MODE=shared) all monitors derive from
        index 0. No DB row is written — derivation happens on demand in
        get_monitor_wallet so the UNIQUE constraint on derivation_index is
        never touched.
        """
        if self._shared:
            return self._derive_shared(monitor_id)

        existing = self.get_monitor_wallet(monitor_id)
        if existing is not None:
            return existing

        seed = self._ensure_seed()
        index = db.next_monitor_derivation_index()
        operating, umbra_meta = derive_monitor_wallet(seed, index)

        db.store_monitor_wallet(
            monitor_id=monitor_id,
            derivation_index=index,
            operating_pubkey=operating.address,
            umbra_meta_address=umbra_meta.meta_address,
        )

        return MonitorWallet(
            monitor_id=monitor_id,
            derivation_index=index,
            operating=operating,
            umbra_meta=umbra_meta,
        )

    def get_monitor_wallet(self, monitor_id: str) -> MonitorWallet | None:
        if self._shared:
            return self._derive_shared(monitor_id)

        record = db.get_monitor_wallet(monitor_id)
        if record is None:
            return None
        seed = self._ensure_seed()
        operating, umbra_meta = derive_monitor_wallet(seed, record["derivation_index"])
        return MonitorWallet(
            monitor_id=monitor_id,
            derivation_index=record["derivation_index"],
            operating=operating,
            umbra_meta=umbra_meta,
        )

    def _derive_shared(self, monitor_id: str) -> MonitorWallet:
        """Derive index-0 wallet without touching the DB (shared-mode only)."""
        seed = self._ensure_seed()
        operating, umbra_meta = derive_monitor_wallet(seed, 0)
        return MonitorWallet(
            monitor_id=monitor_id,
            derivation_index=0,
            operating=operating,
            umbra_meta=umbra_meta,
        )

    # Convenience accessors for chain actions ------------------------------

    def get_operating_keypair(self, monitor_id: str) -> Keypair | None:
        wallet = self.get_monitor_wallet(monitor_id)
        return wallet.operating if wallet else None

    def get_umbra_meta(self, monitor_id: str) -> UmbraMetaKeys | None:
        wallet = self.get_monitor_wallet(monitor_id)
        return wallet.umbra_meta if wallet else None

    def get_funding_address(self, monitor_id: str) -> str | None:
        """
        The address users should send funds to in order to fund this monitor.

        Currently returns the operating keypair's regular 32-byte Solana
        pubkey — i.e. funds should be sent directly, no Umbra in the loop.
        When the Umbra integration lands, switch this to return
        `umbra_meta.meta_address` so funding flows through stealth addresses.
        The Umbra meta keys are still derived and persisted in the meantime,
        so the switch is one-line.
        """
        wallet = self.get_monitor_wallet(monitor_id)
        return wallet.operating.address if wallet else None

    # Lifecycle ----------------------------------------------------------

    def close_monitor_wallet(self, monitor_id: str) -> None:
        """
        Mark a monitor's wallet record as closed. Does NOT move funds —
        the actual sweep-back-to-holding is the close-flow chain's job.
        """
        db.mark_monitor_wallet_closed(monitor_id)
