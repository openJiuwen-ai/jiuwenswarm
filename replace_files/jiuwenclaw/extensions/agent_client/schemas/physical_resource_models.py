from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ResourceConfigUpdateRequest(BaseModel):
    component: str = Field(default="agent_server")
    cpu_request: str | None = None
    cpu_limit: str | None = None
    memory_request: str | None = None
    memory_limit: str | None = None
    storage_request: str | None = None


class ResourceConfigRecord(BaseModel):
    id: int
    component: str
    cpu_request: str
    cpu_limit: str
    memory_request: str
    memory_limit: str
    storage_request: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | str
    updated_at: datetime | str
