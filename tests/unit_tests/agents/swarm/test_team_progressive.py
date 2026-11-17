# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team-member ProgressiveToolRail eager lists and wiring."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
    ProgressiveToolRail,
)
from jiuwenswarm.agents.harness.team.rails.request_scoped_mcp_tools_rail import (
    RequestScopedMcpToolsRail,
)
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers.team_progressive import (
    TEAM_CODE_MODE_EXTRA_EAGER_TOOLS,
    TEAM_LEADER_EXTRA_EAGER_TOOLS,
    TEAM_PROGRESSIVE_TOOL,
    TEAM_SHARED_EAGER_TOOLS,
    TEAM_TEAMMATE_EXTRA_EAGER_TOOLS,
    _build_team_progressive_tool_rail,
    _team_ttse_consult_should_be_eager,
    build_team_eager_tools,
    resolve_team_member_progressive_card_id,
)
from jiuwenswarm.agents.harness.team.rails.team_invoke_aware_permission_rail import (
    synthetic_target_tool_call_from_invoke,
)
from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs


def _card(name: str, tool_id: str | None = None, **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        id=tool_id or f"id-{name}",
        description=f"desc-{name}",
        input_params={},
        **extra,
    )


class _FakeAbilityManager:
    def __init__(self, cards: list[Any]) -> None:
        self._by_name = {str(c.name): c for c in cards}

    def list(self) -> list[Any]:
        return list(self._by_name.values())

    def get(self, name: str) -> Any | None:
        return self._by_name.get(name)

    def add(self, card: Any) -> SimpleNamespace:
        self._by_name[str(card.name)] = card
        return SimpleNamespace(added=True)

    def remove(self, name: str) -> None:
        self._by_name.pop(name, None)


def test_leader_eager_tuple_order_and_role_diff():
    leader = build_team_eager_tools("leader")
    teammate = build_team_eager_tools("teammate")

    assert leader[:2] == ["tools_search", "invoke_tool"]
    assert leader[-1] == "ask_user"
    assert teammate[-1] == "claim_task"
    assert leader[: len(TEAM_SHARED_EAGER_TOOLS)] == list(TEAM_SHARED_EAGER_TOOLS)
    assert teammate[: len(TEAM_SHARED_EAGER_TOOLS)] == list(TEAM_SHARED_EAGER_TOOLS)

    only_leader = set(leader) - set(teammate)
    only_teammate = set(teammate) - set(leader)
    assert only_leader == set(TEAM_LEADER_EXTRA_EAGER_TOOLS)
    assert only_teammate == set(TEAM_TEAMMATE_EXTRA_EAGER_TOOLS)
    assert "verify_task" not in leader
    assert "verify_task" not in teammate
    assert "shutdown_member" not in leader


def test_conditional_ttse_and_task_tool_append_after_role_extras():
    leader = build_team_eager_tools(
        "leader", include_ttse=True, include_task_tool=True
    )
    assert leader[-2:] == ["ttse_consult", "task_tool"]
    assert leader.index("ask_user") < leader.index("ttse_consult")


def test_team_ttse_eager_uses_team_enabled_not_single_agent_enabled():
    """team_enabled=true + enabled=false must still keep ttse_consult eager."""
    assert _team_ttse_consult_should_be_eager(
        {"react": {"ttse": {"enabled": False, "team_enabled": True}}}
    )
    assert not _team_ttse_consult_should_be_eager(
        {"react": {"ttse": {"enabled": True, "team_enabled": False}}}
    )
    assert not _team_ttse_consult_should_be_eager(
        {
            "react": {
                "ttse": {
                    "enabled": False,
                    "team_enabled": True,
                    "inject_enabled": False,
                }
            }
        }
    )


def test_build_rail_includes_ttse_consult_when_team_ttse_on():
    ctx = SwarmBuildContext(
        session_id="sess-ttse",
        member_card_id="team_leader",
        member_name="leader",
        role="leader",
        language="cn",
        config={
            "react": {
                "tool_lazy_load": {
                    "enabled": True,
                    "enable_for_models": ["deepseek"],
                },
                "ttse": {"enabled": False, "team_enabled": True},
            }
        },
    )
    rail = _build_team_progressive_tool_rail({}, ctx)
    assert "ttse_consult" in rail.eager_tools


