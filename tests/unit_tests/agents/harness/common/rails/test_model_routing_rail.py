# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ModelRoutingRail（auto 档模型路由）.

Covers the written-down contract:

    auto -> 分类器打分：score >= 40 换 glm-5.3（凭证不变），否则保留当前模型

and the skip branch: a concrete model name / empty selection keeps the
adapter-applied model and never switches.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.model_routing import (
    ModelCapability,
    ModelRoutingRail,
)
from jiuwenswarm.agents.harness.common.rails.model_routing import (
    model_routing_rail as _rail_module,
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
            # 当前生效模型的凭证：换 glm-5.3 时必须逐字保留（api_base / api_key 不变）
            model_client_config={
                "model_name": model_name,
                "api_base": "https://maas.example.com/v1",
                "api_key": "sk-current",
                "client_provider": "OpenAI",
            },
            model_config_obj={"model_name": model_name, "temperature": 0},
        )

    def set_llm(self, model) -> None:
        self.model_name = model.model_config.model_name


def _fake_model_builder(mcc_fields: dict, mco_fields: dict) -> _FakeModel:
    """记录构造入参，返回只认 model_name 的假 Model（真实 Model 会拉起 Runner）。"""
    built = _FakeModel(mcc_fields.get("model_name", ""))
    built.built_mcc = mcc_fields
    built.built_mco = mco_fields
    return built


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
        model_name="deepseek-v4.1-flash",
        model_id="flash-0731",
        model=_FakeModel("deepseek-v4.1-flash"),
    )
    glm_cap = ModelCapability(
        model_name="glm-5.3",
        model_id="glm-53",
        model=_FakeModel("glm-5.3"),
    )
    return ModelRoutingRail(
        capability_table=[flash_cap, glm_cap],
        model_builder=_fake_model_builder,
    )


# ---- selection resolution ---- #


def test_resolve_request_selection_reads_run_context_extra():
    rail = _rail()
    assert rail._resolve_request_selection(_ctx("fast")) == "fast"
    assert rail._resolve_request_selection(_ctx("  balanced  ")) == "balanced"
    assert rail._resolve_request_selection(_ctx("gpt-5.4")) == "gpt-5.4"
    assert rail._resolve_request_selection(_ctx("")) == ""


# ---- skip branch ---- #


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["", "gpt-5.4", "deepseek-v4.1-flash"])
async def test_concrete_or_empty_selection_skips(selection):
    rail = _rail()
    agent = _FakeAgent("gpt-5.4")
    ctx = _ctx(selection, agent=agent)
    await rail.before_invoke(ctx)
    assert agent.model_name == "gpt-5.4"
    assert "_model_routing_used_cap" not in ctx.extra
    assert "llm_call_kwargs" not in ctx.extra
    assert ctx.extra["model_routing_decision"]["analysis"]["category"] == "skipped"


# ---- _find_cap_by_name ---- #


def test_find_cap_by_name_returns_first_same_name_ignoring_case_and_space():
    rail = ModelRoutingRail(
        capability_table=[
            ModelCapability(model_name="deepseek-v4.1-flash", model_id="first"),
            ModelCapability(model_name="deepseek-v4.1-flash", model_id="second"),
        ],
    )
    cap = rail._find_cap_by_name("  DeepSeek-V4.1-Flash  ")
    assert cap is not None
    assert cap.model_id == "first"
    assert rail._find_cap_by_name("no-such-model") is None


# ---- auto 档 enable_routing：分类器打分 → score >= 40 换 glm-5.3 ---- #


def _ctx_extra(extra: dict, agent: _FakeAgent | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        agent=agent,
        inputs=SimpleNamespace(
            query="你好，帮我写一个函数",
            run_context=SimpleNamespace(extra=extra),
            response=None,
        ),
        extra={},
    )


class _FakeTeamAgent(_FakeAgent):
    """team 成员假 agent：额外带 build_context（成员侧读取链路）。"""

    def __init__(self, model_name: str = "deepseek-v4.1-flash", metadata: dict | None = None) -> None:
        super().__init__(model_name)
        self.build_context = SimpleNamespace(request_metadata=metadata)


