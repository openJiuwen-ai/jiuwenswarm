from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

from jiuwenclaw.extensions.agent_client.schemas import ResponseModel, ResourceConfigUpdateRequest
from jiuwenclaw.utils import get_user_workspace_dir

physical_resource_router = APIRouter()


def _resource_config_file() -> Path:
    root = get_user_workspace_dir() / "gateway"
    root.mkdir(parents=True, exist_ok=True)
    return root / "resource_config.json"


def _read_records() -> list[dict[str, Any]]:
    file_path = _resource_config_file()
    if not file_path.exists():
        return []
    try:
        content = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(content, list):
        return content
    return []


def _write_records(records: list[dict[str, Any]]) -> None:
    _resource_config_file().write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _parse_cpu_m(value: str) -> int:
    val = value.strip().lower()
    if val.endswith("m"):
        return int(val[:-1])
    return int(float(val) * 1000)


def _parse_memory_mi(value: str) -> int:
    val = value.strip().lower()
    if val.endswith("gi"):
        return int(float(val[:-2]) * 1024)
    if val.endswith("mi"):
        return int(float(val[:-2]))
    raise ValueError("memory/storage only supports Mi/Gi")


def _validate_resource_config(payload: dict[str, Any]) -> None:
    cpu_request = payload.get("cpu_request")
    cpu_limit = payload.get("cpu_limit")
    if cpu_request and cpu_limit and _parse_cpu_m(cpu_request) > _parse_cpu_m(cpu_limit):
        raise ValueError("cpu_request cannot be greater than cpu_limit")

    memory_request = payload.get("memory_request")
    memory_limit = payload.get("memory_limit")
    if memory_request and memory_limit and _parse_memory_mi(memory_request) > _parse_memory_mi(memory_limit):
        raise ValueError("memory_request cannot be greater than memory_limit")


def _upsert_resource_config(req: ResourceConfigUpdateRequest) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    records = _read_records()
    normalized_component = (req.component or "agent_server").strip() or "agent_server"

    _validate_resource_config(req.model_dump())
    for row in records:
        if row.get("component") == normalized_component:
            for field in (
                "cpu_request",
                "cpu_limit",
                "memory_request",
                "memory_limit",
                "storage_request",
            ):
                value = getattr(req, field)
                if value is not None:
                    row[field] = value
            row["updated_at"] = now
            _write_records(records)
            return row

    new_row = {
        "id": (max((int(x.get("id", 0)) for x in records), default=0) + 1),
        "component": normalized_component,
        "cpu_request": req.cpu_request or "500m",
        "cpu_limit": req.cpu_limit or "2000m",
        "memory_request": req.memory_request or "1Gi",
        "memory_limit": req.memory_limit or "4Gi",
        "storage_request": req.storage_request,
        "data": {},
        "created_at": now,
        "updated_at": now,
    }
    records.append(new_row)
    _write_records(records)
    return new_row


def _query_resource_configs(component: str | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in _read_records():
        if component and row.get("component") != component:
            continue
        item = dict(row)
        ext_data = item.get("data") if isinstance(item.get("data"), dict) else {}
        item["actual_usage"] = ext_data.get(
            "actual_usage",
            {"cpu": "0m", "memory": "0Mi"},
        )
        items.append(item)
    return items


@physical_resource_router.put("/resources")
async def update_instance_resources(
    request: ResourceConfigUpdateRequest,
) -> ResponseModel[dict[str, Any]]:
    row = _upsert_resource_config(request)
    return ResponseModel(
        code=200,
        message="success",
        data={
            "component": row["component"],
            "cpu_request": row.get("cpu_request"),
            "cpu_limit": row.get("cpu_limit"),
            "memory_request": row.get("memory_request"),
            "memory_limit": row.get("memory_limit"),
            "storage_request": row.get("storage_request"),
            "updated_at": row.get("updated_at"),
        },
    )


@physical_resource_router.get("/resources")
async def get_instance_resources(
    component: str | None = Query(default=None),
) -> ResponseModel[dict[str, Any]]:
    items = _query_resource_configs(component)
    return ResponseModel(code=200, message="success", data={"items": items})


# Backward compatibility alias
router = physical_resource_router