@pytest.mark.asyncio
async def test_team_ttse_consult_stays_in_schema_after_filter():
    ctx = SwarmBuildContext(
        session_id="sess-ttse2",
        member_card_id="team_m",
        member_name="m",
        role="teammate",
        language="cn",
        config={
            "react": {
                "tool_lazy_load": {
                    "enabled": True,
                    "enable_for_models": ["deepseek"],
                },
                "ttse": {"enabled": False, "team_enabled": True},
            }
        },
    )
    rail = _build_team_progressive_tool_rail({}, ctx)
    cards = [
        _card("tools_search"),
        _card("invoke_tool"),
        _card("claim_task"),
        _card("ttse_consult"),
        _card("todo_list"),
    ]
    am = _FakeAbilityManager(cards)
    agent = SimpleNamespace(
        ability_manager=am,
        system_prompt_builder=SimpleNamespace(
            add_section=lambda s: None,
            has_section=lambda _n: False,
            remove_section=lambda _n: None,
        ),
        model_name="deepseek-chat",
    )
    rail._deep_agent = agent
    rail._runtime_agent = agent
    ctx_call = SimpleNamespace(
        agent=agent, inputs=SimpleNamespace(tools=list(am.list()))
    )
    await rail.before_model_call(ctx_call)
    visible = [t.name for t in ctx_call.inputs.tools]
    assert "ttse_consult" in visible
    assert "todo_list" not in visible


def test_synthetic_invoke_target_unwraps_mcp_exec_command():
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall

    outer = ToolCall(
        id="call-1",
        type="function",
        name="invoke_tool",
        arguments=json.dumps(
            {
                "tool_name": "mcp_exec_command",
                "arguments": {"command": "echo hi"},
            },
            ensure_ascii=False,
        ),
    )
    synthetic = synthetic_target_tool_call_from_invoke(outer)
    assert synthetic is not None
    assert synthetic.name == "mcp_exec_command"
    assert synthetic.id == "call-1"
    assert "echo hi" in synthetic.arguments


@pytest.mark.asyncio
async def test_team_permission_rail_unwraps_invoke_tool_for_adjudication():
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.agent_teams.rails.team_permission_rail import TeamPermissionRail
    from jiuwenswarm.agents.harness.team.rails.team_invoke_aware_permission_rail import (
        TeamInvokeAwarePermissionRail,
    )

    seen: dict[str, Any] = {}

    async def _fake_base(self, ctx):  # noqa: ANN001
        seen["name"] = ctx.inputs.tool_name
        seen["args"] = ctx.inputs.tool_call.arguments

    rail = object.__new__(TeamInvokeAwarePermissionRail)
    outer = ToolCall(
        id="call-ask",
        type="function",
        name="invoke_tool",
        arguments=json.dumps(
            {
                "tool_name": "create_terminal",
                "arguments": {"command": "whoami"},
            }
        ),
    )
    inputs = SimpleNamespace(tool_name="invoke_tool", tool_call=outer)
    with patch.object(TeamPermissionRail, "before_tool_call", _fake_base):
        await TeamInvokeAwarePermissionRail.before_tool_call(
            rail, SimpleNamespace(inputs=inputs, extra={})
        )
    assert seen["name"] == "create_terminal"
    assert "whoami" in seen["args"]
    assert inputs.tool_name == "invoke_tool"
    assert inputs.tool_call is outer


def test_capability_specs_append_progressive_rail_for_team_and_code():
    for mode in ("team", "code.team", "team.plan"):
        rails, _tools = build_member_capability_specs({}, mode, "leader")
        types = [rail.type for rail in rails]
        assert types.count(TEAM_PROGRESSIVE_TOOL) == 1
        assert types[-1] == TEAM_PROGRESSIVE_TOOL
        assert registry.TEAM_PROGRESSIVE_TOOL == TEAM_PROGRESSIVE_TOOL


def test_code_mode_eager_keeps_plan_switch_tools_direct():
    """Deferred switch_mode/exit_plan_mode would bypass code confirm / plan rails."""
    chat = build_team_eager_tools("leader")
    code = build_team_eager_tools("leader", include_code_mode_tools=True)
    assert "switch_mode" not in chat
    assert "exit_plan_mode" not in chat
    assert "enter_plan_mode" not in chat
    for name in TEAM_CODE_MODE_EXTRA_EAGER_TOOLS:
        assert name in code
    assert code[-3:] == list(TEAM_CODE_MODE_EXTRA_EAGER_TOOLS)


def test_build_rail_appends_code_mode_tools_when_mode_is_code_team():
    ctx = SwarmBuildContext(
        session_id="sess-code",
        member_card_id="code_leader",
        member_name="leader",
        role="leader",
        mode="code.team",
        language="cn",
        config={
            "react": {
                "tool_lazy_load": {
                    "enabled": True,
                    "enable_for_models": ["deepseek"],
                },
            }
        },
    )
    rail = _build_team_progressive_tool_rail({}, ctx)
    for name in TEAM_CODE_MODE_EXTRA_EAGER_TOOLS:
        assert name in rail.eager_tools


