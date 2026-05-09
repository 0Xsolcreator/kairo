from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator


class MonitorStatus(str, Enum):
    active = "active"
    paused = "paused"
    expired = "expired"


class DataSource(BaseModel):
    endpoints: list[str] = Field(min_length=1)

    @field_validator("endpoints")
    @classmethod
    def _no_empty_urls(cls, v: list[str]) -> list[str]:
        if any(not ep.strip() for ep in v):
            raise ValueError("endpoints must not contain empty strings")
        return v

    @classmethod
    def from_url(cls, url: str) -> "DataSource":
        return cls(endpoints=[url])


# ---------------------------------------------------------------------------
# Supported tokens
# ---------------------------------------------------------------------------

SUPPORTED_TOKENS: dict[str, str] = {
    "wSOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}

# On-chain decimals for each supported mint. The Kamino KTX API takes amounts
# in decimal token format (e.g. "1.234567"), so we need this to convert from
# base units. Keep in sync with SUPPORTED_TOKENS when adding new tokens.
TOKEN_DECIMALS_BY_MINT: dict[str, int] = {
    "So11111111111111111111111111111111111111112": 9,   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6,  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": 6,  # USDT
}


def get_token_decimals(token_mint: str) -> int:
    """Return the on-chain decimals for a supported token mint."""
    try:
        return TOKEN_DECIMALS_BY_MINT[token_mint]
    except KeyError as e:
        raise ValueError(
            f"unknown token_mint {token_mint!r} — extend TOKEN_DECIMALS_BY_MINT "
            "when adding a new supported token"
        ) from e


_SUPPORTED_SYMBOLS = tuple(SUPPORTED_TOKENS.keys())


# ---------------------------------------------------------------------------
# Scope schemas
# ---------------------------------------------------------------------------

class DepositEarnScope(BaseModel):
    token_symbol: Literal["wSOL", "USDC", "USDT"]
    token_mint: str
    jup_api_key: str | None = None

    @field_validator("token_mint")
    @classmethod
    def _valid_mint(cls, v: str) -> str:
        v = v.strip()
        if not (32 <= len(v) <= 44):
            raise ValueError(
                f"token_mint must be a valid Solana public key (32–44 chars), got {len(v)}"
            )
        if v not in SUPPORTED_TOKENS.values():
            raise ValueError(
                f"token_mint {v!r} is not a supported token. "
                f"Supported mints: {list(SUPPORTED_TOKENS.values())}"
            )
        return v


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_SCOPE_REGISTRY: dict[str, type[BaseModel]] = {
    "deposit_earn": DepositEarnScope,
}


def register_monitor_type(type_name: str, scope_schema: type[BaseModel]) -> None:
    _SCOPE_REGISTRY[type_name] = scope_schema


def get_scope_schema(type_name: str) -> type[BaseModel]:
    schema = _SCOPE_REGISTRY.get(type_name)
    if schema is None:
        registered = ", ".join(_SCOPE_REGISTRY)
        raise ValueError(
            f"Unknown monitor type {type_name!r}. Registered types: {registered}"
        )
    return schema


# ---------------------------------------------------------------------------
# Monitor model
# ---------------------------------------------------------------------------

class Monitor(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str
    scope: BaseModel
    source: DataSource
    poll_interval: int = Field(description="Polling interval in seconds")
    status: MonitorStatus = MonitorStatus.active
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    model_config = {"arbitrary_types_allowed": True}

    @field_serializer("scope")
    def _serialize_scope(self, scope: BaseModel) -> dict:
        return scope.model_dump()

    @model_validator(mode="before")
    @classmethod
    def _coerce_fields(cls, data: Any) -> Any:
        monitor_type = data.get("type")
        scope_data = data.get("scope")
        if monitor_type and isinstance(scope_data, dict):
            schema = get_scope_schema(monitor_type)
            data["scope"] = schema(**scope_data)

        source = data.get("source")
        if isinstance(source, str):
            data["source"] = DataSource(endpoints=[source])
        elif isinstance(source, list):
            data["source"] = DataSource(endpoints=source)

        return data
