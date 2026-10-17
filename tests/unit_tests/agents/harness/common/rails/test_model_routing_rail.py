# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ModelRoutingRail（auto 档模型路由）.

Covers the written-down contract:

    auto -> 分类器打分：score >= 45 换 glm-5.3（凭证不变），< 45 留在基准模型上按分数
    调思考深度（<=10 off / <=20 low / <=30 medium / <45 high）

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


# ---- auto 档 enable_routing：分类器打分 → score >= 45 换 glm-5.3 ---- #


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
    """score >= 45 → set_llm 换 glm-5.3，且 api_base / api_key 逐字不变。"""
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
@pytest.mark.parametrize("score", [0, 25, 40])
async def test_enable_routing_low_score_keeps_current_model(score):
    """score < 45（含边界 40）→ 保留 adapter 已应用的保底模型，不 set_llm。"""
    rail = _routing_rail(_classifier(score))
    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _ctx_extra({"enable_routing": True, "model_selection": "auto"}, agent=agent)

    await rail.before_invoke(ctx)

    assert agent.model_name == "deepseek-v4.1-flash"
    assert ctx.extra.get("_model_routing_used_cap") is None
    assert ctx.extra["model_routing_decision"]["analysis"]["target_score"] == score


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [45, 60, 65])
async def test_enable_routing_at_or_above_threshold_switches(score):
    """score >= 45（含边界 45）→ 换 glm-5.3。边界含 45 是本契约的一部分。"""
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
    """真分类器把「debug this code」打成 65（coding/hard）>= 45 → set_llm glm-5.3，凭证不变。"""
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
    """真分类器把「你好」打成 5（chat/easy）< 45 → 保留保底模型，深度 off。"""
    rail = _real_classifier_rail()
    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _ctx_extra({"enable_routing": True, "model_selection": "auto"}, agent=agent)
    ctx.inputs.query = "你好"

    await rail.before_invoke(ctx)

    assert agent.model_name == "deepseek-v4.1-flash"
    assert ctx.extra.get("_model_routing_used_cap") is None


# ---- auto 档 session 内粘滞（升快降慢 + 欠债计分 + 弱档深度带）---- #
#
# 契约：
#   首轮 score >= 45 进强档，否则弱档；
#   弱档 --单轮 score >= 45--> 强档（不设去抖）；
#   弱档不换模型，按分数给 flash 注入思考深度：<=10 off / <=20 low / <=30 medium / <45 high；
#   强档 --欠债 debt += 45 - score 累计到 100--> 弱档，score >= 45 清零。
# 档位按 conversation_id 记在 rail 实例上；ctx 没有 conversation_id（上面全部用例）
# 时退回无状态语义，即上面的既有断言。


class _ScriptedClassifier:
    """按脚本依次返回 ``(score, category, difficulty)``，模拟同一 session 里的难度变化。

    脚本用完后重复最后一项，方便写「一直很闲」这类序列。
    """

    def __init__(self, steps: list[tuple[int, str, str]]) -> None:
        self._steps = list(steps)
        self._i = 0

    async def __call__(self, _prompt_text: str):
        step = self._steps[min(self._i, len(self._steps) - 1)]
        self._i += 1
        return step


def _coding(score: int) -> tuple[int, str, str]:
    """中难度 coding 任务（真实 SCORE_TABLE：coding/medium = 45）。"""
    return score, "coding", "medium"


def _chat_easy() -> tuple[int, str, str]:
    """「你好」这类闲聊（真实 SCORE_TABLE：chat/easy = 5）。"""
    return 5, "chat", "easy"


def _cont() -> tuple[int, str, str]:
    """续写指令哨兵（不参与打分）。"""
    from jiuwenswarm.agents.harness.common.rails.model_routing.classifier import (
        CONTINUATION_CATEGORY,
        CONTINUATION_SCORE,
    )

    return CONTINUATION_SCORE, CONTINUATION_CATEGORY, "easy"


def _ctx_session(conv_id: str, extra: dict, agent: _FakeAgent | None = None) -> SimpleNamespace:
    """带 conversation_id 的 ctx：走 session 粘滞路径。"""
    return SimpleNamespace(
        agent=agent,
        inputs=SimpleNamespace(
            query="你好，帮我写一个函数",
            conversation_id=conv_id,
            run_context=SimpleNamespace(extra=extra),
            response=None,
        ),
        extra={},
    )


def _auto_ctx(conv_id: str, agent: _FakeAgent) -> SimpleNamespace:
    return _ctx_session(conv_id, {"enable_routing": True, "model_selection": "auto"}, agent=agent)