def _team_ctx(agent: _FakeAgent | None = None) -> SimpleNamespace:
    """team 成员 ctx：inputs **没有** run_context（openjiuwen 只传 query + conversation_id）。"""
    return SimpleNamespace(
        agent=agent,
        inputs=SimpleNamespace(query="你好，帮我写一个函数", response=None),
        extra={},
    )


def _classifier(score: int, category: str = "coding", difficulty: str = "hard"):
    """假分类器：返回固定 (score, category, difficulty)。"""

    async def _classify(_prompt_text: str):
        return score, category, difficulty

    return _classify


def _routing_rail(classifier=None) -> ModelRoutingRail:
    return ModelRoutingRail(
        capability_table=[],
        classifier=classifier,
        model_builder=_fake_model_builder,
    )


def test_resolve_request_routing_enabled_only_accepts_strict_true():
    rail = _rail()
    assert rail._resolve_request_routing_enabled(_ctx_extra({"enable_routing": True})) is True
    # 非布尔真值一律不算开启：adapter 侧已按 is True 过滤，这里再兜一层。
    for value in (False, None, "true", "True", 1, 0, {}, []):
        assert rail._resolve_request_routing_enabled(_ctx_extra({"enable_routing": value})) is False
    assert rail._resolve_request_routing_enabled(_ctx_extra({})) is False


def test_resolve_request_routing_enabled_false_without_run_context():
    rail = _rail()
    ctx = SimpleNamespace(agent=None, inputs=SimpleNamespace(query=""), extra={})
    assert rail._resolve_request_routing_enabled(ctx) is False


# ---- team 成员侧读取链路（成员拿不到 run_context）---- #


def test_resolve_request_routing_enabled_reads_team_build_context():
    """team 成员无 run_context → 回落到成员自身 BuildContext.request_metadata。"""
    rail = _rail()
    assert rail._resolve_request_routing_enabled(
        _team_ctx(_FakeTeamAgent(metadata={"enable_routing": True}))
    ) is True
    # 与单 agent 同款严格判 is True
    for value in (False, None, "true", 1, 0, {}, []):
        assert rail._resolve_request_routing_enabled(
            _team_ctx(_FakeTeamAgent(metadata={"enable_routing": value}))
        ) is False
    assert rail._resolve_request_routing_enabled(
        _team_ctx(_FakeTeamAgent(metadata={}))
    ) is False
    assert rail._resolve_request_routing_enabled(_team_ctx(_FakeTeamAgent())) is False
    # 无 build_context（普通单 agent）→ False，不抛
    assert rail._resolve_request_routing_enabled(_team_ctx(_FakeAgent())) is False


def test_resolve_request_routing_enabled_run_context_wins_over_team_metadata():
    """单 agent 链路优先；team 侧只是 run_context 缺省时的兜底。"""
    rail = _rail()
    agent = _FakeTeamAgent(metadata={"enable_routing": True})
    ctx = _ctx_extra({"enable_routing": False}, agent=agent)
    ctx.inputs.run_context.extra = {}  # 单 agent 链路没有开关 → 落到 team 侧
    assert rail._resolve_request_routing_enabled(ctx) is True
    ctx.inputs.run_context.extra = {"enable_routing": False}  # 显式 False → 仍落到 team 侧
    assert rail._resolve_request_routing_enabled(ctx) is True


@pytest.mark.asyncio
async def test_enable_routing_team_member_high_score_switches_to_glm():
    """team 成员（无 run_context）同样按开关打分换模型，凭证不变。"""
    rail = _routing_rail(_classifier(80))
    agent = _FakeTeamAgent(metadata={"enable_routing": True})
    ctx = _team_ctx(agent)

    await rail.before_invoke(ctx)

    assert agent.model_name == "glm-5.3"
    used = ctx.extra["_model_routing_used_cap"]
    assert used.model.built_mcc["api_key"] == "sk-current"
    assert used.model.built_mcc["api_base"] == "https://maas.example.com/v1"


