"""Interactive flow: fund a monitor wallet via an Umbra ETA deposit."""
from __future__ import annotations

import asyncio

import agent.engines as engines
import agent.store as store
from agent.clients.umbra import UmbraClient, UmbraCommandFailed
from agent.pretty_logging import ok, qprint
from agent.schemas.monitor import SUPPORTED_TOKENS, get_token_decimals

_umbra = UmbraClient()


def _parse_umbra_users(raw: str) -> list[str]:
    """Extract user names from `umbra user list` output.

    Each line is: <name>   <type>   <address>
    Only the first whitespace-delimited token is the actual user name.
    """
    names: list[str] = []
    for line in raw.splitlines():
        parts = line.split()
        if parts:
            names.append(parts[0])
    return names


async def run_fund_monitor_flow() -> str:
    """
    Walk the user through picking a monitor, an Umbra wallet, a token, and an
    amount, then execute `umbra eta deposit` to the monitor's funding address.
    Returns an empty string on success or an error/cancel message.
    """
    from agent.terminal import select_from_list

    # ── 1. Pick monitor ──────────────────────────────────────────────────────
    monitors = store.list_monitors()
    if not monitors:
        return "No active monitors to fund. Start a monitor first."

    monitor_labels = [
        f"{m.id[:8]}…  [{getattr(m.scope, 'token_symbol', '?')}]"
        for m in monitors
    ]
    qprint("Which monitor would you like to fund?")
    try:
        chosen_label = await asyncio.to_thread(select_from_list, "Select monitor", monitor_labels)
    except KeyboardInterrupt:
        return "Funding cancelled."

    monitor = monitors[monitor_labels.index(chosen_label)]
    wallet = engines.wallet.get_monitor_wallet(monitor.id)
    if not wallet:
        return f"Could not resolve wallet for monitor {monitor.id[:8]}."
    funding_addr = wallet.operating.address

    # ── 2. Pick Umbra user ───────────────────────────────────────────────────
    try:
        raw_users = await _umbra.user_list_raw()
    except Exception as exc:
        return f"Failed to list Umbra users: {exc}"

    users = _parse_umbra_users(raw_users)
    if not users:
        return "No Umbra users found. Set one up with `umbra user add` first."

    qprint("Which Umbra wallet should send the funds?")
    try:
        chosen_user = await asyncio.to_thread(select_from_list, "Select Umbra wallet", users)
    except KeyboardInterrupt:
        return "Funding cancelled."

    # ── 3. Pick token ────────────────────────────────────────────────────────
    token_names = list(SUPPORTED_TOKENS.keys())
    qprint("Which token would you like to deposit?")
    try:
        chosen_token = await asyncio.to_thread(select_from_list, "Select token", token_names)
    except KeyboardInterrupt:
        return "Funding cancelled."

    token_mint = SUPPORTED_TOKENS[chosen_token]
    decimals = get_token_decimals(token_mint)

    # ── 4. Amount ────────────────────────────────────────────────────────────
    qprint(f"How much {chosen_token} to deposit?")
    try:
        raw_amt = (await asyncio.to_thread(input, "  Amount: ")).strip()
    except (EOFError, KeyboardInterrupt):
        return "Funding cancelled."

    try:
        amount_units = float(raw_amt)
        if amount_units <= 0:
            raise ValueError
        amount_base = int(amount_units * (10 ** decimals))
    except ValueError:
        return f"Invalid amount '{raw_amt}'. Please enter a positive number."

    # ── 5. Confirm ───────────────────────────────────────────────────────────
    token_sym = getattr(monitor.scope, "token_symbol", "?")
    qprint(
        f"\n  Monitor : {monitor.id[:8]}…  [{token_sym}]\n"
        f"  From    : {chosen_user}\n"
        f"  Token   : {amount_units} {chosen_token}  ({amount_base} base units)\n"
        f"  To      : {funding_addr}"
    )
    try:
        confirm = (await asyncio.to_thread(input, "\n  Confirm? [y/N]: ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "Funding cancelled."
    if confirm != "y":
        return "Funding cancelled."

    # ── 6. Execute deposit ───────────────────────────────────────────────────
    try:
        await _umbra.eta_deposit(token_mint, amount_base, recipient=funding_addr, user=chosen_user)
    except UmbraCommandFailed as exc:
        return f"Umbra deposit failed (exit {exc.exit_code}):\n{exc.stderr or exc.stdout}"
    except Exception as exc:
        return f"Deposit failed: {exc}"

    ok(f"{amount_units} {chosen_token} sent to monitor {monitor.id[:8]}.")
    return ""
