# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillDev meta-tool 共享上下文：会话 ID 解析与 Agent outputSchema 加载。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AVAILABLE_AGENTS_REL = Path("resources") / "agents" / "available_agents.json"
AVAILABLE_TOOLS_DIR_REL = Path("resources") / "available-tools"
SKILLDEV_TASK_PREFIX = "todo_"


def resolve_session_id(kwargs: dict[str, Any]) -> str | None:
    """从 tool.invoke 的 kwargs 或当前 asyncio 上下文解析会话 ID。"""
    session = kwargs.get("session")
    if session is None:
        try:
            from openjiuwen.core.session import get_current_session

            session = get_current_session()
        except ImportError:
            session = None
    if session is None:
        return None
    if isinstance(session, str):
        sid = session.strip()
        return sid or None
    getter = getattr(session, "get_session_id", None)
    if callable(getter):
        sid = getter()
        if isinstance(sid, str) and sid.strip():
            return sid.strip()
    return None


def task_id_from_session_id(session_id: str) -> str:
    """SkillDev 框架 session 常为 ``todo_{task_id}``，磁盘目录使用 task_id。"""
    sid = session_id.strip()
    if sid.startswith(SKILLDEV_TASK_PREFIX):
        return sid[len(SKILLDEV_TASK_PREFIX) :]
    return sid


def resolve_available_agents_path(session_id: str) -> Path | None:
    """解析 ``.../skilldev/<task_id>/resources/agents/available_agents.json``。"""
    task_id = task_id_from_session_id(session_id)
    candidates: list[Path] = []

    from jiuwenclaw.agentserver.tools.subagent_executor.context_vars import (
        get_effective_request_workspace_dir,
    )

    effective_workspace = get_effective_request_workspace_dir()
    if effective_workspace:
        candidates.append(Path(effective_workspace) / AVAILABLE_AGENTS_REL)

    try:
        from jiuwenclaw.utils import get_workspace_dir

        base_workspace = get_workspace_dir()
    except ImportError:
        base_workspace = None

    if base_workspace is not None:
        candidates.extend(
            [
                base_workspace / "skilldev" / task_id / AVAILABLE_AGENTS_REL,
                base_workspace / "skilldev" / session_id.strip() / AVAILABLE_AGENTS_REL,
            ]
        )

    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return path
    return None


def schema_from_agent_entry(entry: dict[str, Any]) -> Any | None:
    return entry


def extract_output_schema(data: Any, agent_id: str) -> Any | None:
    """从 available_agents.json 反序列化结果中解析与 *agent_id* 匹配的 outputSchema。"""
    target_agent_id = agent_id.strip()

    if isinstance(data, dict):
        if not (data.get("agentId") or data.get("agent_id")):
            schema = schema_from_agent_entry(data)
            if schema is not None:
                return schema
        entry_agent_id = str(data.get("agentId") or data.get("agent_id") or "").strip()
        if entry_agent_id:
            if not target_agent_id or entry_agent_id == target_agent_id:
                return schema_from_agent_entry(data)
            return None
        nested = data.get("agents") or data.get("agentDefinitions") or data.get("agent_definitions")
        if nested is not None:
            return extract_output_schema(nested, agent_id)

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            item_agent_id = str(item.get("agentId") or item.get("agent_id") or "").strip()
            schema = schema_from_agent_entry(item)
            if schema is None:
                continue
            if not target_agent_id or item_agent_id == target_agent_id:
                return schema

    return None


def load_agent_output_schema(
    session_id: str,
    agent_id: str,
    *,
    log_prefix: str = "skilldev_tool_context",
) -> Any | None:
    """读取任务工作区下的 available_agents.json 并返回匹配的 outputSchema。"""
    path = resolve_available_agents_path(session_id)
    if path is None:
        logger.warning(
            "[%s] available_agents.json not found for session_id=%s",
            log_prefix,
            session_id,
        )
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(
            "[%s] failed to read %s: %s",
            log_prefix,
            path,
            exc,
        )
        return None
    schema = extract_output_schema(data, agent_id)
    if schema is None:
        logger.warning(
            "[%s] outputSchema not found in %s for agentId=%s",
            log_prefix,
            path,
            agent_id,
        )
    return schema


def resolve_available_tool_spec_path(
    session_id: str,
    plugin_id: str,
    tool_name: str,
) -> Path | None:
    """解析 ``.../skilldev/<task_id>/resources/available-tools/<pluginId>__<toolName>.json``。"""
    from jiuwenclaw.agentserver.skilldev_agent.meta_tools.external_tool_registry import (
        tool_spec_filename,
    )

    try:
        filename = tool_spec_filename(plugin_id, tool_name)
    except ValueError:
        return None

    task_id = task_id_from_session_id(session_id)
    candidates: list[Path] = []

    from jiuwenclaw.agentserver.tools.subagent_executor.context_vars import (
        get_effective_request_workspace_dir,
    )

    effective_workspace = get_effective_request_workspace_dir()
    if effective_workspace:
        candidates.append(Path(effective_workspace) / AVAILABLE_TOOLS_DIR_REL / filename)

    try:
        from jiuwenclaw.utils import get_workspace_dir

        base_workspace = get_workspace_dir()
    except ImportError:
        base_workspace = None

    if base_workspace is not None:
        candidates.extend(
            [
                base_workspace / "skilldev" / task_id / AVAILABLE_TOOLS_DIR_REL / filename,
                base_workspace / "skilldev" / session_id.strip() / AVAILABLE_TOOLS_DIR_REL / filename,
            ]
        )

    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return path
    return None


def load_tool_output_schema(
    session_id: str,
    plugin_id: str,
    tool_name: str,
    *,
    log_prefix: str = "skilldev_tool_context",
) -> Any | None:
    """读取任务工作区下的 ``<pluginId>__<toolName>.json`` 并返回其 outputSchema 字段。"""
    path = resolve_available_tool_spec_path(session_id, plugin_id, tool_name)
    if path is None:
        logger.warning(
            "[%s] tool spec not found for session_id=%s pluginId=%s toolName=%s",
            log_prefix,
            session_id,
            plugin_id,
            tool_name,
        )
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(
            "[%s] failed to read %s: %s",
            log_prefix,
            path,
            exc,
        )
        return None
    if not isinstance(data, dict):
        logger.warning(
            "[%s] tool spec %s is not a JSON object",
            log_prefix,
            path,
        )
        return None

    return data


__all__ = [
    "AVAILABLE_AGENTS_REL",
    "AVAILABLE_TOOLS_DIR_REL",
    "SKILLDEV_TASK_PREFIX",
    "resolve_session_id",
    "task_id_from_session_id",
    "resolve_available_agents_path",
    "schema_from_agent_entry",
    "extract_output_schema",
    "load_agent_output_schema",
    "resolve_available_tool_spec_path",
    "load_tool_output_schema",
]
