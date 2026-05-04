from agent.graph import graph


def run(prompt: str, thread_id: str = "default") -> str:
    config = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke({"messages": [("human", prompt)]}, config=config)
    return result["messages"][-1].content


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    thread_id = "default"
    if args and args[0].startswith("--thread="):
        thread_id = args.pop(0).split("=", 1)[1]
    prompt = " ".join(args) or "List files in the current directory."
    print(run(prompt, thread_id=thread_id))
