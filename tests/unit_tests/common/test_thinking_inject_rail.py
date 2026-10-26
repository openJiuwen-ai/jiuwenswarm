# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Unit tests for ThinkingInjectRail."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.common.thinking.types import ThinkingProfile, freeze_llm_call_kwargs
from jiuwenswarm.common.thinking.rail import ThinkingInjectRail


@pytest.mark.asyncio
async def test_thinking_inject_rail_writes_kwargs():
    profile = ThinkingProfile(
        thinking="off",
        llm_call_kwargs=freeze_llm_call_kwargs(
            {"extra_body": {"thinking": {"type": "disabled"}}}
        ),
        injected=True,
        degraded=False,
        model_name="glm-5.1",
    )
    rail = ThinkingInjectRail(profile, role_id="Charlie", agent_id="subagent_abc")
    ctx = SimpleNamespace(extra={})
    await rail.before_model_call(ctx)
    assert ctx.extra["llm_call_kwargs"]["extra_body"]["thinking"]["type"] == "disabled"
    # Nested mutation of injected dict must not mutate frozen profile
    ctx.extra["llm_call_kwargs"]["extra_body"]["thinking"]["type"] = "enabled"
    assert profile.llm_call_kwargs["extra_body"]["thinking"]["type"] == "disabled"


@pytest.mark.asyncio
async def test_thinking_inject_rail_noop_when_empty():
    rail = ThinkingInjectRail(ThinkingProfile.empty())
    ctx = SimpleNamespace(extra={})
    await rail.before_model_call(ctx)
    assert "llm_call_kwargs" not in ctx.extra
