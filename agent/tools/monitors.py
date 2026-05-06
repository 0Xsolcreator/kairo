from __future__ import annotations

from langchain_core.tools import tool

from agent.schemas.monitor import SUPPORTED_TOKENS
from agent.services import monitors as monitor_services

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
    return monitor_services.do_stop_monitor(monitor_id)


@tool
def list_active_monitors() -> str:
    """List all currently running monitors with their IDs and configuration."""
    return monitor_services.render_active_monitors()


@tool
def get_recent_signals(monitor_id: str, limit: int = 5) -> str:
    """
    Fetch the most recent analysis signals for a running monitor.

    Args:
        monitor_id: The monitor ID.
        limit: Number of recent signals to return (1–20, default 5).
    """
    return monitor_services.render_recent_signals(monitor_id, limit)
