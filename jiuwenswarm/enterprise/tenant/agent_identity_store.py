# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""工作区级 Agent 展示身份落盘.

文件位置：``<tenant_root>/agent/workspace/.agent_identity.json``。

文件是**工作区本地记录**，不是前端下拉的数据源；下拉始终以 Manager
``GET /v1/user-console/agent-contexts`` 为准。同一 ``tenant_key`` 只对应一个
``bot_id``，因此 ``agents`` 列表通常只有一条；列表用于身份变更留痕与后续扩展。

写入是懒写入：首个带 ``metadata.agent_identity`` 的请求进入租户作用域时触发，
按 ``agent_id`` upsert，并以 ``current_agent_id`` 指向本次身份。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.enterprise.tenant.tenant_paths import (
    get_agent_workspace_relative_dir,
    get_multi_tenant_user_workspace_dir,
)

logger = logging.getLogger(__name__)

AGENT_IDENTITY_FILE_NAME = ".agent_identity.json"
AGENT_IDENTITY_SCHEMA_VERSION = 1
# 落盘至少要能标识一条身份；展示名由 Manager 实时下发，不落盘。
_REQUIRED_FIELDS = ("agent_id",)
_ENTRY_FIELDS = (
    "agent_id",
    "runtime_template_id",
    "agent_template_id",
)
# 内容没变化就不重写文件（比较时排除 updated_at）。
_COMPARED_FIELDS = _ENTRY_FIELDS


def agent_identity_path(tenant_key: str) -> Path:
    """Return the identity file path for one tenant workspace."""
    root = get_multi_tenant_user_workspace_dir(tenant_key)
    return root / get_agent_workspace_relative_dir() / AGENT_IDENTITY_FILE_NAME


def persist_agent_identity(
    tenant_key: str,
    identity: Mapping[str, Any] | None,
) -> None:
    """Upsert one agent identity into the workspace file.

    缺 ``agent_id`` 时直接跳过，不产生半截身份；``agent_name`` 仅随请求透传，
    不写入文件（展示名始终以 Manager 下拉接口为准）。任何 I/O 或解析异常都
    降级为日志，不影响请求主流程。
    """
    if not isinstance(identity, Mapping):
        return

    cleaned = {
        field: _clean_text(identity.get(field)) for field in _ENTRY_FIELDS
    }
    if any(not cleaned[field] for field in _REQUIRED_FIELDS):
        return

    path = agent_identity_path(tenant_key)
    current_agent_id, entries = _load_entries(path)
    entry = {
        **cleaned,
        "updated_at": _utc_now(),
    }

    index = next(
        (
            i
            for i, item in enumerate(entries)
            if item.get("agent_id") == entry.get("agent_id")
        ),
        None,
    )
    if index is None:
        entries.append(entry)
    else:
        unchanged = current_agent_id == entry.get("agent_id") and all(
            entries[index].get(field) == entry.get(field) for field in _COMPARED_FIELDS
        )
        if unchanged:
            return
        entries[index] = entry

    payload = {
        "schema_version": AGENT_IDENTITY_SCHEMA_VERSION,
        "current_agent_id": entry.get("agent_id"),
        "agents": entries,
    }
    try:
        _atomic_write(path, payload)
    except OSError as exc:
        logger.warning(
            "persist_agent_identity failed: tenant_key=%s agent_id=%s error=%s",
            tenant_key,
            entry.get("agent_id"),
            exc,
        )


def read_agent_identity_file(tenant_key: str) -> dict[str, Any]:
    """Read the current identity (best effort); tests and diagnostics only."""
    current_agent_id, entries = _load_entries(agent_identity_path(tenant_key))
    current = next(
        (item for item in entries if item.get("agent_id") == current_agent_id),
        None,
    )
    return {
        "current_agent_id": current_agent_id,
        "agents": entries,
        "current": current,
    }


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_entries(path: Path) -> tuple[str, list[dict[str, str]]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", []
    if not isinstance(raw, dict):
        return "", []

    current_agent_id = _clean_text(raw.get("current_agent_id"))
    raw_agents = raw.get("agents")
    if not isinstance(raw_agents, list):
        return current_agent_id, []

    entries: list[dict[str, str]] = []
    for item in raw_agents:
        if not isinstance(item, dict):
            continue
        entry = {field: _clean_text(item.get(field)) for field in _ENTRY_FIELDS}
        entry["updated_at"] = _clean_text(item.get("updated_at"))
        if any(not entry.get(field) for field in _REQUIRED_FIELDS):
            continue
        entries.append(entry)
    return current_agent_id, entries


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
