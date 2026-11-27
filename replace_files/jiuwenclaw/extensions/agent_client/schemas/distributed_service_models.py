from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AgentServerConfigUpdateRequest(BaseModel):
    min_replicas: int | None = None
    max_replicas: int | None = None
    autoscale_enabled: bool | None = None
    autoscale_metrics: dict[str, Any] | None = None


class InstanceConfigRecord(BaseModel):
    id: int
    component: str = Field(default="agent_server")
    min_replicas: int
    max_replicas: int
    current_replicas: int
    autoscale_enabled: bool
    autoscale_metrics: dict[str, Any] = Field(default_factory=dict)
    max_concurrency: int
    current_concurrency: int
    queue_size: int
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | str
    updated_at: datetime | str


class ServiceStatusRecord(BaseModel):
    pod_name: str
    component: str
    status: str
    cpu_usage: float | None = None
    memory_usage: float | None = None
    restart_count: int = 0
    start_time: datetime | str | None = None
    ready: bool
    node_name: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)


class SessionMappingRecord(BaseModel):
    session_id: str
    user_id: str | None = None
    group_id: str | None = None
    bot_id: str | None = None
    agent_server_pod: str
    create_time: datetime | str
    last_active_time: datetime | str
    ttl: int


class SessionMappingListQueryRequest(BaseModel):
    user_id: str | None = None
    group_id: str | None = None
    bot_id: str | None = None
    session_id: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=200)


class TenantIsolationPolicyUpdateRequest(BaseModel):
    policy_name: str | None = None
    isolation_level: str | None = None
    selector: dict[str, Any] | None = None
    target_instances: list[str] | dict[str, Any] | None = None
    resource_quota: dict[str, Any] | None = None
    priority: int | None = None
    enabled: bool | None = None


class TenantIsolationPolicyRecord(BaseModel):
    id: int
    policy_name: str
    isolation_level: str
    selector: dict[str, Any]
    target_instances: list[str] | dict[str, Any]
    resource_quota: dict[str, Any] = Field(default_factory=dict)
    priority: int
    enabled: bool
    created_at: datetime | str
    updated_at: datetime | str


class SessionAffinityPolicyUpdateRequest(BaseModel):
    policy_name: str | None = None
    affinity_type: str | None = None
    session_ttl: int | None = None
    max_concurrent_per_session: int | None = None
    failover_enabled: bool | None = None


class SessionAffinityPolicyRecord(BaseModel):
    id: int
    policy_name: str
    affinity_type: str
    session_ttl: int
    max_concurrent_per_session: int | None = None
    failover_enabled: bool
    created_at: datetime | str
    updated_at: datetime | str
