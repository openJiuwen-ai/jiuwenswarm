from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from jiuwenclaw.extensions.agent_client.schemas import (
    ChannelConfigCreateRequest,
    ChannelConfigDeactivateRequest,
    ChannelConfigRecord,
    ModelConfigCreateRequest,
    ModelConfigRecord,
    ModelConfigUpdateRequest,
    ResponseModel,
)
from jiuwenclaw.utils import get_user_workspace_dir

application_config_router = APIRouter()


def _model_config_file() -> Path:
    root = get_user_workspace_dir() / "gateway"
    root.mkdir(parents=True, exist_ok=True)
    return root / "model_config.json"


def _channel_config_file() -> Path:
    root = get_user_workspace_dir() / "gateway"
    root.mkdir(parents=True, exist_ok=True)
    return root / "channel_config.json"


def _read_model_config_records() -> list[dict[str, Any]]:
    path = _model_config_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _write_model_config_records(records: list[dict[str, Any]]) -> None:
    _model_config_file().write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_channel_config_records() -> list[dict[str, Any]]:
    path = _channel_config_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _write_channel_config_records(records: list[dict[str, Any]]) -> None:
    _channel_config_file().write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _validate_model_payload(payload: dict[str, Any]) -> None:
    required = ("model_name", "model_type", "api_endpoint", "api_key_ref")
    for key in required:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} is required")


def _validate_channel_payload(payload: dict[str, Any]) -> None:
    required = ("channel_id", "channel_name", "channel_type", "bot_id")
    for key in required:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} is required")
    if payload.get("status") not in {"active", "inactive"}:
        raise ValueError("status must be active or inactive")


def _create_model_config(request: ModelConfigCreateRequest) -> dict[str, Any]:
    records = _read_model_config_records()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = request.model_dump()
    _validate_model_payload(payload)
    new_id = max((int(row.get("id", 0)) for row in records), default=0) + 1
    row = {
        "id": new_id,
        "model_name": payload["model_name"].strip(),
        "model_type": payload["model_type"].strip(),
        "api_endpoint": payload["api_endpoint"].strip(),
        "api_key_ref": payload["api_key_ref"].strip(),
        "parameters": payload.get("parameters") or {},
        "rate_limit": payload.get("rate_limit") or {},
        "enabled": bool(payload.get("enabled", True)),
        "created_at": now,
        "updated_at": now,
    }
    records.append(row)
    _write_model_config_records(records)
    return row


