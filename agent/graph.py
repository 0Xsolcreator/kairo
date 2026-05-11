from __future__ import annotations

import asyncio
import re as _re
from typing import Annotated

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import NotRequired, TypedDict

import agent.engines as engines
from agent.llm import llm
from agent.pretty_logging import err, ok, print_monitor_box, qprint, sysmsg, warn
from agent.services import monitors as monitor_services
from agent.services.deposit_earn import launch_deposit_earn_monitor
from agent.tools import tools
from agent.tools.monitors import MONITOR_CATALOGUE, catalogue_text

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    return f"""\
You are QVAC — a crypto deposit-yield monitoring assistant.

=== AVAILABLE MONITOR TYPES ===
{catalogue_text()}
=== WHAT YOU DO ===
You help users:
- Understand the deposit_earn monitor and what it tracks
- Start a deposit_earn monitor (the system will show an interactive token picker)
- List, inspect, or stop running monitors
- Interpret signals from running monitors

=== SCOPE RESTRICTION ===
You ONLY handle monitor setup, status queries, and signal interpretation.
Politely refuse anything else and redirect the user.

=== HOW TO START A MONITOR ===
When the user wants to start a deposit earn monitor, tell them to type something like
"start deposit earn monitor" and the system will guide them through token selection
interactively — no need to ask for parameters yourself.
"""

_SYSTEM_PROMPT = _build_system_prompt()

_GUARDRAIL_PROMPT = """\
You are a strict input classifier for a crypto deposit-yield monitoring assistant.

The assistant handles ONLY:
- Questions about the deposit_earn monitor type
- Starting, stopping, or listing deposit earn monitors
- Checking signals or APY recommendations from a running monitor

Reply with exactly one word:
  ALLOWED  — if the message is clearly about any of the above
  BLOCKED  — if off-topic (general chat, trading, coding help, news, etc.)
"""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class State(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    pending_action: NotRequired[str | None]   # "deposit_earn_setup" or None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_think(text: str) -> str:
    return _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).strip()


def _guardrail_verdict(raw: str) -> str:
    clean = _strip_think(raw).upper()
    return "BLOCKED" if "BLOCKED" in clean else "ALLOWED"


_UUID_RE = _re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", _re.I
)

# ---------------------------------------------------------------------------
# Intent detection (regex, no LLM needed)
# ---------------------------------------------------------------------------

_INTENT_LIST    = _re.compile(r"\b(list|show|active|running)\b.*\bmonitor", _re.I)
_INTENT_SIGNALS = _re.compile(r"\b(signal|signals|latest|recent|check)\b", _re.I)
_INTENT_STOP    = _re.compile(r"\bstop\b.*\bmonitor\b|\bcancel\b.*\bmonitor\b", _re.I)
_INTENT_DEPOSIT_EARN = _re.compile(
    r"\bdeposit[\s_]earn\b"
    r"|\bearn[\s_]monitor\b"
    r"|\b(start|setup|set\s+up|create|track)\b.{0,25}\b(deposit|earn|yield|apy)\b"
    r"|\b(deposit|earn|yield|apy)\b.{0,25}\b(monitor|track|watch|start)\b"
    r"|\bmonitor\b.{0,25}\b(earnings?|deposit|yield|apy)\b",
    _re.I,
)
_INTENT_MONITOR_SETUP = _re.compile(
    r"\b(start|setup|set\s+up|create|new|add|launch)\b.{0,20}\bmonitor\b"
    r"|\bmonitor\b.{0,20}\b(setup|start|create|new|add|launch)\b"
    r"|\bi\s+want\s+(a\s+)?monitor\b",
    _re.I,
)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

llm_with_tools = llm.bind_tools(tools)


async def guardrail(state: State) -> dict:
    """Rejects off-topic first messages only."""
    messages = state["messages"]
    if any(isinstance(m, AIMessage) for m in messages[:-1]):
        return {}
    result = await llm.ainvoke(
        [SystemMessage(content=_GUARDRAIL_PROMPT), messages[-1]]
    )
    if _guardrail_verdict(result.content) == "BLOCKED":
        return {
            "messages": [AIMessage(content=(
                "I'm QVAC, a deposit-yield monitoring assistant. "
                "I compare APY across Jupiter Lend and Kamino KVaults "
                "so you can pick the best protocol for your asset. "
                "Type 'start deposit earn monitor' to begin, or ask me anything "
                "about active monitors and their signals."
            ))],
            "pending_action": None,
        }
    return {}


def intent_router(state: State) -> dict:
    """
    Handles list / stop / signals / deposit-earn-setup requests directly —
    no LLM tool call needed, keeping latency low.
    """
    last = state["messages"][-1]
    if not isinstance(last, HumanMessage):
        return {}

    text = last.content

    if _INTENT_LIST.search(text):
        monitor_services.display_active_monitors()
        return {"messages": [AIMessage(content="")], "pending_action": None}

    if _INTENT_STOP.search(text):
        m = _UUID_RE.search(text)
        reply = monitor_services.do_stop_monitor(m.group() if m else None)
        return {"messages": [AIMessage(content=reply)], "pending_action": None}

    if _INTENT_SIGNALS.search(text):
        m = _UUID_RE.search(text)
        monitor_services.display_recent_signals(m.group() if m else None)
        return {"messages": [AIMessage(content="")], "pending_action": None}

    if _INTENT_DEPOSIT_EARN.search(text):
        return {"pending_action": "deposit_earn_setup"}

    if _INTENT_MONITOR_SETUP.search(text):
        return {"pending_action": "monitor_type_picker"}

    return {}


