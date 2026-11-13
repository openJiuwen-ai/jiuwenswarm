"""SkillPack routing requires live facts and an exact user choice."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.common import RunContext
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall, UsageMetadata
from openjiuwen.core.foundation.tool import ToolExposure
from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ModelCallInputs, ToolCallInputs
from openjiuwen.harness import create_deep_agent

from jiuwenswarm.agents.harness.common.rails.ask_user_rail import StructuredAskUserRail
from jiuwenswarm.agents.harness.common.rails.ask_user_contract import build_ask_user_interactive_input
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


def _router(*packs: dict[str, Any], skill_root: Path | None = None) -> tuple[SkillPackRouter, dict[str, Any]]:
    snapshot = {"inventory_revision": "revision-1", "skills": list(packs or [_pack()])}
    skills = []
    if skill_root is not None:
        from openjiuwen.core.single_agent.skills.skill_manager import Skill
        from tests.unit_tests.agentserver.test_skillpack_backend import _write_pack, _write_skill

        for item in snapshot["skills"]:
            root = skill_root / item["id"]
            if item.get("skill_type") == "skillpack":
                _write_pack(root, name=item["id"], members=tuple(item["members"]))
                document = root / "SKILL.md"
                text = document.read_text(encoding="utf-8")
                text = text.replace("description: complete demo workflow", "description: " + item.get("description", "complete demo workflow"))
                document.write_text(text + "\n" + item.get("content", ""), encoding="utf-8")
                for member in item["members"]:
                    _write_skill(skill_root / member, member)
            else:
                _write_skill(root, item["id"])
        skills = [Skill(name=root.name, description="test", directory=root) for root in skill_root.iterdir()]
    environment = SimpleNamespace(
        directory=SimpleNamespace(scoped_records=lambda paths: ["records"]),
        search_content=lambda records, query, **kwargs: tuple(item["id"] for item in snapshot["skills"]),
    )
    router = SkillPackRouter(
        manager=lambda: SimpleNamespace(skillpack_inventory_snapshot=lambda **kwargs: copy.deepcopy(snapshot)),
        toolkit=lambda: SimpleNamespace(environment=environment), model=lambda: None,
        skills_for_session=lambda session: skills,
    )
    router.judge = AsyncMock(side_effect=lambda query, candidates, **kwargs: candidates)
    return router, snapshot


def _config(enabled: bool = True):
    return {"symphony": {"evolution": {"flow": {"enabled": enabled}}}}


async def _active(
    selected: list[str] | None = None, *, enabled: bool = True, automatic_skillpack_enabled=None,
    session: Session | None = None,
):
    router, snapshot = _router()
    rail = SymphonyOrchestrationRail(
        config_base=_config(enabled), skillpack_router=router,
        automatic_skillpack_enabled=automatic_skillpack_enabled,
    )
    outer = _outer_ctx("Write release notes and check risk", run_context=RunContext(extra={
        "capture_mode": "agent", "member_id": "owner-1", "skills": selected or [],
    }))
    if session is not None:
        outer.session = session
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


@pytest.mark.parametrize("legacy_mode", ["off", "confirm", "shadow", "invalid"])
def test_route_mode_uses_discovery_setting_not_legacy_env(monkeypatch, legacy_mode):
    monkeypatch.setenv("SKILLPACK_DIRECT_ROUTE_MODE", legacy_mode)
    monkeypatch.setenv("SKILLPACK_PRODUCTIZATION_ENABLED", "false")
    assert direct_route_mode(None) == "off"
    assert direct_route_mode(_config()) == "confirm"
    assert direct_route_mode(_config(False)) == "off"
    assert direct_route_mode({"skillpack": {"direct_route_mode": "confirm"}}) == "off"


@pytest.mark.asyncio
async def test_disabled_discovery_keeps_manual_safe_direct_route():
    rail, router, _, outer, model = await _active(["weekly-release-pack"], enabled=False)
    route = rail._outer_states[id(outer)].pack_route
    assert route.token.source == "explicit_picker"
    router.judge.assert_not_called()
    assert not any("symphony" in tool["name"] for tool in model.inputs.tools)
    member = _tool_ctx(outer, "skill_tool", {"skill_name": "release-note"})
    await rail.before_tool_call(member)
    assert member.inputs.tool_result["reason"] == "skillpack_root_required"


@pytest.mark.asyncio
async def test_disabled_discovery_does_not_search_or_judge():
    router, _ = _router()
    router.snapshot = AsyncMock(side_effect=AssertionError("automatic discovery must be off"))
    assert await router.choose("task", [], mode="off") is None
    router.snapshot.assert_not_called()
    router.judge.assert_not_called()


@pytest.mark.asyncio
async def test_discovery_disabled_during_match_does_not_offer_confirmation():
    router, _ = _router()
    config = _config()

    async def judge(query, candidates, **kwargs):
        config["symphony"]["evolution"]["flow"]["enabled"] = False
        return candidates

    router.judge = judge
    rail = SymphonyOrchestrationRail(config_base=lambda: config, skillpack_router=router)
    outer = _outer_ctx("Write release notes and check risk", run_context=RunContext(extra={
        "capture_mode": "agent", "member_id": "owner-1",
    }))
    await rail.before_invoke(outer)
    model = _model_ctx(outer)
    await rail.before_model_call(model)
    assert rail._outer_states[id(outer)].pack_route is None
    assert any(tool["name"] == "symphony_compose_graph" for tool in model.inputs.tools)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_manual_unhealthy_pack_is_not_silently_recomposed(enabled):
    pack = _pack()
    pack["blocked_members"] = ["risk-check"]
    router, _ = _router(pack)
    route = await router.choose("task", [pack["id"]], mode=direct_route_mode(_config(enabled)))
    assert route.blocked
    assert route.token is None


@pytest.mark.asyncio
async def test_confirmed_execution_survives_discovery_switch_off():
    rail, _, _, outer, _ = await _active(["weekly-release-pack"])
    rail._config_base = _config(False)
    root = _tool_ctx(outer, "skill_tool", {"skill_name": "weekly-release-pack"})
    await rail.before_tool_call(root)
    assert root.inputs.tool_result is None
    root.inputs.tool_result = {"success": True}
    await rail.after_tool_call(root)
    member = _tool_ctx(outer, "skill_tool", {"skill_name": "release-note"})
    await rail.before_tool_call(member)
    assert member.inputs.tool_result is None
    compose = _tool_ctx(outer, "symphony_compose_graph", {})
    await rail.before_tool_call(compose)
    assert compose.inputs.tool_result["reason"] == "skillpack_recomposition_forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_pending_choice_checks_switch_except_manual_picker(manual):
    selected = ["weekly-release-pack", "other"] if manual else []
    rail, _, _, outer, _ = await _active(selected)
    state = rail._outer_states[id(outer)]
    route = state.pack_route
    route.ask_args()
    rail._config_base = _config(False)
    await rail._resume_pack_choice(state, {"call": {"answers": {route.question: "使用 weekly-release-pack"}}})
    if manual:
        assert state.pack_route.token.source == "explicit_picker"
    else:
        assert state.pack_route is route
        assert route.blocked
        assert route.token is None


@pytest.mark.asyncio
@pytest.mark.parametrize("read_error", [False, True])
@pytest.mark.parametrize("manual", [False, True])
async def test_live_switch_blocks_pending_auto_choice_but_not_manual_with_stale_config(manual, read_error):
    persisted_enabled = True

    def read_switch():
        if not persisted_enabled and read_error:
            raise OSError("settings temporarily unavailable")
        return persisted_enabled

    selected = ["weekly-release-pack", "other"] if manual else []
    rail, _, _, outer, _ = await _active(selected, automatic_skillpack_enabled=read_switch)
    state = rail._outer_states[id(outer)]
    route = state.pack_route
    route.ask_args()
    persisted_enabled = False
    assert direct_route_mode(rail._resolve_config_base()) == "confirm"
    await rail._resume_pack_choice(state, {"call": {"answers": {route.question: "使用 weekly-release-pack"}}})
    if manual:
        assert route.token.source == "explicit_picker"
    else:
        assert route.blocked and route.token is None


@pytest.mark.asyncio
@pytest.mark.parametrize("read_error", [False, True])
async def test_live_switch_read_failure_or_off_keeps_confirmed_execution(read_error):
    persisted_enabled = True

    def read_switch():
        if not persisted_enabled and read_error:
            raise OSError("settings temporarily unavailable")
        return persisted_enabled

    rail, _, _, outer, _ = await _active(automatic_skillpack_enabled=read_switch)
    state = rail._outer_states[id(outer)]
    route = state.pack_route
    route.ask_args()
    await rail._resume_pack_choice(state, {"call": {"answers": {route.question: "使用 weekly-release-pack"}}})
    assert route.token.source == "confirmed_query_match"
    persisted_enabled = False
    root = _tool_ctx(outer, "skill_tool", {"skill_name": "weekly-release-pack"})
    await rail.before_tool_call(root)
    assert root.inputs.tool_result is None
    root.inputs.tool_result = {"success": True}
    await rail.after_tool_call(root)
    member = _tool_ctx(outer, "skill_tool", {"skill_name": "release-note"})
    await rail.before_tool_call(member)
    assert member.inputs.tool_result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_live_switch_read_error_disables_only_automatic_discovery(manual):
    def read_switch():
        raise OSError("settings temporarily unavailable")

    selected = ["weekly-release-pack"] if manual else []
    rail, router, _, outer, _ = await _active(selected, automatic_skillpack_enabled=read_switch)
    route = rail._outer_states[id(outer)].pack_route
    if manual:
        assert route.token.source == "explicit_picker"
    else:
        assert route is None
    router.judge.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["model", "tool"])
async def test_disabling_before_prompt_blocks_new_automatic_confirmation(boundary):
    rail, _, _, outer, _ = await _active()
    rail._config_base = _config(False)
    if boundary == "model":
        model = _model_ctx(outer)
        await rail.before_model_call(model)
        assert model.inputs.tools == []
    else:
        ask = _tool_ctx(outer, "ask_user", {"query": "choose"})
        await rail.before_tool_call(ask)
        assert ask.inputs.tool_result["reason"] == "skillpack_state_changed"
    assert rail._outer_states[id(outer)].pack_route.blocked


@pytest.mark.asyncio
async def test_pending_automatic_offer_can_still_decline_after_switch_off():
    rail, _, _, outer, _ = await _active()
    state = rail._outer_states[id(outer)]
    route = state.pack_route
    route.ask_args()
    rail._config_base = _config(False)
    await rail._resume_pack_choice(state, {"call": {"answers": {route.question: COMPOSE_CHOICE}}})
    assert route.declined
    assert state.pack_route is None


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


@pytest.mark.parametrize("changed", [
    {"id": None}, {"id": ""}, {"id": []}, {"skill_type": "swarm_skill"},
    {"enabled": 1}, {"blocked_members": ["risk-check"]},
    {"members": None}, {"members": "release-note"}, {"members": []},
    {"members": ["release-note"]}, {"members": ["release-note", ""]},
    {"members": ["release-note", {}]}, {"members": ["release-note", "release-note"]},
])
def test_eligibility_rejects_malformed_or_unhealthy_facts(changed):
    candidate = _pack()
    candidate.update(changed)
    snapshot = {"skills": [None, candidate, _pack("healthy-pack")]}
    assert list(SkillPackRouter.eligible(snapshot)) == ["healthy-pack"]


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
    {"reason": None}, {"reason": "  "}, {"reason": {}}, {"excluded": 0},
])
async def test_coverage_judgment_rejects_non_complete_execution(monkeypatch, changed):
    from jiuwenswarm.runtime.symphony import llm

    decision = {"pack_id": "weekly-release-pack", "coverage": "full", "request_kind": "execution",
                "single_member": False, "excluded": False, "reason": "both outputs needed"}
    decision.update(changed)
    client = SimpleNamespace(invoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps({"matches": [decision]}))))
    monkeypatch.setattr(llm.LLMConfig, "from_model", lambda model: llm.LLMConfig(model="route-test"))
    monkeypatch.setattr(llm.LLMConfig, "create_model", lambda config: client)
    router, _ = _router()
    matches = await SkillPackRouter.judge(router, "task", [_pack()], model_reference="")
    assert matches == []


@pytest.mark.asyncio
@pytest.mark.parametrize("request_scoped", [False, True])
async def test_native_model_match_uses_current_model_and_still_requires_confirmation(monkeypatch, request_scoped):
    from openjiuwen.core.foundation.llm.request_context import is_disabled_thinking_fallback_allowed
    from jiuwenswarm.runtime.symphony import llm

    decision = {"pack_id": "weekly-release-pack", "coverage": "full", "request_kind": "execution",
                "single_member": False, "excluded": False, "reason": "both outputs needed"}
    captured = []

    async def invoke(**kwargs):
        assert is_disabled_thinking_fallback_allowed()
        captured.append(kwargs)
        return AssistantMessage(content='```json\n' + json.dumps({"matches": [decision]}) + '\n```')

    client = SimpleNamespace(invoke=AsyncMock(side_effect=invoke))
    config = llm.LLMConfig(model="route-test")
    monkeypatch.setattr(llm.LLMConfig, "create_model", lambda selected: client if selected is config else None)
    if request_scoped:
        monkeypatch.setattr(llm, "resolve_request_llm_config", lambda ref: config if ref == "request-model" else None)
        monkeypatch.setattr(llm.LLMConfig, "from_model", lambda _: pytest.fail("request model must not fall back"))
    else:
        monkeypatch.setattr(llm.LLMConfig, "from_model", lambda _: config)
    router, _ = _router()
    del router.judge
    route = await router.choose(
        "release notes and risk report", [], mode="confirm",
        model_reference="request-model" if request_scoped else "",
    )
    assert route is not None and route.token is None
    assert [item["id"] for item in route.candidates] == ["weekly-release-pack"]
    assert len(captured) == 1 and captured[0]["timeout"] == 60
    assert captured[0]["max_tokens"] == 512
    assert [message["role"] for message in captured[0]["messages"]] == ["system", "user"]
    request = json.loads(captured[0]["messages"][1]["content"])
    assert request["query"] == "release notes and risk report"
    assert request["candidates"][0]["pack_id"] == "weekly-release-pack"
    assert not is_disabled_thinking_fallback_allowed()


@pytest.mark.asyncio
async def test_native_model_failure_returns_to_existing_composition(monkeypatch):
    from jiuwenswarm.runtime.symphony import llm

    client = SimpleNamespace(invoke=AsyncMock(side_effect=TimeoutError("model unavailable")))
    monkeypatch.setattr(llm.LLMConfig, "from_model", lambda _: llm.LLMConfig(model="route-test"))
    monkeypatch.setattr(llm.LLMConfig, "create_model", lambda _: client)
    router, _ = _router()
    del router.judge
    assert await router.choose("release notes and risk report", [], mode="confirm") is None
    client.invoke.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("padding", ["", "  "])
async def test_auto_match_blocks_execution_until_real_user_answer(padding):
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
    answer.update("ask-id", {"answers": {state.pack_route.question: f"{padding}使用 weekly-release-pack{padding}"}})
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
    from jiuwenswarm.runtime.symphony import llm

    decision = {"pack_id": "weekly-release-pack", "coverage": "full", "request_kind": "execution",
                "single_member": False, "excluded": False, "reason": "both outputs needed"}
    client = SimpleNamespace(invoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps({"matches": [decision]}))))
    monkeypatch.setattr(llm.LLMConfig, "from_model", lambda model: llm.LLMConfig(model="route-test"))
    monkeypatch.setattr(llm.LLMConfig, "create_model", lambda config: client)
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
    assert not await router.confirm(route, "weekly-release-pack", source="confirmed_query_match")
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
    assert not await router.confirm(route, "demo-pack", source="confirmed_query_match")
    assert route.candidates == []


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["choose", "confirm", "tool_guard"])
async def test_route_inventory_io_does_not_block_event_loop(monkeypatch, phase):
    rail, router, snapshot, outer, _ = await _active(["weekly-release-pack"])
    started, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    worker_threads = []

    def slow_snapshot(*, lock_timeout):
        assert lock_timeout == 5
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(5)
        return copy.deepcopy(snapshot)

    monkeypatch.setattr(router, "manager", lambda: SimpleNamespace(skillpack_inventory_snapshot=slow_snapshot))
    route = PackRoute(candidates=[_pack()], inventory_revision="revision-1")
    if phase == "choose":
        action = router.choose("task", [], mode="confirm")
    elif phase == "confirm":
        action = router.confirm(route, "weekly-release-pack", source="confirmed_query_match")
    else:
        call = _tool_ctx(outer, "skill_tool", {"skill_name": "weekly-release-pack"})
        action = rail.before_tool_call(call)
    task = asyncio.create_task(action)
    try:
        assert await asyncio.to_thread(started.wait, 3)
        await asyncio.sleep(0)
        assert not task.done()
        assert worker_threads and all(thread != loop_thread for thread in worker_threads)
    finally:
        release.set()
        await task
    if phase == "confirm":
        assert route.token is not None
    elif phase == "tool_guard":
        assert call.inputs.tool_result is None


@pytest.mark.asyncio
async def test_concurrent_route_snapshots_wait_then_detect_content_changes(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.skill import skillpack_inventory
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
    from tests.unit_tests.agentserver.test_skillpack_backend import _write_pack, _write_skill

    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / "workspace"
    root = workspace / "skills"
    _write_skill(root / "first-skill", "first-skill")
    _write_skill(root / "second-skill", "second-skill")
    _write_pack(root / "demo-pack")
    payload = root / "first-skill" / "helper.py"
    payload.write_text("old", encoding="utf-8")
    manager = SkillManager(workspace_dir=str(workspace))
    router = SkillPackRouter(manager=lambda: manager, toolkit=lambda: None, model=lambda: None)
    second_router = SkillPackRouter(manager=lambda: manager, toolkit=lambda: None, model=lambda: None)
    first_route = await router.choose("task", ["demo-pack"], mode="confirm")
    second_route = await second_router.choose("task", ["demo-pack"], mode="confirm")
    started, release = threading.Event(), threading.Event()
    original = skillpack_inventory.skillpack_inventory_snapshot
    calls = []

    def slow_snapshot(*args, **kwargs):
        calls.append(threading.get_ident())
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(skillpack_inventory, "skillpack_inventory_snapshot", slow_snapshot)
    first = asyncio.create_task(router.is_current(first_route))
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 3)
        second = asyncio.create_task(second_router.is_current(second_route))
        await asyncio.sleep(0.05)
        assert not second.done()
    finally:
        release.set()
        assert await first
        if second is not None:
            assert await second
    assert len(calls) == 2
    assert not first_route.blocked and not second_route.blocked

    # Same length and mtime must not bypass the full content check.
    before = payload.stat()
    payload.write_text("new", encoding="utf-8")
    os.utime(payload, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert not await router.is_current(first_route)
    assert first_route.blocked
    # A subsequent snapshot also succeeds, proving the worker released its lock.
    current = await router.snapshot()
    assert current["inventory_revision"] != second_route.token.inventory_revision


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
    session = Session(session_id="s-1")
    rail, router, _, outer, _ = await _active(["weekly-release-pack"], session=session)
    route = rail._outer_states[id(outer)].pack_route
    route.root_loaded = True
    route.member_started = True
    outer.inputs.result = {"result_type": "interrupt", "interrupt_ids": ["permission-id"]}
    await rail.after_invoke(outer)
    assert any(session.get_state("_skillpack_direct_route_pending").values())

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
    assert not any(session.get_state("_skillpack_direct_route_pending").values())


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", [COMPOSE_CHOICE, "使用 weekly-release-pack"])
@pytest.mark.parametrize("rebuild", [False, True])
async def test_real_session_guard_clears_after_completed_choice(choice, rebuild):
    session = Session(session_id="s-1")
    rail, router, _, outer, _ = await _active(session=session)
    state = rail._outer_states[id(outer)]
    ask = _tool_ctx(outer, "ask_user", {})
    await rail.before_tool_call(ask)
    outer.inputs.result = {"result_type": "interrupt", "interrupt_ids": ["call-1"]}
    await rail.after_invoke(outer)
    assert any(session.get_state("_skillpack_direct_route_pending").values())

    answer = build_ask_user_interactive_input("call-1", [{
        "question": state.pack_route.question, "selected_options": [choice],
    }])
    resumed = _outer_ctx(answer)
    resumed.session = session
    await rail.before_invoke(resumed)
    if choice == COMPOSE_CHOICE:
        assert state.pack_route_declined
        assert not any(session.get_state("_skillpack_direct_route_pending").values())
    resumed.inputs.result = {"result_type": "answer", "output": "done"}
    await rail.after_invoke(resumed)
    assert not any(session.get_state("_skillpack_direct_route_pending").values())

    next_rail = SymphonyOrchestrationRail(skillpack_router=router) if rebuild else rail
    fresh = _outer_ctx("Package the session logs")
    fresh.session = session
    await next_rail.before_invoke(fresh)
    assert next_rail._outer_states[id(fresh)].pack_route is None
    call = _tool_ctx(fresh, "write_file", {})
    await next_rail.before_tool_call(call)
    assert call.inputs.tool_result is None


@pytest.mark.asyncio
async def test_real_session_guard_clear_preserves_another_owner():
    session = Session(session_id="s-1")
    rail, _, _, outer, _ = await _active(session=session)
    scope = rail._outer_states[id(outer)].scope
    own_key = rail._pack_guard_scope(scope)
    other_key = json.dumps(["s-1", "agent", "other-owner"])
    session.update_state({"_skillpack_direct_route_pending": {other_key: True}})
    copied = session.get_state("_skillpack_direct_route_pending")
    copied.pop(own_key)
    assert session.get_state("_skillpack_direct_route_pending")[own_key] is True

    rail._save_pack_resume_guard(outer, scope, pending=False)
    stored = session.get_state("_skillpack_direct_route_pending")
    assert stored.get(own_key) is not True
    assert stored[other_key] is True
    assert not rail._has_pack_resume_guard(outer, scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer_text", [
    "用户已跳过此题，未选择任何选项。", "用户已取消本次问答，未作答。",
])
async def test_ui_skip_or_cancel_exits_only_this_pack_offer(answer_text):
    session = Session(session_id="s-1")
    rail, router, _, outer, _ = await _active(session=session)
    state = rail._outer_states[id(outer)]
    route = state.pack_route
    await rail.before_tool_call(_tool_ctx(outer, "ask_user", {}))
    outer.inputs.result = {"result_type": "interrupt", "interrupt_ids": ["call-1"]}
    await rail.after_invoke(outer)
    answer = build_ask_user_interactive_input("call-1", [{
        "question": route.question, "selected_options": [], "custom_input": answer_text,
    }])
    resumed = _outer_ctx(answer)
    resumed.session = session
    await rail.before_invoke(resumed)
    assert state.pack_route is None and state.pack_route_checked and state.pack_route_declined
    assert route.declined and route.token is None
    assert not any(session.get_state("_skillpack_direct_route_pending").values())
    for _ in range(2):
        model = _model_ctx(resumed)
        await rail.before_model_call(model)
        assert any(tool["name"] == "symphony_compose_graph" for tool in model.inputs.tools)
    assert router.judge.call_count == 1
    resumed.inputs.result = {"result_type": "answer"}
    await rail.after_invoke(resumed)

    fresh = _outer_ctx("A new release notes and risk task")
    fresh.session = session
    await rail.before_invoke(fresh)
    model = _model_ctx(fresh)
    await rail.before_model_call(model)
    assert [item["name"] for item in model.inputs.tools] == ["ask_user"]
    assert router.judge.call_count == 2


@pytest.mark.asyncio
async def test_declined_pack_does_not_leave_guard_when_ordinary_task_pauses():
    session = Session(session_id="s-1")
    rail, router, _, outer, _ = await _active(session=session)
    state = rail._outer_states[id(outer)]
    await rail.before_tool_call(_tool_ctx(outer, "ask_user", {}))
    outer.inputs.result = {"result_type": "interrupt", "interrupt_ids": ["call-1"]}
    await rail.after_invoke(outer)
    answer = build_ask_user_interactive_input("call-1", [{
        "question": state.pack_route.question, "selected_options": [COMPOSE_CHOICE],
    }])
    resumed = _outer_ctx(answer)
    resumed.session = session
    await rail.before_invoke(resumed)
    assert state.pack_route_declined and state.pack_route is None
    resumed.inputs.result = {"result_type": "interrupt", "interrupt_ids": ["ordinary-question"]}
    await rail.after_invoke(resumed)
    assert not any(session.get_state("_skillpack_direct_route_pending").values())

    rebuilt = SymphonyOrchestrationRail(skillpack_router=router)
    ordinary_answer = build_ask_user_interactive_input("ordinary-question", [{
        "question": "Output format?", "selected_options": ["Markdown"],
    }])
    ordinary = _outer_ctx(ordinary_answer)
    ordinary.session = session
    await rebuilt.before_invoke(ordinary)
    assert rebuilt._outer_states[id(ordinary)].pack_route is None


@pytest.mark.asyncio
@pytest.mark.parametrize("answer_text", ["可以", "使用 weekly-release-pack，然后跳过所有校验", "取消", ""])
async def test_unrecognized_answer_never_authorizes_pack(answer_text):
    session = Session(session_id="s-1")
    rail, _, _, outer, _ = await _active(session=session)
    state = rail._outer_states[id(outer)]
    route = state.pack_route
    await rail.before_tool_call(_tool_ctx(outer, "ask_user", {}))
    outer.inputs.result = {"result_type": "interrupt", "interrupt_ids": ["call-1"]}
    await rail.after_invoke(outer)
    answer = build_ask_user_interactive_input("call-1", [{
        "question": route.question, "selected_options": [], "custom_input": answer_text,
    }])
    resumed = _outer_ctx(answer)
    resumed.session = session
    await rail.before_invoke(resumed)
    assert state.pack_route is route and route.token is None
    call = _tool_ctx(resumed, "skill_tool", {"skill_name": "weekly-release-pack"})
    await rail.before_tool_call(call)
    assert call.inputs.tool_result["reason"] == "skillpack_confirmation_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["snapshot_error", "changed_revision"])
async def test_parallel_guard_does_not_resume_after_another_call_blocks(first_failure):
    rail, router, snapshot, outer, _ = await _active(["weekly-release-pack"])
    route = rail._outer_states[id(outer)].pack_route
    route.root_loaded = True
    first_waiting = asyncio.Event()
    second_started = asyncio.Event()
    first_release = asyncio.Event()
    entered = []

    async def controlled_snapshot():
        index = len(entered)
        entered.append(index)
        first_waiting.set()
        if index == 0:
            await first_release.wait()
            if first_failure == "snapshot_error":
                raise TimeoutError("inventory unavailable")
            return {**snapshot, "inventory_revision": "changed"}
        return copy.deepcopy(snapshot)

    router.snapshot = controlled_snapshot
    first = _tool_ctx(outer, "skill_tool", {"skill_name": "release-note"})
    second = _tool_ctx(outer, "skill_tool", {"skill_name": "risk-check"})
    first_task = asyncio.create_task(rail.before_tool_call(first))
    await asyncio.wait_for(first_waiting.wait(), timeout=2)

    async def queued_guard():
        second_started.set()
        await rail.before_tool_call(second)

    second_task = asyncio.create_task(queued_guard())
    try:
        await asyncio.wait_for(second_started.wait(), timeout=2)
        assert entered == [0]
        first_release.set()
        await first_task
        assert first.inputs.tool_result["reason"] == "skillpack_state_changed"
        await second_task
        assert second.inputs.tool_result["reason"] == "skillpack_state_changed"
        assert route.blocked and not route.member_started
        assert entered == [0]
    finally:
        first_release.set()
        await asyncio.gather(first_task, second_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("addressed_by", ["tool_call_id", "interrupt_id"])
async def test_real_agent_confirmed_route_never_calls_composer(addressed_by):
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
        config_base=_config(), skillpack_router=router,
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
    value = {"answers": {state.pack_route.question: "使用 weekly-release-pack"}}
    if addressed_by == "interrupt_id":
        answer.answer(first["state"][0].payload.value.interrupt_id, value)
    else:
        answer.update("ask-pack", value)
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
        config_base=_config(), skillpack_router=router,
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
    model_calls_before_resume = client.index
    rail.uninit(agent)
    rail.init(agent)
    answer = InteractiveInput()
    answer.update("follow-up", {"answers": {"Continue with risk check?": "Yes"}})
    result = await agent.invoke(_inputs(answer))
    assert result["result_type"] == "answer"
    assert "skillpack_state_changed" in result["output"]
    assert client.index == model_calls_before_resume
    assert calls == ["weekly-release-pack", "release-note"]


async def _ordinary_pack_context(skill_root=None):
    router, snapshot = _router(skill_root=skill_root)
    router.judge = AsyncMock(return_value=[])
    rail = SymphonyOrchestrationRail(config_base=_config(), skillpack_router=router)
    outer = _outer_ctx("Write release notes and check risk")
    await rail.before_invoke(outer)
    await rail.before_model_call(_model_ctx(outer))
    assert rail._outer_states[id(outer)].pack_route is None
    return rail, router, snapshot, outer


@pytest.mark.asyncio
@pytest.mark.parametrize("relative_file_path", ["SKILL.md", "scripts/execute.py", "references/instructions.md"])
@pytest.mark.parametrize("declined", [False, True])
async def test_unconfirmed_pack_reads_return_only_summary_and_never_shortlist(tmp_path, relative_file_path, declined):
    rail, _, snapshot, outer = await _ordinary_pack_context(tmp_path)
    state = rail._outer_states[id(outer)]
    state.pack_route_declined = declined
    snapshot["skills"][0]["content"] = "EXECUTION_INSTRUCTIONS_MUST_NOT_LEAK"
    pack_file = tmp_path / "weekly-release-pack" / "SKILL.md"
    pack_file.write_text(pack_file.read_text(encoding="utf-8") + "\nEXECUTION_INSTRUCTIONS_MUST_NOT_LEAK\n", encoding="utf-8")
    call = _tool_ctx(outer, "skill_tool", {
        "skill_name": "weekly-release-pack", "relative_file_path": relative_file_path,
    })
    await rail.before_tool_call(call)
    result = call.inputs.tool_result
    assert result["success"] and result["read_only"]
    assert result["skill_type"] == "skillpack"
    assert result["reason"] == "skillpack_confirmation_required"
    assert result["members"] == ["release-note", "risk-check"]
    assert "EXECUTION_INSTRUCTIONS_MUST_NOT_LEAK" not in json.dumps(result)
    if declined:
        assert "user declined" in result["detail"]
    await rail.after_tool_call(call)
    assert "weekly-release-pack" not in rail._shortlisted_skill_ids(call)
    assert state.pack_route is None and state.pack_route_checked
    next_model = _model_ctx(outer)
    await rail.before_model_call(next_model)
    assert any(tool["name"] == "symphony_compose_graph" for tool in next_model.inputs.tools)


@pytest.mark.asyncio
async def test_unconfirmed_pack_gate_preserves_ordinary_skill_reads_and_shortlist(tmp_path):
    rail, _, snapshot, outer = await _ordinary_pack_context(tmp_path)
    snapshot["skills"].append({"id": "release-note", "skill_type": "skill", "enabled": True})
    call = _tool_ctx(outer, "skill_tool", {"skill_name": "release-note"})
    await rail.before_tool_call(call)
    assert call.inputs.tool_result is None
    call.inputs.tool_result = {"success": True, "content": "ordinary instructions"}
    await rail.after_tool_call(call)
    assert rail._shortlisted_skill_ids(call) == ["release-note"]


@pytest.mark.asyncio
async def test_unconfirmed_pack_gate_fails_closed_when_target_unavailable(tmp_path):
    rail, _, _, outer = await _ordinary_pack_context(tmp_path)
    (tmp_path / "weekly-release-pack" / "SKILL.md").unlink()
    call = _tool_ctx(outer, "skill_tool", {"skillName": "weekly-release-pack"})
    await rail.before_tool_call(call)
    assert call.inputs.tool_result["reason"] == "skillpack_inventory_unavailable"
    assert not call.inputs.tool_result["success"]
    await rail.after_tool_call(call)
    assert rail._shortlisted_skill_ids(call) == []


@pytest.mark.asyncio
async def test_real_agent_wrapped_pack_read_cannot_bypass_summary_gate(tmp_path):
    router, _ = _router(skill_root=tmp_path)
    router.judge = AsyncMock(return_value=[])
    router.unconfirmed_pack_summary = AsyncMock(wraps=router.unconfirmed_pack_summary)
    executed = []

    async def skill(**kwargs):
        executed.append(kwargs)
        return {"success": True, "content": "EXECUTION_INSTRUCTIONS_MUST_NOT_LEAK"}

    skill_tool = _tool("skill_tool", skill)
    skill_tool.card.exposure = ToolExposure.DEFERRED
    client = _ScriptedClient([
        _call("tool_search", '{"query":"skill_tool","limit":1}', "find-skill"),
        _call("tool_call", json.dumps({
            "name": "skill_tool", "args": {"skill_name": "weekly-release-pack"},
        }), "wrapped-pack"),
        AssistantMessage(content="Inspect only", usage_metadata=UsageMetadata(model_name="symphony-test")),
    ])
    rail = SymphonyOrchestrationRail(config_base=_config(), skillpack_router=router)
    agent = create_deep_agent(
        model=_RuntimeModel(client), tools=[skill_tool], rails=[rail],
        enable_read_image_multimodal=False, progressive_tool_enabled=True,
        auto_create_workspace=False, enable_task_loop=False, max_iterations=5,
    )
    result = await agent.invoke(_inputs("Write release notes and check risk"))
    assert result["result_type"] == "answer"
    router.unconfirmed_pack_summary.assert_awaited_once()
    assert router.unconfirmed_pack_summary.call_args.args == ("weekly-release-pack",)
    assert router.unconfirmed_pack_summary.call_args.kwargs["session"] is not None
    assert executed == []


@pytest.mark.asyncio
async def test_real_agent_inventory_failure_finishes_without_another_model_call():
    router, snapshot = _router()
    executed = []

    async def skill(**kwargs):
        executed.append(kwargs["skill_name"])
        snapshot["inventory_revision"] = "changed-after-root"
        return {"success": True, "content": "root instructions"}

    client = _ScriptedClient([
        _call("skill_tool", '{"skill_name":"weekly-release-pack"}', "root"),
        _call("skill_tool", '{"skill_name":"release-note"}', "member"),
        AssistantMessage(content="MUST_NOT_RUN", usage_metadata=UsageMetadata(model_name="symphony-test")),
    ])
    rail = SymphonyOrchestrationRail(config_base=_config(), skillpack_router=router)
    agent = create_deep_agent(
        model=_RuntimeModel(client), tools=[_tool("skill_tool", skill)], rails=[rail],
        enable_read_image_multimodal=False, auto_create_workspace=False,
        enable_task_loop=False, max_iterations=5,
    )
    inputs = _inputs("Write release notes and check risk")
    inputs["run"]["context"]["extra"]["skills"] = ["weekly-release-pack"]
    result = await agent.invoke(inputs)
    assert result["result_type"] == "answer"
    assert "skillpack_state_changed" in result["output"]
    assert client.index == 2
    assert executed == ["weekly-release-pack"]


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [
    "本地 SkillPack 可以完成该任务，请选择使用方式。",
    "本地 SkillPack 可以完成该任务，请确认是否使用。",
    "SkillPack 或成员状态已变化，请重新选择。",
])
async def test_no_route_does_not_allow_model_to_repeat_standard_pack_question(question):
    rail, _, _, outer = await _ordinary_pack_context()
    call = _tool_ctx(outer, "ask_user", {"query": question, "questions": [{
        "header": "SkillPack", "question": question,
    }]})
    await rail.before_tool_call(call)
    assert call.inputs.tool_result["reason"] == "skillpack_no_pending_choice"
    assert not rail._outer_states[id(outer)].awaiting_input


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["Which output format?", "你想了解技能包的哪方面？", "是否覆盖已有文件？"])
async def test_no_route_keeps_non_pack_selection_clarification(question):
    rail, _, _, outer = await _ordinary_pack_context()
    call = _tool_ctx(outer, "ask_user", {"query": question, "questions": [{"question": question}]})
    await rail.before_tool_call(call)
    assert call.inputs.tool_result is None


@pytest.mark.asyncio
async def test_no_route_keeps_mixed_questions_including_genuine_clarification():
    rail, _, _, outer = await _ordinary_pack_context()
    call = _tool_ctx(outer, "ask_user", {"query": "Choose", "questions": [
        {"question": "本地 SkillPack 可以完成该任务，请选择使用方式。"},
        {"question": "Which output format?"},
    ]})
    await rail.before_tool_call(call)
    assert call.inputs.tool_result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["same-call", "other-call", "no-result"])
async def test_structured_ask_user_only_respects_completed_per_call_rejection(monkeypatch, marker):
    from openjiuwen.harness.rails.interrupt.ask_user_rail import AskUserRail

    rail, _, _, outer = await _ordinary_pack_context()
    call = _tool_ctx(outer, "ask_user", {"query": "Which output format?"})
    rail._skip_tool(call, {"success": False, "reason": "already_rejected"})
    if marker == "other-call":
        call.inputs.tool_call.id = "another-call"
    elif marker == "no-result":
        call.inputs.tool_msg = None
    base = AsyncMock()
    monkeypatch.setattr(AskUserRail, "before_tool_call", base)
    await StructuredAskUserRail().before_tool_call(call)
    if marker == "same-call":
        base.assert_not_awaited()
        assert call.inputs.tool_result["reason"] == "already_rejected"
    else:
        base.assert_awaited_once_with(call)


@pytest.mark.asyncio
@pytest.mark.parametrize("rebuild", [False, True])
async def test_real_session_history_does_not_reopen_completed_pack_selection(rebuild):
    router, _ = _router()
    recorded_messages = []

    class RecordingClient(_ScriptedClient):
        async def invoke(self, *args, **kwargs):
            recorded_messages.append(copy.deepcopy(kwargs.get("messages", args[0] if args else [])))
            return await super().invoke(*args, **kwargs)

    question = "本地 SkillPack 可以完成该任务，请确认是否使用。"
    client = RecordingClient([
        _call("ask_user", '{"query":"choose"}', "ask-pack"),
        _call("skill_tool", '{"skill_name":"weekly-release-pack"}', "root"),
        AssistantMessage(content="Pack task completed", usage_metadata=UsageMetadata(model_name="symphony-test")),
        _call("ask_user", json.dumps({"query": question, "questions": [{
            "header": "SkillPack", "question": question,
        }]}), "stale-pack-question"),
        AssistantMessage(content="Read-only follow-up completed", usage_metadata=UsageMetadata(model_name="symphony-test")),
    ])

    async def skill(**kwargs):
        return {"success": True, "content": "root instructions"}

    rail = SymphonyOrchestrationRail(config_base=_config(), skillpack_router=router)
    agent = create_deep_agent(
        model=_RuntimeModel(client), tools=[_tool("skill_tool", skill)],
        rails=[rail, StructuredAskUserRail()], enable_read_image_multimodal=False,
        auto_create_workspace=False, enable_task_loop=False, max_iterations=6,
    )
    first = await agent.invoke(_inputs("Write release notes and check risk"))
    assert first["result_type"] == "interrupt"
    state = next(iter(rail._paused_states.values()))
    answer = build_ask_user_interactive_input("ask-pack", [{
        "question": state.pack_route.question,
        "selected_options": ["使用 weekly-release-pack"],
    }])
    assert (await agent.invoke(_inputs(answer)))["result_type"] == "answer"
    if rebuild:
        rail.uninit(agent)
        rail.init(agent)
    router.judge = AsyncMock(return_value=[])
    result = await agent.invoke(_inputs("Read the existing report only, do not run Skills"))
    assert result["result_type"] == "answer"
    assert result["output"] == "Read-only follow-up completed"
    assert not rail._paused_states
    for messages in recorded_messages[-2:]:
        system = "\n".join(
            str(getattr(message, "content", "")) for message in messages
            if getattr(message, "role", "") == "system"
        )
        assert "No SkillPack confirmation is pending for this request" in system
        assert "A local SkillPack selection is pending" not in system
        assert "This request is direct_skillpack" not in system
