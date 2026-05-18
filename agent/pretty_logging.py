from __future__ import annotations

import logging
import os
import re
import textwrap
from pathlib import Path

# ── ANSI codes ───────────────────────────────────────────────────────────────
R      = "\033[0m"
DIM    = "\033[2m"
BOLD   = "\033[1m"
GRAY   = "\033[90m"
ACCENT = "\033[91m"   # Claude-style bright red — primary UI accent
CYAN   = "\033[36m"   # kept for INFO log level only
GREEN  = "\033[32m"   # kept for success indicators (ok())
YELLOW = "\033[33m"
RED    = "\033[31m"

_STYLES: dict[int, tuple[str, str]] = {
    logging.DEBUG:    (GRAY,   "·"),
    logging.INFO:     (CYAN,   "ℹ"),
    logging.WARNING:  (YELLOW, "⚠"),
    logging.ERROR:    (RED,    "✗"),
    logging.CRITICAL: (ACCENT, "✗"),
}

_NAME_W = 22

# ── Log formatter ────────────────────────────────────────────────────────────

class QVACFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        color, sym = _STYLES.get(record.levelno, ("\033[37m", "?"))

        parts = record.name.split(".")
        short = ".".join(parts[-2:]) if len(parts) > 2 else record.name

        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)

        indent = " " * (2 + 1 + 2 + _NAME_W + 2)
        msg = msg.replace("\n", f"\n{indent}")

        return f"  {color}{sym}{R}  {DIM}{short:<{_NAME_W}}{R}  {msg}"


