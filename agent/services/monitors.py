from __future__ import annotations

import agent.engines as engines
import agent.store as store
from agent.pretty_logging import print_monitors_box


def render_active_monitors() -> str:
    """Human-readable listing of every monitor."""
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


def display_active_monitors() -> None:
    """Print a styled box listing all monitors."""
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
