from __future__ import annotations

import asyncio
import sys
import uuid

from agent.pretty_logging import (
    install as _install_logging,
    ACCENT, BOLD, R,
    INPUT_PROMPT, qprint, sysmsg,
    print_welcome_box,
)

_install_logging()

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command, Interrupt

import agent.engines  # initialises polling + analyzer singletons
import agent.store as store
from agent.agent import workflow
from agent.terminal import select_from_list


async def _handle_interrupt(payload: dict) -> str:
    kind = payload.get("type", "text")
    if kind == "list":
        return await asyncio.to_thread(
            select_from_list, payload["prompt"], payload["choices"]
        )
    if kind == "confirm":
        raw = await asyncio.to_thread(input, f"  {payload['prompt']} [y/N]: ")
        return raw.strip().lower()
    return (await asyncio.to_thread(input, f"  {payload['prompt']}: ")).strip()


async def chat_loop(thread_id: str) -> None:
    await agent.engines.restore_active_monitors()

    monitors = store.list_monitors()
    print_welcome_box(monitors)

    sysmsg("Type 'exit' to quit.")
    print()

    graph = workflow.compile(checkpointer=MemorySaver())
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

        input_val = {"messages": [("human", prompt)]}
        while True:
            result = await graph.ainvoke(input_val, config=config)
            interrupts: list[Interrupt] = result.get("__interrupt__", [])
            if not interrupts:
                break
            chosen = await _handle_interrupt(interrupts[0].value)
            input_val = Command(resume=chosen)

        reply = result["messages"][-1].content
        if reply:
            qprint(reply)

    await agent.engines.polling.stop_all()
    print()
    sysmsg("All monitors stopped. Goodbye.")
    print()


if __name__ == "__main__":
    args = sys.argv[1:]
    thread_id = uuid.uuid4().hex

    if args and args[0].startswith("--thread="):
        thread_id = args.pop(0).split("=", 1)[1]

    asyncio.run(chat_loop(thread_id=thread_id))
