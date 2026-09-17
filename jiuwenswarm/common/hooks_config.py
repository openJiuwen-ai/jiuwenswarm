# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Hooks 配置模型 —— 定义 config.yaml 中 hooks 段的 schema 与匹配逻辑."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class HookType(str, Enum):
    COMMAND = "command"
    PROMPT = "prompt"


class HookEvent(str, Enum):
    """当前 17 个底层能力支持的event."""
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    STOP = "Stop"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    NOTIFICATION = "Notification"
    PERMISSION_REQUEST = "PermissionRequest"
    PERMISSION_DENIED = "PermissionDenied"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    CONFIG_CHANGE = "ConfigChange"
    INSTRUCTIONS_LOADED = "InstructionsLoaded"
    SETUP = "Setup"
    BEFORE_MODEL_CALL = "BeforeModelCall"
    AFTER_MODEL_CALL = "AfterModelCall"


# 需要在 AgentServer Rail 层执行的事件（工具/Agent 生命周期）
_AGENT_RAIL_EVENTS = frozenset({
    HookEvent.PRE_TOOL_USE,
    HookEvent.POST_TOOL_USE,
    HookEvent.POST_TOOL_USE_FAILURE,
    HookEvent.STOP,
    HookEvent.PERMISSION_REQUEST,
    HookEvent.PERMISSION_DENIED,
    HookEvent.SUBAGENT_START,
    HookEvent.SUBAGENT_STOP,
    HookEvent.BEFORE_MODEL_CALL,
    HookEvent.AFTER_MODEL_CALL,
})

# 需要在 Gateway 层执行的事件（session/用户交互）
_GATEWAY_EVENTS = frozenset({
    HookEvent.USER_PROMPT_SUBMIT,
    HookEvent.SESSION_START,
    HookEvent.SESSION_END,
    HookEvent.NOTIFICATION,
    HookEvent.CONFIG_CHANGE,
    HookEvent.INSTRUCTIONS_LOADED,
    HookEvent.SETUP,
})


def is_rail_event(event: HookEvent) -> bool:
    return event in _AGENT_RAIL_EVENTS


def is_gateway_event(event: HookEvent) -> bool:
    return event in _GATEWAY_EVENTS


@dataclass
class CommandHookConfig:
    type: str = "command"
    command: str = ""
    timeout: int = 30
    shell: str = "bash"
    status_message: str = ""


@dataclass
class PromptHookConfig:
    type: str = "prompt"
    prompt: str = ""
    timeout: int = 15
    model: str = ""
    status_message: str = ""


@dataclass
class HookMatcher:
    matcher: str = "*"        # 匹配表达式（工具名 / 管道OR / 正则 / "*"）
    hooks: list[dict] = field(default_factory=list)

    def matches(self, query: str) -> bool:
        """检查 query 是否匹配此 matcher.

        query: 主要匹配字段（PreToolUse 时为 tool_name，SessionStart 时为 source）
        """
        pattern = self.matcher.strip()
        if pattern == "*" or pattern == "":
            return True

        # "|" 分隔的 OR 匹配
        if "|" in pattern and not pattern.startswith("^"):
            parts = [p.strip() for p in pattern.split("|")]
            return any(self._match_single(p, query) for p in parts)

        return self._match_single(pattern, query)

    @staticmethod
    def _match_single(pattern: str, query: str) -> bool:
        if pattern == query:
            return True
        if pattern.startswith("^") or pattern.endswith("$") or ".*" in pattern:
            try:
                return bool(re.match(pattern, query))
            except re.error:
                return False
        return False


@dataclass
class HooksConfig:
    events: dict[str, list[HookMatcher]] = field(default_factory=dict)
    disable_all_hooks: bool = False

    def match(self, event: str, query: str = "") -> list[dict]:
        """获取匹配该事件 + query 的所有 hook 配置."""
        if self.disable_all_hooks:
            return []

        matchers = self.events.get(event, [])
        result: list[dict] = []
        for m in matchers:
            if m.matches(query):
                result.extend(m.hooks)
        return result

    def get_event_summary(self) -> list[dict]:
        """返回各事件的 hook 数量摘要（供 /hooks 命令 UI 使用）."""
        summaries = []
        for event in HookEvent:
            matchers = self.events.get(event.value, [])
            total_hooks = sum(len(m.hooks) for m in matchers)
            matcher_details = [
                {
                    "matcher": m.matcher,
                    "hook_count": len(m.hooks),
                    "hooks": m.hooks,
                }
                for m in matchers
            ]
            summaries.append({
                "name": event.value,
                "total_hooks": total_hooks,
                "matchers": matcher_details,
            })
        return summaries


def load_hooks_config(config_base: dict | None = None) -> HooksConfig:
    """从 config.yaml 的 hooks 段加载配置.

    Args:
        config_base: 完整 config.yaml 字典。为 None 时自动从 get_config() 读取。
    """
    if config_base is None:
        from jiuwenswarm.common.config import get_config
        config_base = get_config()

    hooks_section = config_base.get("hooks", {}) if config_base else {}
    if not hooks_section or not isinstance(hooks_section, dict):
        return HooksConfig()

    disable_all = bool(hooks_section.get("disable_all_hooks", False))

    events: dict[str, list[HookMatcher]] = {}
    for event in HookEvent:
        event_configs = hooks_section.get(event.value, [])
        if not isinstance(event_configs, list):
            logger.warning("hooks config: event %s expects a list, got %s", event.value, type(event_configs).__name__)
            continue
        matchers = []
        for entry in event_configs:
            if not isinstance(entry, dict):
                msg = "hooks config: entry in event %s expects a dict, got %s"
                logger.warning(msg, event.value, type(entry).__name__)
                continue
            matchers.append(HookMatcher(
                matcher=entry.get("matcher", "*"),
                hooks=entry.get("hooks", []),
            ))
        if matchers:
            events[event.value] = matchers

    return HooksConfig(events=events, disable_all_hooks=disable_all)