async def _run_session(steps, conv_id: str = "sess-1", rail: ModelRoutingRail | None = None):
    """依次跑完脚本里的每一轮，返回 ``(rail, 每轮模型名, 每轮思考深度)``。

    每轮前把 agent 模型重置回档位基准，模拟 adapter 的每请求重置
    （``interface_deep._apply_model_to_react_agent``）—— 不重置的话「保持强档」测不出来。
    """
    if rail is None:
        rail = _routing_rail(_ScriptedClassifier(steps))
    agent = _FakeAgent("deepseek-v4.1-flash")
    models: list[str] = []
    depths: list[str] = []
    for _ in steps:
        agent.model_name = "deepseek-v4.1-flash"
        ctx = _auto_ctx(conv_id, agent)
        await rail.before_invoke(ctx)
        models.append(agent.model_name)
        depths.append(ctx.extra.get("_model_routing_thinking_level", ""))
    return rail, models, depths


def test_session_key_falls_back_to_empty_without_conversation_id():
    """拿不到 conversation_id（team 成员 / 假 ctx）→ 空键，调用方不写档位表。"""
    rail = _rail()
    assert rail._session_key(_ctx_extra({"enable_routing": True})) == ""
    assert rail._session_key(_ctx_session("  sess-a  ", {})) == "sess-a"


def test_session_tier_table_evicts_oldest_beyond_capacity():
    """档位表超容量按 LRU 淘汰，防长跑进程无界增长。"""
    from jiuwenswarm.agents.harness.common.rails.model_routing.model_routing_rail import (
        _SESSION_TIER_CAPACITY,
    )

    rail = _rail()
    for i in range(_SESSION_TIER_CAPACITY + 5):
        rail._set_session_tier(f"s{i}", "weak")

    assert len(rail._session_tiers) == _SESSION_TIER_CAPACITY
    assert "s0" not in rail._session_tiers
    assert f"s{_SESSION_TIER_CAPACITY + 4}" in rail._session_tiers


# ---- 弱档思考深度带 ---- #


@pytest.mark.parametrize(
    "score,expected_depth",
    [(5, "off"), (10, "off"), (15, "low"), (20, "low"), (25, "medium"), (30, "medium"), (40, "high")],
)
def test_depth_for_score_bands(score, expected_depth):
    """深度带落在真实分数上：5/10→off、15/20→low、25/30→medium、40→high。"""
    assert ModelRoutingRail._depth_for_score(score) == expected_depth


@pytest.mark.parametrize(
    "score,expected_depth",
    [(5, "off"), (10, "off"), (15, "low"), (20, "low"), (25, "medium"), (30, "medium"), (40, "high")],
)
@pytest.mark.asyncio
async def test_weak_tier_keeps_flash_and_maps_depth(score, expected_depth):
    """弱档一律留在 flash 上，只按分数换思考深度 —— 这是「不换模型」的主路径。"""
    rail, models, depths = await _run_session([_coding(score)])

    assert models == ["deepseek-v4.1-flash"]
    assert depths == [expected_depth]
    assert rail._get_session_tier("sess-1") == ("weak", 0)


@pytest.mark.asyncio
async def test_before_model_call_injects_flash_depth():
    """弱档深度经 before_model_call 落到 llm_call_kwargs（deepseek 走 extra_body_thinking_type）。"""
    rail = _routing_rail(_ScriptedClassifier([_coding(40)]))
    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _auto_ctx("sess-depth", agent)

    await rail.before_invoke(ctx)
    await rail.before_model_call(ctx)

    assert ctx.extra["llm_call_kwargs"] == {
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "high",
    }


@pytest.mark.asyncio
async def test_before_model_call_injects_off_for_chat():
    """低分闲聊 → off：直接关掉思考（thinking.type = disabled）。"""
    rail = _routing_rail(_ScriptedClassifier([_chat_easy()]))
    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _auto_ctx("sess-depth-off", agent)

    await rail.before_invoke(ctx)
    await rail.before_model_call(ctx)

    assert ctx.extra["llm_call_kwargs"] == {"extra_body": {"thinking": {"type": "disabled"}}}


@pytest.mark.asyncio
async def test_before_model_call_strong_tier_uses_glm_default_depth():
    """强档是 glm-5.3：不按分数映射深度，走 vendor 默认档（low）。"""
    rail = _routing_rail(_ScriptedClassifier([_coding(65)]))
    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _auto_ctx("sess-depth-strong", agent)

    await rail.before_invoke(ctx)
    await rail.before_model_call(ctx)

    assert ctx.extra["llm_call_kwargs"] == {
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "low",
    }


# ---- 升档（不设去抖） ---- #


