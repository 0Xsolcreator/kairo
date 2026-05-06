"""
Wallet value types.

All keys are stored as raw 32-byte ed25519 material — convert at the
boundary when an action needs a `solders.Keypair` or similar SDK type.
This keeps the wallet module dependency-light.
"""
from __future__ import annotations

from dataclasses import dataclass

from bip_utils import Base58Encoder


@dataclass(frozen=True)
class Keypair:
    """A Solana ed25519 keypair. Both fields are 32 bytes."""

    private_key: bytes
    public_key: bytes

    @property
    def address(self) -> str:
        """Base58-encoded public key — the canonical Solana address."""
        return Base58Encoder.Encode(self.public_key)

    def to_solana_secret(self) -> bytes:
        """
        Returns the 64-byte (private || public) blob most Solana SDKs accept
        as a "secret key". `solders.Keypair.from_bytes(secret)` will work.
        """
        return self.private_key + self.public_key


@dataclass(frozen=True)
class UmbraMetaKeys:
    """
    Umbra dual-key stealth-address keypair.

    The recipient publishes `meta_address` (spending_pubkey || viewing_pubkey,
    base58-encoded). Senders use it to derive one-time stealth addresses.
    The viewing key is what we use to scan the chain; the spending key is
    what we use to actually spend discovered UTXOs.
    """

    spending_keypair: Keypair
    viewing_keypair: Keypair

    @property
    def meta_address(self) -> str:
        return Base58Encoder.Encode(
            self.spending_keypair.public_key + self.viewing_keypair.public_key
        )


@dataclass(frozen=True)
class MonitorWallet:
    """All keys allocated to one monitor."""

    monitor_id: str
    derivation_index: int
    operating: Keypair
    umbra_meta: UmbraMetaKeys