@pytest.mark.asyncio
async def test_enable_routing_team_member_low_score_keeps_fallback_model():
    """team 成员低分 → 保留保底模型。"""
    rail = _routing_rail(_classifier(5, category="chat", difficulty="easy"))
    agent = _FakeTeamAgent(metadata={"enable_routing": True})
    ctx = _team_ctx(agent)

    await rail.before_invoke(ctx)

    assert agent.model_name == "deepseek-v4.1-flash"
    assert ctx.extra.get("_model_routing_used_cap") is None


@pytest.mark.asyncio
async def test_enable_routing_high_score_switches_to_glm():
    """score >= 40 → set_llm 换 glm-5.3，且 api_base / api_key 逐字不变。"""
    rail = _routing_rail(_classifier(80))
    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _ctx_extra({"enable_routing": True, "model_selection": "auto"}, agent=agent)

    await rail.before_invoke(ctx)

    assert agent.model_name == "glm-5.3"
    used = ctx.extra["_model_routing_used_cap"]
    assert used.model_name == "glm-5.3"
    # 换模型不换凭证
    assert used.model.built_mcc["api_base"] == "https://maas.example.com/v1"
    assert used.model.built_mcc["api_key"] == "sk-current"
    assert used.model.built_mcc["client_provider"] == "OpenAI"
    assert used.model.built_mcc["model_name"] == "glm-5.3"


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [0, 25, 39])
async def test_enable_routing_low_score_keeps_current_model(score):
    """score < 40（含边界 39）→ 保留 adapter 已应用的保底模型，不 set_llm。"""
    rail = _routing_rail(_classifier(score))
    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _ctx_extra({"enable_routing": True, "model_selection": "auto"}, agent=agent)

    await rail.before_invoke(ctx)

    assert agent.model_name == "deepseek-v4.1-flash"
    assert ctx.extra.get("_model_routing_used_cap") is None
    assert ctx.extra["model_routing_decision"]["analysis"]["target_score"] == score


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [40, 41, 45, 60, 65])
async def test_enable_routing_at_or_above_threshold_switches(score):
    """score >= 40（含边界 40）→ 换 glm-5.3。边界含 40 是本契约的一部分。"""
    rail = _routing_rail(_classifier(score))
    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _ctx_extra({"enable_routing": True, "model_selection": "auto"}, agent=agent)

    await rail.before_invoke(ctx)

    assert agent.model_name == "glm-5.3"
    assert ctx.extra["_model_routing_used_cap"].model_name == "glm-5.3"


@pytest.mark.asyncio
async def test_enable_routing_continuation_keeps_current_model():
    """续写/确认指令（分类器哨兵）→ 不换模型，即使哨兵值会被 validate_score 兜底成 60。"""
    from jiuwenswarm.agents.harness.common.rails.model_routing.classifier import (
        CONTINUATION_CATEGORY,
        CONTINUATION_SCORE,
    )

    rail = _routing_rail(_classifier(CONTINUATION_SCORE, category=CONTINUATION_CATEGORY))
    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _ctx_extra({"enable_routing": True, "model_selection": "auto"}, agent=agent)

    await rail.before_invoke(ctx)

    assert agent.model_name == "deepseek-v4.1-flash"
    assert ctx.extra["model_routing_decision"]["analysis"]["category"] == "skipped"


@pytest.mark.asyncio
async def test_enable_routing_without_classifier_keeps_current_model():
    """显式 classifier=None（关闭 auto 档路由）→ 保留保底模型，不报错、不换模型。"""
    rail = _routing_rail(None)
    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _ctx_extra({"enable_routing": True, "model_selection": "auto"}, agent=agent)

    await rail.before_invoke(ctx)

    assert agent.model_name == "deepseek-v4.1-flash"
    assert ctx.extra.get("_model_routing_used_cap") is None