async def _ensure_holding_address() -> str | None:
    """
    Make sure a holding-wallet address is configured before any monitor setup.

    Returns the configured address on success, or None if the user cancelled
    the prompt (the caller should bail out of monitor setup).
    """
    if engines.wallet.has_holding_address():
        return engines.wallet.get_holding_address()

    qprint(
        "Before we set up a monitor, I need your main (holding) wallet address.\n"
        "This is where funds will be returned via Umbra when monitors close."
    )

    while True:
        try:
            raw = await asyncio.to_thread(input, "  Holding address: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        try:
            engines.wallet.set_holding_address(raw)
        except ValueError as e:
            warn(f"Invalid: {e}. Try again or Ctrl-C to cancel.")
            continue
        addr = engines.wallet.get_holding_address()
        ok(f"Saved: {addr}")
        print()
        return addr


async def monitor_type_picker(_state: State) -> dict:
    """Shows an interactive monitor-type menu and sets pending_action to the chosen setup."""
    from agent.terminal import select_monitor_type

    names = list(MONITOR_CATALOGUE.keys())
    try:
        chosen = await asyncio.to_thread(select_monitor_type, names)
    except KeyboardInterrupt:
        return {
            "messages": [AIMessage(content="Monitor setup cancelled.")],
            "pending_action": None,
        }
    return {"pending_action": f"{chosen}_setup"}


async def deposit_earn_setup(state: State) -> dict:
    """
    Collects I/O (holding address, token, API key) then delegates all
    monitor-creation side-effects to the service layer.
    """
    from agent.terminal import select_token

    holding_addr = await _ensure_holding_address()
    if holding_addr is None:
        return {
            "messages": [AIMessage(content="Setup cancelled — no holding address provided.")],
            "pending_action": None,
        }

    qprint("Let's set up your Deposit Earn monitor.")

    try:
        token = await asyncio.to_thread(select_token)
    except KeyboardInterrupt:
        return {
            "messages": [AIMessage(content="Monitor setup cancelled.")],
            "pending_action": None,
        }

    print()
    sysmsg("Jupiter API key (from beta.jup.ag/api — required for executor actions):")
    try:
        jup_api_key = (await asyncio.to_thread(input, "  Key: ")).strip() or None
    except (EOFError, KeyboardInterrupt):
        return {
            "messages": [AIMessage(content="Monitor setup cancelled.")],
            "pending_action": None,
        }
    if not jup_api_key:
        warn("No API key set — polling will be unauthenticated and executor actions will abort.")
        print()

    result = launch_deposit_earn_monitor(
        token_symbol=token["symbol"],
        token_mint=token["mint"],
        jup_api_key=jup_api_key,
    )

    print_monitor_box(
        monitor_id=result.monitor.id,
        token_symbol=token["symbol"],
        token_mint=token["mint"],
        poll_interval=result.monitor.poll_interval,
        funding_address=result.funding_address,
        holding_addr=holding_addr,
    )
    return {"messages": [AIMessage(content="")], "pending_action": None}


_MAX_HISTORY = 20  # keep last N messages to stay within local model context limit

async def call_model(state: State) -> dict:
    messages = [SystemMessage(content=_SYSTEM_PROMPT)] + state["messages"][-_MAX_HISTORY:]
    response = await llm_with_tools.ainvoke(messages)

    clean = _strip_think(response.content)
    if not clean and not response.tool_calls:
        clean = (
            "Could you clarify what you'd like to do? "
            "I can start a deposit earn monitor, list active monitors, or check signals."
        )
    if clean != response.content:
        response = response.model_copy(update={"content": clean})

    return {"messages": [response]}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def after_guardrail(state: State) -> str:
    if isinstance(state["messages"][-1], AIMessage):
        return END
    return "intent_router"


def after_intent_router(state: State) -> str:
    action = state.get("pending_action")
    if action == "deposit_earn_setup":
        return "deposit_earn_setup"
    if action == "monitor_type_picker":
        return "monitor_type_picker"
    if isinstance(state["messages"][-1], AIMessage):
        return END
    return "agent"


def after_monitor_type_picker(state: State) -> str:
    action = state.get("pending_action")
    if action == "deposit_earn_setup":
        return "deposit_earn_setup"
    return END


def after_agent(state: State) -> str:
    last = state["messages"][-1]
    if last.tool_calls:
        return "tools"
    return END


# ---------------------------------------------------------------------------
# Build graph
# ---------------------------------------------------------------------------

def make_graph(checkpointer: BaseCheckpointSaver):
    builder = StateGraph(State)

    builder.add_node("guardrail",           guardrail)
    builder.add_node("intent_router",       intent_router)
    builder.add_node("monitor_type_picker", monitor_type_picker)
    builder.add_node("deposit_earn_setup",  deposit_earn_setup)
    builder.add_node("agent",               call_model)
    builder.add_node("tools",               ToolNode(tools))

    builder.set_entry_point("guardrail")
    builder.add_conditional_edges("guardrail",           after_guardrail)
    builder.add_conditional_edges("intent_router",       after_intent_router)
    builder.add_conditional_edges("monitor_type_picker", after_monitor_type_picker)
    builder.add_conditional_edges("agent",               after_agent)
    builder.add_edge("deposit_earn_setup", END)
    builder.add_edge("tools",              "agent")

    return builder.compile(checkpointer=checkpointer)
