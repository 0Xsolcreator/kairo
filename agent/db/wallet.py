from __future__ import annotations

from datetime import datetime, timezone

from agent.db._core import _open


def get_master_mnemonic() -> str | None:
    conn = _open()
    try:
        row = conn.execute(
            "SELECT mnemonic FROM agent_master_seed ORDER BY id ASC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def put_master_mnemonic(mnemonic: str) -> None:
    """Insert if absent; never overwrite — only one master mnemonic ever exists."""
    conn = _open()
    try:
        existing = conn.execute(
            "SELECT 1 FROM agent_master_seed LIMIT 1"
        ).fetchone()
        if existing:
            return
        conn.execute(
            """INSERT INTO agent_master_seed (mnemonic, encrypted, created_at)
               VALUES (?, 0, ?)""",
            (mnemonic, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_holding_address() -> str | None:
    conn = _open()
    try:
        row = conn.execute(
            "SELECT address FROM holding_wallet ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def put_holding_address(address: str) -> None:
    """Replace any existing holding address with the new one."""
    conn = _open()
    try:
        conn.execute("DELETE FROM holding_wallet")
        conn.execute(
            "INSERT INTO holding_wallet (address, created_at) VALUES (?, ?)",
            (address, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def next_monitor_derivation_index() -> int:
    """Return MAX(derivation_index) + 1, or 0 if the table is empty."""
    conn = _open()
    try:
        row = conn.execute(
            "SELECT MAX(derivation_index) FROM monitor_wallets"
        ).fetchone()
        return 0 if row[0] is None else int(row[0]) + 1
    finally:
        conn.close()


def store_monitor_wallet(
    *,
    monitor_id: str,
    derivation_index: int,
    operating_pubkey: str,
    umbra_meta_address: str,
) -> None:
    conn = _open()
    try:
        conn.execute(
            """INSERT INTO monitor_wallets
               (monitor_id, derivation_index, operating_pubkey,
                umbra_meta_address, status, created_at)
               VALUES (?, ?, ?, ?, 'active', ?)""",
            (
                monitor_id,
                derivation_index,
                operating_pubkey,
                umbra_meta_address,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_monitor_wallet(monitor_id: str) -> dict | None:
    conn = _open()
    try:
        row = conn.execute(
            """SELECT derivation_index, operating_pubkey, umbra_meta_address,
                      status, created_at, closed_at
               FROM monitor_wallets WHERE monitor_id = ?""",
            (monitor_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "derivation_index": int(row[0]),
            "operating_pubkey": row[1],
            "umbra_meta_address": row[2],
            "status": row[3],
            "created_at": row[4],
            "closed_at": row[5],
        }
    finally:
        conn.close()


def mark_monitor_wallet_closed(monitor_id: str) -> None:
    conn = _open()
    try:
        conn.execute(
            """UPDATE monitor_wallets
               SET status='closed', closed_at=?
               WHERE monitor_id=? AND status='active'""",
            (datetime.now(timezone.utc).isoformat(), monitor_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_kamino_deposited_vault(monitor_id: str, vault_address: str) -> None:
    """Record which Kamino vault funds were most recently deposited into."""
    conn = _open()
    try:
        conn.execute(
            "UPDATE monitor_wallets SET deposited_kamino_vault=? WHERE monitor_id=?",
            (vault_address, monitor_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_kamino_deposited_vault(monitor_id: str) -> str | None:
    """Return the Kamino vault last deposited into for this monitor, or None."""
    conn = _open()
    try:
        row = conn.execute(
            "SELECT deposited_kamino_vault FROM monitor_wallets WHERE monitor_id=?",
            (monitor_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()
