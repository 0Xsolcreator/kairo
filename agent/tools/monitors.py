from __future__ import annotations

from langchain_core.tools import tool

import agent.engines as engines
import agent.store as store
from agent.schemas.monitor import SUPPORTED_TOKENS

_MONITOR_CATALOGUE: dict[str, dict] = {
    "deposit_earn": {
        "description": (
            "Monitors deposit APY across Jupiter Lend and Kamino KVaults "
            "and recommends the better protocol for your chosen asset."
        ),
        "tokens": list(SUPPORTED_TOKENS.keys()),
        "protocols": ["Jupiter Lend", "Kamino KVaults"],
        "poll_interval_seconds": 60,
        "signals": {
            "JUPITER": "Jupiter Lend currently offers the higher net APY.",
            "KAMINO":  "Kamino KVaults currently offer the higher net APY.",
            "EQUAL":   "Both protocols are offering equivalent APY (< 0.1% difference).",
            "ERROR":   "One or both protocols could not be reached.",
        },
    },
}


def catalogue_text() -> str:
    """Return a plain-text summary embedded in the system prompt."""
    lines: list[str] = []
    for name, info in _MONITOR_CATALOGUE.items():
        lines.append(f"Monitor type: {name}")
        lines.append(f"  Description : {info['description']}")
        lines.append(f"  Tokens      : {', '.join(info['tokens'])}")
        lines.append(f"  Protocols   : {', '.join(info['protocols'])}")
        lines.append(f"  Poll interval: {info['poll_interval_seconds']}s")
        lines.append("  Signals:")
        for sig, meaning in info["signals"].items():
            lines.append(f"    {sig:7s} — {meaning}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LangChain tools
# ---------------------------------------------------------------------------

@tool
def list_monitor_types() -> str:
    """Show available monitor types and what they track."""
    lines = ["Available monitor types:\n"]
    for name, info in _MONITOR_CATALOGUE.items():
        lines.append(f"**{name}**")
        lines.append(f"  {info['description']}")
        lines.append(f"  Tokens   : {', '.join(info['tokens'])}")
        lines.append(f"  Protocols: {', '.join(info['protocols'])}")
        lines.append("")
    return "\n".join(lines)


@tool
def stop_monitor(monitor_id: str) -> str:
    """
    Stop and remove a running monitor.

    Args:
        monitor_id: The ID returned when the monitor was started.
    """
    if store.get_monitor(monitor_id) is None:
        return f"No active monitor found with ID '{monitor_id}'."
    engines.polling.stop(monitor_id)
    store.unregister_monitor(monitor_id)
    return f"Monitor '{monitor_id}' stopped and removed."


@tool
def list_active_monitors() -> str:
    """List all currently running monitors with their IDs and configuration."""
    monitors = store.list_monitors()
    if not monitors:
        return "No monitors are currently running."
    lines = [f"{len(monitors)} active monitor(s):\n"]
    for m in monitors:
        scope_info = ""
        if hasattr(m.scope, "token_symbol"):
            scope_info = f" — {m.scope.token_symbol}"
        lines.append(f"  • {m.id}")
        lines.append(f"      Type    : {m.type}{scope_info}")
        lines.append(f"      Interval: every {m.poll_interval}s")
        lines.append(f"      Status  : {m.status.value}")
    return "\n".join(lines)


@tool
def get_recent_signals(monitor_id: str, limit: int = 5) -> str:
    """
    Fetch the most recent analysis signals for a running monitor.

    Args:
        monitor_id: The monitor ID.
        limit: Number of recent signals to return (1–20, default 5).
    """
    limit = max(1, min(limit, 20))

    if store.get_monitor(monitor_id) is None:
        return f"No active monitor found with ID '{monitor_id}'."

    signals = store.get_signals(monitor_id, limit)
    if not signals:
        return "No signals yet — the monitor is still on its first poll."

    lines = [f"Last {len(signals)} signal(s) for monitor {monitor_id}:\n"]
    for s in reversed(signals):
        ts  = s.timestamp.strftime("%H:%M:%S")
        meta = s.metadata
        lines.append(f"[{ts}] {s.signal}")
        lines.append(f"  {s.reason}")
        jup_apy = meta.get("jupiter_apy_pct")
        kam_apy = meta.get("kamino_apy_pct")
        if jup_apy is not None:
            tvl = meta.get("jupiter_tvl_usd", 0)
            lines.append(f"  Jupiter : {jup_apy:.2f}% APY | TVL ${tvl:,.0f}")
        if kam_apy is not None:
            tvl     = meta.get("kamino_tvl_usd", 0)
            vol_tag = " ⚠ volatile" if meta.get("kamino_yield_volatile") else ""
            lines.append(f"  Kamino  : {kam_apy:.2f}% APY | TVL ${tvl:,.0f}{vol_tag}")
        lines.append("")
    return "\n".join(lines)
