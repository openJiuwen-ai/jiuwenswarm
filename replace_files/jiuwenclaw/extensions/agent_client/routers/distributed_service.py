from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from jiuwenclaw.extensions.agent_client.schemas import (
    AgentServerConfigUpdateRequest,
    ResponseModel,
    SessionAffinityPolicyRecord,
    SessionMappingListQueryRequest,
    SessionAffinityPolicyUpdateRequest,
    SessionMappingRecord,
    ServiceStatusRecord,
    TenantIsolationPolicyRecord,
    TenantIsolationPolicyUpdateRequest,
)
from jiuwenclaw.utils import get_user_workspace_dir

distributed_service_router = APIRouter()


def _instance_config_file() -> Path:
    root = get_user_workspace_dir() / "gateway"
    root.mkdir(parents=True, exist_ok=True)
    return root / "instance_config.json"


def _service_status_file() -> Path:
    root = get_user_workspace_dir() / "gateway"
    root.mkdir(parents=True, exist_ok=True)
    return root / "service_status.json"


def _session_mapping_file() -> Path:
    root = get_user_workspace_dir() / "gateway"
    root.mkdir(parents=True, exist_ok=True)
    return root / "session_mapping.json"


def _isolation_policy_file() -> Path:
    root = get_user_workspace_dir() / "gateway"
    root.mkdir(parents=True, exist_ok=True)
    return root / "tenant_isolation_policy.json"


def _session_affinity_policy_file() -> Path:
    root = get_user_workspace_dir() / "gateway"
    root.mkdir(parents=True, exist_ok=True)
    return root / "session_affinity_policy.json"


def _read_records() -> list[dict[str, Any]]:
    path = _instance_config_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _write_records(records: list[dict[str, Any]]) -> None:
    _instance_config_file().write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_service_status_records() -> list[dict[str, Any]]:
    path = _service_status_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _read_session_mapping_records() -> list[dict[str, Any]]:
    path = _session_mapping_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _read_isolation_policy_records() -> list[dict[str, Any]]:
    path = _isolation_policy_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _write_isolation_policy_records(records: list[dict[str, Any]]) -> None:
    _isolation_policy_file().write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_session_affinity_policy_records() -> list[dict[str, Any]]:
    path = _session_affinity_policy_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _write_session_affinity_policy_records(records: list[dict[str, Any]]) -> None:
    _session_affinity_policy_file().write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _validate_update(req: AgentServerConfigUpdateRequest, old: dict[str, Any] | None = None) -> None:
    min_r = req.min_replicas if req.min_replicas is not None else (old or {}).get("min_replicas", 1)
    max_r = req.max_replicas if req.max_replicas is not None else (old or {}).get("max_replicas", 1)

    if min_r < 1:
        raise ValueError("min_replicas must be >= 1")
    if max_r < min_r:
        raise ValueError("max_replicas must be >= min_replicas")

    metrics = req.autoscale_metrics if req.autoscale_metrics is not None else (old or {}).get("autoscale_metrics", {})
    if not isinstance(metrics, dict):
        raise ValueError("autoscale_metrics must be an object")
    max_concurrency = metrics.get("max_concurrency")
    if max_concurrency is not None and int(max_concurrency) < 1:
        raise ValueError("autoscale_metrics.max_concurrency must be >= 1")
    cpu_target = metrics.get("cpu_target")
    if cpu_target is not None and not (1 <= int(cpu_target) <= 100):
        raise ValueError("autoscale_metrics.cpu_target must be in [1, 100]")
    memory_target = metrics.get("memory_target")
    if memory_target is not None and not (1 <= int(memory_target) <= 100):
        raise ValueError("autoscale_metrics.memory_target must be in [1, 100]")


