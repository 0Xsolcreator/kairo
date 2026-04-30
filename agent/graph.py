from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_core.messages import BaseMessage
from typing import Annotated
from typing_extensions import TypedDict

from agent.llm import llm
from agent.tools import tools


class State(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


llm_with_tools = llm.bind_tools(tools)


def call_model(state: State) -> State:
    response = llm_with_tools.invoke(state["messages"])
    return {"messages": [response]}


def should_continue(state: State) -> str:
    last = state["messages"][-1]
    return "tools" if last.tool_calls else END


builder = StateGraph(State)
builder.add_node("agent", call_model)
builder.add_node("tools", ToolNode(tools))

builder.set_entry_point("agent")
builder.add_conditional_edges("agent", should_continue)
builder.add_edge("tools", "agent")

graph = builder.compile()