@pytest.mark.asyncio
async def test_first_turn_decides_initial_tier():
    """首轮仍用 45 定档：低分进弱档，达标进强档。"""
    _, models, depths = await _run_session([_chat_easy()], conv_id="sess-cold")
    assert models == ["deepseek-v4.1-flash"]
    assert depths == ["off"]

    _, models, depths = await _run_session([_coding(45)], conv_id="sess-hot")
    assert models == ["glm-5.3"]
    assert depths == ["default"]


@pytest.mark.asyncio
async def test_weak_session_upgrades_on_medium_coding():
    """回归：先一句「你好」(chat/easy=5) 再持续做中难度 coding(coding/medium=45)。

    45 是分类器对中难度 coding 的**常态**分，不是罕见高分 —— 升档阈值若高于它，
    这类 session 会被永久锁在弱档。
    """
    _, models, _ = await _run_session([_chat_easy(), _coding(45), _coding(45)])

    assert models == ["deepseek-v4.1-flash", "glm-5.3", "glm-5.3"]


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [45, 60, 65])
async def test_weak_session_upgrades_at_threshold(score):
    """弱档 session：单轮 score >= 45（含边界 45）立即升强档。"""
    rail, models, _ = await _run_session([_chat_easy(), _coding(score)])

    assert models[-1] == "glm-5.3"
    assert rail._get_session_tier("sess-1") == ("strong", 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [0, 5, 25, 40])
async def test_weak_session_holds_below_threshold(score):
    """弱档 session：score < 45（含边界 40）保持弱档，不换模型。"""
    rail, models, _ = await _run_session([_chat_easy(), _coding(score)])

    assert models[-1] == "deepseek-v4.1-flash"
    assert rail._get_session_tier("sess-1") == ("weak", 0)


# ---- 降档（欠债计分制） ---- #


@pytest.mark.asyncio
async def test_single_chat_turn_does_not_drop_strong_tier():
    """强档 session 里夹一轮闲聊（chat/easy=5）不掉档，且该轮仍重新 set_llm。"""
    rail, models, depths = await _run_session([_coding(65), _chat_easy(), _coding(65)])

    assert models == ["glm-5.3", "glm-5.3", "glm-5.3"]
    assert depths == ["default", "default", "default"]
    assert rail._get_session_tier("sess-1") == ("strong", 0)


@pytest.mark.asyncio
async def test_debt_drops_strong_tier_after_three_idle_turns():
    """一句「你好」欠 45-5=40，3 轮欠满 100 → 第 3 轮降回弱档。"""
    rail, models, _ = await _run_session([_coding(65), _chat_easy(), _chat_easy(), _chat_easy()])

    assert models == ["glm-5.3", "glm-5.3", "glm-5.3", "deepseek-v4.1-flash"]
    assert rail._get_session_tier("sess-1") == ("weak", 0)


@pytest.mark.asyncio
async def test_debt_for_near_threshold_score_is_slow():
    """一直 40 分（near-threshold）每轮只欠 5：19 轮欠 95 仍持强档，第 20 轮才降。

    这正是计分制相对「连续 N 轮」的价值 —— 两者都算「一轮低分」，但欠债速度差 8 倍。
    """
    rail, models, _ = await _run_session([_coding(65)] + [_coding(40)] * 19)
    assert models[-1] == "glm-5.3"
    assert rail._get_session_tier("sess-1") == ("strong", 95)

    rail, models, _ = await _run_session([_coding(65)] + [_coding(40)] * 20)
    assert models[-1] == "deepseek-v4.1-flash"
    assert rail._get_session_tier("sess-1") == ("weak", 0)


@pytest.mark.asyncio
async def test_debt_is_cleared_by_high_score():
    """欠债在任意一轮达标时清零：低-低-高-低-低 只欠到 80，不降档。"""
    rail, models, _ = await _run_session(
        [_coding(65), _chat_easy(), _chat_easy(), _coding(65), _chat_easy(), _chat_easy()]
    )

    assert models == ["glm-5.3"] * 6
    assert rail._get_session_tier("sess-1") == ("strong", 80)


@pytest.mark.asyncio
async def test_debt_is_recorded_in_decision_reasoning():
    """欠债数写进决策 reasoning（llm.log 是 E2E 观测面）。"""
    rail = _routing_rail(_ScriptedClassifier([_coding(65), _chat_easy(), _chat_easy()]))
    agent = _FakeAgent("deepseek-v4.1-flash")
    for _ in range(3):
        agent.model_name = "deepseek-v4.1-flash"
        ctx = _auto_ctx("sess-log", agent)
        await rail.before_invoke(ctx)

    assert "80/100" in ctx.extra["model_routing_decision"]["reasoning"]


# ---- 续写指令 ---- #


