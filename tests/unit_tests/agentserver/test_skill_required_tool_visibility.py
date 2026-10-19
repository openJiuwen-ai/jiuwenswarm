# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for ToolCard-declared skill-gated tools."""

from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ToolCallInputs,
)

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
    ProgressiveToolRail,
)
from jiuwenswarm.agents.harness.common.rails.skill_active_state import (
    SkillActiveStateRail,
    clear_session_skill_state,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.execution import (
    deepresearch_execute,
)
from jiuwenswarm.agents.harness.common.tools.invoke_tool_tool import (
    InvokeToolInput,
)
from jiuwenswarm.agents.harness.common.tools.tools_search_tool import (
    ToolsSearchInput,
)

_MISSING = object()


def _assert_tool_not_available_result(result: object) -> None:
    """skip 拒绝结果已统一为 schema dict：display 保留原文本前缀。"""
    assert isinstance(result, dict)
    assert result["success"] is False
    assert result["error_code"] == "tool_not_available"
    assert result["display"].startswith("[TOOL_NOT_AVAILABLE]")


def _card(name: str, required_skill: object = _MISSING) -> ToolCard:
    properties = {}
    if required_skill is not _MISSING:
        properties["required_skill"] = required_skill
    return ToolCard(
        id=f"id-{name}",
        name=name,
        description=f"description-{name}",
        input_params={},
        properties=properties,
    )


def _agent(cards: list[ToolCard]) -> SimpleNamespace:
    return SimpleNamespace(
        ability_manager=SimpleNamespace(list=lambda: list(cards)),
        system_prompt_builder=None,
    )


def _model_ctx(agent: SimpleNamespace, cards: list[ToolCard]) -> SimpleNamespace:
    return SimpleNamespace(
        agent=agent,
        inputs=SimpleNamespace(tools=list(cards)),
    )


def _tool_call_ctx(agent: SimpleNamespace, tool_name: str) -> SimpleNamespace:
    tool_call = SimpleNamespace(id="call-gated", name=tool_name, arguments={})
    return SimpleNamespace(
        agent=agent,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name=tool_name,
            tool_args={},
            tool_result=None,
            tool_msg=None,
        ),
        extra={},
    )


def test_main_progressive_builder_does_not_force_skill_gated_tool_eager() -> None:
    """A direct-only HITL tool must not bypass its skill activation gate."""
    pytest.importorskip("google.genai")
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        build_progressive_tool_rail_from_config,
    )

    rail = build_progressive_tool_rail_from_config(
        {
            "tool_lazy_load": {
                "enabled": True,
                "eager_tools": ["read_file"],
            }
        },
        language="cn",
    )

    assert rail is not None
    assert "deepresearch_execute" not in rail.eager_tools


def test_deepresearch_tool_declares_its_required_skill() -> None:
    assert deepresearch_execute.card.properties["required_skill"] == "deepresearch"


@pytest.mark.asyncio
async def test_gated_tool_moves_from_hidden_to_direct_only_after_activation() -> None:
    active_skill = {"name": None}
    ordinary_eager = _card("read_file")
    ordinary_deferred = _card("ordinary_deferred")
    gated = _card("gated_direct", "research-skill")
    cards = [ordinary_eager, ordinary_deferred, gated]
    agent = _agent(cards)
    navigation_sections: list[object] = []
    agent.system_prompt_builder = SimpleNamespace(
        add_section=navigation_sections.append,
    )
    rail = ProgressiveToolRail(
        eager_tools=["read_file"],
        active_skill_provider=lambda: active_skill["name"],
    )
    rail.init(agent)

    before = _model_ctx(agent, cards)
    await rail.before_model_call(before)

    assert [tool.name for tool in before.inputs.tools] == ["read_file"]
    assert [tool.name for tool in rail._cached_deferred_tool_infos] == [
        "ordinary_deferred"
    ]
    assert navigation_sections
    navigation = str(navigation_sections[0].content)
    assert "ordinary_deferred" in navigation
    assert "gated_direct" not in navigation
    hidden_search = await rail._search_tools(
        None,
        ToolsSearchInput(tool_name="gated_direct"),
    )
    hidden_invoke = await rail._invoke_target_tool(
        None,
        InvokeToolInput(tool_name="gated_direct", arguments={}),
    )
    assert hidden_search["success"] is False
    assert hidden_search["matches"] == []
    assert hidden_invoke["success"] is False

    active_skill["name"] = "research-skill"
    after = _model_ctx(agent, cards)
    await rail.before_model_call(after)

    assert [tool.name for tool in after.inputs.tools] == [
        "read_file",
        "gated_direct",
    ]
    assert [tool.name for tool in rail._cached_deferred_tool_infos] == [
        "ordinary_deferred"
    ]
    still_not_wrapped = await rail._invoke_target_tool(
        None,
        InvokeToolInput(tool_name="gated_direct", arguments={}),
    )
    assert still_not_wrapped["success"] is False