@pytest.mark.asyncio
async def test_enable_routing_lazily_loads_default_classifier():
    """未注入 classifier → 懒加载内置 classifier.py 打分。"""
    rail = ModelRoutingRail(capability_table=[], model_builder=_fake_model_builder)
    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _ctx_extra({"enable_routing": True, "model_selection": "auto"}, agent=agent)
    ctx.inputs.query = "debug this code"

    await rail.before_invoke(ctx)

    assert agent.model_name == "glm-5.3"


@pytest.mark.asyncio
async def test_enable_routing_default_classifier_load_failure_keeps_current_model(monkeypatch):
    """内置分类器装载失败（import 报错）→ 降级保留保底模型，不影响 invoke。"""
    monkeypatch.setattr(_rail_module, "_load_default_classifier", lambda: None)
    rail = ModelRoutingRail(capability_table=[], model_builder=_fake_model_builder)
    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _ctx_extra({"enable_routing": True, "model_selection": "auto"}, agent=agent)

    await rail.before_invoke(ctx)

    assert agent.model_name == "deepseek-v4.1-flash"
    assert ctx.extra.get("_model_routing_used_cap") is None


@pytest.mark.asyncio
async def test_enable_routing_classifier_exception_keeps_current_model():
    """分类器抛异常 → 保留保底模型，不让异常冒泡出 before_invoke。"""

    async def _boom(_prompt_text: str):
        raise RuntimeError("classifier exploded")

    rail = _routing_rail(_boom)
    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _ctx_extra({"enable_routing": True, "model_selection": "auto"}, agent=agent)

    await rail.before_invoke(ctx)

    assert agent.model_name == "deepseek-v4.1-flash"


@pytest.mark.asyncio
async def test_enable_routing_logs_high_score_switch(monkeypatch):
    """换模型时日志可见（llm.log 是 E2E 的观测面）。

    直接 patch 模块级 ``logger``：``jiuwenswarm.common.utils`` 的 logger 自带 handler、
    不向 root propagate，且 handler 在导入期就绑定了原始 stderr 流 —— caplog（root 级）
    与 capfd（fd 级）都抓不到。
    """
    logged: list[str] = []
    monkeypatch.setattr(
        _rail_module.logger,
        "info",
        lambda msg, *args, **kwargs: logged.append(msg % args if args else msg),
    )
    rail = _routing_rail(_classifier(80))
    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _ctx_extra({"enable_routing": True, "model_selection": "auto"}, agent=agent)

    await rail.before_invoke(ctx)

    assert any("set_llm glm-5.3" in line for line in logged)


# ---- auto 档端到端：真分类器 + 真阈值 ---- #


def _real_classifier_rail() -> ModelRoutingRail:
    from jiuwenswarm.agents.harness.common.rails.model_routing.classifier import classify

    return ModelRoutingRail(
        capability_table=[],
        classifier=classify,
        model_builder=_fake_model_builder,
    )


@pytest.mark.asyncio
async def test_real_classifier_high_score_prompt_switches_to_glm():
    """真分类器把「debug this code」打成 65（coding/hard）>= 40 → set_llm glm-5.3，凭证不变。"""
    rail = _real_classifier_rail()
    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _ctx_extra({"enable_routing": True, "model_selection": "auto"}, agent=agent)
    ctx.inputs.query = "debug this code"

    await rail.before_invoke(ctx)

    assert agent.model_name == "glm-5.3"
    used = ctx.extra["_model_routing_used_cap"]
    assert used.model.built_mcc["api_key"] == "sk-current"
    assert used.model.built_mcc["api_base"] == "https://maas.example.com/v1"


@pytest.mark.asyncio
async def test_real_classifier_low_score_prompt_keeps_fallback_model():
    """真分类器把「你好」打成 5（chat/easy）< 40 → 保留保底模型。"""
    rail = _real_classifier_rail()
    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _ctx_extra({"enable_routing": True, "model_selection": "auto"}, agent=agent)
    ctx.inputs.query = "你好"

    await rail.before_invoke(ctx)

    assert agent.model_name == "deepseek-v4.1-flash"
    assert ctx.extra.get("_model_routing_used_cap") is None
