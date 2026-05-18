from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
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


def _recent(messages: list[BaseMessage]) -> list[BaseMessage]:
    return messages[-_MAX_HISTORY:]


SupportedIntents = Literal["monitor", "observe", "fund", "out_of_scope"]
SupportedMonitorIntents = Literal[
    "start", "pause", "retrieve", "review", "out_of_scope"
]
AvailableMonitors = Literal["lending"]


class CurrentMessageState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    intent: SupportedIntents
    monitor_intent: NotRequired[SupportedMonitorIntents]
    monitor_type: NotRequired[AvailableMonitors]


class ClassificationToolOutput(BaseModel):
    intent: SupportedIntents
    reason: str = Field(description="Why this classification; what the user asked for")


CLASSIFICATION_SYSTEM_PROMPT = """You classify user requests for <your tool>.

Supported intents (and ONLY these):
- monitor: user wants to setup monitoring for on-chain activity
- observe: user wants to observe on-chain activity of a setup up monitoring
- fund: user wants to fund their monitor wallet

If the request does not clearly match one of the above, return `out_of_scope`.
Be strict. Ambiguous → out_of_scope. Off-topic (weather, general chat, other
protocols, code help) → out_of_scope.
"""


async def classify_intent(state: CurrentMessageState) -> dict:
    classification_llm = llm.with_structured_output(ClassificationToolOutput)
    response: ClassificationToolOutput = await classification_llm.ainvoke(
        [SystemMessage(content=CLASSIFICATION_SYSTEM_PROMPT)] + _recent(state["messages"])
    )
    return {
        "intent": response.intent,
        "messages": [AIMessage(content=response.reason, name="intent_classifier")],
    }


def route_intent(state: CurrentMessageState):
    return state["intent"]


async def fund_monitor(state: CurrentMessageState) -> dict:
    reply = await run_fund_monitor_flow()
    return {
        "messages": [AIMessage(content=reply)] if reply else [],
    }


def observe_monitors(state: CurrentMessageState) -> dict:
    # TODO: move monitor status here or move this to monitors subgraph
    display_active_monitors()
    return {
        "messages": [AIMessage(content=render_active_monitors())],
    }


def out_of_scope(state: CurrentMessageState) -> dict:
    return {
        "messages": [AIMessage(content=(
            "I'm Kairo, an agentic terminal for Defi Automation."
            "I can set up and manage monitors, check their status, or show recent signals. "
            "Type 'start monitor' to begin, or ask me about an active monitor."
        ))],
    }


class MonitorToolOutput(BaseModel):
    intent: SupportedMonitorIntents
    monitor_type: AvailableMonitors | None = Field(
        default=None,
        description="Only relevant when intent is 'start'. Null if not specified by the user.",
    )
    reason: str = Field(description="Why this classification; what the user asked for")


MONITOR_SYSTEM_PROMPT = """You classify user requests for <your tool>.

Supported intents (and ONLY these):
- start: user wants to setup a new or resume previously stopped monitoring for on-chain activity
- pause: user wants to stop/pause an active running (started) monitor
- retrieve: user wants to look for previously stopped monitors.
- review: user wants to get status/metadata about the running monitor.

Available monitor types (only for 'start' intent):
- lending: monitors deposit APY across protocols for a chosen token

If the request does not clearly match one of the above, return `out_of_scope`.
Be strict. Ambiguous → out_of_scope. Off-topic (weather, general chat, other
protocols, code help) → out_of_scope.
"""


async def classify_monitor_action(state: CurrentMessageState) -> dict:
    monitor_type_classification_llm = llm.with_structured_output(MonitorToolOutput)
    response: MonitorToolOutput = await monitor_type_classification_llm.ainvoke(
        [SystemMessage(content=MONITOR_SYSTEM_PROMPT)] + _recent(state["messages"])
    )
    return {
        "monitor_intent": response.intent,
        "monitor_type": response.monitor_type,
        "messages": [AIMessage(content=response.reason, name="monitor_classifier")],
    }


def route_monitor_action(state: CurrentMessageState):
    return state["monitor_intent"]


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
    extraction_prompt = (
        "Extract the requested params the user mentioned. Use null if not mentioned.\n\n"
        f"Params:\n{param_descriptions}"
    )

    extractor = llm.with_structured_output(ExtractedModel)
    extracted = await extractor.ainvoke(
        [{"role": "system", "content": extraction_prompt}] + messages
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
                "prompt": param.description,
                "choices": param.choices,
            })
        else:
            user_value = interrupt({
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
        "monitor_intent": None,
    }


async def monitor_pause(state: CurrentMessageState):
    extracted = await extract_params(
        _recent(state["messages"]),
        [MonitorParam(name="monitor_id", description="The monitor UUID the user mentioned", type="string", required=False)],
    )
    monitor_id = extracted.get("monitor_id") or interrupt({"prompt": "Which monitor ID should be paused?"})

    engines.polling.stop(monitor_id)
    store.update_monitor_status(monitor_id, "paused")
    return {
        "messages": [AIMessage(content=f"Monitor '{monitor_id}' paused.")],
        "monitor_intent": None,
    }

async def monitor_review(state: CurrentMessageState):
    extracted = await extract_params(
        _recent(state["messages"]),
        [MonitorParam(name="monitor_id", description="The monitor UUID the user mentioned", type="string", required=False)],
    )
    monitor_id = extracted.get("monitor_id") or interrupt({"prompt": "Which monitor ID would you like to review?"})

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
        "monitor_intent": None,
    }

async def monitor_retrieve(state: CurrentMessageState):
    extracted = await extract_params(
        _recent(state["messages"]),
        [MonitorParam(name="monitor_id", description="The monitor UUID the user mentioned", type="string", required=False)],
    )
    monitor_id = extracted.get("monitor_id") or interrupt({"prompt": "Which monitor ID would you like to resume?"})

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
        "monitor_intent": None,
    }



workflow = StateGraph(CurrentMessageState)

workflow.add_node(classify_intent)
workflow.add_node(classify_monitor_action)
workflow.add_node(fund_monitor)
workflow.add_node(observe_monitors)
workflow.add_node(out_of_scope)
workflow.add_node(monitor_start)
workflow.add_node(monitor_pause)
workflow.add_node(monitor_review)
workflow.add_node(monitor_retrieve)

workflow.add_edge(START, "classify_intent")
workflow.add_conditional_edges(
    "classify_intent",
    route_intent,
    {
        "monitor": "classify_monitor_action",
        "observe": "observe_monitors",
        "fund": "fund_monitor",
        "out_of_scope": "out_of_scope",
    },
)
workflow.add_conditional_edges(
    "classify_monitor_action",
    route_monitor_action,
    {
        "start": "monitor_start",
        "pause": "monitor_pause",
        "review": "monitor_review",
        "retrieve": "monitor_retrieve",
        "out_of_scope": END,
    },
)
workflow.add_edge("fund_monitor", END)
workflow.add_edge("observe_monitors", END)
workflow.add_edge("out_of_scope", END)
workflow.add_edge("monitor_start", END)
workflow.add_edge("monitor_pause", END)
workflow.add_edge("monitor_review", END)
workflow.add_edge("monitor_retrieve", END)