@pytest.mark.asyncio
async def test_missing_required_skill_preserves_existing_visibility() -> None:
    ordinary_eager = _card("ordinary_eager")
    ordinary_deferred = _card("ordinary_deferred")
    cards = [ordinary_eager, ordinary_deferred]
    agent = _agent(cards)
    rail = ProgressiveToolRail(
        eager_tools=["ordinary_eager"],
        active_skill_provider=lambda: None,
    )
    rail.init(agent)

    ctx = _model_ctx(agent, cards)
    await rail.before_model_call(ctx)

    assert [tool.name for tool in ctx.inputs.tools] == ["ordinary_eager"]
    assert [tool.name for tool in rail._cached_deferred_tool_infos] == [
        "ordinary_deferred"
    ]


@pytest.mark.parametrize(
    "invalid_required_skill",
    [
        None,
        "",
        " ",
        42,
        ".",
        "..",
        "../research",
        "research/skill",
        "research\\skill",
    ],
)
@pytest.mark.asyncio
async def test_invalid_required_skill_fails_closed_everywhere(
    invalid_required_skill: object,
) -> None:
    gated = _card("invalid_gate", invalid_required_skill)
    agent = _agent([gated])
    rail = ProgressiveToolRail(
        eager_tools=["invalid_gate"],
        active_skill_provider=lambda: str(invalid_required_skill),
    )
    rail.init(agent)

    ctx = _model_ctx(agent, [gated])
    await rail.before_model_call(ctx)
    search = await rail._search_tools(
        None,
        ToolsSearchInput(tool_name="invalid_gate"),
    )
    invoke = await rail._invoke_target_tool(
        None,
        InvokeToolInput(tool_name="invalid_gate", arguments={}),
    )

    assert ctx.inputs.tools == []
    assert rail._cached_deferred_tool_infos == []
    assert search["success"] is False
    assert search["matches"] == []
    assert invoke["success"] is False


@pytest.mark.parametrize(
    "required_skill",
    [
        "Research-Skill",
        "research_skill",
        "openJiuwen-DeepSearch",
    ],
)
@pytest.mark.asyncio
async def test_required_skill_uses_runtime_skill_name_rules(
    required_skill: str,
) -> None:
    gated = _card("gated_direct", required_skill)
    agent = _agent([gated])
    rail = ProgressiveToolRail(
        eager_tools=[],
        active_skill_provider=lambda: required_skill,
    )
    rail.init(agent)

    ctx = _model_ctx(agent, [gated])
    await rail.before_model_call(ctx)

    assert [tool.name for tool in ctx.inputs.tools] == ["gated_direct"]


@pytest.mark.asyncio
async def test_gate_remains_active_when_lazy_loading_is_disabled() -> None:
    pytest.importorskip("google.genai")
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        build_progressive_tool_rail_from_config,
    )

    gated = _card("gated_direct", "research-skill")
    ordinary = _card("ordinary")
    agent = _agent([gated, ordinary])
    active_skill = {"name": None}
    rail = build_progressive_tool_rail_from_config(
        {"tool_lazy_load": {"enabled": False}},
        language="cn",
        active_skill_provider=lambda: active_skill["name"],
    )

    assert rail is not None
    assert rail.enabled is False
    rail.init(agent)
    ctx = _model_ctx(agent, [gated, ordinary])
    await rail.before_model_call(ctx)

    assert [tool.name for tool in ctx.inputs.tools] == ["ordinary"]

    active_skill["name"] = "research-skill"
    active_ctx = _model_ctx(agent, [gated, ordinary])
    await rail.before_model_call(active_ctx)

    assert [tool.name for tool in active_ctx.inputs.tools] == [
        "gated_direct",
        "ordinary",
    ]


@pytest.mark.asyncio
async def test_gate_remains_active_for_non_allowlisted_model() -> None:
    gated = _card("gated_direct", "research-skill")
    ordinary = _card("ordinary")
    agent = _agent([gated, ordinary])
    agent.model_name = "gpt-6"
    active_skill = {"name": None}
    rail = ProgressiveToolRail(
        eager_tools=[],
        enable_for_models=["glm"],
        active_skill_provider=lambda: active_skill["name"],
    )
    rail.init(agent)

    ctx = _model_ctx(agent, [gated, ordinary])
    await rail.before_model_call(ctx)

    assert [tool.name for tool in ctx.inputs.tools] == ["ordinary"]

    active_skill["name"] = "research-skill"
    active_ctx = _model_ctx(agent, [gated, ordinary])
    await rail.before_model_call(active_ctx)

    assert [tool.name for tool in active_ctx.inputs.tools] == [
        "gated_direct",
        "ordinary",
    ]


