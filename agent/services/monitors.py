"""
Generic monitor operations: list, stop, and signal rendering.

Both the regex intent router (agent/graph.py) and the LangChain tool
wrappers (agent/tools/monitors.py) call into this module.

Monitor-type-specific launch logic lives in its own service file:
  agent/services/deposit_earn.py  — launch_deposit_earn_monitor
"""
from __future__ import annotations

import agent.engines as engines
import agent.store as store
from agent.pretty_logging import print_monitors_box, print_signals_box, qprint


def render_active_monitors() -> str:
    """Human-readable listing of every active monitor."""
    monitors = store.list_monitors()
    if not monitors:
        return "No monitors are currently running."

    lines = [f"{len(monitors)} monitor(s):", ""]
    for m in monitors:
        sym = getattr(m.scope, "token_symbol", "?")
        funding = engines.wallet.get_funding_address(m.id)
        lines.append(f"  • {m.id}")
        lines.append(f"      Type    : {m.type} — {sym}")
        lines.append(f"      Interval: every {m.poll_interval}s")
        lines.append(f"      Status  : {m.status.value}")
        if funding:
            lines.append(f"      Funding : {funding}")
        lines.append("")
    return "\n".join(lines).rstrip()


def do_stop_monitor(monitor_id: str | None) -> str:
    """Stop a monitor by ID. Returns a status string."""
    if not monitor_id:
        return "Please include the monitor ID. Example: 'stop monitor <id>'"
    if store.get_monitor(monitor_id) is None:
        return f"No active monitor found with ID '{monitor_id}'."
    engines.polling.stop(monitor_id)
    store.unregister_monitor(monitor_id)
    # Mark the wallet record closed. Actual fund sweep-back to the holding
    # address is the close-flow chain's job (not yet implemented).
    engines.wallet.close_monitor_wallet(monitor_id)
    return f"Monitor '{monitor_id}' stopped and removed."


def render_recent_signals(monitor_id: str | None, limit: int = 5) -> str:
    """
    Render the most recent signals for a monitor.

    If `monitor_id` is None, falls back to the most recently registered monitor
    (matching the previous regex-router behaviour). The `limit` argument is
    clamped to the inclusive range [1, 20].
    """
    limit = max(1, min(limit, 20))

    if not monitor_id:
        all_monitors = store.list_monitors()
        if not all_monitors:
            return "No monitors running. Start one first."
        monitor_id = all_monitors[-1].id

    if store.get_monitor(monitor_id) is None:
        return f"No active monitor found with ID '{monitor_id}'."

    signals = store.get_signals(monitor_id, limit)
    if not signals:
        return "No signals yet — the monitor is still on its first poll."

    lines = [f"Last {len(signals)} signal(s) for monitor {monitor_id}:", ""]
    for s in reversed(signals):
        ts = s.timestamp.strftime("%H:%M:%S")
        meta = s.metadata
        lines.append(f"[{ts}] {s.signal}")
        lines.append(f"  {s.reason}")
        jup_apy = meta.get("jupiter_apy_pct")
        kam_apy = meta.get("kamino_apy_pct")
        if jup_apy is not None:
            tvl = meta.get("jupiter_tvl_usd", 0)
            lines.append(f"  Jupiter : {jup_apy:.2f}% APY | TVL ${tvl:,.0f}")
        if kam_apy is not None:
            tvl = meta.get("kamino_tvl_usd", 0)
            vol_tag = " ⚠ volatile" if meta.get("kamino_yield_volatile") else ""
            lines.append(f"  Kamino  : {kam_apy:.2f}% APY | TVL ${tvl:,.0f}{vol_tag}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ── Direct display (bypasses qprint — renders styled boxes) ──────────────────

def display_active_monitors() -> None:
    """Print a styled box listing all active monitors."""
    monitors = store.list_monitors()
    print_monitors_box([
        {
            "id": m.id,
            "type": m.type,
            "token": getattr(m.scope, "token_symbol", "?"),
            "poll_interval": m.poll_interval,
            "status": m.status.value,
            "funding": engines.wallet.get_funding_address(m.id),
        }
        for m in monitors
    ])


def display_recent_signals(monitor_id: str | None, limit: int = 5) -> None:
    """Print a styled signals box for the given monitor."""
    limit = max(1, min(limit, 20))

    if not monitor_id:
        all_monitors = store.list_monitors()
        if not all_monitors:
            qprint("No monitors running. Start one first.")
            return
        monitor_id = all_monitors[-1].id

    if store.get_monitor(monitor_id) is None:
        qprint(f"No active monitor found with ID '{monitor_id}'.")
        return

    signals = store.get_signals(monitor_id, limit)
    print_signals_box(monitor_id, [
        {
            "timestamp": s.timestamp.strftime("%H:%M:%S"),
            "signal":    s.signal,
            "reason":    s.reason,
            "jupiter_apy":    s.metadata.get("jupiter_apy_pct"),
            "kamino_apy":     s.metadata.get("kamino_apy_pct"),
            "jupiter_tvl":    s.metadata.get("jupiter_tvl_usd", 0),
            "kamino_tvl":     s.metadata.get("kamino_tvl_usd", 0),
            "kamino_volatile": s.metadata.get("kamino_yield_volatile", False),
        }
        for s in reversed(signals)
    ])
