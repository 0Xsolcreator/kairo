from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

from agent.schemas.monitor import Monitor


class Decision(BaseModel):
    monitor_id: str
    monitor_type: str
    signal: str
    reason: str

    @field_validator("signal")
    @classmethod
    def _signal_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("signal must not be empty")
        return v
    price: float | None = None
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    metadata: dict = Field(default_factory=dict)


class BaseAnalyzer(ABC):
    @abstractmethod
    def analyze(self, monitor: Monitor, raw: dict) -> Decision:
        """Parse raw poll data and return a Decision. Called on every tick."""
        ...
