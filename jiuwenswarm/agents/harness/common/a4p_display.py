# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Localized A4P intent display-text fallback."""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from jiuwenswarm.common.config import get_config


@dataclass(frozen=True)
class IntentDisplayContext:
    language: str
    cron_target: dict[str, Any] | None = None
    reason: str = ""


INTENT_DISPLAY_CONTEXT: ContextVar[IntentDisplayContext | None] = ContextVar(
    "a4p_intent_display_context",
    default=None,
)
_BEIJING_TIMEZONE = timezone(timedelta(hours=8))

_ACTION_DISPLAY_FIELDS = {
    "bash": "command",
    "mcp_exec_command": "command",
    "create_terminal": "cmd",
    "write_file": "file_path",
    "edit_file": "file_path",
    "acp_chat": "agent",
}
_ACTION_DISPLAY_NAMES = {
    "zh": {
        "bash": "执行 Shell 命令",
        "mcp_exec_command": "执行命令",
        "create_terminal": "创建终端",
        "write_file": "写入文件",
        "edit_file": "编辑文件",
        "acp_chat": "与 Agent 对话",
    },
    "en": {
        "bash": "Run shell command",
        "mcp_exec_command": "Run command",
        "create_terminal": "Create terminal",
        "write_file": "Write file",
        "edit_file": "Edit file",
        "acp_chat": "Chat with agent",
    },
}


def preferred_display_language(config: dict[str, Any] | None = None) -> str:
    cfg = config if isinstance(config, dict) else get_config()
    language = str(cfg.get("preferred_language") or "zh").strip().lower()
    return "en" if language.startswith("en") else "zh"


def _display_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _format_display_action(action: dict[str, Any], language: str) -> str:
    name = str(action.get("name") or "").strip() or "unknown"
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    display_name = _ACTION_DISPLAY_NAMES[language].get(name)
    primary_field = _ACTION_DISPLAY_FIELDS.get(name)
    separator = "：" if language == "zh" else ": "
    if display_name and primary_field and primary_field in params:
        return f"{display_name}{separator}{_display_value(params[primary_field])}"
    params_text = json.dumps(params, ensure_ascii=False, sort_keys=True)
    return f"{display_name or name}{separator}{params_text}"


def format_beijing_time(value: Any) -> str:
    raw = str(value or "").strip()
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(_BEIJING_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def format_intent_display_text(mandate: dict[str, Any]) -> str:
    context = INTENT_DISPLAY_CONTEXT.get()
    language = context.language if context is not None else "zh"
    cron_target = context.cron_target if context is not None else None
    intent = mandate.get("intent") if isinstance(mandate.get("intent"), dict) else {}
    actions = intent.get("actions") if isinstance(intent.get("actions"), list) else []
    valid_time = mandate.get("validTime") if isinstance(mandate.get("validTime"), dict) else {}
    action_lines = [
        f"- {_format_display_action(action, language)}"
        for action in actions
        if isinstance(action, dict)
    ]
    start = format_beijing_time(valid_time.get("start"))
    end = format_beijing_time(valid_time.get("end"))
    reason = context.reason if context is not None else ""

    if language == "en":
        blocks: list[str] = [
            f"Authorization reason\n{reason or 'Pre-authorize the following tool operations.'}"
        ]
        if cron_target:
            schedule = cron_target.get("schedule") if isinstance(cron_target.get("schedule"), dict) else {}
            blocks.append(
                "\n".join(
                    [
                        "Scheduled task",
                        f"Task ID: {cron_target.get('cronJobId') or '-'}",
                        f"Task instructions: {cron_target.get('description') or '-'}",
                        f"Schedule: {schedule.get('cronExpression') or '-'}"
                        f" ({schedule.get('timezone') or '-'})",
                    ]
                )
            )
        blocks.extend(
            [
                "\n".join(["Authorized actions", *action_lines]),
                f"Valid time\nBeijing Time: {start} to {end}",
            ]
        )
        return "\n\n".join(blocks)

    blocks = [f"授权原因\n{reason or '为以下工具操作申请预授权。'}"]
    if cron_target:
        schedule = cron_target.get("schedule") if isinstance(cron_target.get("schedule"), dict) else {}
        blocks.append(
            "\n".join(
                [
                    "定时任务",
                    f"任务 ID：{cron_target.get('cronJobId') or '-'}",
                    f"任务内容：{cron_target.get('description') or '-'}",
                    f"执行计划：{schedule.get('cronExpression') or '-'}"
                    f"（{schedule.get('timezone') or '-'}）",
                ]
            )
        )
    blocks.extend(
        [
            "\n".join(["授权操作", *action_lines]),
            f"有效时间\n北京时间：{start} 至 {end}",
        ]
    )
    return "\n\n".join(blocks)


__all__ = [
    "INTENT_DISPLAY_CONTEXT",
    "IntentDisplayContext",
    "format_beijing_time",
    "format_intent_display_text",
    "preferred_display_language",
]
