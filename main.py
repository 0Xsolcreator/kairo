from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(level=logging.WARNING)
logging.getLogger("agent.executor").setLevel(logging.DEBUG)

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

import agent.engines  # initialises polling + analyzer singletons
from agent.graph import _strip_think, make_graph

_DB = "agent_memory.db"


async def chat_loop(thread_id: str = "default") -> None:
    print("QVAC — Crypto Market Monitor Agent")
    print("Type 'exit' to quit.\n")

    await agent.engines.restore_active_monitors()

    async with AsyncSqliteSaver.from_conn_string(_DB) as checkpointer:
        graph = make_graph(checkpointer)
        config = {"configurable": {"thread_id": thread_id}}

        while True:
            try:
                prompt = await asyncio.to_thread(input, "You: ")
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
            reply = _strip_think(result["messages"][-1].content)
            print(f"\nQVAC: {reply}\n")

    await agent.engines.polling.stop_all()
    print("All monitors stopped. Goodbye.")


if __name__ == "__main__":
    args = sys.argv[1:]
    thread_id = "default"

    if args and args[0].startswith("--thread="):
        thread_id = args.pop(0).split("=", 1)[1]

    asyncio.run(chat_loop(thread_id=thread_id))