def _upsert_config(req: AgentServerConfigUpdateRequest) -> dict[str, Any]:
    records = _read_records()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    target = None
    for row in records:
        if row.get("component") == "agent_server":
            target = row
            break

    _validate_update(req, target)

    if target is None:
        metrics = req.autoscale_metrics if isinstance(req.autoscale_metrics, dict) else {"max_concurrency": 100}
        max_concurrency = int(metrics.get("max_concurrency", 100))
        min_replicas = req.min_replicas if req.min_replicas is not None else 1
        max_replicas = req.max_replicas if req.max_replicas is not None else 3
        target = {
            "id": max((int(r.get("id", 0)) for r in records), default=0) + 1,
            "component": "agent_server",
            "min_replicas": min_replicas,
            "max_replicas": max_replicas,
            "current_replicas": min_replicas,
            "autoscale_enabled": req.autoscale_enabled if req.autoscale_enabled is not None else True,
            "autoscale_metrics": metrics,
            "max_concurrency": max_concurrency,
            "current_concurrency": 0,
            "queue_size": 200,
            "data": {},
            "created_at": now,
            "updated_at": now,
        }
        records.append(target)
    else:
        for field in (
            "min_replicas",
            "max_replicas",
            "autoscale_enabled",
            "autoscale_metrics",
        ):
            value = getattr(req, field)
            if value is not None:
                target[field] = value
        # Keep derived runtime fields consistent when only autoscale_metrics is provided.
        metrics = target.get("autoscale_metrics") if isinstance(target.get("autoscale_metrics"), dict) else {}
        target["max_concurrency"] = int(metrics.get("max_concurrency", target.get("max_concurrency", 100)))
        min_r = int(target.get("min_replicas", 1))
        max_r = int(target.get("max_replicas", min_r))
        current_r = int(target.get("current_replicas", min_r))
        target["current_replicas"] = min(max(current_r, min_r), max_r)
        target["updated_at"] = now

    _write_records(records)
    return target


def _get_config() -> dict[str, Any]:
    for row in _read_records():
        if row.get("component") == "agent_server":
            return row
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "id": 0,
        "component": "agent_server",
        "min_replicas": 1,
        "max_replicas": 3,
        "current_replicas": 1,
        "autoscale_enabled": True,
        "autoscale_metrics": {"max_concurrency": 100},
        "max_concurrency": 100,
        "current_concurrency": 0,
        "queue_size": 200,
        "data": {},
        "created_at": now,
        "updated_at": now,
    }


def _default_service_statuses() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return [
        {
            "pod_name": "agent-server-0",
            "component": "agent_server",
            "status": "Running",
            "cpu_usage": 0.0,
            "memory_usage": 0.0,
            "restart_count": 0,
            "start_time": now,
            "ready": True,
            "node_name": "node-1",
            "labels": {"app": "agent-server"},
            "events": [
                {
                    "type": "Normal",
                    "reason": "Scheduled",
                    "message": "Successfully assigned to node-1",
                    "timestamp": now,
                }
            ],
        }
    ]


def _default_session_mappings() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return [
        {
            "session_id": "sess-123456",
            "user_id": "user-001",
            "group_id": "group-001",
            "bot_id": "bot-001",
            "agent_server_pod": "agent-server-0",
            "create_time": now,
            "last_active_time": now,
            "ttl": 3600,
        }
    ]


def _default_isolation_policies() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return [
        {
            "id": 1,
            "policy_name": "VIP用户隔离策略",
            "isolation_level": "user",
            "selector": {"user_tier": "vip"},
            "target_instances": ["agent-server-0"],
            "resource_quota": {},
            "priority": 100,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
        }
    ]


def _default_session_affinity_policies() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return [
        {
            "id": 1,
            "policy_name": "默认session亲和策略",
            "affinity_type": "session_id",
            "session_ttl": 3600,
            "max_concurrent_per_session": 10,
            "failover_enabled": True,
            "created_at": now,
            "updated_at": now,
        }
    ]