def test_build_rail_omits_code_mode_tools_for_chat_team():
    ctx = SwarmBuildContext(
        session_id="sess-chat",
        member_card_id="team_leader",
        member_name="leader",
        role="leader",
        mode="team",
        language="cn",
        config={
            "react": {
                "tool_lazy_load": {
                    "enabled": True,
                    "enable_for_models": ["deepseek"],
                },
            }
        },
    )
    rail = _build_team_progressive_tool_rail({}, ctx)
    for name in TEAM_CODE_MODE_EXTRA_EAGER_TOOLS:
        assert name not in rail.eager_tools


def test_resolve_member_card_id_is_session_scoped():
    ctx = SwarmBuildContext(
        session_id="sess-1",
        member_card_id="exam_team_chief",
        member_name="chief",
    )
    assert resolve_team_member_progressive_card_id(ctx) == "exam_team_chief_s_sess-1"


def test_rail_init_keeps_constructor_agent_card_id():
    ctx = SwarmBuildContext(
        session_id="sess-a",
        member_card_id="team_leader",
        member_name="leader",
        role="leader",
        language="cn",
        config={
            "react": {
                "tool_lazy_load": {"enabled": True, "enable_for_models": ["deepseek"]},
            }
        },
    )
    rail = _build_team_progressive_tool_rail({}, ctx)
    assert isinstance(rail, ProgressiveToolRail)
    assert rail.agent_card_id == "team_leader_s_sess-a"
    shared = SimpleNamespace(card=SimpleNamespace(id="shared-agent-card"))
    rail.init(shared)
    assert rail.agent_card_id == "team_leader_s_sess-a"


@pytest.mark.asyncio
async def test_filter_skips_unregistered_role_tools_and_keeps_verify_in_nav():
    eager = build_team_eager_tools("teammate")
    rail = ProgressiveToolRail(
        enabled=True,
        eager_tools=eager,
        agent_card_id="member_s_sess",
        agent_id="member_s_sess",
    )
    registered = [
        _card("tools_search"),
        _card("invoke_tool"),
        _card("send_message"),
        _card("view_task"),
        _card("claim_task"),
        _card("read_file"),
        _card("verify_task"),
        _card("create_task"),  # must not appear for teammate eager path
        _card("todo_list"),
    ]
    # Teammate ability_manager must not expose create_task (tool_permissions).
    am = _FakeAbilityManager(
        [c for c in registered if c.name != "create_task"]
    )
    agent = SimpleNamespace(
        ability_manager=am,
        system_prompt_builder=SimpleNamespace(
            sections=[],
            _by_name={},
            add_section=lambda s: None,
            has_section=lambda _n: False,
            remove_section=lambda _n: None,
        ),
        model_name="deepseek-chat",
    )
    rail._deep_agent = agent
    rail._runtime_agent = agent
    inputs_tools = list(am.list())
    ctx = SimpleNamespace(
        agent=agent,
        inputs=SimpleNamespace(tools=inputs_tools),
    )

    await rail.before_model_call(ctx)

    visible = [t.name for t in ctx.inputs.tools]
    assert "claim_task" in visible
    assert "create_task" not in visible
    assert "verify_task" not in visible  # deferred: nav only, not schema
    assert "todo_list" not in visible

    deferred_names = {t.name for t in rail._cached_deferred_tool_infos}
    assert "verify_task" in deferred_names
    assert "create_task" not in deferred_names


@pytest.mark.asyncio
async def test_leader_filter_omits_claim_task_even_if_in_eager_tuple():
    """Safety: unregistered claim_task must not be invented for leader."""
    eager = build_team_eager_tools("leader")
    assert "claim_task" not in eager
    rail = ProgressiveToolRail(
        enabled=True,
        eager_tools=eager,
        agent_card_id="leader_s_sess",
    )
    am = _FakeAbilityManager(
        [
            _card("tools_search"),
            _card("invoke_tool"),
            _card("ask_user"),
            _card("create_task"),
            _card("send_message"),
        ]
    )
    agent = SimpleNamespace(
        ability_manager=am,
        system_prompt_builder=SimpleNamespace(
            add_section=lambda s: None,
            has_section=lambda _n: False,
            remove_section=lambda _n: None,
        ),
        model_name="glm-4",
    )
    rail._deep_agent = agent
    ctx = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=list(am.list())))
    await rail.before_model_call(ctx)
    assert "claim_task" not in [t.name for t in ctx.inputs.tools]
    assert "create_task" in [t.name for t in ctx.inputs.tools]