@pytest.mark.asyncio
async def test_unregistered_model_tool_is_hidden_when_lazy_loading_is_disabled() -> None:
    registered = _card("registered")
    stale = _card("stale")
    agent = _agent([registered])
    rail = ProgressiveToolRail(enabled=False, eager_tools=[])
    rail.init(agent)
    ctx = _model_ctx(agent, [registered, stale])

    await rail.before_model_call(ctx)

    assert [tool.name for tool in ctx.inputs.tools] == ["registered"]


@pytest.mark.asyncio
async def test_other_skill_and_disabled_override_keep_gated_tool_hidden() -> None:
    gated = _card("gated_direct", "research-skill")
    agent = _agent([gated])

    other_skill = ProgressiveToolRail(
        eager_tools=[],
        active_skill_provider=lambda: "other-skill",
    )
    other_skill.init(agent)
    other_ctx = _model_ctx(agent, [gated])
    await other_skill.before_model_call(other_ctx)
    assert other_ctx.inputs.tools == []

    disabled = ProgressiveToolRail(
        eager_tools=[],
        active_skill_provider=lambda: "research-skill",
        disabled_tools=["gated_direct"],
    )
    disabled.init(agent)
    disabled_ctx = _model_ctx(agent, [gated])
    await disabled.before_model_call(disabled_ctx)
    assert disabled_ctx.inputs.tools == []


@pytest.mark.asyncio
async def test_inactive_skill_search_returns_skill_reload_guidance() -> None:
    """tools_search 无匹配且命中门控卡片时返回 skill_tool 重载指引。

    回归续跑降级缺口：skill 未激活（会话中断重置/闲置过期）时
    deferred 搜索缓存被过滤，tools_search 只返回裸「未找到」，
    模型缺乏重载指引而降级到非技能路径。修复后反查全量注册表
    命中 required_skill 卡片，返回与 invoke_tool 门控拒绝分支一致
    的重载口径。
    """
    cards = [deepresearch_execute.card, _card("ordinary_deferred")]
    agent = _agent(cards)
    rail = ProgressiveToolRail(
        eager_tools=["read_file"],
        active_skill_provider=lambda: None,
    )
    rail.init(agent)
    ctx = _model_ctx(agent, cards)
    await rail.before_model_call(ctx)

    result = await rail._search_tools(
        None,
        ToolsSearchInput(tool_name="deepresearch_execute"),
    )

    assert result["success"] is False
    assert result["matches"] == []
    message = str(result["message"])
    assert 'skill_name="deepresearch"' in message
    assert "不要据此降级" in message


@pytest.mark.asyncio
async def test_active_skill_search_reports_direct_visibility_without_reload() -> None:
    """技能激活态下 tools_search 命中门控工具不再误报「技能未激活」。

    回归检视意见：declared 卡片被 _deferred_tools 无条件排除，激活态下
    deferred 搜索同样 0 命中；修复前反查全量注册表命中 required_skill 后
    无条件返回 skill_tool 重载指引，诱导模型对已激活技能重复重载。修复后
    先比对当前激活技能：已激活提示直接调用，未激活才给重载指引。
    """
    cards = [deepresearch_execute.card, _card("ordinary_deferred")]
    agent = _agent(cards)
    rail = ProgressiveToolRail(
        eager_tools=["read_file"],
        active_skill_provider=lambda: "deepresearch",
    )
    rail.init(agent)
    ctx = _model_ctx(agent, cards)
    await rail.before_model_call(ctx)

    result = await rail._search_tools(
        None,
        ToolsSearchInput(tool_name="deepresearch_execute"),
    )

    assert result["success"] is False
    assert result["matches"] == []
    message = str(result["message"])
    assert "已随技能 'deepresearch' 激活并直接可见" in message
    assert "直接调用" in message
    assert "skill_tool" not in message


