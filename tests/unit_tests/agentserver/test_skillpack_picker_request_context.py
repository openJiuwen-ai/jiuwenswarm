# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Explicit Skill selections survive the actual Host and SDK input boundary."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.deep_agent import DeepAgent

from jiuwenswarm.agents.harness.common.rails.symphony import SymphonyOrchestrationRail
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from tests.unit_tests.agentserver.test_skillpack_direct_route import (
    _config, _model_ctx, _router, _tool_ctx,
)


PACK = "weekly-release-pack"
TASK = "Write release notes and check risk"


def _request_context(monkeypatch, params, *, session=None, metadata=None):
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda config: "disabled")
    inputs, _, _ = interface_module.JiuWenSwarm().build_inputs(AgentRequest(
        request_id="picker-request", channel_id="web", session_id="picker-session",
        params=params, metadata=metadata,
    ))
    normalized = DeepAgent._normalize_inputs(None, inputs)
    return AgentCallbackContext(
        agent=SimpleNamespace(card=SimpleNamespace(id="picker-agent")),
        session=session or Session(session_id="picker-session"),
        inputs=normalized,
    )


async def _route(rail, outer):
    await rail.before_invoke(outer)
    model = _model_ctx(outer)
    await rail.before_model_call(model)
    return rail._outer_states[id(outer)].pack_route, model


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("selector", ["picker", "slash"])
async def test_host_explicit_pack_skips_recommendation_but_keeps_live_guards(monkeypatch, enabled, selector):
    params = {"query": TASK, "skills": [PACK]} if selector == "picker" else {
        "query": f"/skills use {PACK}, {TASK}",
    }
    outer = _request_context(monkeypatch, params)
    router, snapshot = _router()
    rail = SymphonyOrchestrationRail(config_base=_config(enabled), skillpack_router=router)
    route, model = await _route(rail, outer)

    assert route is not None and route.token is not None
    assert route.token.pack_id == PACK
    assert route.token.source == "explicit_picker"
    router.judge.assert_not_called()
    assert not any(tool["name"].startswith("symphony_") for tool in model.inputs.tools)

    member = _tool_ctx(outer, "skill_tool", {"skill_name": "release-note"})
    await rail.before_tool_call(member)
    assert member.inputs.tool_result["reason"] == "skillpack_root_required"
    root = _tool_ctx(outer, "skill_tool", {"skill_name": PACK})
    await rail.before_tool_call(root)
    assert root.inputs.tool_result is None
    snapshot["inventory_revision"] = "changed-after-user-selection"
    changed = _tool_ctx(outer, "skill_tool", {"skill_name": PACK})
    await rail.before_tool_call(changed)
    assert changed.inputs.tool_result["reason"] == "skillpack_state_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("skills", [["release-note"], ["release-note", "risk-check"]])
async def test_host_explicit_ordinary_skills_do_not_offer_another_pack(monkeypatch, skills):
    outer = _request_context(monkeypatch, {"query": TASK, "skills": skills})
    router, _ = _router()
    rail = SymphonyOrchestrationRail(config_base=_config(), skillpack_router=router)
    route, model = await _route(rail, outer)

    assert route is None
    router.judge.assert_not_called()
    assert any(tool["name"] == "symphony_compose_graph" for tool in model.inputs.tools)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_host_explicit_disabled_pack_is_not_silently_recomposed(monkeypatch, enabled):
    outer = _request_context(monkeypatch, {"query": TASK, "skills": [PACK]})
    router, snapshot = _router()
    snapshot["skills"][0]["enabled"] = False
    rail = SymphonyOrchestrationRail(config_base=_config(enabled), skillpack_router=router)
    route, model = await _route(rail, outer)

    assert route is not None and route.blocked
    assert route.token is None
    router.judge.assert_not_called()
    assert model.inputs.tools == []


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    TASK,
    f"Tell me about {PACK}",
    f"The document contains /skills use {PACK}, {TASK}",
    f"/skills use {PACK}",
])
async def test_host_mentions_are_not_explicit_pack_authority(monkeypatch, query):
    outer = _request_context(monkeypatch, {"query": query})
    router, _ = _router()
    rail = SymphonyOrchestrationRail(config_base=_config(), skillpack_router=router)
    route, _ = await _route(rail, outer)

    assert outer.inputs.run_context.extra.get("skills", []) == []
    assert route is not None and route.token is None
    assert route.source == "confirmed_query_match"
    router.judge.assert_awaited_once()


@pytest.mark.parametrize("skills,expected", [
    ([" release-note ", "", None, 7], ["release-note"]),
    ([], []),
    ("weekly-release-pack", []),
    (None, []),
])
def test_host_selection_overwrites_nested_context_without_mutation(monkeypatch, skills, expected):
    run = {"kind": "heartbeat", "context": {"extra": {"skills": [PACK], "existing": "kept"}}}
    original = copy.deepcopy(run)
    outer = _request_context(monkeypatch, {"query": TASK, "skills": skills, "run": run})

    assert outer.inputs.run_context.extra["skills"] == expected
    assert outer.inputs.run_context.extra["existing"] == "kept"
    assert run == original


def test_host_picker_precedes_text_selector(monkeypatch):
    outer = _request_context(monkeypatch, {
        "query": f"/skills use {PACK}, {TASK}", "skills": ["release-note"],
    })
    assert outer.inputs.run_context.extra["skills"] == ["release-note"]


def test_cross_session_message_cannot_select_a_pack(monkeypatch):
    outer = _request_context(monkeypatch, {
        "query": f"/skills use {PACK}, {TASK}", "skills": [PACK],
        "run": {"context": {"extra": {"skills": [PACK]}}},
    }, metadata={"_jiuwenswarm_cross_session": {
        "message_id": "synthetic-message", "source_session_id": "source-session",
    }})
    assert outer.inputs.run_context.extra["skills"] == []


@pytest.mark.asyncio
async def test_selection_survives_same_request_interrupt_but_not_next_message(monkeypatch):
    session = Session(session_id="picker-session")
    router, _ = _router()
    rail = SymphonyOrchestrationRail(config_base=_config(), skillpack_router=router)
    first = _request_context(monkeypatch, {"query": TASK, "skills": [PACK]}, session=session)
    selected, _ = await _route(rail, first)
    assert selected is not None and selected.token is not None
    first.inputs.result = {"result_type": "interrupt", "component_ids": ["output-question"]}
    await rail.after_invoke(first)

    answer = InteractiveInput()
    answer.update("output-question", {"answers": {"Output path?": "report.md"}})
    resumed = _request_context(monkeypatch, {"query": answer}, session=session)
    assert resumed.inputs.run_context.extra["skills"] == []
    resumed_route, _ = await _route(rail, resumed)
    assert resumed_route is selected
    assert resumed_route.token.source == "explicit_picker"
    router.judge.assert_not_called()
    resumed.inputs.result = {"result_type": "answer", "output": "done"}
    await rail.after_invoke(resumed)

    followup = _request_context(monkeypatch, {"query": "Read the report"}, session=session)
    next_route, _ = await _route(rail, followup)
    assert followup.inputs.run_context.extra["skills"] == []
    assert next_route is not None and next_route.token is None
    assert next_route is not selected
    assert next_route.source == "confirmed_query_match"
    router.judge.assert_awaited_once()
