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


if __name__ == "__main__":
    args = sys.argv[1:]
    thread_id = "default"

    if args and args[0].startswith("--thread="):
        thread_id = args.pop(0).split("=", 1)[1]

    asyncio.run(chat_loop(thread_id=thread_id))
