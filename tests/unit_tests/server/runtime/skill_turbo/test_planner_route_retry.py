# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Planner route retry: unparseable LLM output retries once, then degrades to None."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.server.runtime.skill_turbo.planner import SkillTurboPlanner

_ROUTE_OK = json.dumps({"skill_name": "ppt", "confidence": 0.95, "reason": "PPT 任务"})
_ROUTE_NULL = json.dumps({"skill_name": None, "confidence": 0.0, "reason": "无适配技能"})


def _client_with_outputs(outputs: list[list[str]]) -> tuple[MagicMock, list[dict]]:
    """构建按调用次序返回 outputs 的 fake model client，并记录每次收到的 messages。"""
    calls: list[dict] = []

    async def stream(messages):
        idx = len(calls)
        calls.append(messages)
        for piece in outputs[min(idx, len(outputs) - 1)]:
            yield SimpleNamespace(content=piece)

    client = MagicMock()
    client.stream = stream
    return client, calls


def _env(client: MagicMock) -> MagicMock:
    skill = SimpleNamespace(
        name="ppt", description="ppt 任务流", match_keywords=["ppt"], plan_code="code"
    )
    env = MagicMock()
    env.skills = {"ppt": skill}
    env.model_client = client
    return env


class TestMatchSkillRouteRetry:
    @pytest.mark.asyncio
    async def test_retry_succeeds_after_unparseable_output(self):
        """首次输出不可解析（如空 content），重试后返回合法 JSON → 路由成功。"""
        client, calls = _client_with_outputs([[""], [_ROUTE_OK]])
        planner = SkillTurboPlanner(_env(client))

        skill = await planner.match_skill("生成一份PPT")

        assert skill is not None and skill.name == "ppt"
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_retry_exhausted_returns_none(self):
        """两次输出均不可解析 → 返回 None 降级，且恰好重试到上限。"""
        client, calls = _client_with_outputs([["垃圾输出"], ["还是不行 {"]])
        planner = SkillTurboPlanner(_env(client))

        assert await planner.match_skill("生成一份PPT") is None
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_valid_first_output_no_retry(self):
        """首次即返回合法 JSON → 不发起多余重试。"""
        client, calls = _client_with_outputs([[_ROUTE_OK]])
        planner = SkillTurboPlanner(_env(client))

        skill = await planner.match_skill("生成一份PPT")

        assert skill is not None and skill.name == "ppt"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_null_skill_name_not_retried(self):
        """skill_name=null 是模型正常判定，不属于瞬态失败，不触发重试。"""
        client, calls = _client_with_outputs([[_ROUTE_NULL]])
        planner = SkillTurboPlanner(_env(client))

        assert await planner.match_skill("你好") is None
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_low_confidence_not_retried(self):
        """confidence 低于阈值是正常判定，不触发重试。"""
        low = json.dumps({"skill_name": "ppt", "confidence": 0.3, "reason": "弱匹配"})
        client, calls = _client_with_outputs([[low]])
        planner = SkillTurboPlanner(_env(client))

        assert await planner.match_skill("生成一份PPT") is None
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_stream_error_then_success(self):
        """首次流调用抛异常（如网络抖动），重试成功 → 路由成功。"""
        calls: list[dict] = []

        async def flaky_stream(messages):
            calls.append(messages)
            if len(calls) == 1:
                raise ConnectionError("network blip")
            yield SimpleNamespace(content=_ROUTE_OK)

        client = MagicMock()
        client.stream = flaky_stream
        planner = SkillTurboPlanner(_env(client))

        skill = await planner.match_skill("生成一份PPT")

        assert skill is not None and skill.name == "ppt"
        assert len(calls) == 2
