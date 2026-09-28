# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only Runtime queries and presentation for interactive CLI controls."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from jiuwenswarm.channels.process_cli.protocol.version import CURRENT_SCHEMA_VERSION
from jiuwenswarm.channels.process_cli.ui import ProcessCliUI


class ControlQueryError(RuntimeError):
    """A query worker failed without exposing its diagnostic stream."""


async def query_runtime(
    operation: str,
    *,
    cwd: str,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Run a read-only public Runtime query in its own process."""

    request = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "type": "query",
        "operation": operation,
        "params": params or {},
        "workspace": {"cwd": cwd},
        "timeout_seconds": timeout,
    }
    environment = os.environ.copy()
    environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "jiuwenswarm.channels.process_cli.main",
        "--query-json",
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(
                json.dumps(request, ensure_ascii=False).encode("utf-8")
            ),
            timeout=timeout + 5,
        )
    except (asyncio.CancelledError, TimeoutError):
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlQueryError("Runtime 查询未返回有效结果") from exc
    if not isinstance(result, dict) or result.get("operation") != operation:
        raise ControlQueryError("Runtime 查询结果与请求不匹配")
    if process.returncode != 0 or result.get("status") != "completed":
        error = result.get("error")
        code = error.get("code") if isinstance(error, dict) else "QUERY_FAILED"
        raise ControlQueryError(f"Runtime 查询失败：{code}")
    data = result.get("data")
    if not isinstance(data, dict):
        raise ControlQueryError("Runtime 查询结果缺少数据")
    return data


def show_sessions(ui: ProcessCliUI, data: dict[str, Any], current: str | None) -> None:
    """Render only Process CLI sessions returned by the Runtime boundary."""

    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        raise ControlQueryError("会话列表格式无效")
    lines = [f"共 {data.get('total', 0)} 个会话；当前页偏移 {data.get('offset', 0)}"]
    for item in sessions:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("session_id") or "")
        marker = "*" if session_id == current else " "
        title = str(item.get("title") or "未命名")
        mode = str(item.get("mode") or "未知模式")
        lines.append(f"{marker} {session_id} · {title} · {mode}")
    if not sessions:
        lines.append("暂无进程式 CLI 会话")
    ui.details("会话", lines)


def show_models(ui: ProcessCliUI, data: dict[str, Any], selected: str = "") -> None:
    models = data.get("models")
    if not isinstance(models, list):
        raise ControlQueryError("模型列表格式无效")
    lines: list[str] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        key = str(item.get("selection_key") or "")
        marker = (
            "*" if key == selected or (not selected and item.get("is_current")) else " "
        )
        name = str(item.get("display_name") or item.get("model_name") or key)
        provider = str(item.get("provider") or "")
        lines.append(f"{marker} {name} [{key}] {provider}".rstrip())
    if not lines:
        lines.append("暂无可用模型")
    lines.append("使用 /model <名称或选择键> 切换下一轮模型")
    ui.details("模型", lines)


def show_status(
    ui: ProcessCliUI,
    *,
    session_id: str | None,
    session: dict[str, Any] | None,
    next_mode: str,
    next_model: str,
    cwd: str,
) -> None:
    lines = [f"当前会话：{session_id or '尚未创建'}"]
    if session is not None:
        lines.extend(
            (
                f"会话模式：{session.get('mode') or '未记录'}",
                f"会话模型：{session.get('model') or '未记录'}",
                f"消息数：{session.get('message_count', 0)}",
                f"项目目录：{session.get('project_dir') or cwd}",
            )
        )
    lines.extend(
        (f"下一轮模式：{next_mode}", f"下一轮模型：{next_model}", f"工作目录：{cwd}")
    )
    ui.details("状态", lines)


def show_permissions(ui: ProcessCliUI, data: dict[str, Any]) -> None:
    effective = data.get("effective")
    if not isinstance(effective, dict):
        raise ControlQueryError("权限快照格式无效")
    scope = "当前会话" if data.get("scope") == "session" else "全局"
    enabled = effective.get("enabled")
    state = "启用" if enabled is True else "关闭" if enabled is False else "未指定"
    lines = [f"范围：{scope}", f"权限开关：{state}"]
    tools = effective.get("tools")
    if isinstance(tools, list):
        counts = {level: 0 for level in ("allow", "ask", "deny")}
        for item in tools:
            if isinstance(item, dict) and item.get("level") in counts:
                counts[item["level"]] += 1
        lines.append(
            "工具配置："
            + "、".join(f"{level} {count}" for level, count in counts.items())
        )
        lines.extend(
            f"{item.get('name')}: {item.get('level')}"
            for item in tools
            if isinstance(item, dict) and item.get("level") in {"ask", "deny"}
        )
    rules = effective.get("rules")
    if isinstance(rules, list):
        lines.append(f"规则配置：{len(rules)} 条")
        for rule in rules:
            if isinstance(rule, dict):
                names = ", ".join(str(name) for name in rule.get("tools") or [])
                lines.append(
                    f"{rule.get('id') or '规则'} · {rule.get('action') or '未指定'}"
                    f" · {names}"
                )
    if enabled is False:
        lines.append("当前权限检查已关闭；以上为配置快照。")
    ui.details("权限（只读）", lines)


__all__ = [
    "ControlQueryError",
    "query_runtime",
    "show_models",
    "show_permissions",
    "show_sessions",
    "show_status",
]
