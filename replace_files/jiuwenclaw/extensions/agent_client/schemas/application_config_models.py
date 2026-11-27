from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ModelConfigCreateRequest(BaseModel):
    model_name: str
    model_type: str
    api_endpoint: str
    api_key_ref: str
    parameters: dict[str, Any] | None = None
    rate_limit: dict[str, Any] | None = None
    enabled: bool = True


class ModelConfigUpdateRequest(BaseModel):
    model_name: str | None = None
    model_type: str | None = None
    api_endpoint: str | None = None
    api_key_ref: str | None = None
    parameters: dict[str, Any] | None = None
    rate_limit: dict[str, Any] | None = None
    enabled: bool | None = None


class ModelConfigRecord(BaseModel):
    id: int
    model_name: str
    model_type: str
    api_endpoint: str
    api_key_ref: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    rate_limit: dict[str, Any] = Field(default_factory=dict)
    enabled: bool
    created_at: datetime | str
    updated_at: datetime | str


class ChannelConfigCreateRequest(BaseModel):
    channel_id: str
    channel_name: str
    channel_type: str
    bot_id: str
    config: dict[str, Any] | None = None
    status: str = "active"


class ChannelConfigDeactivateRequest(BaseModel):
    graceful: bool = True
    timeout: int = 30


class ChannelConfigRecord(BaseModel):
    id: int
    channel_id: str
    channel_name: str
    channel_type: str
    bot_id: str
    config: dict[str, Any] = Field(default_factory=dict)
    status: str
    created_at: datetime | str
    updated_at: datetime | str
