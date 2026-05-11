from __future__ import annotations

import sys
import termios
import tty

from agent.schemas.monitor import SUPPORTED_TOKENS

_TOKENS: list[dict[str, str]] = [
    {"symbol": sym, "mint": mint}
    for sym, mint in SUPPORTED_TOKENS.items()
]

_UP   = "\x1b[A"
_DOWN = "\x1b[B"
_ESC_PREFIX = "\x1b"


def _read_key() -> str:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == _ESC_PREFIX:
            ch2 = sys.stdin.read(1)
            ch3 = sys.stdin.read(1)
            return ch + ch2 + ch3
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _render(selected: int) -> None:
    print("\n  Select a token to monitor (↑↓ arrows, Enter to confirm, q to cancel):\n")
    for i, tok in enumerate(_TOKENS):
        cursor = "▶" if i == selected else " "
        print(f"    {cursor} {tok['symbol']}")
    print()


def _clear(n_lines: int) -> None:
    print(f"\033[{n_lines}A\033[J", end="", flush=True)


def _render_monitor_types(names: list[str], selected: int) -> None:
    print("\n  Select a monitor type (↑↓ arrows, Enter to confirm, q to cancel):\n")
    for i, name in enumerate(names):
        cursor = "▶" if i == selected else " "
        print(f"    {cursor} {name}")
    print(f"      · more coming soon")
    print()


def select_monitor_type(names: list[str]) -> str:
    """
    Blocking interactive monitor-type selector. Returns the chosen monitor name.
    Raises KeyboardInterrupt if the user presses Ctrl-C or q.
    """
    selected = 0
    total_lines = len(names) + 5  # +1 for "more coming soon" line

    _render_monitor_types(names, selected)

    while True:
        key = _read_key()

        if key in ("\r", "\n"):
            _clear(total_lines)
            return names[selected]
        elif key == _UP:
            selected = (selected - 1) % len(names)
        elif key == _DOWN:
            selected = (selected + 1) % len(names)
        elif key in ("\x03", "q"):
            _clear(total_lines)
            raise KeyboardInterrupt

        _clear(total_lines)
        _render_monitor_types(names, selected)


def select_token() -> dict[str, str]:
    """
    Blocking interactive token selector. Returns {"symbol": ..., "mint": ...}.
    Raises KeyboardInterrupt if the user presses Ctrl-C or q.
    """
    selected = 0
    total_lines = len(_TOKENS) + 4  # header (blank + title + blank) + items + trailing blank

    _render(selected)

    while True:
        key = _read_key()

        if key in ("\r", "\n"):
            _clear(total_lines)
            return _TOKENS[selected]
        elif key == _UP:
            selected = (selected - 1) % len(_TOKENS)
        elif key == _DOWN:
            selected = (selected + 1) % len(_TOKENS)
        elif key in ("\x03", "q"):
            _clear(total_lines)
            raise KeyboardInterrupt

        _clear(total_lines)
        _render(selected)
