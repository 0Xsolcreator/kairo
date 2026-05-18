from langchain_core.messages import AIMessage, SystemMessage

from agent.graph._utils import _invoke_structured, _last_human
from agent.graph.state import ActionOutput, CurrentMessageState

ACTION_SYSTEM_PROMPT = """You classify user requests for Kairo, a DeFi monitoring terminal.

Actions (choose exactly one):
- list: user wants to show, list, view, or check all existing monitors
- start: user wants to create or set up a new monitor
- pause: user wants to stop, pause, or cancel a specific active monitor
- retrieve: user wants to resume a previously stopped/paused monitor
- review: user wants to get status or details about a specific monitor
- fund: user wants to fund or top up their monitor wallet

Available monitor types (only for 'start'):
- lending: monitors deposit APY across protocols for a chosen token

Examples:
- "show my monitors" → list
- "get active monitors" → list
- "start a monitor" → start
- "set up deposit earn" → start
- "stop monitor abc-123" → pause
- "pause this monitor abc-123" → pause
- "cancel monitor abc-123" → pause
- "resume monitor abc-123" → retrieve
- "get details on monitor abc-123" → review
- "fund my wallet" → fund

If the request does not clearly match any of the above, return `out_of_scope`.
Be strict. Greetings, off-topic, capability questions → out_of_scope.

Respond with a JSON object only, no other text:
{"action": "<list|start|pause|retrieve|review|fund|out_of_scope>", "monitor_type": "<lending or null>", "reason": "<why>"}
"""


async def classify_action(state: CurrentMessageState) -> dict:
    response: ActionOutput = await _invoke_structured(
        [SystemMessage(content=ACTION_SYSTEM_PROMPT)] + _last_human(state["messages"]),
        ActionOutput,
    )
    return {
        "action": response.action,
        "monitor_type": response.monitor_type,
        "messages": [AIMessage(content=response.reason, name="action_classifier")],
    }


def route_action(state: CurrentMessageState) -> str:
    return state["action"]
