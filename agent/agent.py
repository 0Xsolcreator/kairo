import re
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from pydantic import BaseModel, Field, create_model
from typing_extensions import NotRequired

import agent.engines as engines
from agent import store
from agent.llm import llm
from agent.pretty_logging import print_monitor_box, print_monitor_review_box
from agent.schemas.monitor import SUPPORTED_TOKENS
from agent.services.deposit_earn import launch_deposit_earn_monitor
from agent.services.fund_monitor import run_fund_monitor_flow
from agent.services.monitors import display_active_monitors, render_active_monitors

_MAX_HISTORY = 20
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _recent(messages: list[BaseMessage]) -> list[BaseMessage]:
    return messages[-_MAX_HISTORY:]


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def _extract_json(text: str) -> str:
    """Return the first complete JSON object from text, handling nested braces."""
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


async def _invoke_structured(messages: list, model_class: type[BaseModel]) -> BaseModel:
    raw = await llm.ainvoke(messages)
    clean = _strip_think(raw.content)
    return model_class.model_validate_json(_extract_json(clean))


SupportedActions = Literal["list", "start", "pause", "retrieve", "review", "fund", "out_of_scope"]
AvailableMonitors = Literal["lending"]


class CurrentMessageState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    action: NotRequired[SupportedActions]
    monitor_type: NotRequired[AvailableMonitors]


class ActionOutput(BaseModel):
    action: SupportedActions
    monitor_type: AvailableMonitors | None = Field(
        default=None,
        description="Only relevant when action is 'start'. Null if not specified.",
    )
    reason: str = Field(description="Why this classification")


ACTION_SYSTEM_PROMPT = """You classify user requests for Kairo, a DeFi monitoring terminal.

Actions (choose exactly one):
- list: user wants to show, list, view, or check all existing monitors
- start: user wants to create or set up a new monitor
- pause: user wants to stop, pause, or cancel a specific active monitor
- retrieve: user wants to resume a previously stopped/paused monitor
- review: user wants to get status or details about a specific monitor
- fund: user wants to fund or top up their monitor wallet

Available monitor types (only for 'start'):
- lending: monitors deposit APY across protocols for a chosen token

Examples:
- "show my monitors" → list
- "get active monitors" → list
- "start a monitor" → start
- "set up deposit earn" → start
- "stop monitor abc-123" → pause
- "pause this monitor abc-123" → pause
- "cancel monitor abc-123" → pause
- "resume monitor abc-123" → retrieve
- "get details on monitor abc-123" → review
- "fund my wallet" → fund

If the request does not clearly match any of the above, return `out_of_scope`.
Be strict. Greetings, off-topic, capability questions → out_of_scope.

Respond with a JSON object only, no other text:
{"action": "<list|start|pause|retrieve|review|fund|out_of_scope>", "monitor_type": "<lending or null>", "reason": "<why>"}
"""


def _last_human(state: CurrentMessageState) -> list[HumanMessage]:
    return [m for m in state["messages"] if isinstance(m, HumanMessage)][-1:]


async def classify_action(state: CurrentMessageState) -> dict:
    response: ActionOutput = await _invoke_structured(
        [SystemMessage(content=ACTION_SYSTEM_PROMPT)] + _last_human(state),
        ActionOutput,
    )
    return {
        "action": response.action,
        "monitor_type": response.monitor_type,
        "messages": [AIMessage(content=response.reason, name="action_classifier")],
    }


def route_action(state: CurrentMessageState) -> str:
    return state["action"]


async def fund_monitor(state: CurrentMessageState) -> dict:
    reply = await run_fund_monitor_flow()
    return {
        "messages": [AIMessage(content=reply)] if reply else [],
    }


def observe_monitors(state: CurrentMessageState) -> dict:
    display_active_monitors()
    return {
        "messages": [AIMessage(content=render_active_monitors())],
    }


def out_of_scope(state: CurrentMessageState) -> dict:
    return {
        "messages": [AIMessage(content=(
            "I'm Kairo, a DeFi monitoring terminal. "
            "I can set up and manage monitors, check their status, or fund your monitor wallet. "
            "Type 'start monitor' to begin."
        ))],
    }


