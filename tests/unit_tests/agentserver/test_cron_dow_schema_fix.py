# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the cron tool description dow-semantics fix.

背景：openjiuwen 的 cron 工具描述把星期字段声明为 Quartz 的 1=SUN...7=SAT，
而 jiuwenswarm 后端（croniter）与前端 CronPanel 统一按 0=SUN...6=SAT 解析，
LLM 照描述生成数字 dow 时会发生 +1 天偏移（周三、周五 → 周四、周六）。
该测试锁定 build_tools 产出工具描述的后置修正行为（中英文）。
"""

from __future__ import annotations

from typing import Any

import pytest

from openjiuwen.harness.tools.cron import CronToolContext

from jiuwenswarm.agents.harness.common.tools.cron.cron_runtime import (
    _QUARTZ_DOW_DECLARATION_PATTERNS,
    CronRuntimeBridge,
)


class _DummyCronBackend:
    """满足 CronToolBackend 协议的最小桩；create_cron_tools 构造工具时不调用任何方法。"""

    async def list_jobs(self, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    async def create_job(self, params: dict[str, Any], *, context: CronToolContext | None = None) -> dict[str, Any]:
        raise NotImplementedError

    async def update_job(self, job_id: str, patch: dict[str, Any], *, context: CronToolContext | None = None) -> dict[str, Any]:
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


def _build_tools(language: str) -> list[Any]:
    bridge = CronRuntimeBridge()
    bridge.set_backend(_DummyCronBackend())
    return bridge.build_tools(
        context=CronToolContext(channel_id="web", session_id="sess_test"),
        agent_id="test-agent",
        language=language,
    )


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


def _all_text(tools: list[Any]) -> list[str]:
    out: list[str] = []
    for tool in tools:
        card = getattr(tool, "card", None)
        if card is None:
            continue
        out.append(str(getattr(card, "description", None) or ""))
        out.extend(_collect(card.input_params))
    return out


def _has_quartz_declaration(text: str) -> bool:
    return any(pattern.search(text) for pattern in _QUARTZ_DOW_DECLARATION_PATTERNS)


@pytest.mark.parametrize(
    ("language", "dow_marker"),
    [
        ("cn", "0=周日"),
        ("en", "0=SUN"),
    ],
)
def test_build_tools_cards_have_croniter_dow_semantics(language: str, dow_marker: str) -> None:
    tools = _build_tools(language)
    texts = _all_text(tools)
    assert not any(_has_quartz_declaration(text) for text in texts), \
        f"{language} 工具描述仍残留 Quartz 语义声明"
    assert any(dow_marker in text for text in texts), \
        f"{language} 工具描述缺少 {dow_marker} 语义说明"
    assert any("WED,FRI" in text for text in texts), \
        f"{language} 工具描述缺少字母缩写示例 WED,FRI"
