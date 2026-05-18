from langchain_core.messages import AIMessage
from langgraph.types import interrupt

import agent.engines as engines
from agent import store
from agent.graph._utils import _recent
from agent.graph.catalogue import MONITOR_CATALOGUE, MonitorParam, extract_params, launch_monitor
from agent.graph.state import AvailableMonitors, CurrentMessageState
from agent.pretty_logging import print_monitor_box, print_monitor_review_box
from agent.services.fund_monitor import run_fund_monitor_flow
from agent.services.monitors import display_active_monitors, render_active_monitors


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


async def monitor_start(state: CurrentMessageState) -> dict:
    monitor_type: AvailableMonitors = state.get("monitor_type") or interrupt({
        "type": "list",
        "prompt": "Which monitor type would you like to start?",
        "choices": list(MONITOR_CATALOGUE.keys()),
    })
    monitor_schema = MONITOR_CATALOGUE[monitor_type]
    monitor_params = monitor_schema.params

    extracted = await extract_params(_recent(state["messages"]), monitor_params)

    final_params: dict[str, str] = {}
    for param in monitor_params:
        value = extracted.get(param.name)
        if value:
            final_params[param.name] = value
            continue
        if not param.required:
            continue
        final_params[param.name] = interrupt({
            "type": "list" if param.choices else "text",
            "prompt": param.description,
            **({"choices": param.choices} if param.choices else {}),
        })

    result = launch_monitor(monitor_type=monitor_type, monitor_params=final_params)

    print_monitor_box(
        monitor_id=result.monitor.id,
        token_symbol=final_params["token_symbol"],
        token_mint=final_params["token_mint"],
        poll_interval=result.monitor.poll_interval,
        funding_address=result.funding_address,
        holding_addr=final_params["withdraw_address"],
    )
    return {
        "messages": [AIMessage(content=f"Monitor started. ID: {result.monitor.id}")],
    }


async def monitor_pause(state: CurrentMessageState) -> dict:
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


async def monitor_review(state: CurrentMessageState) -> dict:
    extracted = await extract_params(
        _recent(state["messages"]),
        [MonitorParam(name="monitor_id", description="The monitor UUID the user mentioned", type="string", required=False)],
    )
    monitor_id = extracted.get("monitor_id") or interrupt({"type": "text", "prompt": "Which monitor ID would you like to review?"})

    monitor = store.get_monitor(monitor_id)
    if not monitor:
        return {
            "messages": [AIMessage(content=f"No monitor found with ID '{monitor_id}'.")],
        }

    print_monitor_review_box(monitor)

    # TODO: make agentic — handle analyzer results, monitor status & metadata & portfolio
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


async def monitor_retrieve(state: CurrentMessageState) -> dict:
    extracted = await extract_params(
        _recent(state["messages"]),
        [MonitorParam(name="monitor_id", description="The monitor UUID the user mentioned", type="string", required=False)],
    )
    monitor_id = extracted.get("monitor_id") or interrupt({"type": "text", "prompt": "Which monitor ID would you like to resume?"})

    monitor = store.get_monitor(monitor_id)
    if not monitor:
        return {
            "messages": [AIMessage(content=f"No monitor found with ID '{monitor_id}'.")],
        }

    if monitor.status.value == "active":
        return {
            "messages": [AIMessage(content=f"Monitor '{monitor_id}' is already active.")],
        }

    engines.polling.start(monitor)
    store.update_monitor_status(monitor_id, "active")
    return {
        "messages": [AIMessage(content=f"Monitor '{monitor_id}' resumed.")],
    }
