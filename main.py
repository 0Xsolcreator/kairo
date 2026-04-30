from agent.graph import graph


def run(prompt: str) -> str:
    result = graph.invoke({"messages": [("human", prompt)]})
    return result["messages"][-1].content


if __name__ == "__main__":
    import sys
    prompt = " ".join(sys.argv[1:]) or "List files in the current directory."
    print(run(prompt))
