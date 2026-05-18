from __future__ import annotations

from dataclasses import dataclass

import agent.engines as engines
import agent.store as store
from agent.schemas.monitor import DepositEarnScope, Monitor


@dataclass(frozen=True)
class MonitorLaunchResult:
    monitor: Monitor
    funding_address: str


def launch_deposit_earn_monitor(
    *,
    token_symbol: str,
    token_mint: str,
    jup_api_key: str | None,
) -> MonitorLaunchResult:
    """
    Create, register, wallet-allocate, and start a deposit_earn monitor.
    Called by the graph node after I/O collection is complete.
    """
    scope = DepositEarnScope(
        token_symbol=token_symbol,
        token_mint=token_mint,
        jup_api_key=jup_api_key,
    )
    monitor = Monitor(
        type="deposit_earn",
        scope=scope,
        poll_interval=60,
    )
    store.register_monitor(monitor)
    monitor_wallet = engines.wallet.create_monitor_wallet(monitor.id)
    engines.polling.start(monitor)
    return MonitorLaunchResult(
        monitor=monitor,
        funding_address=monitor_wallet.operating.address,
    )