MonitorParamType = Literal["token", "address", "api_key", "string", "bool"]


class MonitorParam(BaseModel):
    name: str
    description: str
    type: MonitorParamType
    required: bool = True
    choices: list[str] | None = None


class MonitorSchema(BaseModel):
    type: AvailableMonitors
    description: str
    params: list[MonitorParam]


MONITOR_CATALOGUE: dict[AvailableMonitors, MonitorSchema] = {
    "lending": MonitorSchema(
        type="lending",
        description="Monitors deposit assets for lending across protocols and recommends the best yield for your asset.",
        params=[
            MonitorParam(
                name="token_symbol",
                description="Token to monitor",
                type="token",
                choices=list(SUPPORTED_TOKENS.keys()),
            ),
            MonitorParam(
                name="token_mint",
                description="On-chain mint address of the token",
                type="address",
                choices=list(SUPPORTED_TOKENS.values()),
            ),
            MonitorParam(
                name="jup_api_key",
                description="Jupiter API key for executor actions",
                type="api_key",
                required=False,
            ),
            MonitorParam(
                name="withdraw_address",
                description="The address to which the funds should be withdrawed on stopping the monitor",
                type="address",
            ),
        ],
    ),
}


async def extract_params(
    messages: list[BaseMessage],
    params: list[MonitorParam],
) -> dict[str, str | None]:
    fields = {param.name: (str | None, None) for param in params}
    ExtractedModel = create_model("ExtractedParams", **fields)

    param_descriptions = "\n".join(
        f"- {param.name}: {param.description}"
        + (f" (choices: {', '.join(param.choices)})" if param.choices else "")
        for param in params
    )
    json_shape = "{" + ", ".join(f'"{p.name}": null' for p in params) + "}"
    extraction_prompt = (
        "Extract the requested params from the conversation. "
        "Replace null with the actual value if the user mentioned it; keep null if not.\n\n"
        f"Params:\n{param_descriptions}\n\n"
        f"Respond with a JSON object only, no other text. Example shape: {json_shape}"
    )

    extracted = await _invoke_structured(
        [{"role": "system", "content": extraction_prompt}] + messages,
        ExtractedModel,
    )
    return {param.name: getattr(extracted, param.name, None) for param in params}


def launch_monitor(monitor_type: AvailableMonitors, monitor_params: dict[str, str]):
    match monitor_type:
        case "lending":
            return launch_deposit_earn_monitor(
                token_mint=monitor_params["token_mint"],
                token_symbol=monitor_params["token_symbol"],
                jup_api_key=monitor_params.get("jup_api_key"),
            )


async def monitor_start(state: CurrentMessageState):
    monitor_type: AvailableMonitors = state.get("monitor_type") or interrupt({
        "type": "list",
        "prompt": "Which monitor type would you like to start?",
        "choices": list(MONITOR_CATALOGUE.keys()),
    })
    monitor_schema: MonitorSchema = MONITOR_CATALOGUE[monitor_type]
    monitor_params = monitor_schema.params

    extracted = await extract_params(_recent(state["messages"]), monitor_params)

    final_monitor_params: dict[str, str] = {}

    for param in monitor_params:
        extracted_param_value = extracted.get(param.name)

        if extracted_param_value:
            final_monitor_params[param.name] = extracted_param_value
            continue
        if not param.required:
            continue
        
        if param.choices:
            user_value = interrupt({
                "type": "list",
                "prompt": param.description,
                "choices": param.choices,
            })
        else:
            user_value = interrupt({
                "type": "text",
                "prompt": param.description,
            })

        final_monitor_params[param.name] = user_value

    result = launch_monitor(monitor_type=monitor_type, monitor_params=final_monitor_params)

    print_monitor_box(
        monitor_id=result.monitor.id,
        token_symbol=final_monitor_params["token_symbol"],
        token_mint=final_monitor_params["token_mint"],
        poll_interval=result.monitor.poll_interval,
        funding_address=result.funding_address,
        holding_addr=final_monitor_params["withdraw_address"], # NOTE: the withdraw address shouldn't be handled by infrastructure, it should be handled in monitor. # TODO: move to monitor specific
    )

    return {
        "messages": [AIMessage(content=f"Monitor started. ID: {result.monitor.id}")],
    }


