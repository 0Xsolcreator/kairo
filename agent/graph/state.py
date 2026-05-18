from typing import Annotated, Literal

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from typing_extensions import NotRequired, TypedDict

SupportedActions = Literal["list", "start", "pause", "retrieve", "review", "fund", "out_of_scope"]
AvailableMonitors = Literal["lending"]


class CurrentMessageState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    action: NotRequired[SupportedActions]
    monitor_type: NotRequired[AvailableMonitors]


class ActionOutput(BaseModel):
    action: SupportedActions
    monitor_type: AvailableMonitors | None = Field(
        default=None,
        description="Only relevant when action is 'start'. Null if not specified.",
    )
    reason: str = Field(description="Why this classification")