@pytest.mark.asyncio
async def test_ordinary_missing_tool_search_keeps_generic_message() -> None:
    """非门控工具的无匹配保持通用文案，不误报技能重载指引。"""
    ordinary = _card("ordinary_deferred")
    agent = _agent([ordinary])
    rail = ProgressiveToolRail(
        eager_tools=["read_file"],
        active_skill_provider=lambda: None,
    )
    rail.init(agent)
    ctx = _model_ctx(agent, [ordinary])
    await rail.before_model_call(ctx)

    result = await rail._search_tools(
        None,
        ToolsSearchInput(tool_name="missing_ordinary"),
    )

    assert result["success"] is False
    assert result["matches"] == []
    message = str(result["message"])
    assert "skill_tool" not in message
    assert "未找到" in message


@pytest.mark.asyncio
async def test_required_skill_card_in_place_update_invalidates_deferred_cache() -> None:
    active_skill = {"name": "research-skill"}
    original = _card("mutable_tool")
    cards = [original]
    agent = _agent(cards)
    rail = ProgressiveToolRail(
        eager_tools=[],
        active_skill_provider=lambda: active_skill["name"],
    )
    rail.init(agent)
    await rail._refresh_deferred_tool_cache()
    assert [tool.name for tool in rail._cached_deferred_tool_infos] == [
        "mutable_tool"
    ]

    original.properties["required_skill"] = "research-skill"
    await rail._refresh_deferred_tool_cache_if_stale()

    assert rail._cached_deferred_tool_infos == []
    assert rail._cached_all_tool_infos == [original]


@pytest.mark.asyncio
async def test_active_skill_provider_failure_keeps_gated_tool_hidden() -> None:
    gated = _card("gated_direct", "research-skill")
    agent = _agent([gated])

    def _fail_provider() -> str:
        raise RuntimeError("state unavailable")

    rail = ProgressiveToolRail(
        eager_tools=[],
        active_skill_provider=_fail_provider,
    )
    rail.init(agent)
    ctx = _model_ctx(agent, [gated])

    await rail.before_model_call(ctx)

    assert ctx.inputs.tools == []


@pytest.mark.asyncio
async def test_replayed_direct_call_is_skipped_before_tool_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gated = _card("gated_direct", "research-skill")
    manager = AbilityManager()
    manager.add(gated)
    callbacks = AgentCallbackManager(f"required-skill-{uuid4().hex}")
    agent = SimpleNamespace(
        ability_manager=manager,
        agent_callback_manager=callbacks,
        card=SimpleNamespace(id="required-skill-agent"),
    )
    rail = ProgressiveToolRail(
        eager_tools=[],
        active_skill_provider=lambda: None,
    )
    await callbacks.register_rail(rail, agent)
    native_execute = AsyncMock(
        return_value=(
            "executed",
            ToolMessage(content="executed", tool_call_id="call-gated"),
        )
    )
    monkeypatch.setattr(manager, "_execute_single_tool_call", native_execute)
    ctx = AgentCallbackContext(agent=agent, extra={})
    call = ToolCall(
        id="call-gated",
        type="function",
        name="gated_direct",
        arguments="{}",
    )

    result, tool_message = (
        await manager.execute(
            ctx,
            call,
            SimpleNamespace(),
            parallel_tool_calls=False,
        )
    )[0]

    native_execute.assert_not_awaited()
    _assert_tool_not_available_result(result)
    assert tool_message.content.startswith("[TOOL_NOT_AVAILABLE]")


@pytest.mark.asyncio
async def test_registered_card_list_failure_does_not_bypass_direct_gate() -> None:
    manager = SimpleNamespace(
        list=lambda: (_ for _ in ()).throw(RuntimeError("metadata unavailable")),
    )
    agent = SimpleNamespace(ability_manager=manager)
    rail = ProgressiveToolRail(
        eager_tools=[],
        active_skill_provider=lambda: None,
    )
    rail.init(agent)
    ctx = _tool_call_ctx(agent, "gated_direct")

    await rail.before_tool_call(ctx)

    assert ctx.extra["_skip_tool"] is True
    _assert_tool_not_available_result(ctx.inputs.tool_result)


@pytest.mark.asyncio
async def test_registered_card_lookup_failure_fails_closed() -> None:
    manager = SimpleNamespace(
        get=lambda _name: (_ for _ in ()).throw(RuntimeError("lookup unavailable")),
    )
    agent = SimpleNamespace(ability_manager=manager)
    rail = ProgressiveToolRail(
        eager_tools=[],
        active_skill_provider=lambda: None,
    )
    rail.init(agent)
    ctx = _tool_call_ctx(agent, "gated_direct")

    await rail.before_tool_call(ctx)

    assert ctx.extra["_skip_tool"] is True
    _assert_tool_not_available_result(ctx.inputs.tool_result)


