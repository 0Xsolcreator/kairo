"""
Single source of truth for monitor list/stop/signal operations.

Both the regex intent router (agent/graph.py) and the LangChain @tool
wrappers (agent/tools/monitors.py) call into this module, so behaviour
and output formatting stay aligned across the two entry points.
"""
from __future__ import annotations

import agent.engines as engines
import agent.store as store


def render_active_monitors() -> str:
    """Human-readable listing of every active monitor."""
    monitors = store.list_monitors()
    if not monitors:
        return "No monitors are currently running."

    lines = [f"{len(monitors)} active monitor(s):", ""]
    for m in monitors:
        sym = getattr(m.scope, "token_symbol", "?")
        lines.append(f"  • {m.id}")
        lines.append(f"      Type    : {m.type} — {sym}")
        lines.append(f"      Interval: every {m.poll_interval}s")
        lines.append(f"      Status  : {m.status.value}")
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
