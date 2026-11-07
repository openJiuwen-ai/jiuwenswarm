# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the cron tool "fresh-session execution" description patch.

背景（Issue #7924）：定时任务到点后在全新会话中执行，description 直接作为该
会话的用户消息，执行模型看不到创建任务的对话历史。openjiuwen 的 cron 工具
描述没有声明这一点，模型会把用户原话（"提醒我…"）原样当作任务内容；统一
cron 工具描述还宣传 "优先使用 payload.kind=systemEvent"，而本后端把
systemEvent 按 agentTurn 处理（无独立投递通道，见 _extract_legacy_params）。

本测试锁定 build_tools 后的 ToolCard 描述：创建/修改类工具必须带「执行环境」
声明，systemEvent 宣传句必须被修正为与后端行为一致的文本（中英文）。
"""

from __future__ import annotations

from typing import Any

import pytest

from openjiuwen.harness.tools.cron import CronToolContext

from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import (
    _CRON_FRESH_SESSION_NOTICE,
    _CRON_FRESH_SESSION_TOOL_NAMES,
    _CRON_SYSTEM_EVENT_LEGACY_PATTERNS,
    CronRuntimeBridge,
    _patch_cron_fresh_session_semantics,
)


class _DummyCronBackend:
    """满足 CronToolBackend 协议的最小桩；create_cron_tools 构造工具时不调用任何方法。"""

    async def list_jobs(self, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    async def create_job(self, params: dict[str, Any], *, context: Any = None) -> dict[str, Any]:
        raise NotImplementedError

    async def update_job(self, job_id: str, patch: dict[str, Any], *, context: Any = None) -> dict[str, Any]:
        raise NotImplementedError

    async def delete_job(self, job_id: str) -> bool:
        raise NotImplementedError

    async def toggle_job(self, job_id: str, enabled: bool) -> dict[str, Any]:
        raise NotImplementedError

    async def preview_job(self, job_id: str, count: int = 5) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def run_now(self, job_id: str) -> str:
        raise NotImplementedError

    async def status(self) -> dict[str, Any]:
        raise NotImplementedError

    async def get_runs(self, job_id: str, limit: int = 20) -> list[dict[str, Any]]:
        raise NotImplementedError


def _build_tools(
    language: str,
    *,
    allow_create: bool = True,
    allow_update: bool = True,
) -> list[Any]:
    bridge = CronRuntimeBridge()
    bridge.set_backend(_DummyCronBackend())
    return bridge.build_tools(
        context=CronToolContext(channel_id="web", session_id="sess_test"),
        agent_id="test-agent",
        language=language,
        allow_create=allow_create,
        allow_update=allow_update,
    )


def _by_name(tools: list[Any]) -> dict[str, Any]:
    return {
        str(getattr(getattr(tool, "card", None), "name", "") or ""): tool
        for tool in tools
    }


def _collect(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            out.extend(_collect(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_collect(item))
    return out


@pytest.mark.parametrize("language", ["cn", "en"])
def test_creation_tools_carry_fresh_session_notice(language: str) -> None:
    """创建/修改类工具的 description 必须以「执行环境」声明开头。"""
    tools = _build_tools(language)
    notice = _CRON_FRESH_SESSION_NOTICE[language]
    by_name = _by_name(tools)
    for name in _CRON_FRESH_SESSION_TOOL_NAMES:
        assert name in by_name, f"expected tool {name} in built tools"
        description = str(by_name[name].card.description or "")
        assert description.startswith(notice), (
            f"{name} description must start with the fresh-session notice"
        )


@pytest.mark.parametrize("language", ["cn", "en"])
def test_other_tools_do_not_carry_notice(language: str) -> None:
    """只读/管理类工具不追加声明，避免工具清单噪声。"""
    tools = _build_tools(language)
    notice = _CRON_FRESH_SESSION_NOTICE[language]
    by_name = _by_name(tools)
    for name, tool in by_name.items():
        if name in _CRON_FRESH_SESSION_TOOL_NAMES:
            continue
        description = str(tool.card.description or "")
        assert notice not in description, f"{name} must not carry the notice"


@pytest.mark.parametrize("language", ["cn", "en"])
def test_system_event_preference_sentence_is_corrected(language: str) -> None:
    """统一 cron 工具里的 systemEvent 宣传句必须修正为与后端行为一致的文本。"""
    tools = _build_tools(language)
    cron_tool = _by_name(tools)["cron"]
    description = str(cron_tool.card.description or "")
    blob = _collect(description) + _collect(getattr(cron_tool.card, "input_params", None) or {})
    for pattern in _CRON_SYSTEM_EVENT_LEGACY_PATTERNS:
        assert not any(pattern.search(text) for text in blob), (
            "legacy 'prefer systemEvent' guidance must not survive the patch"
        )
    if language == "en":
        assert "executed as an agent turn in a fresh session" in description
    else:
        assert "本后端将其按 agentTurn 处理" in description


@pytest.mark.parametrize("language", ["cn", "en"])
def test_patch_is_idempotent(language: str) -> None:
    """重复应用补丁不重复追加声明、不破坏已修正文本。"""
    tools = _build_tools(language)
    notice = _CRON_FRESH_SESSION_NOTICE[language]
    _patch_cron_fresh_session_semantics(tools, language=language)
    by_name = _by_name(tools)
    for name in _CRON_FRESH_SESSION_TOOL_NAMES:
        description = str(by_name[name].card.description or "")
        assert description.count(notice) == 1, f"{name} notice duplicated"


def test_restricted_session_keeps_both_notices() -> None:
    """cron 执行会话（禁 add/update）下，两套声明共存且被禁工具已下架。"""
    tools = _build_tools("cn", allow_create=False, allow_update=False)
    by_name = _by_name(tools)
    assert "cron_create_job" not in by_name
    assert "cron_update_job" not in by_name
    cron_tool = by_name["cron"]
    description = str(cron_tool.card.description or "")
    assert _CRON_FRESH_SESSION_NOTICE["cn"] in description
    assert "以下动作不可用" in description
