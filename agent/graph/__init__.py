from langgraph.graph import END, START, StateGraph

from agent.graph.classify import classify_action, route_action
from agent.graph.nodes import (
    fund_monitor,
    monitor_pause,
    monitor_retrieve,
    monitor_review,
    monitor_start,
    observe_monitors,
    out_of_scope,
)
from agent.graph.state import CurrentMessageState

workflow = StateGraph(CurrentMessageState)

workflow.add_node(classify_action)
workflow.add_node(fund_monitor)
workflow.add_node(observe_monitors)
workflow.add_node(out_of_scope)
workflow.add_node(monitor_start)
workflow.add_node(monitor_pause)
workflow.add_node(monitor_review)
workflow.add_node(monitor_retrieve)

workflow.add_edge(START, "classify_action")
workflow.add_conditional_edges(
    "classify_action",
    route_action,
    {
        "list":        "observe_monitors",
        "start":       "monitor_start",
        "pause":       "monitor_pause",
        "review":      "monitor_review",
        "retrieve":    "monitor_retrieve",
        "fund":        "fund_monitor",
        "out_of_scope": "out_of_scope",
    },
)
workflow.add_edge("fund_monitor",      END)
workflow.add_edge("observe_monitors",  END)
workflow.add_edge("out_of_scope",      END)
workflow.add_edge("monitor_start",     END)
workflow.add_edge("monitor_pause",     END)
workflow.add_edge("monitor_review",    END)
workflow.add_edge("monitor_retrieve",  END)