def _query_service_statuses(
    component: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    records = _read_service_status_records()
    scoped = records if records else _default_service_statuses()

    items: list[dict[str, Any]] = []
    for row in scoped:
        if component and row.get("component") != component:
            continue
        if status and row.get("status") != status:
            continue
        item = ServiceStatusRecord(**row).model_dump(mode="json")
        items.append(item)
    return items


def _get_service_detail(pod_name: str) -> dict[str, Any] | None:
    items = _query_service_statuses()
    for item in items:
        if item.get("pod_name") == pod_name:
            return item
    return None


def _query_session_mappings(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    group_id: str | None = None,
    bot_id: str | None = None,
) -> list[dict[str, Any]]:
    records = _read_session_mapping_records()
    scoped = records if records else _default_session_mappings()

    items: list[dict[str, Any]] = []
    for row in scoped:
        if session_id and row.get("session_id") != session_id:
            continue
        if user_id and row.get("user_id") != user_id:
            continue
        if group_id and row.get("group_id") != group_id:
            continue
        if bot_id and row.get("bot_id") != bot_id:
            continue
        items.append(SessionMappingRecord(**row).model_dump(mode="json"))
    return items


def _get_session_detail(session_id: str) -> dict[str, Any] | None:
    items = _query_session_mappings(session_id=session_id)
    if not items:
        return None
    return items[0]


def _query_isolation_policies(
    *,
    isolation_level: str | None = None,
    enabled: bool | None = None,
) -> list[dict[str, Any]]:
    records = _read_isolation_policy_records()
    scoped = records if records else _default_isolation_policies()

    items: list[dict[str, Any]] = []
    for row in scoped:
        if isolation_level and row.get("isolation_level") != isolation_level:
            continue
        if enabled is not None and bool(row.get("enabled")) != enabled:
            continue
        item = TenantIsolationPolicyRecord(**row).model_dump(mode="json")
        items.append(item)
    return items


def _update_isolation_policy(
    policy_id: int,
    request: TenantIsolationPolicyUpdateRequest,
) -> bool:
    records = _read_isolation_policy_records()
    if not records:
        records = _default_isolation_policies()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    updated = False
    for row in records:
        if int(row.get("id", -1)) != policy_id:
            continue
        for field in (
            "policy_name",
            "isolation_level",
            "selector",
            "target_instances",
            "resource_quota",
            "priority",
            "enabled",
        ):
            value = getattr(request, field)
            if value is not None:
                row[field] = value
        row["updated_at"] = now
        updated = True
        break

    if updated:
        _write_isolation_policy_records(records)
    return updated


def _validate_session_affinity_update(
    request: SessionAffinityPolicyUpdateRequest,
    old: dict[str, Any] | None = None,
) -> None:
    affinity_type = request.affinity_type or (old or {}).get("affinity_type", "session_id")
    if affinity_type not in {"user_id", "session_id", "ip"}:
        raise ValueError("affinity_type must be one of: user_id, session_id, ip")

    session_ttl = request.session_ttl if request.session_ttl is not None else (old or {}).get("session_ttl", 3600)
    if int(session_ttl) <= 0:
        raise ValueError("session_ttl must be > 0")

    max_concurrent = (
        request.max_concurrent_per_session
        if request.max_concurrent_per_session is not None
        else (old or {}).get("max_concurrent_per_session", 10)
    )
    if max_concurrent is not None and int(max_concurrent) <= 0:
        raise ValueError("max_concurrent_per_session must be > 0")


def _upsert_session_affinity_policy(
    request: SessionAffinityPolicyUpdateRequest,
) -> dict[str, Any]:
    records = _read_session_affinity_policy_records()
    if not records:
        records = _default_session_affinity_policies()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    target = records[0]

    _validate_session_affinity_update(request, target)

    for field in (
        "policy_name",
        "affinity_type",
        "session_ttl",
        "max_concurrent_per_session",
        "failover_enabled",
    ):
        value = getattr(request, field)
        if value is not None:
            target[field] = value
    target["updated_at"] = now

    _write_session_affinity_policy_records(records)
    return SessionAffinityPolicyRecord(**target).model_dump(mode="json")


def _get_session_affinity_policy() -> dict[str, Any]:
    records = _read_session_affinity_policy_records()
    scoped = records if records else _default_session_affinity_policies()
    row = scoped[0]
    return SessionAffinityPolicyRecord(**row).model_dump(mode="json")


@distributed_service_router.put("/agent-server/config")
async def update_instance_agent_server_config(
    request: AgentServerConfigUpdateRequest,
) -> ResponseModel[dict[str, Any]]:
    row = _upsert_config(request)
    return ResponseModel(
        code=200,
        message="success",
        data={
            "component": row.get("component", "agent_server"),
            "min_replicas": row.get("min_replicas"),
            "max_replicas": row.get("max_replicas"),
            "current_replicas": row.get("current_replicas"),
            "autoscale_enabled": row.get("autoscale_enabled"),
            "autoscale_metrics": row.get("autoscale_metrics", {}),
            "max_concurrency": row.get("max_concurrency"),
            "current_concurrency": row.get("current_concurrency"),
            "queue_size": row.get("queue_size"),
            "updated_at": row.get("updated_at"),
        },
    )


@distributed_service_router.get("/agent-server/config")
async def get_instance_agent_server_config() -> ResponseModel[dict[str, Any]]:
    row = _get_config()
    return ResponseModel(
        code=200,
        message="success",
        data={
            "component": row.get("component", "agent_server"),
            "min_replicas": row.get("min_replicas"),
            "max_replicas": row.get("max_replicas"),
            "current_replicas": row.get("current_replicas"),
            "autoscale_enabled": row.get("autoscale_enabled"),
            "autoscale_metrics": row.get("autoscale_metrics", {}),
            "max_concurrency": row.get("max_concurrency"),
            "current_concurrency": row.get("current_concurrency"),
            "queue_size": row.get("queue_size"),
            "updated_at": row.get("updated_at"),
        },
    )


@distributed_service_router.get("/services/status")
async def get_instance_service_status_list(
    component: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> ResponseModel[dict[str, Any]]:
    items = _query_service_statuses(
        component=component,
        status=status,
    )
    return ResponseModel(code=200, message="success", data={"items": items})


@distributed_service_router.get("/services/{pod_name}")
async def get_instance_service_detail(pod_name: str) -> ResponseModel[dict[str, Any]]:
    detail = _get_service_detail(pod_name=pod_name)
    if detail is None:
        raise HTTPException(status_code=404, detail="service pod not found")
    return ResponseModel(code=200, message="success", data=detail)


@distributed_service_router.get("/sessions")
async def get_instance_session_mapping_list(
    query: Annotated[SessionMappingListQueryRequest, Depends()],
) -> ResponseModel[dict[str, Any]]:
    items = _query_session_mappings(
        session_id=query.session_id,
        user_id=query.user_id,
        group_id=query.group_id,
        bot_id=query.bot_id,
    )
    total = len(items)
    start = (query.page - 1) * query.page_size
    end = start + query.page_size
    paged_items = items[start:end]
    return ResponseModel(
        code=200,
        message="success",
        data={
            "total": total,
            "page": query.page,
            "page_size": query.page_size,
            "items": paged_items,
        },
    )


@distributed_service_router.get("/sessions/{session_id}")
async def get_instance_session_mapping_detail(session_id: str) -> ResponseModel[dict[str, Any]]:
    detail = _get_session_detail(session_id=session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="session mapping not found")
    return ResponseModel(code=200, message="success", data=detail)


@distributed_service_router.get("/isolation-policies")
async def get_instance_isolation_policy_list(
    isolation_level: str | None = Query(default=None),
    enabled: bool | None = Query(default=None),
) -> ResponseModel[dict[str, Any]]:
    items = _query_isolation_policies(
        isolation_level=isolation_level,
        enabled=enabled,
    )
    brief_items = [
        {
            "policy_id": item.get("id"),
            "policy_name": item.get("policy_name"),
            "isolation_level": item.get("isolation_level"),
            "enabled": item.get("enabled"),
            "priority": item.get("priority"),
        }
        for item in items
    ]
    return ResponseModel(code=200, message="success", data={"items": brief_items})


@distributed_service_router.put("/isolation-policies/{policy_id}")
async def update_instance_isolation_policy(
    policy_id: int,
    request: TenantIsolationPolicyUpdateRequest,
) -> ResponseModel[None]:
    updated = _update_isolation_policy(
        policy_id=policy_id,
        request=request,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="isolation policy not found")
    return ResponseModel(code=200, message="success")


@distributed_service_router.put("/session-affinity")
async def update_instance_session_affinity_policy(
    request: SessionAffinityPolicyUpdateRequest,
) -> ResponseModel[dict[str, Any]]:
    row = _upsert_session_affinity_policy(request)
    return ResponseModel(
        code=200,
        message="success",
        data={
            "affinity_type": row.get("affinity_type"),
            "session_ttl": row.get("session_ttl"),
            "max_concurrent_per_session": row.get("max_concurrent_per_session"),
            "failover_enabled": row.get("failover_enabled"),
            "updated_at": row.get("updated_at"),
        },
    )


@distributed_service_router.get("/session-affinity")
async def get_instance_session_affinity_policy() -> ResponseModel[dict[str, Any]]:
    row = _get_session_affinity_policy()
    return ResponseModel(
        code=200,
        message="success",
        data={
            "affinity_type": row.get("affinity_type"),
            "session_ttl": row.get("session_ttl"),
            "max_concurrent_per_session": row.get("max_concurrent_per_session"),
            "failover_enabled": row.get("failover_enabled"),
        },
    )


# Backward compatibility alias
router = distributed_service_router