def _list_model_configs(
    model_type: str | None = None,
    enabled: bool | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in _read_model_config_records():
        if model_type and row.get("model_type") != model_type:
            continue
        if enabled is not None and bool(row.get("enabled")) != enabled:
            continue
        items.append(ModelConfigRecord(**row).model_dump(mode="json"))
    return items


def _update_model_config(model_id: int, request: ModelConfigUpdateRequest) -> bool:
    records = _read_model_config_records()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    updated = False
    for row in records:
        if int(row.get("id", -1)) != model_id:
            continue
        for field in (
            "model_name",
            "model_type",
            "api_endpoint",
            "api_key_ref",
            "parameters",
            "rate_limit",
            "enabled",
        ):
            value = getattr(request, field)
            if value is not None:
                row[field] = value
        _validate_model_payload(row)
        row["updated_at"] = now
        updated = True
        break
    if updated:
        _write_model_config_records(records)
    return updated


def _delete_model_config(model_id: int) -> bool:
    records = _read_model_config_records()
    kept: list[dict[str, Any]] = []
    deleted = False
    for row in records:
        if int(row.get("id", -1)) == model_id:
            deleted = True
            continue
        kept.append(row)
    if deleted:
        _write_model_config_records(kept)
    return deleted


def _parse_import_file(file_name: str, text: str) -> list[dict[str, Any]]:
    lower_name = file_name.lower()
    if lower_name.endswith(".json"):
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("JSON file must be an array")
        return [item for item in data if isinstance(item, dict)]
    if lower_name.endswith(".csv"):
        reader = csv.DictReader(StringIO(text))
        rows: list[dict[str, Any]] = []
        for row in reader:
            mapped = dict(row)
            if "enabled" in mapped:
                mapped["enabled"] = str(mapped["enabled"]).strip().lower() in {"1", "true", "yes", "y"}
            rows.append(mapped)
        return rows
    raise ValueError("file must be .csv or .json")


def _register_channel(request: ChannelConfigCreateRequest) -> dict[str, Any]:
    records = _read_channel_config_records()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = request.model_dump()
    _validate_channel_payload(payload)

    for row in records:
        if row.get("channel_id") == payload["channel_id"]:
            raise ValueError("channel_id already exists")

    new_id = max((int(row.get("id", 0)) for row in records), default=0) + 1
    row = {
        "id": new_id,
        "channel_id": payload["channel_id"].strip(),
        "channel_name": payload["channel_name"].strip(),
        "channel_type": payload["channel_type"].strip(),
        "bot_id": payload["bot_id"].strip(),
        "config": payload.get("config") or {},
        "status": payload.get("status", "active"),
        "created_at": now,
        "updated_at": now,
    }
    records.append(row)
    _write_channel_config_records(records)
    return row


def _list_channels(
    channel_type: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in _read_channel_config_records():
        if channel_type and row.get("channel_type") != channel_type:
            continue
        if status and row.get("status") != status:
            continue
        items.append(ChannelConfigRecord(**row).model_dump(mode="json"))
    return items


def _set_channel_status(
    channel_id: str,
    target_status: str,
) -> dict[str, Any] | None:
    records = _read_channel_config_records()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for row in records:
        if row.get("channel_id") == channel_id:
            row["status"] = target_status
            row["updated_at"] = now
            _write_channel_config_records(records)
            return row
    return None


def _delete_channel(channel_id: str) -> bool:
    records = _read_channel_config_records()
    kept: list[dict[str, Any]] = []
    deleted = False
    for row in records:
        if row.get("channel_id") == channel_id:
            deleted = True
            continue
        kept.append(row)
    if deleted:
        _write_channel_config_records(kept)
    return deleted


@application_config_router.post("/models")
async def create_model_config(
    request: ModelConfigCreateRequest,
) -> ResponseModel[dict[str, Any]]:
    try:
        row = _create_model_config(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResponseModel(
        code=200,
        message="success",
        data={
            "model_id": row["id"],
            "model_name": row["model_name"],
            "created_at": row["created_at"],
        },
    )


@application_config_router.get("/models")
async def list_model_configs(
    model_type: str | None = Query(default=None),
    enabled: bool | None = Query(default=None),
) -> ResponseModel[dict[str, Any]]:
    items = _list_model_configs(model_type=model_type, enabled=enabled)
    brief_items = [
        {
            "model_id": item["id"],
            "model_name": item["model_name"],
            "model_type": item["model_type"],
            "enabled": item["enabled"],
        }
        for item in items
    ]
    return ResponseModel(code=200, message="success", data={"items": brief_items})


@application_config_router.post("/models/import")
async def import_model_configs(
    file: UploadFile = File(...),
) -> ResponseModel[dict[str, Any]]:
    raw = await file.read()
    text = raw.decode("utf-8", errors="replace")
    try:
        rows = _parse_import_file(file.filename or "", text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid import file: {exc}") from exc

    success = 0
    errors: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        try:
            req = ModelConfigCreateRequest(
                model_name=str(row.get("model_name") or "").strip(),
                model_type=str(row.get("model_type") or "").strip(),
                api_endpoint=str(row.get("api_endpoint") or "").strip(),
                api_key_ref=str(row.get("api_key_ref") or "").strip(),
                parameters=row.get("parameters") if isinstance(row.get("parameters"), dict) else {},
                rate_limit=row.get("rate_limit") if isinstance(row.get("rate_limit"), dict) else {},
                enabled=bool(row.get("enabled", True)),
            )
            _create_model_config(req)
            success += 1
        except Exception as exc:
            errors.append({"row": idx, "error": str(exc)})

    return ResponseModel(
        code=200,
        message="success",
        data={
            "total": len(rows),
            "success": success,
            "failed": len(rows) - success,
            "errors": errors,
        },
    )


@application_config_router.put("/models/{model_id}")
async def update_model_config(
    model_id: int,
    request: ModelConfigUpdateRequest,
) -> ResponseModel[None]:
    try:
        updated = _update_model_config(model_id, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="model config not found")
    return ResponseModel(code=200, message="success")


@application_config_router.delete("/models/{model_id}")
async def delete_model_config(model_id: int) -> ResponseModel[None]:
    deleted = _delete_model_config(model_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="model config not found")
    return ResponseModel(code=200, message="success")


@application_config_router.post("/channels")
async def register_channel(
    request: ChannelConfigCreateRequest,
) -> ResponseModel[dict[str, Any]]:
    try:
        row = _register_channel(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResponseModel(
        code=200,
        message="success",
        data={
            "id": row["id"],
            "channel_id": row["channel_id"],
            "created_at": row["created_at"],
        },
    )


@application_config_router.get("/channels")
async def list_channels(
    channel_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> ResponseModel[dict[str, Any]]:
    items = _list_channels(channel_type=channel_type, status=status)
    brief_items = [
        {
            "id": item["id"],
            "channel_id": item["channel_id"],
            "channel_name": item["channel_name"],
            "channel_type": item["channel_type"],
            "status": item["status"],
        }
        for item in items
    ]
    return ResponseModel(code=200, message="success", data={"items": brief_items})


@application_config_router.post("/channels/{channel_id}/activate")
async def activate_channel(channel_id: str) -> ResponseModel[dict[str, Any]]:
    row = _set_channel_status(channel_id, "active")
    if row is None:
        raise HTTPException(status_code=404, detail="channel not found")
    return ResponseModel(
        code=200,
        message="success",
        data={"channel_id": row["channel_id"], "status": row["status"]},
    )


@application_config_router.post("/channels/{channel_id}/deactivate")
async def deactivate_channel(
    channel_id: str,
    request: ChannelConfigDeactivateRequest,
) -> ResponseModel[dict[str, Any]]:
    # graceful/timeout are accepted for workflow compatibility; persisted in config metadata.
    row = _set_channel_status(channel_id, "inactive")
    if row is None:
        raise HTTPException(status_code=404, detail="channel not found")
    meta = row.get("config") if isinstance(row.get("config"), dict) else {}
    meta["deactivate"] = {"graceful": bool(request.graceful), "timeout": int(request.timeout)}
    row["config"] = meta
    row["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    records = _read_channel_config_records()
    for item in records:
        if item.get("channel_id") == channel_id:
            item.update(row)
            break
    _write_channel_config_records(records)
    return ResponseModel(
        code=200,
        message="success",
        data={"channel_id": row["channel_id"], "status": row["status"]},
    )


@application_config_router.delete("/channels/{channel_id}")
async def unregister_channel(channel_id: str) -> ResponseModel[None]:
    deleted = _delete_channel(channel_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="channel not found")
    return ResponseModel(code=200, message="success")


# Backward compatibility alias
router = application_config_router
