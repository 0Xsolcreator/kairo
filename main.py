from __future__ import annotations

import asyncio
import sys

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

import agent.engines  # initialises polling + analyzer singletons
from agent.graph import _strip_think, make_graph

_DB = "agent_memory.db"


async def chat_loop(thread_id: str = "default") -> None:
    print("QVAC — Crypto Market Monitor Agent")
    print("Type 'exit' to quit.\n")

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


async def run_once(prompt: str, thread_id: str | None = None) -> str:
    import uuid
    # One-shot calls always use a fresh thread so stale history never bleeds in.
    tid = thread_id or str(uuid.uuid4())
    async with AsyncSqliteSaver.from_conn_string(_DB) as checkpointer:
        graph = make_graph(checkpointer)
        config = {"configurable": {"thread_id": tid}}
        result = await graph.ainvoke(
            {"messages": [("human", prompt)]}, config=config
        )
        return result["messages"][-1].content


if __name__ == "__main__":
    args = sys.argv[1:]
    thread_id = "default"

    if args and args[0].startswith("--thread="):
        thread_id = args.pop(0).split("=", 1)[1]

    if args:
        # One-shot mode: python main.py "your prompt"
        print(asyncio.run(run_once(" ".join(args), thread_id=thread_id)))
    else:
        asyncio.run(chat_loop(thread_id=thread_id))
