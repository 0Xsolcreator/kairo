import re

from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel

from agent.llm import llm

_MAX_HISTORY = 20
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _recent(messages: list[BaseMessage]) -> list[BaseMessage]:
    return messages[-_MAX_HISTORY:]


def _last_human(messages: list[BaseMessage]) -> list[HumanMessage]:
    return [m for m in messages if isinstance(m, HumanMessage)][-1:]


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def _extract_json(text: str) -> str:
    """Return the first complete JSON object from text, handling nested braces."""
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


async def _invoke_structured(messages: list, model_class: type[BaseModel]) -> BaseModel:
    raw = await llm.ainvoke(messages)
    clean = _strip_think(raw.content)
    return model_class.model_validate_json(_extract_json(clean))
