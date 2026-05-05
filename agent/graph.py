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
import agent.store as store
from agent.llm import llm
from agent.schemas.monitor import DataSource, DepositEarnScope, Monitor, SUPPORTED_TOKENS
from agent.tools import tools
from agent.tools.monitors import catalogue_text

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


def _run_intent(intent: str, args: dict) -> str:
    if intent == "list_monitors":
        monitors = store.list_monitors()
        if not monitors:
            return "No monitors are currently running."
        lines = [f"{len(monitors)} active monitor(s):\n"]
        for mon in monitors:
            sym = getattr(mon.scope, "token_symbol", "?")
            lines.append(f"  ID      : {mon.id}")
            lines.append(f"  Type    : {mon.type} — {sym}")
            lines.append(f"  Interval: every {mon.poll_interval}s")
            lines.append(f"  Status  : {mon.status.value}\n")
        return "\n".join(lines)

    if intent == "stop_monitor":
        mid = args.get("monitor_id")
        if not mid:
            return "Please include the monitor ID. Example: 'stop monitor <id>'"
        if store.get_monitor(mid) is None:
            return f"No active monitor found with ID '{mid}'."
        engines.polling.stop(mid)
        store.unregister_monitor(mid)
        return f"Monitor '{mid}' has been stopped."

    if intent == "get_signals":
        mid = args.get("monitor_id")
        if not mid:
            all_monitors = store.list_monitors()
            if not all_monitors:
                return "No monitors running. Start one first."
            mid = all_monitors[-1].id
        if store.get_monitor(mid) is None:
            return f"No active monitor found with ID '{mid}'."
        signals = store.get_signals(mid, 5)
        if not signals:
            return "No signals yet — the monitor is still on its first poll."
        lines = [f"Last {len(signals)} signal(s) for {mid}:\n"]
        for s in reversed(signals):
            ts   = s.timestamp.strftime("%H:%M:%S")
            meta = s.metadata
            lines.append(f"[{ts}] {s.signal}")
            lines.append(f"  {s.reason}")
            jup_apy = meta.get("jupiter_apy_pct")
            kam_apy = meta.get("kamino_apy_pct")
            if jup_apy is not None:
                lines.append(
                    f"  Jupiter : {jup_apy:.2f}% APY | "
                    f"TVL ${meta.get('jupiter_tvl_usd', 0):,.0f}"
                )
            if kam_apy is not None:
                vol = " ⚠ volatile" if meta.get("kamino_yield_volatile") else ""
                lines.append(
                    f"  Kamino  : {kam_apy:.2f}% APY | "
                    f"TVL ${meta.get('kamino_tvl_usd', 0):,.0f}{vol}"
                )
            lines.append("")
        return "\n".join(lines)

    return "Unknown intent."


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
        reply = _run_intent("list_monitors", {})
        return {"messages": [AIMessage(content=reply)], "pending_action": None}

    if _INTENT_STOP.search(text):
        m = _UUID_RE.search(text)
        reply = _run_intent("stop_monitor", {"monitor_id": m.group() if m else None})
        return {"messages": [AIMessage(content=reply)], "pending_action": None}

    if _INTENT_SIGNALS.search(text):
        m = _UUID_RE.search(text)
        reply = _run_intent("get_signals", {"monitor_id": m.group() if m else None})
        return {"messages": [AIMessage(content=reply)], "pending_action": None}

    if _INTENT_DEPOSIT_EARN.search(text):
        return {"pending_action": "deposit_earn_setup"}

    return {}


async def deposit_earn_setup(state: State) -> dict:
    """
    Interactively collects a token choice (arrow-key menu) and launches
    a deposit_earn monitor. Runs the blocking menu in a thread.
    """
    from agent.terminal import select_token

    print("\nQVAC: Let's set up your Deposit Earn monitor.")

    try:
        token = await asyncio.to_thread(select_token)
    except KeyboardInterrupt:
        return {
            "messages": [AIMessage(content="Monitor setup cancelled.")],
            "pending_action": None,
        }

    scope = DepositEarnScope(
        token_symbol=token["symbol"],
        token_mint=token["mint"],
    )
    monitor = Monitor(
        type="deposit_earn",
        scope=scope,
        source=DataSource(endpoints=[
            "https://api.jup.ag/lend/v1",
            "https://api.kamino.finance",
        ]),
        poll_interval=60,
    )
    store.register_monitor(monitor)
    engines.polling.start(monitor)

    return {
        "messages": [AIMessage(content=(
            f"Deposit Earn monitor started for {token['symbol']}!\n\n"
            f"  ID       : {monitor.id}\n"
            f"  Token    : {token['symbol']} ({token['mint'][:8]}…)\n"
            f"  Protocols: Jupiter Lend + Kamino KVaults\n"
            f"  Interval : every {monitor.poll_interval}s\n\n"
            f"Say 'get signals' to check the latest APY recommendation.\n"
            f"Say 'list active monitors' to see all running monitors."
        ))],
        "pending_action": None,
    }


async def call_model(state: State) -> dict:
    messages = [SystemMessage(content=_SYSTEM_PROMPT)] + state["messages"]
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
    if state.get("pending_action") == "deposit_earn_setup":
        return "deposit_earn_setup"
    if isinstance(state["messages"][-1], AIMessage):
        return END
    return "agent"


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

    builder.add_node("guardrail",          guardrail)
    builder.add_node("intent_router",      intent_router)
    builder.add_node("deposit_earn_setup", deposit_earn_setup)
    builder.add_node("agent",              call_model)
    builder.add_node("tools",              ToolNode(tools))

    builder.set_entry_point("guardrail")
    builder.add_conditional_edges("guardrail",     after_guardrail)
    builder.add_conditional_edges("intent_router", after_intent_router)
    builder.add_conditional_edges("agent",         after_agent)
    builder.add_edge("deposit_earn_setup", END)
    builder.add_edge("tools",              "agent")

    return builder.compile(checkpointer=checkpointer)
