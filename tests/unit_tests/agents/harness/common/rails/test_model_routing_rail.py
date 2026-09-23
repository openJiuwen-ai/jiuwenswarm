# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ModelRoutingRail (四档模型路由).

Covers the written-down four-tier contract:

    fast     -> deepseek-v4-flash-0731  思考 off
    balanced -> deepseek-v4-flash-0731  思考 medium
    extreme  -> glm-5.2                 思考 deep
    auto     -> deepseek-v4-flash-0731  思考 medium

and the skip branch: a concrete model name / empty selection keeps the
adapter-applied model and never injects thinking kwargs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.model_routing import (
    ModelCapability,
    ModelRoutingRail,
)


class _FakeModel:
    """Fake openjiuwen Model — only the attributes the rail touches on switch."""

    def __init__(self, name: str) -> None:
        self.model_config = SimpleNamespace(model_name=name)
        self.model_client_config = {"client_provider": "deepseek"}


class _FakeAgent:
    """Fake DeepAgent/ReActAgent — exposes set_llm + the config the rail syncs."""

    def __init__(self, model_name: str = "gpt-5.4") -> None:
        self.model_name = model_name
        self._config = SimpleNamespace(
            model_name=model_name,
            model_client_config={},
            model_config_obj=None,
        )

    def set_llm(self, model) -> None:
        self.model_name = model.model_config.model_name


def _ctx(selection: str = "", agent: _FakeAgent | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        agent=agent,
        inputs=SimpleNamespace(
            query="你好，帮我写一个函数",
            run_context=SimpleNamespace(extra={"model_selection": selection}),
            response=None,
        ),
        extra={},
    )


def _rail() -> ModelRoutingRail:
    flash_cap = ModelCapability(
        model_name="deepseek-v4-flash-0731",
        model_id="flash-0731",
        model=_FakeModel("deepseek-v4-flash-0731"),
    )
    glm_cap = ModelCapability(
        model_name="glm-5.2",
        model_id="glm-52",
        model=_FakeModel("glm-5.2"),
    )
    return ModelRoutingRail(capability_table=[flash_cap, glm_cap])


# ---- selection resolution ---- #


def test_resolve_request_selection_reads_run_context_extra():
    rail = _rail()
    assert rail._resolve_request_selection(_ctx("fast")) == "fast"
    assert rail._resolve_request_selection(_ctx("  balanced  ")) == "balanced"
    assert rail._resolve_request_selection(_ctx("gpt-5.4")) == "gpt-5.4"
    assert rail._resolve_request_selection(_ctx("")) == ""


# ---- four-tier thinking selection ---- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selection, thinking",
    [
        ("fast", "off"),
        ("balanced", "medium"),
        ("extreme", "deep"),
        ("auto", "medium"),
    ],
)
async def test_before_invoke_sets_thinking_per_tier(selection, thinking):
    rail = _rail()
    await rail.before_invoke(_ctx(selection))
    assert rail._request_thinking == thinking
    assert rail._request_mode == selection


@pytest.mark.asyncio
async def test_before_model_call_injects_off_kwargs():
    rail = _rail()
    ctx = _ctx("fast")
    await rail.before_invoke(ctx)
    await rail.before_model_call(ctx)
    assert ctx.extra["llm_call_kwargs"] == {"extra_body": {"thinking": {"type": "disabled"}}}


@pytest.mark.asyncio
async def test_before_model_call_injects_deep_kwargs():
    rail = _rail()
    ctx = _ctx("extreme")
    await rail.before_invoke(ctx)
    await rail.before_model_call(ctx)
    assert ctx.extra["llm_call_kwargs"] == {
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "high",
    }


@pytest.mark.asyncio
async def test_injected_kwargs_are_isolated_from_class_constant():
    rail = _rail()
    ctx = _ctx("balanced")
    await rail.before_invoke(ctx)
    await rail.before_model_call(ctx)
    # Mutating the injected dict must not corrupt the shared _THINKING_KWARGS.
    ctx.extra["llm_call_kwargs"]["extra_body"]["thinking"]["type"] = "disabled"
    ctx2 = _ctx("balanced")
    await rail.before_invoke(ctx2)
    await rail.before_model_call(ctx2)
    assert ctx2.extra["llm_call_kwargs"]["extra_body"]["thinking"]["type"] == "enabled"