async def monitor_pause(state: CurrentMessageState):
    extracted = await extract_params(
        _recent(state["messages"]),
        [MonitorParam(name="monitor_id", description="The monitor UUID the user mentioned", type="string", required=False)],
    )
    monitor_id = extracted.get("monitor_id") or interrupt({"type": "text", "prompt": "Which monitor ID should be paused?"})

    engines.polling.stop(monitor_id)
    store.update_monitor_status(monitor_id, "paused")
    return {
        "messages": [AIMessage(content=f"Monitor '{monitor_id}' paused.")],
    }

async def monitor_review(state: CurrentMessageState):
    extracted = await extract_params(
        _recent(state["messages"]),
        [MonitorParam(name="monitor_id", description="The monitor UUID the user mentioned", type="string", required=False)],
    )
    monitor_id = extracted.get("monitor_id") or interrupt({"type": "text", "prompt": "Which monitor ID would you like to review?"})

    monitor = store.get_monitor(monitor_id)
    if not monitor:
        return {
            "messages": [AIMessage(content=f"No monitor found with ID '{monitor_id}'.")],
            "monitor_intent": None,
        }

    print_monitor_review_box(monitor)

    # TODO: Needs rework, should be agentic and handle analyzer results, monitor status & metadata & portfolio

    scope = monitor.scope
    summary = (
        f"Monitor '{monitor_id}' — type: {monitor.type}, status: {monitor.status.value}, "
        f"token: {getattr(scope, 'token_symbol', '?')}, "
        f"poll interval: every {monitor.poll_interval}s, "
        f"created: {monitor.created_at.strftime('%Y-%m-%d %H:%M UTC')}."
    )
    return {
        "messages": [AIMessage(content=summary)],
    }

async def monitor_retrieve(state: CurrentMessageState):
    extracted = await extract_params(
        _recent(state["messages"]),
        [MonitorParam(name="monitor_id", description="The monitor UUID the user mentioned", type="string", required=False)],
    )
    monitor_id = extracted.get("monitor_id") or interrupt({"type": "text", "prompt": "Which monitor ID would you like to resume?"})

    monitor = store.get_monitor(monitor_id)
    if not monitor:
        return {
            "messages": [AIMessage(content=f"No monitor found with ID '{monitor_id}'.")],
            "monitor_intent": None,
        }

    if monitor.status.value == "active":
        return {
            "messages": [AIMessage(content=f"Monitor '{monitor_id}' is already active.")],
            "monitor_intent": None,
        }

    engines.polling.start(monitor)
    store.update_monitor_status(monitor_id, "active")
    return {
        "messages": [AIMessage(content=f"Monitor '{monitor_id}' resumed.")],
    }



workflow = StateGraph(CurrentMessageState)

workflow.add_node(classify_action)
workflow.add_node(fund_monitor)
workflow.add_node(observe_monitors)
workflow.add_node(out_of_scope)
workflow.add_node(monitor_start)
workflow.add_node(monitor_pause)
workflow.add_node(monitor_review)
workflow.add_node(monitor_retrieve)

workflow.add_edge(START, "classify_action")
workflow.add_conditional_edges(
    "classify_action",
    route_action,
    {
        "list": "observe_monitors",
        "start": "monitor_start",
        "pause": "monitor_pause",
        "review": "monitor_review",
        "retrieve": "monitor_retrieve",
        "fund": "fund_monitor",
        "out_of_scope": "out_of_scope",
    },
)
workflow.add_edge("fund_monitor", END)
workflow.add_edge("observe_monitors", END)
workflow.add_edge("out_of_scope", END)
workflow.add_edge("monitor_start", END)
workflow.add_edge("monitor_pause", END)
workflow.add_edge("monitor_review", END)
workflow.add_edge("monitor_retrieve", END)
