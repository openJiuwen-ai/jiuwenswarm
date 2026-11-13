"""SkillPack creation must use persisted evidence and Host confirmation."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.symphony.flow import PackageReviewGate, SkillPackAdapter, SymphonyFlowEngine
from openjiuwen.symphony.orchestration import SymphonyFlowConfig
from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.common.tools.skillpack_toolkit import SkillPackToolkit
from jiuwenswarm.agents.harness.common.tools.symphony_toolkits import SymphonyToolkit
from jiuwenswarm.agents.harness.common.tools.session_messaging_toolkit import (
    SessionMessagingRoute, SessionMessagingRouteRail,
    bind_session_messaging_route, current_session_messaging_route,
    reset_session_messaging_route, with_session_messaging_route,
)
from jiuwenswarm.common.symphony_config import symphony_config_from_dict
from jiuwenswarm.runtime.symphony.service import SwarmSymphonyService, experience_request_id
from jiuwenswarm.runtime.symphony.skillpack_candidates import (
    CandidateEvidenceScope, CandidateScopeStore, ScopedCandidateStore, annotate_member_types, member_snapshot,
)
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.runtime.symphony.experience import _EdgeEvaluationModelAdapter
from jiuwenswarm.common.host_services import install_runtime_push_handler, restore_runtime_push_handler


class Approver:
    async def review(self, package):
        return "approved"


@pytest.fixture(autouse=True)
def isolated_discovery_config(monkeypatch):
    config = {"symphony": {"evolution": {"flow": {"enabled": False}}}}
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: config)
    return config


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.asyncio
async def test_edge_adapter_removes_only_a_complete_json_fence(wrapped):
    content = '```json\n{"status":"success","reason":"target consumed source output"}\n```'
    response = SimpleNamespace(content=content) if wrapped else content
    model = SimpleNamespace(invoke=AsyncMock(return_value=response))
    result = await _EdgeEvaluationModelAdapter(model).invoke([], temperature=0)
    assert result == '{"status":"success","reason":"target consumed source output"}'
    model.invoke.assert_awaited_once_with([], temperature=0)


@pytest.mark.parametrize("content", [
    'explanation\n```json\n{"status":"success"}\n```',
    '```json\n{"status":"success"}\n```\n{"status":"failure"}',
    '```json\n{broken}\n```',
    '```json\n{"status":"success","status":"failure","reason":"duplicate"}\n```',
])
@pytest.mark.asyncio
async def test_edge_adapter_does_not_repair_or_extract_ambiguous_verdicts(content):
    from openjiuwen.harness.rails.evolution.symphony_edge_evaluator import _strict_response_object

    model = SimpleNamespace(invoke=AsyncMock(return_value=content))
    result = await _EdgeEvaluationModelAdapter(model).invoke([])
    assert _strict_response_object(result) is None


def _execution(trace_id):
    return {
        "trace_id": trace_id, "query": "Search sources and write a report", "outcome": "success",
        "graph": {
            "id": trace_id, "type": "execution_graph", "directed": True,
            "nodes": {
                f"skill:{name}": {"label": "skill", "metadata": {"capability_type": "skill", "version": "1.0.0"}}
                for name in ("search", "writer")
            },
            "edges": [{"source": "skill:search", "target": "skill:writer", "relation": "can_feed", "metadata": {"success": True}}],
        },
    }


@pytest.fixture
def environment(tmp_path, monkeypatch, isolated_discovery_config):
    from jiuwenswarm.common.utils import get_agent_workspace_dir

    isolated_discovery_config["symphony"]["evolution"]["flow"]["enabled"] = True
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    skills = tmp_path / "skills"
    for name in ("search", "writer"):
        directory = skills / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: ordinary member\n---\nDo the task.\n")
    config = symphony_config_from_dict({
        "enabled": True, "paths": {"skills_root": str(skills), "graph_dir": str(tmp_path / "graph")},
        "evolution": {"enabled": True},
    })
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.load_symphony_config", lambda: config)
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.load_execution_disabled_skills", lambda: [])
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.trusted_session_user", lambda session_id: "")
    flow = SymphonyFlowEngine(
        get_agent_workspace_dir() / "symphony" / "flow", config=SymphonyFlowConfig(min_successes=3),
        gate=PackageReviewGate(review_agent=Approver()), skill_adapter=SkillPackAdapter(),
    )

    async def submit(planned, execution, *, session_id, capture_mode):
        return SimpleNamespace(new_candidates=await flow.submit(execution))

    runtime = SimpleNamespace(flow_engine=flow, submit_evolution=submit)
    service = SwarmSymphonyService()
    monkeypatch.setattr(service, "_runtime_for", lambda config: runtime)
    monkeypatch.setattr(service, "runtime", lambda: runtime)
    monkeypatch.setattr(service, "_notify_candidate", AsyncMock())
    return service, flow, skills


async def _collect(service, session="session-a"):
    for index in range(3):
        await service.submit_evolution_and_notify(
            None, _execution(f"trace-{index}"), session_id=session, capture_mode="agent", channel_id="web",
        )


def _disable_discovery(monkeypatch, config):
    from jiuwenswarm.runtime.symphony import service as service_module

    config["symphony"]["evolution"]["flow"]["enabled"] = False
    current = service_module.load_symphony_config()
    disabled = replace(current, evolution=replace(
        current.evolution, flow=replace(current.evolution.flow, enabled=False),
    ))
    monkeypatch.setattr(service_module, "load_symphony_config", lambda: disabled)


@pytest.mark.asyncio
async def test_automatic_discovery_uses_scoped_confirmation_and_deduplicates(environment, monkeypatch):
    service, source, skills = environment
    monkeypatch.setattr(service, "_notify_candidate", SwarmSymphonyService._notify_candidate.__get__(service))
    reviewer = AsyncMock(wraps=source.gate._review_agent.review)
    monkeypatch.setattr(source.gate._review_agent, "review", reviewer)
    pushes = []

    async def push(message):
        pushes.append(message)
        return True

    previous = install_runtime_push_handler(push)
    try:
        for index in range(3):
            await service.submit_evolution_and_notify(
                None, _execution(f"trace-{index}"), session_id="session-a", capture_mode="agent", channel_id="web",
            )
            assert len(pushes) == (1 if index == 2 else 0)
        assert reviewer.await_count == 1
        reviewer.reset_mock()
        await service.submit_evolution_and_notify(
            None, _execution("trace-2"), session_id="session-a", capture_mode="agent", channel_id="web",
        )
        reviewer.assert_not_awaited()
    finally:
        restore_runtime_push_handler(push, previous)

    assert len(pushes) == 1
    payload = pushes[0]["payload"]
    assert payload["request_id"].startswith("symphony_experience_tool_")
    approval = CandidateScopeStore(source.store.root).approval(payload["request_id"])
    assert approval["session_id"] == "session-a"
    assert approval["evidence_scope"]["scope"] == "current_session"
    assert approval["members"] == ["search", "writer"]
    assert {path.name for path in skills.iterdir()} == {"search", "writer"}


@pytest.mark.asyncio
async def test_automatic_discovery_preserves_scoped_acknowledgements_after_restart(environment, monkeypatch):
    service, source, skills = environment
    await _collect(service)
    scoped = next(iter(service._skillpack_candidate_flows.values()))
    candidate = scoped.list_candidates()[0]
    assert scoped.acknowledge_candidate(candidate.recipe_id, candidate.version)
    reviewer = AsyncMock(wraps=source.gate._review_agent.review)
    monkeypatch.setattr(source.gate._review_agent, "review", reviewer)
    runtime = service._runtime_for(None)
    restarted = SwarmSymphonyService()
    monkeypatch.setattr(restarted, "_runtime_for", lambda _config: runtime)
    monkeypatch.setattr(restarted, "_notify_candidate", AsyncMock())
    await restarted.submit_evolution_and_notify(
        None, _execution("trace-2"), session_id="session-a", capture_mode="agent", channel_id="web",
    )
    restarted._notify_candidate.assert_not_awaited()
    reviewer.assert_not_awaited()
    # A user's explicit request may still inspect the retained candidate.
    manual = await restarted.prepare_skillpack_candidates(session_id="session-a")
    assert len(manual["candidates"]) == 1
    reviewer.assert_awaited_once()


@pytest.mark.asyncio
async def test_automatic_discovery_deduplicates_only_within_the_same_owner_scope(environment, monkeypatch):
    service, source, skills = environment
    monkeypatch.setattr(service, "_notify_candidate", SwarmSymphonyService._notify_candidate.__get__(service))
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.trusted_session_user",
        lambda session: {"session-a": "user-a", "session-b": "user-b"}[session],
    )
    pushes = []

    async def push(message):
        pushes.append(message)
        return True

    previous = install_runtime_push_handler(push)
    try:
        for session in ("session-a", "session-b"):
            for index in range(3):
                await service.submit_evolution_and_notify(
                    None, _execution(f"{session}-{index}"), session_id=session, capture_mode="agent", channel_id="web",
                )
    finally:
        restore_runtime_push_handler(push, previous)
    assert len(pushes) == 2
    request_ids = {message["payload"]["request_id"] for message in pushes}
    assert len(request_ids) == 2
    store = CandidateScopeStore(source.store.root)
    assert {store.approval(request_id)["evidence_scope"]["user_id"] for request_id in request_ids} == {"user-a", "user-b"}


@pytest.mark.asyncio
async def test_switch_off_blocks_tool_new_candidates_and_pending_confirmation(
    environment, monkeypatch, isolated_discovery_config,
):
    service, source, skills = environment
    await _collect(service)
    automatic = service._notify_candidate.call_args.kwargs
    candidate = automatic["preview"]
    service._notify_candidate.reset_mock()
    _disable_discovery(monkeypatch, isolated_discovery_config)
    assert (await SkillPackToolkit(service).distill("save workflow"))["reason"] == "skillpack_disabled"
    assert (await service.prepare_skillpack_candidates(session_id="session-a"))["reason"] == "skillpack_disabled"
    assert not (await service.request_skillpack_candidate(
        recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        session_id="session-a", channel_id="web",
    ))["success"]
    await service.submit_evolution_and_notify(
        None, _execution("disabled-trace"), session_id="session-a", capture_mode="agent", channel_id="web",
    )
    assert source.store.find_evidence("disabled-trace") is None
    manager = Mock()
    result = await service.install_candidate(
        request_id=automatic["request_id_override"], recipe_id=candidate["recipe_id"],
        recipe_version=candidate["recipe_version"], package_id=None, integrity=None,
        skill_manager=manager, session_id="session-a",
    )
    assert result == {"installed": False, "reason": "flow_disabled"}
    service._notify_candidate.assert_not_awaited()
    manager.install_symphony_skill_artifact.assert_not_called()


@pytest.mark.asyncio
async def test_switch_off_during_review_does_not_offer_a_new_confirmation(
    environment, monkeypatch, isolated_discovery_config,
):
    service, source, skills = environment
    await _collect(service)
    candidate = (await service.prepare_skillpack_candidates(session_id="session-a"))["candidates"][0]
    service._notify_candidate.reset_mock()
    original = service._prepare_skillpack_candidates

    async def prepare_and_disable(**kwargs):
        result = await original(**kwargs)
        _disable_discovery(monkeypatch, isolated_discovery_config)
        monkeypatch.setattr(service, "runtime", lambda: SimpleNamespace(flow_engine=None))
        return result

    monkeypatch.setattr(service, "_prepare_skillpack_candidates", prepare_and_disable)
    result = await service.request_skillpack_candidate(
        recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        session_id="session-a", channel_id="web",
    )
    assert result == {"success": False, "reason": "skillpack_disabled"}
    service._notify_candidate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["request", "offer"])
async def test_reviewed_candidate_does_not_offer_without_flow(environment, monkeypatch, phase):
    service, _, _ = environment
    await _collect(service)
    candidate = (await service.prepare_skillpack_candidates(session_id="session-a"))["candidates"][0]
    service._notify_candidate.reset_mock()
    monkeypatch.setattr(service, "runtime", lambda: SimpleNamespace(flow_engine=None))
    if phase == "request":
        result = await service.request_skillpack_candidate(
            recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
            session_id="session-a", channel_id="web",
        )
    else:
        identity = CandidateEvidenceScope.resolve("session-a", "", "current_session", None)
        result = await service._offer_reviewed_skillpack_candidate(
            candidate, identity, session_id="session-a", channel_id="web",
        )
    assert result == {"success": False, "reason": "flow_disabled"}
    service._notify_candidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_accepted_install_finishes_if_discovery_is_disabled_during_review(
    environment, monkeypatch, isolated_discovery_config, allow_macos_pytest_temp_sources,
):
    service, source, skills = environment
    await _collect(service)
    automatic = service._notify_candidate.call_args.kwargs
    candidate = automatic["preview"]
    scoped = next(iter(service._skillpack_candidate_flows.values()))
    original = scoped.review_and_prepare_install

    async def review_and_disable(*args, **kwargs):
        result = await original(*args, **kwargs)
        _disable_discovery(monkeypatch, isolated_discovery_config)
        return result

    monkeypatch.setattr(scoped, "review_and_prepare_install", review_and_disable)
    manager = SkillManager(workspace_dir=str(skills.parent))
    result = await service.install_candidate(
        request_id=automatic["request_id_override"], recipe_id=candidate["recipe_id"],
        recipe_version=candidate["recipe_version"], package_id=None, integrity=None,
        skill_manager=manager, session_id="session-a",
    )
    assert result["installed"] is True
    assert (skills / "writer-pack" / "SKILL.md").is_file()


@pytest.mark.parametrize("field,value", [
    ("package_id", None), ("package_id", ""), ("integrity", None), ("integrity", 42),
    ("members", None), ("members", "search"), ("members", ["search", None]),
    ("members", []), ("members", ["search"]), ("members", ["search", {}]),
    ("members", ["search", "search"]), ("members", ["search", "  "]),
])
@pytest.mark.asyncio
async def test_malformed_saved_approval_cannot_recover_or_install(environment, field, value):
    service, flow, skills = environment
    await _collect(service)
    candidate = (await service.prepare_skillpack_candidates(session_id="session-a"))["candidates"][0]
    request = await service.request_skillpack_candidate(
        recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        session_id="session-a", channel_id="web",
    )
    store = CandidateScopeStore(flow.store.root)
    approval = store.approval(request["request_id"])
    approval[field] = value
    store.grant(request["request_id"], approval)
    manager = Mock()
    result = await service.install_candidate(
        request_id=request["request_id"], recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        package_id=None, integrity=None, skill_manager=manager, session_id="session-a",
    )
    assert result == {"installed": False, "reason": "invalid_request_id"}
    manager.recover_symphony_skill_install.assert_not_called()
    manager.install_symphony_skill_artifact.assert_not_called()


@pytest.mark.asyncio
async def test_scope_cache_close_failure_retains_worker_and_limit(environment):
    from jiuwenswarm.runtime.symphony.skillpack_candidates import CandidateEvidenceScope

    service, flow, skills = environment
    scope = CandidateEvidenceScope.resolve("session-a", "", "current_session", None)
    config = SimpleNamespace(paths=SimpleNamespace(skills_root=skills))
    retired = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("close failed")))
    for index in range(8):
        service._skillpack_candidate_flows[("old", str(index))] = retired
    with pytest.raises(RuntimeError, match="close failed"):
        await service._skillpack_candidate_flow(flow, config, scope)
    assert len(service._skillpack_candidate_flows) == 8
    assert next(iter(service._skillpack_candidate_flows.values())) is retired
    retired.close.side_effect = None
    await service._skillpack_candidate_flow(flow, config, scope)
    assert len(service._skillpack_candidate_flows) == 8
    assert retired.close.await_count == 2


def test_scope_store_fails_fast_when_another_writer_holds_lock(tmp_path):
    from filelock import FileLock, Timeout

    store = CandidateScopeStore(tmp_path)
    with FileLock(str(store.path) + ".lock"):
        with pytest.raises(Timeout):
            store.grant("request", {"package_id": "test"})
    assert store.approval("request") is None


def test_trace_ownership_is_a_detached_snapshot_without_approvals(tmp_path):
    store = CandidateScopeStore(tmp_path)
    store.record_trace("trace-1", "session-a", {"search": "digest"}, "user-a")
    store.grant("request", {"package_id": "package-1"})
    ownership = store.trace_ownership()
    assert ownership == {
        "trace-1": {"session_id": "session-a", "user_id": "user-a", "members": {"search": "digest"}},
    }
    ownership["trace-1"]["members"]["search"] = "tampered"
    ownership["other"] = {}
    assert store.trace_ownership()["trace-1"]["members"] == {"search": "digest"}
    assert "other" not in store.trace_ownership()
    assert store.approval("request") == {"package_id": "package-1"}


@pytest.mark.asyncio
async def test_real_evidence_to_reviewed_candidate_and_confirmation_without_install(environment):
    service, flow, skills = environment
    await _collect(service)
    service._notify_candidate.assert_awaited_once()
    automatic = service._notify_candidate.call_args.kwargs
    assert automatic["request_id_override"].startswith("symphony_experience_tool_")
    assert automatic["session_id"] == "session-a"
    assert automatic["force"] is False
    preview = await service.prepare_skillpack_candidates(session_id="session-a")
    assert len(preview["candidates"]) == 1
    candidate = preview["candidates"][0]
    assert candidate["members"] == ["search", "writer"]
    assert candidate["name"] == "writer-pack"
    assert candidate["evidence_count"] == 3 and candidate["review"] == "approved"
    assert "## Failure handling" in candidate["root_document"]
    assert {path.name for path in skills.iterdir()} == {"search", "writer"}
    requested = await service.request_skillpack_candidate(
        recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        session_id="session-a", channel_id="web",
    )
    assert requested["awaiting_user_confirmation"]
    assert service._notify_candidate.call_args.kwargs["preview"]["members"] == ["search", "writer"]
    assert {path.name for path in skills.iterdir()} == {"search", "writer"}
    assert not (skills / "skills_state.json").exists()


@pytest.mark.asyncio
async def test_scope_drift_and_wrong_session_block_install(environment):
    service, flow, skills = environment
    await _collect(service)
    assert (await service.prepare_skillpack_candidates(session_id="another-user-session"))["candidates"] == []
    candidate = (await service.prepare_skillpack_candidates(session_id="session-a"))["candidates"][0]
    request = await service.request_skillpack_candidate(
        recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"], session_id="session-a", channel_id="web",
    )
    manager = Mock()
    parameters = {
        "request_id": request["request_id"], "recipe_id": candidate["recipe_id"],
        "recipe_version": candidate["recipe_version"], "package_id": None, "integrity": None,
        "skill_manager": manager,
    }
    denied = await service.install_candidate(**parameters, session_id="another-user-session")
    assert denied["reason"] == "invalid_request_id"
    (skills / "writer" / "run.py").write_text("changed content")
    denied = await service.install_candidate(**parameters, session_id="session-a")
    assert denied["reason"] == "members_changed_reconfirm"
    manager.install_symphony_skill_artifact.assert_not_called()


@pytest.mark.parametrize("change", ["content", "disable"])
@pytest.mark.asyncio
async def test_members_changed_during_review_cannot_reach_install(environment, monkeypatch, change):
    service, flow, skills = environment
    await _collect(service)
    candidate = (await service.prepare_skillpack_candidates(session_id="session-a"))["candidates"][0]
    request = await service.request_skillpack_candidate(
        recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        session_id="session-a", channel_id="web",
    )
    flow = next(iter(service._skillpack_candidate_flows.values()))
    original_review = flow.review_and_prepare_install

    async def review_with_concurrent_change(*args, **kwargs):
        prepared = await original_review(*args, **kwargs)
        if change == "content":
            (skills / "writer" / "run.py").write_text("new implementation after confirmation")
        else:
            monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.load_execution_disabled_skills", lambda: ["writer"])
        return prepared

    monkeypatch.setattr(flow, "review_and_prepare_install", review_with_concurrent_change)
    manager = Mock()
    result = await service.install_candidate(
        request_id=request["request_id"], recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        package_id=None, integrity=None, skill_manager=manager, session_id="session-a",
    )
    assert result == {"installed": False, "reason": "members_changed_reconfirm"}
    manager.recover_symphony_skill_install.assert_not_called()
    manager.install_symphony_skill_artifact.assert_not_called()
    assert not (skills / "writer-pack").exists()


@pytest.mark.asyncio
async def test_tool_accepts_only_intent_and_server_candidate_references(environment):
    service, flow, skills = environment
    await _collect(service)
    toolkit = SkillPackToolkit(service)
    assert (await toolkit.distill("save this workflow"))["reason"] == "trusted_session_required"
    token = bind_session_messaging_route(session_id="session-a", request_id="chat-1", user_id="user-a")
    try:
        result = await toolkit.distill("save this workflow")
        assert len(result["candidates"]) == 1
        assert "member_snapshot" not in result["candidates"][0]
        schema = toolkit.get_tools()[0].card.input_params
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == {"intent", "recipe_id", "recipe_version", "scope", "source_refs"}
        forged = await toolkit.distill("save this workflow", "recipe_forged", 1)
        assert forged["reason"] == "candidate_not_available_in_session"
    finally:
        reset_session_messaging_route(token)


@pytest.mark.parametrize("source_refs", ["trace-1", [], ["trace-1"] * 101, [None], [{}], ["  "], ["x" * 257]])
@pytest.mark.asyncio
async def test_tool_rejects_invalid_source_refs_without_calling_service(isolated_discovery_config, source_refs):
    isolated_discovery_config["symphony"]["evolution"]["flow"]["enabled"] = True
    service = SimpleNamespace(prepare_skillpack_candidates=AsyncMock())
    token = bind_session_messaging_route(session_id="session-a", request_id="chat-1", user_id="user-a")
    try:
        result = await SkillPackToolkit(service).distill("save this workflow", source_refs=source_refs)
        assert result == {"success": False, "reason": "invalid_source_refs"}
        service.prepare_skillpack_candidates.assert_not_awaited()
    finally:
        reset_session_messaging_route(token)


@pytest.mark.asyncio
async def test_distill_route_survives_supervisor_and_parallel_tools(isolated_discovery_config):
    isolated_discovery_config["symphony"]["evolution"]["flow"]["enabled"] = True
    service = SimpleNamespace(prepare_skillpack_candidates=AsyncMock(return_value={"success": True, "candidates": []}))
    toolkit = SkillPackToolkit(service)
    rail = SessionMessagingRouteRail()
    both_bound = asyncio.Event()
    bound = []

    async def invoke(session_id):
        assert current_session_messaging_route() is None
        route = SessionMessagingRoute(session_id=session_id, request_id=f"request-{session_id}")
        ctx = SimpleNamespace(
            inputs=ToolCallInputs(tool_call={"id": f"call-{session_id}", "name": "skillpack_distill"}, tool_name="skillpack_distill"),
            extra={"run_context": with_session_messaging_route({}, route)["run"]["context"]},
        )
        await rail.before_tool_call(ctx)
        bound.append(session_id)
        if len(bound) == 2:
            both_bound.set()
        await both_bound.wait()
        try:
            assert (await toolkit.distill("save this workflow"))["success"]
            assert current_session_messaging_route().session_id == session_id
        finally:
            await rail.after_tool_call(ctx)
        assert current_session_messaging_route() is None

    await asyncio.gather(invoke("session-a"), invoke("session-b"))
    assert {call.kwargs["session_id"] for call in service.prepare_skillpack_candidates.await_args_list} == {"session-a", "session-b"}


@pytest.mark.parametrize("kind", ["swarm-skill", "team-skill", "skillpack", "unknown"])
@pytest.mark.asyncio
async def test_nonordinary_real_member_is_never_sedimented(environment, kind):
    service, flow, skills = environment
    (skills / "writer" / "SKILL.md").write_text(f"---\nname: writer\nkind: {kind}\ndescription: member\n---\n")
    await _collect(service)
    assert flow.store.read_evidence() == []
    assert (await service.prepare_skillpack_candidates(session_id="session-a"))["candidates"] == []


def test_snapshot_rejects_symlink_and_disabled_member(environment, tmp_path):
    service, flow, skills = environment
    with pytest.raises(ValueError, match="member_disabled"):
        member_snapshot(skills, ["search", "writer"], ["writer"])
    (skills / "writer" / "outside").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="member_symlink|non_ordinary_member"):
        member_snapshot(skills, ["search", "writer"], [])


def test_tool_is_absent_by_default(monkeypatch):
    monkeypatch.setenv("SKILLPACK_PRODUCTIZATION_ENABLED", "true")
    assert SkillPackToolkit().get_tools() == []


@pytest.mark.parametrize("symphony_enabled,discovery_enabled", [(False, False), (False, True), (True, False), (True, True)])
def test_tool_registration_follows_discovery_switch(monkeypatch, symphony_enabled, discovery_enabled):
    monkeypatch.setenv("SKILLPACK_PRODUCTIZATION_ENABLED", str(not discovery_enabled).lower())
    tools = SymphonyToolkit().get_tools({"symphony": {
        "enabled": symphony_enabled, "evolution": {"flow": {"enabled": discovery_enabled}},
    }})
    assert ("skillpack_distill" in [tool.card.name for tool in tools]) is discovery_enabled


@pytest.mark.asyncio
async def test_productization_blocks_unscoped_experiment_endpoints(environment):
    service, flow, skills = environment
    await _collect(service)
    service._notify_candidate.reset_mock()
    candidate = flow.list_candidates()[0]
    assert service.list_experience_candidates() == {
        "success": False, "reason": "scoped_skillpack_tool_required", "candidates": [],
    }
    denied = await service.request_experience_candidate(
        recipe_id=candidate.recipe_id, recipe_version=candidate.version,
        session_id="another-user-session", channel_id="web",
    )
    assert denied["reason"] == "scoped_skillpack_tool_required"
    service._notify_candidate.assert_not_called()
    manager = Mock()
    request_id = experience_request_id(candidate.recipe_id, candidate.version)
    service._install_receipts[request_id] = {"installed": True}
    denied = await service.install_candidate(
        request_id=request_id, recipe_id=candidate.recipe_id, recipe_version=candidate.version,
        package_id=None, integrity=None, skill_manager=manager, session_id="another-user-session",
    )
    assert denied == {"installed": False, "reason": "scoped_skillpack_tool_required"}
    manager.install_symphony_skill_artifact.assert_not_called()
    assert {path.name for path in skills.iterdir()} == {"search", "writer"}


def test_trace_scope_is_immutable(tmp_path):
    store = CandidateScopeStore(tmp_path / "flow")
    store.record_trace("trace-1", "session-a", {"member": "hash"}, "user-a")
    with pytest.raises(ValueError, match="trace_scope_changed"):
        store.record_trace("trace-1", "session-b", {"member": "changed"}, "user-b")


@pytest.mark.asyncio
async def test_old_core_contract_cannot_enable_productized_sedimentation(environment, monkeypatch):
    service, flow, skills = environment
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.SkillPackAdapter", object)
    assert (await service.prepare_skillpack_candidates(session_id="session-a"))["reason"] == "core_skillpack_contract_missing"


@pytest.mark.asyncio
async def test_same_user_cross_session_evidence_is_reusable_but_other_user_is_not(environment, monkeypatch):
    service, flow, skills = environment
    owners = {"session-a": "user-a", "session-b": "user-a", "session-c": "user-b"}
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.trusted_session_user", lambda session: owners.get(session, ""))
    for index, session in enumerate(("session-a", "session-b", "session-a")):
        await service.submit_evolution_and_notify(
            None, _execution(f"trace-{index}"), session_id=session, capture_mode="agent", channel_id="web",
        )
    assert len((await service.prepare_skillpack_candidates(session_id="session-b"))["candidates"]) == 1
    assert (await service.prepare_skillpack_candidates(session_id="session-b", scope="current_session"))["candidates"] == []
    assert (await service.prepare_skillpack_candidates(session_id="session-c"))["candidates"] == []
    assert (await service.prepare_skillpack_candidates(session_id="session-a", source_refs=["forged-trace"]))["candidates"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", ["", "desktop-user"])
async def test_personal_web_new_sessions_accumulate_verified_candidates(environment, monkeypatch, user_id):
    from jiuwenswarm.runtime.symphony.skillpack_candidates import trusted_session_user
    from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata

    service, source, skills = environment
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_PROFILE", raising=False)
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.trusted_session_user", trusted_session_user)
    for index in range(3):
        session = f"local-session-{index}"
        init_session_metadata(session_id=session, channel_id="web", user_id=user_id)
        assert trusted_session_user(session) == user_id
        await service.submit_evolution_and_notify(
            None, _execution(f"local-trace-{index}"), session_id=session, capture_mode="agent", channel_id="web",
        )
        assert service._notify_candidate.await_count == (1 if index == 2 else 0)

    assert len(source.store.read_evidence()) == 3
    ownership = CandidateScopeStore(source.store.root).trace_ownership()
    assert all(record["user_id"] == user_id for record in ownership.values())
    result = await service.prepare_skillpack_candidates(session_id="local-session-2")
    assert result["scope"] == "current_user"
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["evidence_count"] == 3
    limited = await service.prepare_skillpack_candidates(session_id="local-session-2", scope="current_session")
    assert limited["candidates"] == []
    assert {path.name for path in skills.iterdir()} == {"search", "writer"}


@pytest.fixture
def local_web_sessions(monkeypatch):
    from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata

    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_PROFILE", raising=False)
    for session_id in ("local-a", "local-b"):
        init_session_metadata(session_id=session_id, channel_id="web")


def _personal_evidence_scope(session_id, user_id, scope, source_refs):
    from jiuwenswarm.common.utils import get_agent_workspace_dir

    return CandidateEvidenceScope.resolve(
        session_id, user_id, scope, source_refs,
        flow_root=get_agent_workspace_dir() / "symphony" / "flow",
    )


def test_local_scope_requires_each_persisted_owner_and_honors_refs(local_web_sessions):
    identity = _personal_evidence_scope("local-b", "", "current_user", ["trace-a"])
    assert identity.local_workspace
    assert identity.user_id == ""
    assert identity.allows("trace-a", {"session_id": "local-a", "user_id": ""})
    assert not identity.allows("trace-b", {"session_id": "local-a", "user_id": ""})
    assert not identity.allows("trace-a", {"session_id": "missing-session", "user_id": ""})
    assert not identity.allows("trace-a", {"session_id": "local-a", "user_id": "other-user"})
    assert not identity.allows("trace-a", {"session_id": "local-a"})
    limited = _personal_evidence_scope("local-b", "", "current_session", None)
    assert not limited.local_workspace
    assert not limited.allows("trace-a", {"session_id": "local-a", "user_id": ""})
    owned = _personal_evidence_scope("local-b", "user-a", "current_user", None)
    assert not owned.local_workspace
    assert not owned.allows("trace-a", {"session_id": "local-a", "user_id": ""})
    assert owned.allows("trace-a", {"session_id": "authenticated-a", "user_id": "user-a"})


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("user_id", "another-user"), ("channel_id", "feishu")])
@pytest.mark.parametrize("initially_valid", [True, False])
async def test_evidence_scan_reads_each_session_once_without_reusing_identity_cache(
    environment, local_web_sessions, monkeypatch, field, value, initially_valid,
):
    from jiuwenswarm.server.runtime.session import lifecycle

    service, source, skills = environment
    await _collect(service, "local-a")
    original = source.store.read_evidence()[0]
    snapshot = member_snapshot(skills, ["search", "writer"], [])
    ownership = CandidateScopeStore(source.store.root)
    for index in range(3):
        trace_id = f"local-b-trace-{index}"
        source.store.append_evidence(replace(original, trace_id=trace_id))
        ownership.record_trace(trace_id, "local-b", snapshot)
    identity = _personal_evidence_scope("local-b", "", "current_user", None)
    scoped = ScopedCandidateStore(source.store, identity, skills, lambda: [])
    path = lifecycle.resolve_session("local-a") / "metadata.json"
    original_metadata = json.loads(path.read_text())
    changed_metadata = {**original_metadata, field: value}
    path.write_text(json.dumps(original_metadata if initially_valid else changed_metadata))
    reads = Mock(wraps=lifecycle.raw_metadata)
    monkeypatch.setattr(lifecycle, "raw_metadata", reads)

    first = scoped.read_evidence()
    assert len(first) == (6 if initially_valid else 3)
    assert Counter(call.args[0] for call in reads.call_args_list) == {"local-a": 1, "local-b": 1}
    reads.reset_mock()
    path.write_text(json.dumps(changed_metadata if initially_valid else original_metadata))
    second = scoped.read_evidence()
    assert len(second) == (3 if initially_valid else 6)
    assert Counter(call.args[0] for call in reads.call_args_list) == {"local-a": 1, "local-b": 1}
    assert all(item.trace_id.startswith("local-b-") for item in (second if initially_valid else first))


@pytest.mark.asyncio
async def test_scoped_recipe_reads_metadata_once_per_call_and_checks_source_refs_first(
    environment, local_web_sessions, monkeypatch,
):
    from jiuwenswarm.server.runtime.session import lifecycle

    service, source, skills = environment
    await _collect(service, "local-a")
    recipe = source.get_recipe(source.list_candidates()[0].recipe_id)
    store = CandidateScopeStore(source.store.root)
    snapshot = member_snapshot(skills, ["search", "writer"], [])
    path = lifecycle.resolve_session("local-a") / "metadata.json"
    metadata = json.loads(path.read_text())
    reads = Mock(wraps=lifecycle.raw_metadata)
    monkeypatch.setattr(lifecycle, "raw_metadata", reads)

    assert set(store.scoped_evidence(recipe, "local-b", snapshot)) == {"trace-0", "trace-1", "trace-2"}
    assert Counter(call.args[0] for call in reads.call_args_list) == {"local-b": 1, "local-a": 1}
    reads.reset_mock()
    path.write_text(json.dumps({**metadata, "channel_id": "feishu"}))
    assert store.scoped_evidence(recipe, "local-b", snapshot) == []
    assert Counter(call.args[0] for call in reads.call_args_list) == {"local-b": 1, "local-a": 1}
    reads.reset_mock()
    assert store.scoped_evidence(recipe, "local-b", snapshot, source_refs=["not-this-recipe"]) == []
    assert Counter(call.args[0] for call in reads.call_args_list) == {"local-b": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [1, 4])
async def test_preview_evidence_scan_count_is_bounded_independently_of_candidate_count(
    environment, local_web_sessions, monkeypatch, candidate_count,
):
    service, source, skills = environment
    ownership = CandidateScopeStore(source.store.root)
    for group in range(candidate_count):
        writer = f"writer-{group}"
        directory = skills / writer
        directory.mkdir()
        (directory / "SKILL.md").write_text(f"---\nname: {writer}\ndescription: ordinary member\n---\nWrite report.\n")
        snapshot = member_snapshot(skills, ["search", writer], [])
        for index in range(3):
            trace_id = f"group-{group}-trace-{index}"
            execution = _execution(trace_id)
            execution["graph"]["nodes"][f"skill:{writer}"] = execution["graph"]["nodes"].pop("skill:writer")
            execution["graph"]["edges"][0]["target"] = f"skill:{writer}"
            ownership.record_trace(trace_id, "local-a", snapshot)
            await source.submit(annotate_member_types(execution, skills))
    reads = Mock(wraps=source.store.read_evidence)
    monkeypatch.setattr(source.store, "read_evidence", reads)

    first = await service.prepare_skillpack_candidates(session_id="local-b")
    assert len(first["candidates"]) == candidate_count
    cold_scans = reads.call_count
    reads.reset_mock()
    second = await service.prepare_skillpack_candidates(session_id="local-b")
    assert len(second["candidates"]) == candidate_count
    warm_scans = reads.call_count
    await service.close()
    assert 1 <= cold_scans <= 5, f"{candidate_count} candidates: cold={cold_scans}, warm={warm_scans}"
    assert 1 <= warm_scans <= 3, f"{candidate_count} candidates: cold={cold_scans}, warm={warm_scans}"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError, ValueError])
async def test_preview_batch_read_failure_returns_no_candidates(environment, local_web_sessions, monkeypatch, error):
    from threading import current_thread, main_thread

    service, source, _ = environment
    await _collect(service, "local-a")
    read_evidence = ScopedCandidateStore.read_evidence
    reviewer = AsyncMock(wraps=source.gate._review_agent.review)
    monkeypatch.setattr(source.gate._review_agent, "review", reviewer)
    batch_reads = Mock()

    def fail_batch_read(store):
        if current_thread() is not main_thread():
            batch_reads()
            raise error("evidence read failed")
        return read_evidence(store)

    monkeypatch.setattr(ScopedCandidateStore, "read_evidence", fail_batch_read)
    result = await service.prepare_skillpack_candidates(session_id="local-b")
    assert result["success"] is True
    assert result["candidates"] == []
    batch_reads.assert_called_once()
    reviewer.assert_not_awaited()
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["contributor_owner", "contributor_channel", "evidence_removed"])
@pytest.mark.parametrize("after_review", [False, True])
async def test_preview_scan_cache_cannot_authorize_install_after_contributor_change(
    environment, local_web_sessions, monkeypatch, change, after_review,
):
    from jiuwenswarm.server.runtime.session import lifecycle

    service, source, _ = environment
    await _collect(service, "local-a")
    candidate = (await service.prepare_skillpack_candidates(session_id="local-b"))["candidates"][0]
    request = await service.request_skillpack_candidate(
        recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        session_id="local-b", channel_id="web",
    )

    def invalidate_original_evidence():
        if change == "evidence_removed":
            source.store.evidence_path().unlink()
        else:
            path = lifecycle.resolve_session("local-a") / "metadata.json"
            metadata = json.loads(path.read_text())
            metadata["user_id" if change == "contributor_owner" else "channel_id"] = (
                "another-user" if change == "contributor_owner" else "feishu"
            )
            path.write_text(json.dumps(metadata))

    if after_review:
        identity = _personal_evidence_scope("local-b", "", "current_user", None)
        scoped = service._skillpack_candidate_flows[(str(source.store.root.resolve()), identity.key)]
        prepare = scoped.review_and_prepare_install

        async def review_then_change(*args, **kwargs):
            prepared = await prepare(*args, **kwargs)
            invalidate_original_evidence()
            return prepared

        monkeypatch.setattr(scoped, "review_and_prepare_install", review_then_change)
    else:
        invalidate_original_evidence()
    manager = Mock()
    result = await service.install_candidate(
        request_id=request["request_id"], recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        package_id=None, integrity=None, skill_manager=manager, session_id="local-b",
    )
    assert result["reason"] == "evidence_changed_reconfirm"
    manager.install_symphony_skill_artifact.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("session_id", "other-session"), ("channel_id", "feishu"),
    ("user_id", "other-user"), ("user_id", None), ("user_id", "missing-field"),
])
def test_local_scope_rejects_unproven_metadata(local_web_sessions, field, value):
    from jiuwenswarm.server.runtime.session import lifecycle

    identity = _personal_evidence_scope("local-b", "", "current_user", None)
    path = lifecycle.resolve_session("local-a") / "metadata.json"
    metadata = json.loads(path.read_text())
    if value == "missing-field":
        metadata.pop(field)
    else:
        metadata[field] = value
    path.write_text(json.dumps(metadata))
    assert not identity.allows("trace-a", {"session_id": "local-a", "user_id": ""})
    assert not _personal_evidence_scope("local-a", "", "current_user", None).local_workspace


@pytest.mark.parametrize("setting,value", [
    ("JIUWENSWARM_EDITION", "enterprise"), ("JIUWENSWARM_RUNTIME_PROFILE", "agentos"),
])
def test_local_scope_never_broadens_enterprise_or_agentos(local_web_sessions, monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    identity = _personal_evidence_scope("local-b", "", "current_user", None)
    assert identity.scope == "current_session"
    assert not identity.allows("trace-a", {"session_id": "local-a", "user_id": ""})
    assert identity.allows("trace-b", {"session_id": "local-b", "user_id": ""})


@pytest.mark.parametrize("root_function", ["get_agent_root_dir", "get_agent_sessions_dir"])
def test_local_scope_rejects_nonpersonal_host_root(local_web_sessions, monkeypatch, tmp_path, root_function):
    monkeypatch.setattr(f"jiuwenswarm.common.utils.{root_function}", lambda tenant_key=None: tmp_path / "tenant-root")
    assert _personal_evidence_scope("local-a", "", "current_user", None).scope == "current_session"


@pytest.mark.parametrize("session_id", ["../local-a", r"..\local-a", "C:local-a", "CON", "local-a/child"])
def test_local_scope_rejects_path_traversal(local_web_sessions, session_id):
    assert not _personal_evidence_scope(session_id, "", "current_user", None).local_workspace


@pytest.mark.parametrize("target", ["metadata", "session", "archive-root"])
def test_local_scope_does_not_follow_session_symlinks(local_web_sessions, tmp_path, target):
    from jiuwenswarm.server.runtime.session import lifecycle

    session_dir = lifecycle.resolve_session("local-a")
    if target == "metadata":
        original = session_dir / "metadata.json"
        outside = tmp_path / "outside-metadata.json"
        original.rename(outside)
        original.symlink_to(outside)
    elif target == "session":
        outside = tmp_path / "outside-session"
        session_dir.rename(outside)
        session_dir.symlink_to(outside, target_is_directory=True)
    else:
        outside = tmp_path / "outside-archive"
        outside.mkdir()
        session_dir.rename(outside / "local-a")
        (session_dir.parent.parent / "sessions_archived").symlink_to(outside, target_is_directory=True)
    assert not _personal_evidence_scope("local-a", "", "current_user", None).local_workspace


@pytest.mark.parametrize("user_id,scope", [("", "current_session"), ("user-a", "current_user")])
def test_existing_scope_keys_keep_their_original_serialization(user_id, scope):
    original = {"session_id": "session-a", "user_id": user_id, "scope": scope, "source_refs": None}
    expected = hashlib.sha256(json.dumps(original, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    identity = CandidateEvidenceScope.resolve("session-a", user_id, scope, None)
    assert identity.to_dict() == original
    assert identity.key == expected


def test_local_scope_rejects_external_or_symlinked_flow(local_web_sessions, tmp_path):
    from jiuwenswarm.common.utils import get_agent_workspace_dir

    external = tmp_path / "shared-flow"
    external.mkdir()
    identity = CandidateEvidenceScope.resolve("local-b", "", "current_user", None, flow_root=external)
    assert identity.scope == "current_session"
    assert not identity.allows("trace-a", {"session_id": "local-a", "user_id": ""})
    managed = get_agent_workspace_dir() / "symphony" / "flow"
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.symlink_to(external, target_is_directory=True)
    assert not _personal_evidence_scope("local-b", "", "current_user", None).local_workspace


@pytest.mark.parametrize("partition,allowed", [("", True), ("domain", False), ("infra", False)])
def test_local_scope_uses_only_managed_partition(local_web_sessions, partition, allowed):
    from jiuwenswarm.common.utils import get_user_workspace_dir

    flow_root = get_user_workspace_dir() / partition / "agent" / "workspace" / "symphony" / "flow"
    flow_root.mkdir(parents=True, exist_ok=True)
    identity = CandidateEvidenceScope.resolve("local-b", "", "current_user", None, flow_root=flow_root)
    assert bool(identity.local_workspace) is allowed
    assert identity.allows("trace-a", {"session_id": "local-a", "user_id": ""}) is allowed


@pytest.mark.asyncio
async def test_shared_flow_cannot_borrow_other_local_roots_matching_session_ids(
    environment, local_web_sessions, monkeypatch, tmp_path,
):
    from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata

    service, source, _ = environment
    for index in range(3):
        session_id = f"shared-session-{index}"
        init_session_metadata(session_id=session_id, channel_id="web")
        await service.submit_evolution_and_notify(
            None, _execution(f"shared-trace-{index}"), session_id=session_id, capture_mode="agent", channel_id="web",
        )
    assert len((await service.prepare_skillpack_candidates(session_id="shared-session-2"))["candidates"]) == 1
    evidence = source.store.evidence_path().read_bytes()
    await service.close()
    other_root = tmp_path / "other-personal-root"
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: other_root)
    for index in range(3):
        init_session_metadata(session_id=f"shared-session-{index}", channel_id="web")
    restarted = SwarmSymphonyService()
    monkeypatch.setattr(restarted, "_runtime_for", lambda _config: SimpleNamespace(flow_engine=source))
    result = await restarted.prepare_skillpack_candidates(session_id="shared-session-2")
    assert result["scope"] == "current_session"
    assert result["candidates"] == []
    assert source.store.evidence_path().read_bytes() == evidence
    await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "channel", "root", "enterprise", "agentos"])
async def test_local_approval_requires_unchanged_host_scope(environment, local_web_sessions, monkeypatch, tmp_path, change):
    from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata, update_session_metadata

    service, source, _ = environment
    await _collect(service, "local-a")
    candidate = (await service.prepare_skillpack_candidates(session_id="local-b"))["candidates"][0]
    request = await service.request_skillpack_candidate(
        recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        session_id="local-b", channel_id="web",
    )
    assert CandidateScopeStore(source.store.root).approval(request["request_id"])["evidence_scope"]["local_workspace"]
    if change == "owner":
        update_session_metadata(session_id="local-b", user_id="other-user", sync=True)
    elif change == "channel":
        from jiuwenswarm.server.runtime.session import lifecycle

        path = lifecycle.resolve_session("local-b") / "metadata.json"
        metadata = json.loads(path.read_text())
        metadata["channel_id"] = "feishu"
        path.write_text(json.dumps(metadata))
    elif change == "root":
        monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path / "other-personal-root")
        init_session_metadata(session_id="local-b", channel_id="web")
    else:
        setting = "JIUWENSWARM_EDITION" if change == "enterprise" else "JIUWENSWARM_RUNTIME_PROFILE"
        monkeypatch.setenv(setting, change)
    manager = Mock()
    result = await service.install_candidate(
        request_id=request["request_id"], recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        package_id=None, integrity=None, skill_manager=manager, session_id="local-b",
    )
    assert result["reason"] == "evidence_scope_changed_reconfirm"
    manager.install_symphony_skill_artifact.assert_not_called()


@pytest.mark.asyncio
async def test_local_cross_session_history_and_confirmation_survive_restart(
    environment, local_web_sessions, monkeypatch, allow_macos_pytest_temp_sources,
):
    service, source, skills = environment
    await _collect(service, "local-a")
    store = CandidateScopeStore(source.store.root)
    original_ownership = store.trace_ownership()
    candidate = (await service.prepare_skillpack_candidates(session_id="local-b"))["candidates"][0]
    request = await service.request_skillpack_candidate(
        recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        session_id="local-b", channel_id="web",
    )
    await service.close()
    restarted = SwarmSymphonyService()
    monkeypatch.setattr(restarted, "runtime", lambda: SimpleNamespace(flow_engine=source))
    monkeypatch.setattr(restarted, "_runtime_for", lambda _config: SimpleNamespace(flow_engine=source))
    arguments = {
        "request_id": request["request_id"], "recipe_id": candidate["recipe_id"],
        "recipe_version": candidate["recipe_version"], "package_id": None, "integrity": None,
        "skill_manager": SkillManager(workspace_dir=str(skills.parent)), "session_id": "local-b",
    }
    receipt = await restarted.install_candidate(**arguments)
    assert receipt["installed"] and receipt["newly_installed"]
    replay = await restarted.install_candidate(**arguments)
    assert replay["installed"] and replay["replayed"]
    assert (skills / "writer-pack" / "SKILL.md").is_file()
    assert store.trace_ownership() == original_ownership
    assert all(record["user_id"] == "" for record in original_ownership.values())
    await restarted.close()


@pytest.mark.asyncio
async def test_confirmed_candidate_installs_through_real_skill_manager(environment, allow_macos_pytest_temp_sources):
    service, flow, skills = environment
    await _collect(service)
    candidate = (await service.prepare_skillpack_candidates(session_id="session-a"))["candidates"][0]
    request = await service.request_skillpack_candidate(
        recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"], session_id="session-a", channel_id="web",
    )
    manager = SkillManager(workspace_dir=str(skills.parent))
    receipt = await service.install_candidate(
        request_id=request["request_id"], recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        package_id=None, integrity=None, skill_manager=manager, session_id="session-a",
    )
    assert receipt["installed"] and receipt["newly_installed"]
    assert (skills / "writer-pack" / "SKILL.md").is_file()
    assert (skills / "search" / "SKILL.md").is_file()
    replay = await service.install_candidate(
        request_id=request["request_id"], recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        package_id=None, integrity=None, skill_manager=manager, session_id="session-a",
    )
    assert replay["installed"] and replay["replayed"] and not replay["newly_installed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sessions", [
    ("session-a", "session-a", "session-a", "session-b"),
    ("session-a", "session-b", "session-a", "session-a"),
])
async def test_other_session_cannot_hide_or_borrow_verified_evidence(environment, sessions):
    service, source, _ = environment
    for index, session in enumerate(sessions):
        await service.submit_evolution_and_notify(
            None, _execution(f"isolated-{index}"), session_id=session, capture_mode="agent", channel_id="web",
        )
        if index == 2 and session == "session-a" and sessions[1] == "session-a":
            assert len((await service.prepare_skillpack_candidates(session_id="session-a"))["candidates"]) == 1
    original = source.store.evidence_path().read_bytes()
    a = await service.prepare_skillpack_candidates(session_id="session-a")
    b = await service.prepare_skillpack_candidates(session_id="session-b")
    assert len(a["candidates"]) == 1
    assert a["candidates"][0]["evidence_count"] == 3
    assert b["candidates"] == []
    assert source.store.evidence_path().read_bytes() == original
    assert len(source.store.read_evidence()) == 4
    assert all(not flow.store.evidence_path().exists() for flow in service._skillpack_candidate_flows.values())


@pytest.mark.asyncio
async def test_source_refs_partition_threshold_and_confirmation_identity(environment):
    service, source, _ = environment
    await _collect(service)
    await service.submit_evolution_and_notify(
        None, _execution("trace-3"), session_id="session-a", capture_mode="agent", channel_id="web",
    )
    full = (await service.prepare_skillpack_candidates(session_id="session-a"))["candidates"][0]
    assert full["evidence_count"] == 4
    short = await service.prepare_skillpack_candidates(session_id="session-a", source_refs=["trace-0", "trace-1"])
    assert short["candidates"] == []
    references = ["trace-2", "trace-0", "trace-1"]
    subset = (await service.prepare_skillpack_candidates(session_id="session-a", source_refs=references))["candidates"][0]
    assert subset["evidence_count"] == 3
    full_request = await service.request_skillpack_candidate(
        recipe_id=full["recipe_id"], recipe_version=full["recipe_version"], session_id="session-a", channel_id="web",
    )
    subset_request = await service.request_skillpack_candidate(
        recipe_id=subset["recipe_id"], recipe_version=subset["recipe_version"], session_id="session-a", channel_id="web",
        source_refs=references,
    )
    repeated = await service.request_skillpack_candidate(
        recipe_id=subset["recipe_id"], recipe_version=subset["recipe_version"], session_id="session-a", channel_id="web",
        source_refs=sorted(references),
    )
    assert full_request["request_id"] != subset_request["request_id"]
    assert repeated["request_id"] == subset_request["request_id"]
    approval = CandidateScopeStore(source.store.root).approval(subset_request["request_id"])
    assert sorted(approval["evidence_trace_ids"]) == sorted(references)


@pytest.mark.asyncio
async def test_same_user_remains_eligible_after_another_user_executes_same_workflow(environment, monkeypatch):
    service, _, _ = environment
    owners = {"a-1": "user-a", "a-2": "user-a", "b-1": "user-b"}
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.trusted_session_user", lambda session: owners.get(session, ""))
    for index, session in enumerate(("a-1", "b-1", "a-2", "a-1")):
        await service.submit_evolution_and_notify(
            None, _execution(f"owned-{index}"), session_id=session, capture_mode="agent", channel_id="web",
        )
    assert (await service.prepare_skillpack_candidates(session_id="a-2"))["candidates"][0]["evidence_count"] == 3
    assert (await service.prepare_skillpack_candidates(session_id="a-1", scope="current_session"))["candidates"] == []
    assert (await service.prepare_skillpack_candidates(session_id="b-1"))["candidates"] == []


@pytest.mark.asyncio
async def test_approval_cannot_survive_changed_trusted_owner(environment, monkeypatch):
    service, _, _ = environment
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.trusted_session_user", lambda session: "user-a")
    await _collect(service)
    candidate = (await service.prepare_skillpack_candidates(session_id="session-a"))["candidates"][0]
    request = await service.request_skillpack_candidate(
        recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"], session_id="session-a", channel_id="web",
    )
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.trusted_session_user", lambda session: "user-b")
    manager = Mock()
    result = await service.install_candidate(
        request_id=request["request_id"], recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        package_id=None, integrity=None, skill_manager=manager, session_id="session-a",
    )
    assert result["reason"] == "evidence_scope_changed_reconfirm"
    manager.install_symphony_skill_artifact.assert_not_called()


@pytest.mark.asyncio
async def test_foreign_evidence_after_confirmation_does_not_change_install(environment, allow_macos_pytest_temp_sources):
    service, _, skills = environment
    await _collect(service)
    candidate = (await service.prepare_skillpack_candidates(session_id="session-a"))["candidates"][0]
    request = await service.request_skillpack_candidate(
        recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"], session_id="session-a", channel_id="web",
    )
    await service.submit_evolution_and_notify(
        None, _execution("foreign-after-confirmation"), session_id="session-b", capture_mode="agent", channel_id="web",
    )
    manager = SkillManager(workspace_dir=str(skills.parent))
    result = await service.install_candidate(
        request_id=request["request_id"], recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        package_id=None, integrity=None, skill_manager=manager, session_id="session-a",
    )
    assert result["installed"]
    assert (skills / "writer-pack/SKILL.md").is_file()


@pytest.mark.asyncio
async def test_scope_workers_are_bounded_and_closed(environment):
    service, _, _ = environment
    await service.prepare_skillpack_candidates(session_id="empty-0")
    first = next(iter(service._skillpack_candidate_flows.values()))
    for index in range(1, 9):
        await service.prepare_skillpack_candidates(session_id=f"empty-{index}")
    assert len(service._skillpack_candidate_flows) == 8
    assert first._closed
    retained = tuple(service._skillpack_candidate_flows.values())
    await service.close()
    assert service._skillpack_candidate_flows == {}
    assert all(flow._closed for flow in retained)


@pytest.mark.asyncio
async def test_retained_recipe_cannot_replace_missing_original_evidence(environment):
    service, source, _ = environment
    await _collect(service)
    candidate = (await service.prepare_skillpack_candidates(session_id="session-a"))["candidates"][0]
    request = await service.request_skillpack_candidate(
        recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"], session_id="session-a", channel_id="web",
    )
    # Simulate storage loss in this test's temporary inventory only.
    source.store.evidence_path().unlink()
    assert (await service.prepare_skillpack_candidates(session_id="session-a"))["candidates"] == []
    manager = Mock()
    result = await service.install_candidate(
        request_id=request["request_id"], recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        package_id=None, integrity=None, skill_manager=manager, session_id="session-a",
    )
    assert result["reason"] == "evidence_changed_reconfirm"
    manager.install_symphony_skill_artifact.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["request", "install"])
async def test_trusted_owner_changed_during_review_cannot_authorize_install(environment, monkeypatch, phase):
    service, source, _ = environment
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.trusted_session_user", lambda session: "user-a")
    await _collect(service)
    candidate = (await service.prepare_skillpack_candidates(session_id="session-a"))["candidates"][0]
    arguments = {
        "recipe_id": candidate["recipe_id"], "recipe_version": candidate["recipe_version"],
        "session_id": "session-a", "channel_id": "web",
    }
    if phase == "install":
        request = await service.request_skillpack_candidate(**arguments)
    scoped = next(iter(service._skillpack_candidate_flows.values()))
    prepare = scoped.review_and_prepare_install

    async def change_owner(*args, **kwargs):
        result = await prepare(*args, **kwargs)
        monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.trusted_session_user", lambda session: "user-b")
        return result

    monkeypatch.setattr(scoped, "review_and_prepare_install", change_owner)
    if phase == "request":
        approvals_before = CandidateScopeStore(source.store.root)._read().get("approvals", {})
        service._notify_candidate.reset_mock()
        result = await service.request_skillpack_candidate(**arguments)
        assert not result["success"]
        assert CandidateScopeStore(source.store.root)._read().get("approvals", {}) == approvals_before
        service._notify_candidate.assert_not_awaited()
    else:
        manager = Mock()
        result = await service.install_candidate(
            request_id=request["request_id"], recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
            package_id=None, integrity=None, skill_manager=manager, session_id="session-a",
        )
        manager.install_symphony_skill_artifact.assert_not_called()
    assert result["reason"] == "evidence_scope_changed_reconfirm"


@pytest.mark.asyncio
@pytest.mark.parametrize("installed_before_restart", [False, True])
async def test_scoped_confirmation_and_receipt_survive_service_restart(
    environment, monkeypatch, allow_macos_pytest_temp_sources, installed_before_restart,
):
    service, source, skills = environment
    await _collect(service)
    references = ["trace-0", "trace-1", "trace-2"]
    candidate = (await service.prepare_skillpack_candidates(
        session_id="session-a", source_refs=references,
    ))["candidates"][0]
    request = await service.request_skillpack_candidate(
        recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"], session_id="session-a", channel_id="web",
        source_refs=references,
    )
    manager = SkillManager(workspace_dir=str(skills.parent))
    arguments = {
        "request_id": request["request_id"], "recipe_id": candidate["recipe_id"],
        "recipe_version": candidate["recipe_version"], "package_id": None, "integrity": None,
        "skill_manager": manager, "session_id": "session-a",
    }
    if installed_before_restart:
        assert (await service.install_candidate(**arguments))["installed"]
    await service.close()
    restarted = SwarmSymphonyService()
    monkeypatch.setattr(restarted, "runtime", lambda: SimpleNamespace(flow_engine=source))
    monkeypatch.setattr(restarted, "_runtime_for", lambda _config: SimpleNamespace(flow_engine=source))
    result = await restarted.install_candidate(**arguments)
    assert result["installed"]
    assert result["replayed"] is installed_before_restart
    assert result["newly_installed"] is not installed_before_restart
    assert (skills / "writer-pack/SKILL.md").is_file()
    await restarted.close()