# ---- skip branch ---- #


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["", "gpt-5.4", "deepseek-v4-flash-0731"])
async def test_concrete_or_empty_selection_skips(selection):
    rail = _rail()
    agent = _FakeAgent("gpt-5.4")
    ctx = _ctx(selection, agent=agent)
    await rail.before_invoke(ctx)
    await rail.before_model_call(ctx)
    assert rail._request_thinking == "default"
    assert agent.model_name == "gpt-5.4"
    assert "_model_routing_used_cap" not in ctx.extra
    assert "llm_call_kwargs" not in ctx.extra
    assert ctx.extra["model_routing_decision"]["analysis"]["category"] == "skipped"


# ---- switch branch ---- #


@pytest.mark.asyncio
async def test_mode_switch_applies_set_llm():
    rail = _rail()
    agent = _FakeAgent("gpt-5.4")
    ctx = _ctx("balanced", agent=agent)
    await rail.before_invoke(ctx)
    assert agent.model_name == "deepseek-v4-flash-0731"
    assert ctx.extra["_model_routing_used_cap"].model_name == "deepseek-v4-flash-0731"


@pytest.mark.asyncio
async def test_extreme_switches_to_glm():
    rail = _rail()
    agent = _FakeAgent("gpt-5.4")
    ctx = _ctx("extreme", agent=agent)
    await rail.before_invoke(ctx)
    assert agent.model_name == "glm-5.2"


@pytest.mark.asyncio
async def test_mode_model_missing_keeps_current_but_sets_thinking():
    rail = ModelRoutingRail(capability_table=[])
    agent = _FakeAgent("gpt-5.4")
    ctx = _ctx("fast", agent=agent)
    await rail.before_invoke(ctx)
    assert agent.model_name == "gpt-5.4"
    assert rail._request_thinking == "off"


# ---- 优先 maas 官方模型（避免撞用户自定义同名模型）---- #


def test_find_cap_prefers_marked_provider():
    """同名 cap 里带 maas 标的（即使排后面）优先命中，不撞用户自定义同名模型。"""
    rail = ModelRoutingRail(
        capability_table=[
            ModelCapability(model_name="deepseek-v4-flash-0731", model_id="custom", model_provider="openai"),
            ModelCapability(model_name="deepseek-v4-flash-0731", model_id="maas", model_provider="huawei_maas"),
        ],
    )
    cap = rail._find_cap_by_name("deepseek-v4-flash-0731", prefer_provider="huawei_maas")
    assert cap is not None
    assert cap.model_id == "maas"


def test_find_cap_falls_back_to_first_when_no_mark():
    """无标能力表（现状/向后兼容）：退回第一个同名，行为与旧实现一致。"""
    rail = ModelRoutingRail(
        capability_table=[
            ModelCapability(model_name="deepseek-v4-flash-0731", model_id="first"),
            ModelCapability(model_name="deepseek-v4-flash-0731", model_id="second"),
        ],
    )
    cap = rail._find_cap_by_name("deepseek-v4-flash-0731", prefer_provider="huawei_maas")
    assert cap is not None
    assert cap.model_id == "first"


@pytest.mark.asyncio
async def test_mode_switch_prefers_marked_model_even_when_later():
    """四档 before_invoke 端到端：同名自定义模型排前、maas 排后，仍切到 maas。"""
    rail = ModelRoutingRail(
        capability_table=[
            ModelCapability(
                model_name="deepseek-v4-flash-0731",
                model_id="custom",
                model=_FakeModel("deepseek-v4-flash-0731"),
                model_provider="openai",
            ),
            ModelCapability(
                model_name="deepseek-v4-flash-0731",
                model_id="maas",
                model=_FakeModel("deepseek-v4-flash-0731"),
                model_provider="huawei_maas",
            ),
        ],
    )
    agent = _FakeAgent("gpt-5.4")
    ctx = _ctx("balanced", agent=agent)
    await rail.before_invoke(ctx)
    used = ctx.extra.get("_model_routing_used_cap")
    assert used is not None
    assert used.model_id == "maas"