@pytest.mark.asyncio
async def test_removed_registered_gated_tool_is_rejected_on_replay() -> None:
    gated = _card("gated_direct", "research-skill")
    cards = [gated]
    agent = _agent(cards)
    rail = ProgressiveToolRail(
        eager_tools=[],
        active_skill_provider=lambda: "research-skill",
    )
    rail.init(agent)
    await rail._refresh_deferred_tool_cache()
    cards.clear()
    ctx = _tool_call_ctx(agent, "gated_direct")

    await rail.before_tool_call(ctx)

    assert ctx.extra["_skip_tool"] is True
    _assert_tool_not_available_result(ctx.inputs.tool_result)


@pytest.mark.asyncio
async def test_matching_active_skill_allows_registered_direct_call() -> None:
    gated = _card("gated_direct", "research-skill")
    agent = _agent([gated])
    rail = ProgressiveToolRail(
        eager_tools=[],
        active_skill_provider=lambda: "research-skill",
    )
    rail.init(agent)
    ctx = _tool_call_ctx(agent, "gated_direct")

    await rail.before_tool_call(ctx)

    assert "_skip_tool" not in ctx.extra


@pytest.mark.asyncio
async def test_adapter_session_activation_exposes_tool_only_in_that_session(tmp_path, monkeypatch) -> None:
    pytest.importorskip("google.genai")
    from openjiuwen.harness.security.skill_authorization import (
        get_skill_grant_store,
        set_skill_grant_store,
    )

    from jiuwenswarm.agents.harness.common.rails import skill_active_state
    from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization import (
        file_store,
    )
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    first_session = "required-skill-first"
    second_session = "required-skill-second"
    sessions_root = tmp_path / "sessions"
    for session_id in (first_session, second_session):
        (sessions_root / session_id).mkdir(parents=True)
    original_store = get_skill_grant_store()
    store = file_store.FileSkillGrantStore(
        sessions_root=lambda: sessions_root, data_root=lambda: tmp_path,
    )
    set_skill_grant_store(store)
    monkeypatch.setattr(file_store, "_session_file_context", ContextVar("test_skill_files", default=None))
    monkeypatch.setattr(skill_active_state, "_current_session_var", ContextVar("test_active_skill_session", default=None))
    active_state = SkillActiveStateRail(session_id=first_session, sessions_root=sessions_root)
    activation_call = SimpleNamespace(
        id="call-skill",
        name="skill_tool",
        arguments={"skill_name": "deepresearch"},
    )
    activation_ctx = SimpleNamespace(
        inputs=ToolCallInputs(
            tool_call=activation_call,
            tool_name="skill_tool",
            tool_args={"skill_name": "deepresearch"},
            tool_result="loaded",
            tool_msg=SimpleNamespace(metadata={"skill_name": "deepresearch"}),
        ),
        extra={},
    )

    def _adapter(session_id: str) -> JiuWenSwarmDeepAdapter:
        adapter = object.__new__(JiuWenSwarmDeepAdapter)
        adapter._parent_session_id = session_id
        adapter._is_session_scoped_adapter = True
        adapter._current_request_route = {}
        adapter._runtime_cron_tool_context = SimpleNamespace(session_id=None)
        adapter._resolve_runtime_language = lambda: "cn"
        adapter._tool_owner_id = lambda: f"owner-{session_id}"
        return adapter

    gated = _card("gated_direct", "deepresearch")
    try:
        first_adapter = _adapter(first_session)
        second_adapter = _adapter(second_session)
        first_rail = first_adapter._build_progressive_tool_rail(
            {"tool_lazy_load": {"enabled": True, "eager_tools": []}}
        )
        second_rail = second_adapter._build_progressive_tool_rail(
            {"tool_lazy_load": {"enabled": True, "eager_tools": []}}
        )
        assert first_rail is not None
        assert second_rail is not None
        first_agent = _agent([gated])
        second_agent = _agent([gated])
        first_rail.init(first_agent)
        second_rail.init(second_agent)

        before = _model_ctx(first_agent, [gated])
        await first_rail.before_model_call(before)
        assert before.inputs.tools == []

        await active_state.after_tool_call(activation_ctx)

        first_after = _model_ctx(first_agent, [gated])
        second_after = _model_ctx(second_agent, [gated])
        await first_rail.before_model_call(first_after)
        await second_rail.before_model_call(second_after)
        assert [tool.name for tool in first_after.inputs.tools] == ["gated_direct"]
        assert second_after.inputs.tools == []
    finally:
        clear_session_skill_state(first_session)
        clear_session_skill_state(second_session)
        set_skill_grant_store(original_store)