def install(*, root_level: int = logging.WARNING) -> None:
    _debug = os.environ.get("QVAC_DEBUG", "").lower() in ("1", "true", "yes", "on")

    handler = logging.StreamHandler()
    handler.setFormatter(QVACFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(root_level)

    if _debug:
        # All agent.* modules at DEBUG — external libs (langchain, httpx…) stay quiet
        logging.getLogger("agent").setLevel(logging.DEBUG)
        logging.getLogger(__name__).warning(
            "QVAC_DEBUG=1 — verbose logging enabled for all agent modules"
        )


# ── Shared UI helpers ─────────────────────────────────────────────────────────

_QVAC_PREFIX = f"  {ACCENT}{BOLD}◆{R}  {BOLD}QVAC{R}  "
_QVAC_INDENT = " " * 11

INPUT_PROMPT = f"  {ACCENT}You{R}  "


def qprint(msg: str) -> None:
    """Styled QVAC agent response block."""
    lines = msg.split("\n")
    print()
    print(f"{_QVAC_PREFIX}{lines[0]}")
    for line in lines[1:]:
        print(f"{_QVAC_INDENT}{line}")
    print()


def sysmsg(msg: str) -> None:
    """Indented dim system note."""
    print(f"  {DIM}{msg}{R}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}⚠{R}  {msg}")


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{R}  {msg}")


def err(msg: str) -> None:
    print(f"  {RED}✗{R}  {msg}")


# ── Shared box helpers ───────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _vlen(s: str) -> int:
    """Visual (on-screen) length of a string, ignoring ANSI codes."""
    return len(_ANSI_RE.sub("", s))


def _vcell(s: str, w: int) -> str:
    """Pad or truncate string to visual width w, ignoring ANSI codes."""
    v = _vlen(s)
    if v > w:
        plain = _ANSI_RE.sub("", s)
        return plain[:w - 1] + "…"
    return s + " " * (w - v)


def _short_cwd() -> str:
    cwd = Path.cwd()
    try:
        return "~/" + str(cwd.relative_to(Path.home()))
    except ValueError:
        return str(cwd)


def print_welcome_box(monitors: list) -> None:
    """
    Render a Claude Code-style startup box showing agent info and
    any active monitors that were restored from the database.

    Layout (76 chars wide):
      ╭─ QVAC ─────────────────────────────────────────────────────────────╮
      │  <left panel: 33 chars>  │  <right panel: 34 chars>  │
      ╰─────────────────────────────────────────────────────────────────────╯
    """
    L_W, R_W = 33, 34
    DASHES = 72  # inner dash count for top/bottom border

    def row(left: str = "", right: str = "") -> str:
        return (
            f"  {ACCENT}│{R} {_vcell(left, L_W)} "
            f"{ACCENT}│{R} {_vcell(right, R_W)} {ACCENT}│{R}"
        )

    # ── Right panel: active monitors ─────────────────────────────────────
    # Blank at index 0 = top padding row (no external print(row()) needed)
    rp: list[str] = [
        "",
        f"{ACCENT}{BOLD}Active monitors{R}",
        f"{DIM}{'─' * R_W}{R}",
    ]
    if monitors:
        for m in monitors[:3]:
            sym = getattr(m.scope, "token_symbol", "?")
            short_id = m.id[:8] + "…" + m.id[-4:]
            rp.append(f"{ACCENT}◆{R}  {m.type} · {sym}")
            rp.append(f"   {DIM}{short_id}{R}")
        if len(monitors) > 3:
            rp.append(f"{DIM}  + {len(monitors) - 3} more{R}")
    else:
        rp.append(f"{DIM}No recent activity{R}")
    rp.append("")  # bottom padding

    # ── Left panel: agent identity ────────────────────────────────────────
    # ◆ is horizontally centered: (L_W - 1) // 2 = 16 leading spaces
    lp: list[str] = [
        "",
        f"{BOLD}Deposit-yield monitor{R}",
        "Solana DeFi agent",
        "",
        f"{' ' * 16}{ACCENT}{BOLD}◆{R}",
        "",
        f"{DIM}{_short_cwd()}{R}",
        "",
    ]

    n = max(len(lp), len(rp))
    lp += [""] * (n - len(lp))
    rp += [""] * (n - len(rp))

    # ── Render ────────────────────────────────────────────────────────────
    print()
    header = "─ QVAC "
    print(f"  {ACCENT}╭{header}{'─' * (DASHES - len(header))}╮{R}")
    for l, r in zip(lp, rp):
        print(row(l, r))
    print(f"  {ACCENT}╰{'─' * DASHES}╯{R}")
    print()


def print_monitor_box(
    *,
    monitor_id: str,
    token_symbol: str,
    token_mint: str,
    poll_interval: int,
    funding_address: str,
    holding_addr: str,
) -> None:
    """
    Render a single-column info box after a monitor is created.

    Total line width matches the welcome box (76 chars):
      ╭─ Monitor started ────────────────────────────────────────────────────╮
      │  <content: 70 chars>                                                 │
      ╰──────────────────────────────────────────────────────────────────────╯
    """
    W = 70       # inner content width
    DASHES = 72  # top/bottom dash count (= W + 2 for the border spaces)

    def row(content: str = "") -> str:
        return f"  {ACCENT}│{R} {_vcell(content, W)} {ACCENT}│{R}"

    def field(label: str, value: str) -> str:
        return f"  {DIM}{label:<10}{R}  {value}"

    lines: list[str] = [
        "",
        f"  {GREEN}✓{R}  {BOLD}Deposit Earn monitor started{R}  ·  {ACCENT}{BOLD}{token_symbol}{R}",
        "",
        field("ID",         monitor_id),
        field("Token",      f"{token_symbol}  {DIM}({token_mint[:8]}…){R}"),
        field("Protocols",  "Jupiter Lend + Kamino KVaults"),
        field("Interval",   f"every {poll_interval}s"),
        field("Funding",    f"{ACCENT}{funding_address}{R}"),
        field("Returns to", f"{DIM}{holding_addr}{R}"),
        "",
        f"  {DIM}Send funds to Funding to begin operations.{R}",
        f"  {DIM}Say 'get signals' or 'list active monitors' anytime.{R}",
        "",
    ]

    print()
    header = "─ Monitor started "
    print(f"  {ACCENT}╭{header}{'─' * (DASHES - len(header))}╮{R}")
    for line in lines:
        print(row(line))
    print(f"  {ACCENT}╰{'─' * DASHES}╯{R}")
    print()


def print_monitors_box(monitors_data: list[dict]) -> None:
    """Styled box for the active monitors list."""
    W = 70
    DASHES = 72

    def row(content: str = "") -> str:
        return f"  {ACCENT}│{R} {_vcell(content, W)} {ACCENT}│{R}"

    lines: list[str] = [""]

    if not monitors_data:
        lines.append(f"  {DIM}No monitors are currently running.{R}")
    else:
        n = len(monitors_data)
        lines.append(f"  {BOLD}{n} monitor{'s' if n != 1 else ''}{R}")
        for i, m in enumerate(monitors_data):
            lines.append("")
            lines.append(f"  {ACCENT}◆{R}  {DIM}{m['id']}{R}")
            lines.append(
                f"     {BOLD}{m['type']}{R} · {ACCENT}{BOLD}{m['token']}{R}"
                f"   every {m['poll_interval']}s   {DIM}{m['status']}{R}"
            )
            if m.get("funding"):
                lines.append(f"     {DIM}Funding{R}  {m['funding']}")
            if i < n - 1:
                lines.append("")
                lines.append(f"  {DIM}{'─' * (W - 4)}{R}")

    lines.append("")

    print()
    header = "─ Active monitors "
    print(f"  {ACCENT}╭{header}{'─' * (DASHES - len(header))}╮{R}")
    for line in lines:
        print(row(line))
    print(f"  {ACCENT}╰{'─' * DASHES}╯{R}")
    print()


def print_monitor_review_box(monitor) -> None:
    """Styled box for monitor metadata review."""
    W = 70
    DASHES = 72

    def row(content: str = "") -> str:
        return f"  {ACCENT}│{R} {_vcell(content, W)} {ACCENT}│{R}"

    def field(label: str, value: str) -> str:
        return f"  {DIM}{label:<12}{R}  {value}"

    status = monitor.status.value
    status_color = GREEN if status == "active" else (YELLOW if status == "paused" else GRAY)
    scope = monitor.scope

    token_sym  = getattr(scope, "token_symbol", "—")
    token_mint = getattr(scope, "token_mint",   "—")
    jup_key    = getattr(scope, "jup_api_key",  None)
    short_mint = f"{token_mint[:8]}…{token_mint[-4:]}" if len(token_mint) > 14 else token_mint
    created    = monitor.created_at.strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = [
        "",
        f"  {BOLD}{monitor.type}{R}   {status_color}{BOLD}{status}{R}",
        "",
        field("ID",        monitor.id),
        field("Token",     f"{ACCENT}{BOLD}{token_sym}{R}  {DIM}({short_mint}){R}"),
        field("Interval",  f"every {monitor.poll_interval}s"),
        field("Created",   created),
        field("Jup key",   f"{GREEN}configured{R}" if jup_key else f"{DIM}not set{R}"),
        "",
    ]

    short_id = f"{monitor.id[:8]}…{monitor.id[-4:]}"
    header = f"─ Monitor review  ·  {short_id} "
    print()
    print(f"  {ACCENT}╭{header}{'─' * (DASHES - len(header))}╮{R}")
    for line in lines:
        print(row(line))
    print(f"  {ACCENT}╰{'─' * DASHES}╯{R}")
    print()


def print_signals_box(monitor_id: str, signals: list[dict]) -> None:
    """Styled box for recent signals of a monitor."""
    W = 70
    DASHES = 72
    TEXT_W = W - 2  # 2-space indent inside content

    def row(content: str = "") -> str:
        return f"  {ACCENT}│{R} {_vcell(content, W)} {ACCENT}│{R}"

    lines: list[str] = [""]

    if not signals:
        lines.append(f"  {DIM}No signals yet — monitor is still on its first poll.{R}")
    else:
        for i, s in enumerate(signals):
            if i > 0:
                lines.append("")
                lines.append(f"  {DIM}{'─' * (W - 4)}{R}")
                lines.append("")

            sig = s["signal"]
            sig_color = ACCENT if sig in ("KAMINO", "JUPITER") else (RED if sig == "ERROR" else GRAY)
            lines.append(f"  {DIM}{s['timestamp']}{R}   {sig_color}{BOLD}{sig}{R}")
            lines.append("")

            for wline in textwrap.wrap(s["reason"], TEXT_W - 2):
                lines.append(f"  {wline}")

            jup_apy = s.get("jupiter_apy")
            kam_apy = s.get("kamino_apy")
            if jup_apy is not None or kam_apy is not None:
                lines.append("")
            if jup_apy is not None:
                tvl = s.get("jupiter_tvl", 0)
                lines.append(f"  {DIM}Jupiter{R}  {BOLD}{jup_apy:.2f}%{R}  APY   TVL ${tvl:,.0f}")
            if kam_apy is not None:
                tvl = s.get("kamino_tvl", 0)
                vol = f"   {YELLOW}⚠ volatile{R}" if s.get("kamino_volatile") else ""
                lines.append(f"  {DIM}Kamino {R}  {BOLD}{kam_apy:.2f}%{R}  APY   TVL ${tvl:,.0f}{vol}")

    lines.append("")

    short_id = f"{monitor_id[:8]}…{monitor_id[-8:]}" if len(monitor_id) > 18 else monitor_id
    header = f"─ Signals  ·  {short_id} "
    print()
    print(f"  {ACCENT}╭{header}{'─' * (DASHES - len(header))}╮{R}")
    for line in lines:
        print(row(line))
    print(f"  {ACCENT}╰{'─' * DASHES}╯{R}")
    print()
