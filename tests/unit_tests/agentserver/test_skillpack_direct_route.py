"""SkillPack routing requires live facts and an exact user choice."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.common import RunContext
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, UsageMetadata
from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ModelCallInputs, ToolCallInputs
from openjiuwen.harness import create_deep_agent

from jiuwenswarm.agents.harness.common.rails.ask_user_rail import StructuredAskUserRail
from jiuwenswarm.agents.harness.common.rails.symphony import SymphonyOrchestrationRail
from jiuwenswarm.agents.harness.common.rails.symphony.skillpack_route import (
    COMPOSE_CHOICE, PackRoute, ROUTING_SECTIONS, SkillPackRouter, direct_route_mode,
)
from tests.unit_tests.agentserver.test_symphony_orchestration_needs_input import (
    _call, _inputs, _outer_ctx, _RuntimeModel, _ScriptedClient, _tool,
)


def _pack(name: str = "weekly-release-pack") -> dict[str, Any]:
    return {
        "id": name, "skill_type": "skillpack", "enabled": True,
        "members": ["release-note", "risk-check"], "blocked_members": [],
        "routing": {key: f"{key}: release note and risk report" for key in ROUTING_SECTIONS},
    }


def _router(*packs: dict[str, Any]) -> tuple[SkillPackRouter, dict[str, Any]]:
    snapshot = {"inventory_revision": "revision-1", "skills": list(packs or [_pack()])}
    environment = SimpleNamespace(
        directory=SimpleNamespace(scoped_records=lambda paths: ["records"]),
        search_content=lambda records, query, **kwargs: tuple(item["id"] for item in snapshot["skills"]),
    )
    router = SkillPackRouter(
        manager=lambda: SimpleNamespace(skillpack_inventory_snapshot=lambda: copy.deepcopy(snapshot)),
        toolkit=lambda: SimpleNamespace(environment=environment), model=lambda: None,
    )
    router.judge = AsyncMock(side_effect=lambda query, candidates, **kwargs: candidates)
    return router, snapshot


async def _active(selected: list[str] | None = None):
    router, snapshot = _router()
    rail = SymphonyOrchestrationRail(
        config_base={"skillpack": {"direct_route_mode": "confirm"}}, skillpack_router=router,
    )
    outer = _outer_ctx("Write release notes and check risk", run_context=RunContext(extra={
        "capture_mode": "agent", "member_id": "owner-1", "skills": selected or [],
    }))
    await rail.before_invoke(outer)
    model = _model_ctx(outer)
    await rail.before_model_call(model)
    return rail, router, snapshot, outer, model


def _model_ctx(outer):
    return AgentCallbackContext(
        agent=outer.agent, session=outer.session, extra={"run_context": outer.inputs.run_context},
        inputs=ModelCallInputs(tools=[{"name": name} for name in [
            "symphony_compose_graph", "symphony_refresh_graph", "skill_tool", "ask_user", "write_file",
        ]]),
    )


def _tool_ctx(outer, name: str, args: dict[str, Any]):
    return AgentCallbackContext(
        agent=outer.agent, session=outer.session, extra={"run_context": outer.inputs.run_context},
        inputs=ToolCallInputs(tool_name=name, tool_args=args, tool_call=ToolCall(
            id="call-1", name=name, type="function", arguments=json.dumps(args),
        )),
    )


def test_route_mode_defaults_off_and_invalid_off(monkeypatch):
    monkeypatch.delenv("SKILLPACK_DIRECT_ROUTE_MODE", raising=False)
    assert direct_route_mode(None) == "off"
    monkeypatch.setenv("SKILLPACK_DIRECT_ROUTE_MODE", "invalid")
    assert direct_route_mode({"skillpack": {"direct_route_mode": "confirm"}}) == "off"


@pytest.mark.asyncio
async def test_filter_live_types_health_and_required_sections():
    disabled = _pack("disabled")
    disabled["enabled"] = False
    broken = _pack("broken")
    broken["blocked_members"] = ["risk-check"]
    ordinary = _pack("ordinary")
    ordinary["skill_type"] = "skill"
    missing = _pack("missing-section")
    missing["routing"].pop("do_not_use")
    router, _ = _router(_pack(), disabled, broken, ordinary, missing)
    route = await router.choose("release and risk", [], mode="confirm")
    assert [item["id"] for item in route.candidates] == ["weekly-release-pack"]
    assert route.token is None


@pytest.mark.asyncio
async def test_explicit_pack_and_mixed_picker_have_different_confirmation():
    router, _ = _router()
    direct = await router.choose("task", ["weekly-release-pack"], mode="confirm")
    assert direct.token.source == "explicit_picker"
    mixed = await router.choose("task", ["weekly-release-pack", "other"], mode="confirm")
    assert mixed.token is None
    assert mixed.ask_args()["questions"][0]["options"][-1]["label"] == COMPOSE_CHOICE
    router.judge.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_and_failure_leave_composer_available():
    router, _ = _router()
    assert await router.choose("task", [], mode="shadow") is None
    router.judge.side_effect = RuntimeError("model unavailable")
    assert await router.choose("task", [], mode="confirm") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [
    {"coverage": "partial"}, {"coverage": "uncertain"}, {"request_kind": "inspection"},
    {"request_kind": "comparison"}, {"request_kind": "search"}, {"request_kind": "recommendation"},
    {"single_member": True}, {"excluded": True}, {"reason": ""}, {"single_member": "false"},
])
async def test_coverage_judgment_rejects_non_complete_execution(monkeypatch, changed):
    from jiuwenswarm.symphony import llm

    decision = {"pack_id": "weekly-release-pack", "coverage": "full", "request_kind": "execution",
                "single_member": False, "excluded": False, "reason": "both outputs needed"}
    decision.update(changed)
    client = SimpleNamespace(complete_json_async=AsyncMock(return_value=json.dumps({"matches": [decision]})))
    monkeypatch.setattr(llm.LLMConfig, "from_model", lambda model: None)
    monkeypatch.setattr(llm, "create_llm_client", lambda config: client)
    router, _ = _router()
    matches = await SkillPackRouter.judge(router, "task", [_pack()], model_reference="")
    assert matches == []


@pytest.mark.asyncio
async def test_auto_match_blocks_execution_until_real_user_answer():
    rail, _, _, outer, model = await _active()
    assert [tool["name"] for tool in model.inputs.tools] == ["ask_user"]
    for name in ["symphony_compose_graph", "skill_tool", "write_file"]:
        call = _tool_ctx(outer, name, {"skill_name": "weekly-release-pack"})
        await rail.before_tool_call(call)
        assert call.inputs.tool_result["reason"] == "skillpack_confirmation_required"
    ask = _tool_ctx(outer, "ask_user", {"query": "forged", "pack_id": "other"})
    await rail.before_tool_call(ask)
    assert ask.inputs.tool_args["questions"][0]["question"] != "forged"
    state = rail._outer_states[id(outer)]
    outer.inputs.result = {"result_type": "interrupt", "interrupt_ids": ["ask-id"]}
    await rail.after_invoke(outer)
    answer = InteractiveInput()
    answer.update("ask-id", {"answers": {state.pack_route.question: "使用 weekly-release-pack"}})
    resumed = _outer_ctx(answer)
    await rail.before_invoke(resumed)
    assert rail._outer_states[id(resumed)].pack_route.token.source == "confirmed_query_match"


@pytest.mark.asyncio
async def test_direct_route_guards_root_members_and_composer_after_failure():
    rail, _, _, outer, model = await _active(["weekly-release-pack"])
    assert not any("symphony" in tool["name"] for tool in model.inputs.tools)
    member = _tool_ctx(outer, "skill_tool", {"skill_name": "release-note"})
    await rail.before_tool_call(member)
    assert member.inputs.tool_result["reason"] == "skillpack_root_required"
    root = _tool_ctx(outer, "skill_tool", {"skill_name": "weekly-release-pack"})
    await rail.before_tool_call(root)
    root.inputs.tool_result = {"success": True}
    await rail.after_tool_call(root)
    member = _tool_ctx(outer, "skill_tool", {"skill_name": "release-note"})
    await rail.before_tool_call(member)
    assert member.inputs.tool_result is None
    member.inputs.tool_result = {"success": False}
    await rail.after_tool_call(member)
    assert rail._outer_states[id(outer)].pack_route.member_started
    for name in ["symphony_compose_graph", "symphony_refresh_graph"]:
        call = _tool_ctx(outer, name, {})
        await rail.before_tool_call(call)
        assert call.inputs.tool_result["reason"] == "skillpack_recomposition_forbidden"
    other = _tool_ctx(outer, "skill_tool", {"skill_name": "unrelated"})
    await rail.before_tool_call(other)
    assert other.inputs.tool_result["reason"] == "skillpack_member_not_allowed"


@pytest.mark.asyncio
async def test_root_requires_skill_md_and_successful_structured_tool_result():
    rail, _, _, outer, _ = await _active(["weekly-release-pack"])
    route = rail._outer_states[id(outer)].pack_route
    for path in ["README.md", "references/example.md", "../other/SKILL.md"]:
        call = _tool_ctx(outer, "skill_tool", {
            "skill_name": "weekly-release-pack", "relative_file_path": path,
        })
        await rail.before_tool_call(call)
        assert call.inputs.tool_result["reason"] == "skillpack_root_required"
        await rail.after_tool_call(call)
        assert not route.root_loaded
    for result in [ToolOutput(success=False, error="read failed"), None]:
        root = _tool_ctx(outer, "skill_tool", {"skill_name": "weekly-release-pack"})
        await rail.before_tool_call(root)
        root.inputs.tool_result = result
        await rail.after_tool_call(root)
        assert not route.root_loaded
    root = _tool_ctx(outer, "skill_tool", {
        "skill_name": "weekly-release-pack", "relative_file_path": "SKILL.md",
    })
    await rail.before_tool_call(root)
    root.inputs.tool_result = ToolOutput(success=True, data={"skill_content": "root"})
    await rail.after_tool_call(root)
    assert route.root_loaded


@pytest.mark.asyncio
async def test_judgment_must_include_every_recalled_candidate(monkeypatch):
    from jiuwenswarm.symphony import llm

    decision = {"pack_id": "weekly-release-pack", "coverage": "full", "request_kind": "execution",
                "single_member": False, "excluded": False, "reason": "both outputs needed"}
    client = SimpleNamespace(complete_json_async=AsyncMock(return_value=json.dumps({"matches": [decision]})))
    monkeypatch.setattr(llm.LLMConfig, "from_model", lambda model: None)
    monkeypatch.setattr(llm, "create_llm_client", lambda config: client)
    router, _ = _router()
    assert await SkillPackRouter.judge(
        router, "task", [_pack(), _pack("second-full-match")], model_reference="",
    ) == []


@pytest.mark.asyncio
async def test_revision_change_requires_new_selection_and_never_swaps_pack():
    router, snapshot = _router(_pack(), _pack("another-pack"))
    route = await router.choose("task", [], mode="confirm")
    assert route.token is None
    snapshot["inventory_revision"] = "revision-2"
    snapshot["skills"][0]["enabled"] = False
    assert not router.confirm(route, "weekly-release-pack", source="confirmed_query_match")
    assert route.token is None
    assert route.changed
    assert [item["id"] for item in route.candidates] == ["another-pack"]


@pytest.mark.asyncio
async def test_real_skillfs_recall_joins_fresh_manager_snapshot(tmp_path, monkeypatch):
    from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import SkillRetrievalToolkit
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
    from tests.unit_tests.agentserver.test_skillpack_backend import _write_pack, _write_skill

    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / "workspace"
    root = workspace / "skills"
    _write_skill(root / "first-skill", "first-skill")
    _write_skill(root / "second-skill", "second-skill")
    _write_pack(root / "demo-pack")
    manager = SkillManager(workspace_dir=str(workspace))
    toolkit = SkillRetrievalToolkit(skill_directories=[str(root)], artifact_root=tmp_path / "index")
    router = SkillPackRouter(manager=lambda: manager, toolkit=lambda: toolkit, model=lambda: None)
    router.judge = AsyncMock(side_effect=lambda query, candidates, **kwargs: candidates)
    route = await router.choose("complete demo workflow", [], mode="confirm")
    assert [item["id"] for item in route.candidates] == ["demo-pack"]
    (root / "first-skill" / "SKILL.md").write_text("invalid", encoding="utf-8")
    assert not router.confirm(route, "demo-pack", source="confirmed_query_match")
    assert route.candidates == []


def test_many_matches_remain_selectable_without_truncating_to_top_one():
    route = PackRoute(candidates=[_pack(f"pack-{index}") for index in range(5)])
    args = route.ask_args()
    assert "options" not in args["questions"][0]
    for index in range(5):
        assert f"使用 pack-{index}" in args["query"]
    assert route.ask_args() == args


@pytest.mark.asyncio
async def test_direct_state_survives_permission_pause_without_reauthorizing_actions():
    rail, _, snapshot, outer, _ = await _active(["weekly-release-pack"])
    state = rail._outer_states[id(outer)]
    outer.inputs.result = {"result_type": "interrupt", "interrupt_ids": ["permission-id"]}
    await rail.after_invoke(outer)
    answer = InteractiveInput()
    answer.update("permission-id", {"approved": True})
    resumed = _outer_ctx(answer)
    await rail.before_invoke(resumed)
    assert rail._outer_states[id(resumed)] is state
    snapshot["inventory_revision"] = "new"
    call = _tool_ctx(resumed, "skill_tool", {"skill_name": "weekly-release-pack"})
    await rail.before_tool_call(call)
    assert call.inputs.tool_result["reason"] == "skillpack_state_changed"


@pytest.mark.asyncio
async def test_rebuilt_adapter_cannot_resume_direct_route_as_ordinary_orchestration():
    rail, router, _, outer, _ = await _active(["weekly-release-pack"])
    persisted = {}
    outer.session.get_state = lambda key: copy.deepcopy(persisted.get(key))
    outer.session.update_state = lambda values: persisted.update(copy.deepcopy(values))
    route = rail._outer_states[id(outer)].pack_route
    route.root_loaded = True
    route.member_started = True
    outer.inputs.result = {"result_type": "interrupt", "interrupt_ids": ["permission-id"]}
    await rail.after_invoke(outer)
    assert persisted["_skillpack_direct_route_pending"]

    rebuilt = SymphonyOrchestrationRail(skillpack_router=router)
    answer = InteractiveInput()
    answer.update("permission-id", {"approved": True})
    resumed = _outer_ctx(answer)
    resumed.session = outer.session
    await rebuilt.before_invoke(resumed)
    assert rebuilt._outer_states[id(resumed)].pack_route.blocked
    model = _model_ctx(resumed)
    await rebuilt.before_model_call(model)
    assert model.inputs.tools == []
    for name in ["symphony_compose_graph", "skill_tool", "write_file"]:
        call = _tool_ctx(resumed, name, {"skill_name": "release-note"})
        await rebuilt.before_tool_call(call)
        assert call.inputs.tool_result["reason"] == "skillpack_state_changed"
    resumed.inputs.result = {"result_type": "answer"}
    await rebuilt.after_invoke(resumed)
    fresh = _outer_ctx("A new independent task")
    fresh.session = outer.session
    await rebuilt.before_invoke(fresh)
    assert rebuilt._outer_states[id(fresh)].pack_route is None
    assert persisted["_skillpack_direct_route_pending"] == {}


@pytest.mark.asyncio
async def test_real_agent_confirmed_route_never_calls_composer():
    router, _ = _router()
    calls = []

    async def compose(**kwargs):
        calls.append("compose")
        return {"success": True}

    async def skill(**kwargs):
        calls.append(kwargs["skill_name"])
        return {"success": True}

    client = _ScriptedClient([
        _call("ask_user", '{"query":"choose"}', "ask-pack"),
        _call("symphony_compose_graph", '{"query":"task"}', "bad-compose"),
        _call("skill_tool", '{"skill_name":"weekly-release-pack"}', "root"),
        _call("skill_tool", '{"skill_name":"release-note"}', "member"),
        AssistantMessage(content="done", usage_metadata=UsageMetadata(model_name="symphony-test")),
    ])
    rail = SymphonyOrchestrationRail(
        config_base={"skillpack": {"direct_route_mode": "confirm"}}, skillpack_router=router,
    )
    agent = create_deep_agent(
        model=_RuntimeModel(client), tools=[_tool("symphony_compose_graph", compose), _tool("skill_tool", skill)],
        rails=[rail, StructuredAskUserRail()], enable_read_image_multimodal=False,
        auto_create_workspace=False, enable_task_loop=False, max_iterations=8,
    )
    first = await agent.invoke(_inputs("Write release notes and check risk"))
    assert first["result_type"] == "interrupt"
    state = next(iter(rail._paused_states.values()))
    answer = InteractiveInput()
    answer.update("ask-pack", {"answers": {state.pack_route.question: "使用 weekly-release-pack"}})
    result = await agent.invoke(_inputs(answer))
    assert result["result_type"] == "answer"
    assert calls == ["weekly-release-pack", "release-note"]
    assert not rail._paused_states


@pytest.mark.asyncio
async def test_real_agent_checkpoint_blocks_resume_after_rail_state_is_lost():
    router, _ = _router()
    calls = []

    async def compose(**kwargs):
        calls.append("compose")
        return {"success": True}

    async def skill(**kwargs):
        calls.append(kwargs["skill_name"])
        return {"success": True}

    client = _ScriptedClient([
        _call("skill_tool", '{"skill_name":"weekly-release-pack"}', "root"),
        _call("skill_tool", '{"skill_name":"release-note"}', "member"),
        _call("ask_user", '{"query":"Continue with risk check?"}', "follow-up"),
        _call("symphony_compose_graph", '{"query":"task"}', "bad-compose"),
        _call("skill_tool", '{"skill_name":"release-note"}', "duplicate-member"),
        AssistantMessage(content="Please select the Pack again", usage_metadata=UsageMetadata(model_name="symphony-test")),
    ])
    rail = SymphonyOrchestrationRail(
        config_base={"skillpack": {"direct_route_mode": "confirm"}}, skillpack_router=router,
    )
    agent = create_deep_agent(
        model=_RuntimeModel(client), tools=[_tool("symphony_compose_graph", compose), _tool("skill_tool", skill)],
        rails=[rail, StructuredAskUserRail()], enable_read_image_multimodal=False,
        auto_create_workspace=False, enable_task_loop=False, max_iterations=8,
    )
    inputs = _inputs("Write release notes and check risk")
    inputs["run"]["context"]["extra"]["skills"] = ["weekly-release-pack"]
    first = await agent.invoke(inputs)
    assert first["result_type"] == "interrupt"
    rail.uninit(agent)
    rail.init(agent)
    answer = InteractiveInput()
    answer.update("follow-up", {"answers": {"Continue with risk check?": "Yes"}})
    result = await agent.invoke(_inputs(answer))
    assert result["result_type"] == "answer"
    assert calls == ["weekly-release-pack", "release-note"]