@pytest.mark.asyncio
async def test_lazy_load_disabled_or_model_bypass_keeps_full_schema():
    tools = [_card("bash"), _card("todo_list"), _card("cron_list_jobs")]
    am = _FakeAbilityManager(tools)

    disabled_rail = ProgressiveToolRail(
        enabled=False,
        eager_tools=["tools_search", "invoke_tool", "bash"],
        enable_for_models=["deepseek"],
    )
    agent = SimpleNamespace(
        ability_manager=am,
        system_prompt_builder=SimpleNamespace(
            add_section=lambda s: None,
            has_section=lambda _n: False,
            remove_section=lambda _n: None,
        ),
        model_name="deepseek-chat",
    )
    disabled_rail._deep_agent = agent
    ctx = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=list(tools)))
    await disabled_rail.before_model_call(ctx)
    assert {t.name for t in ctx.inputs.tools} == {"bash", "todo_list", "cron_list_jobs"}

    bypass_rail = ProgressiveToolRail(
        enabled=True,
        eager_tools=["tools_search", "invoke_tool", "bash"],
        enable_for_models=["deepseek"],
    )
    agent.model_name = "gpt-4o"
    bypass_rail._deep_agent = agent
    ctx2 = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=list(tools)))
    await bypass_rail.before_model_call(ctx2)
    assert {t.name for t in ctx2.inputs.tools} == {"bash", "todo_list", "cron_list_jobs"}


@pytest.mark.asyncio
async def test_office_claw_id_only_rebind_keeps_navigation_fingerprint():
    rail = ProgressiveToolRail(
        enabled=True,
        eager_tools=["tools_search", "invoke_tool", "bash"],
        agent_card_id="m_s_s",
    )
    deferred_name = "office_claw_list_wishes"
    old = _card(deferred_name, "id-old")
    new = _card(deferred_name, "id-new")
    bash = _card("bash")
    meta = [_card("tools_search"), _card("invoke_tool"), bash]

    class _SPB:
        def __init__(self) -> None:
            self._by_name: dict = {}
            self.sections: list = []

        def add_section(self, section) -> None:
            self.sections.append(section)
            self._by_name[section.name] = section

        def has_section(self, name) -> bool:
            return name in self._by_name

        def remove_section(self, name) -> None:
            self._by_name.pop(name, None)

    spb = _SPB()
    am = _FakeAbilityManager([*meta, old])
    agent = SimpleNamespace(
        ability_manager=am,
        system_prompt_builder=spb,
        model_name="deepseek-chat",
    )
    rail._deep_agent = agent
    rail._runtime_agent = agent
    ctx = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=list(meta)))
    await rail.before_model_call(ctx)
    fp_before = rail._navigation_name_fingerprint
    assert fp_before is not None
    assert deferred_name in fp_before

    rail.set_office_claw_active_tool_ids(["id-new"])
    am._by_name[deferred_name] = new
    await rail.before_model_call(ctx)
    assert rail._navigation_name_fingerprint == fp_before


@pytest.mark.asyncio
async def test_request_scoped_mcp_pins_member_progressive_rail():
    from jiuwenswarm.common.mcp_config import (
        OfficeClawMcpRegistration,
        publish_request_scoped_mcp_registration,
        revoke_request_scoped_mcp_registration,
    )

    progressive = ProgressiveToolRail(
        eager_tools=["tools_search", "invoke_tool"],
        agent_card_id="member_s_sess",
    )

    tool_id = "office-claw-request-aaa.office-claw.office_claw_list_wishes"
    card = _card("office_claw_list_wishes", tool_id)
    tool = SimpleNamespace(
        card=card,
        _params={"env": {"OFFICE_CLAW_THREAD_ID": "thread-1"}},
    )
    am = _FakeAbilityManager([])
    agent = SimpleNamespace(
        ability_manager=am,
        find_rails_by_type=lambda types: [progressive],
    )
    registration = OfficeClawMcpRegistration(
        request_id="req-1",
        tool_ids=(tool_id,),
        tool_names=("office_claw_list_wishes",),
        tool_instances=(tool,),  # type: ignore[arg-type]
        invocation_id="inv-1",
        session_id="sess",
    )
    publish_request_scoped_mcp_registration(registration)
    try:
        rail = RequestScopedMcpToolsRail("sess")
        ctx = SimpleNamespace(agent=agent, extra={})
        await rail.before_invoke(ctx)
        assert progressive._office_claw_active_tool_ids == frozenset({tool_id})
        assert progressive._office_claw_invocation_id == "inv-1"
        assert progressive._office_claw_delivery_thread_id == "thread-1"

        await rail.after_invoke(ctx)
        assert progressive._office_claw_active_tool_ids is None
    finally:
        revoke_request_scoped_mcp_registration(registration)