@pytest.mark.asyncio
async def test_continuation_replays_strong_tier():
    """强档 session 里的「继续」→ 重放强档，不能静默掉回基准模型。"""
    _, models, _ = await _run_session([_coding(65), _cont()])

    assert models == ["glm-5.3", "glm-5.3"]


@pytest.mark.asyncio
async def test_continuation_in_weak_session_keeps_current_model():
    """弱档 session 里的「继续」→ 保持基准模型，档位不变，深度回落 default。"""
    rail, models, depths = await _run_session([_chat_easy(), _cont()])

    assert models == ["deepseek-v4.1-flash", "deepseek-v4.1-flash"]
    assert depths == ["off", "default"]
    assert rail._get_session_tier("sess-1") == ("weak", 0)


@pytest.mark.asyncio
async def test_continuation_does_not_accumulate_debt():
    """续写指令不参与打分，也不欠债：低-继续-低-低 只欠到 80，仍未降档。"""
    rail, models, _ = await _run_session(
        [_coding(65), _chat_easy(), _cont(), _chat_easy(), _chat_easy()]
    )

    assert models == ["glm-5.3"] * 5
    assert rail._get_session_tier("sess-1") == ("strong", 80)


# ---- 边界 ---- #


@pytest.mark.asyncio
async def test_strong_session_keeps_tier_when_cap_build_fails():
    """强档 session 但 glm-5.3 构造失败 → 留在 flash 并给高深度兜底，档位保留可重试。"""
    rail = _routing_rail(_ScriptedClassifier([_coding(65), _coding(45)]))
    agent = _FakeAgent("deepseek-v4.1-flash")

    await rail.before_invoke(_auto_ctx("sess-fail", agent))  # 首轮成功进强档
    assert rail._get_session_tier("sess-fail") == ("strong", 0)

    rail._model_builder = lambda *_: None  # 之后构造失败
    agent.model_name = "deepseek-v4.1-flash"
    ctx = _auto_ctx("sess-fail", agent)
    await rail.before_invoke(ctx)

    assert agent.model_name == "deepseek-v4.1-flash"
    assert ctx.extra["_model_routing_thinking_level"] == "high"
    assert rail._get_session_tier("sess-fail") == ("strong", 0)


@pytest.mark.asyncio
async def test_session_tiers_are_isolated_per_conversation():
    """档位按 conversation_id 隔离：不同 session 各按自己的档位走。"""
    rail = _routing_rail(_ScriptedClassifier([_chat_easy(), _chat_easy()]))
    agent_weak = _FakeAgent("deepseek-v4.1-flash")
    agent_strong = _FakeAgent("deepseek-v4.1-flash")
    rail._set_session_tier("sess-weak", "weak")
    rail._set_session_tier("sess-strong", "strong")

    await rail.before_invoke(_auto_ctx("sess-weak", agent_weak))
    await rail.before_invoke(_auto_ctx("sess-strong", agent_strong))

    assert agent_weak.model_name == "deepseek-v4.1-flash"
    assert agent_strong.model_name == "glm-5.3"


@pytest.mark.asyncio
async def test_no_conversation_id_stays_stateless():
    """ctx 无 conversation_id（team 成员 / 老链路）→ 每轮独立按 45 决策，不写档位表。"""
    rail = _routing_rail(_ScriptedClassifier([_coding(65), _chat_easy()]))
    agent = _FakeAgent("deepseek-v4.1-flash")

    await rail.before_invoke(
        _ctx_extra({"enable_routing": True, "model_selection": "auto"}, agent=agent)
    )
    assert agent.model_name == "glm-5.3"

    agent.model_name = "deepseek-v4.1-flash"
    await rail.before_invoke(
        _ctx_extra({"enable_routing": True, "model_selection": "auto"}, agent=agent)
    )
    assert agent.model_name == "deepseek-v4.1-flash"  # 第二轮低分 → 独立决策为弱档
    assert rail._session_tiers == {}


@pytest.mark.asyncio
async def test_leaving_auto_clears_session_tier():
    """用户切离 auto（enable_routing 不再为 true）→ 清掉 session 档位与深度。"""
    rail, _, _ = await _run_session([_coding(65)], conv_id="sess-leave")
    assert rail._get_session_tier("sess-leave") == ("strong", 0)

    agent = _FakeAgent("deepseek-v4.1-flash")
    ctx = _ctx_session("sess-leave", {"model_selection": "fast"}, agent=agent)
    await rail.before_invoke(ctx)

    assert rail._get_session_tier("sess-leave") == (None, 0)
    assert ctx.extra["_model_routing_thinking_level"] == "default"
    assert agent.model_name == "deepseek-v4.1-flash"
