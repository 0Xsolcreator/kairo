from __future__ import annotations

import asyncio
import sys

from agent.pretty_logging import (
    install as _install_logging,
    ACCENT, BOLD, R,
    INPUT_PROMPT, qprint, sysmsg,
    print_welcome_box,
)

_install_logging()

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

import agent.engines  # initialises polling + analyzer singletons
import agent.store as store
from agent.graph import make_graph

# TODO [PORT]: When switching to agent.py, replace the import above with:
#
#   from langgraph.types import Command
#   from langgraph.errors import GraphInterrupt
#   from agent.terminal import select_from_list
#   from agent.agent import workflow
#
# And replace `make_graph(checkpointer)` with:
#   graph = workflow.compile(checkpointer=checkpointer)
#
# Note: agent.py currently builds `workflow = StateGraph(...)` at module level
# but never calls .compile(). The compile step must happen here so the
# checkpointer is wired in at runtime, not at import time.

_DB = "agent_memory.db"


async def chat_loop(thread_id: str = "default") -> None:
    await agent.engines.restore_active_monitors()

    monitors = store.list_monitors()
    print_welcome_box(monitors)

    sysmsg("Type 'exit' to quit.")
    print()

    async with AsyncSqliteSaver.from_conn_string(_DB) as checkpointer:
        graph = make_graph(checkpointer)
        config = {"configurable": {"thread_id": thread_id}}

        while True:
            try:
                prompt = await asyncio.to_thread(input, INPUT_PROMPT)
            except (EOFError, KeyboardInterrupt):
                break

            prompt = prompt.strip()
            if not prompt:
                continue
            if prompt.lower() in ("exit", "quit"):
                break

            # TODO [PORT]: Replace this single ainvoke with an interrupt-aware
            # loop so the terminal pickers are shown whenever a node calls
            # interrupt(). Pattern:
            #
            #   input_val = {"messages": [("human", prompt)]}
            #   while True:
            #       try:
            #           result = await graph.ainvoke(input_val, config=config)
            #           break
            #       except GraphInterrupt as e:
            #           payload = e.args[0]
            #           chosen = await _handle_interrupt(payload)
            #           input_val = Command(resume=chosen)
            #
            # Where _handle_interrupt routes to the right picker based on the
            # payload shape (see TODO [INTERRUPT PARAMS] below).

            result = await graph.ainvoke(
                {"messages": [("human", prompt)]}, config=config
            )
            reply = result["messages"][-1].content
            if reply:
                qprint(reply)

    await agent.engines.polling.stop_all()
    print()
    sysmsg("All monitors stopped. Goodbye.")
    print()


# TODO [INTERRUPT PARAMS]: Standardise the interrupt() payload shape across
# agent.py before implementing _handle_interrupt here. Currently the calls are
# inconsistent — some have "choices", some don't, and nothing signals which
# picker to show. Proposed convention:
#
#   List picker  → interrupt({"type": "list",  "prompt": "...", "choices": [...]})
#   Text input   → interrupt({"type": "text",  "prompt": "..."})
#   Confirm y/n  → interrupt({"type": "confirm", "prompt": "..."})
#
# Then _handle_interrupt becomes a clean dispatch:
#
#   async def _handle_interrupt(payload: dict) -> str:
#       kind = payload.get("type", "text")
#       if kind == "list":
#           return await asyncio.to_thread(
#               select_from_list, payload["prompt"], payload["choices"]
#           )
#       if kind == "confirm":
#           raw = await asyncio.to_thread(input, f"  {payload['prompt']} [y/N]: ")
#           return raw.strip().lower()
#       return (await asyncio.to_thread(input, f"  {payload['prompt']}: ")).strip()
#
# All interrupt() calls in agent.py need to be updated to include "type".
# Current calls to audit:
#   - monitor_start:    monitor type picker    → type: "list"
#   - monitor_start:    each required param    → type: "list" if choices else "text"
#   - monitor_pause:    monitor ID             → type: "text"
#   - monitor_review:   monitor ID             → type: "text"
#   - monitor_retrieve: monitor ID             → type: "text"


if __name__ == "__main__":
    args = sys.argv[1:]
    thread_id = "default"

    if args and args[0].startswith("--thread="):
        thread_id = args.pop(0).split("=", 1)[1]

    asyncio.run(chat_loop(thread_id=thread_id))
