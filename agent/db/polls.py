from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from agent.db._core import _open


def store_poll(
    *,
    monitor_id: str,
    token: str,
    protocol: str,
    vault_addr: str | None,
    apy_base: float | None,
    apy_rewards: float | None,
    apy_net: float | None,
    tvl_usd: float | None,
    apy_7d: float | None,
    apy_30d: float | None,
    raw: dict,
) -> None:
    """Synchronous insert — run via asyncio.to_thread from async code."""
    conn = _open()
    try:
        conn.execute(
            """INSERT INTO deposit_earn_polls
               (monitor_id, polled_at, token, protocol, vault_addr,
                apy_base, apy_rewards, apy_net, tvl_usd, apy_7d, apy_30d, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                monitor_id,
                datetime.now(timezone.utc).isoformat(),
                token,
                protocol,
                vault_addr,
                apy_base,
                apy_rewards,
                apy_net,
                tvl_usd,
                apy_7d,
                apy_30d,
                json.dumps(raw),
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def async_store_poll(**kwargs) -> None:
    await asyncio.to_thread(store_poll, **kwargs)
