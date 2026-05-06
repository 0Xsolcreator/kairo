"""
HD-derivation helpers.

Solana uses ed25519, so derivation must be SLIP-0010 with all-hardened
paths (no soft children for ed25519). bip_utils.Bip32Slip10Ed25519 does
the right thing.

Per-monitor paths:
  operating       : m/44'/501'/<index>'/0'
  umbra_spending  : m/44'/501'/<index>'/1'
  umbra_viewing   : m/44'/501'/<index>'/2'

Index `<index>` is the per-monitor sequence number from monitor_wallets;
each monitor gets its own account so monitors share no on-chain footprint.
"""
from __future__ import annotations

from bip_utils import (
    Base58Decoder,
    Bip32Slip10Ed25519,
    Bip39MnemonicGenerator,
    Bip39SeedGenerator,
    Bip39WordsNum,
)

from agent.wallet.types import Keypair, UmbraMetaKeys

_SOLANA_COIN_TYPE = 501


def generate_mnemonic() -> str:
    """Generate a fresh 24-word BIP39 mnemonic."""
    return Bip39MnemonicGenerator().FromWordsNumber(
        Bip39WordsNum.WORDS_NUM_24
    ).ToStr()


def mnemonic_to_seed(mnemonic: str, passphrase: str = "") -> bytes:
    return Bip39SeedGenerator(mnemonic).Generate(passphrase)


def _derive_keypair(seed: bytes, path: str) -> Keypair:
    node = Bip32Slip10Ed25519.FromSeed(seed).DerivePath(path)
    private_key = node.PrivateKey().Raw().ToBytes()
    # bip_utils returns a 33-byte ed25519 pubkey with a 0x00 prefix; strip it.
    raw_pub = node.PublicKey().RawCompressed().ToBytes()
    public_key = raw_pub[1:] if len(raw_pub) == 33 else raw_pub
    return Keypair(private_key=private_key, public_key=public_key)


def derive_monitor_wallet(seed: bytes, monitor_index: int) -> tuple[Keypair, UmbraMetaKeys]:
    """Derive the operating + Umbra meta keys for one monitor."""
    base = f"m/44'/{_SOLANA_COIN_TYPE}'/{monitor_index}'"
    operating = _derive_keypair(seed, f"{base}/0'")
    umbra_spend = _derive_keypair(seed, f"{base}/1'")
    umbra_view = _derive_keypair(seed, f"{base}/2'")
    return operating, UmbraMetaKeys(
        spending_keypair=umbra_spend,
        viewing_keypair=umbra_view,
    )


def validate_solana_address(address: str) -> None:
    """Raise ValueError if `address` isn't a base58-encoded 32-byte pubkey."""
    if not address or not address.strip():
        raise ValueError("address is empty")
    try:
        decoded = Base58Decoder.Decode(address)
    except Exception as e:
        raise ValueError(f"not valid base58: {e}") from e
    if len(decoded) != 32:
        raise ValueError(
            f"Solana address must decode to 32 bytes, got {len(decoded)}"
        )
