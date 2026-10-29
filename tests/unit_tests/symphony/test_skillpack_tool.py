"""SkillPack creation must use persisted evidence and Host confirmation."""

from __future__ import annotations

import asyncio
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
from jiuwenswarm.symphony.config import symphony_config_from_dict
from jiuwenswarm.symphony.service import SwarmSymphonyService, experience_request_id
from jiuwenswarm.symphony.skillpack_candidates import CandidateScopeStore, member_snapshot
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.symphony.experience import _EdgeEvaluationModelAdapter


class Approver:
    async def review(self, package):
        return "approved"


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
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLPACK_PRODUCTIZATION_ENABLED", "true")
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
    monkeypatch.setattr("jiuwenswarm.symphony.service.load_symphony_config", lambda: config)
    monkeypatch.setattr("jiuwenswarm.symphony.service.load_execution_disabled_skills", lambda: [])
    monkeypatch.setattr("jiuwenswarm.symphony.service.trusted_session_user", lambda session_id: "")
    flow = SymphonyFlowEngine(
        tmp_path / "flow", config=SymphonyFlowConfig(min_successes=3),
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


@pytest.mark.asyncio
async def test_real_evidence_to_reviewed_candidate_and_confirmation_without_install(environment):
    service, flow, skills = environment
    await _collect(service)
    service._notify_candidate.assert_not_called()
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
            monkeypatch.setattr("jiuwenswarm.symphony.service.load_execution_disabled_skills", lambda: ["writer"])
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


@pytest.mark.asyncio
async def test_distill_route_survives_supervisor_and_parallel_tools(monkeypatch):
    monkeypatch.setenv("SKILLPACK_PRODUCTIZATION_ENABLED", "true")
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
    monkeypatch.delenv("SKILLPACK_PRODUCTIZATION_ENABLED", raising=False)
    assert SkillPackToolkit().get_tools() == []


@pytest.mark.parametrize("symphony_enabled,productization_enabled", [(False, False), (False, True), (True, False), (True, True)])
def test_tool_registration_requires_symphony_and_productization(monkeypatch, symphony_enabled, productization_enabled):
    monkeypatch.setenv("SKILLPACK_PRODUCTIZATION_ENABLED", str(productization_enabled).lower())
    tools = SymphonyToolkit().get_tools({"symphony": {"enabled": symphony_enabled}})
    assert ("skillpack_distill" in [tool.card.name for tool in tools]) is (symphony_enabled and productization_enabled)


@pytest.mark.asyncio
async def test_productization_blocks_unscoped_experiment_endpoints(environment):
    service, flow, skills = environment
    await _collect(service)
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
    monkeypatch.setattr("jiuwenswarm.symphony.service.SkillPackAdapter", object)
    assert (await service.prepare_skillpack_candidates(session_id="session-a"))["reason"] == "core_skillpack_contract_missing"


@pytest.mark.asyncio
async def test_same_user_cross_session_evidence_is_reusable_but_other_user_is_not(environment, monkeypatch):
    service, flow, skills = environment
    owners = {"session-a": "user-a", "session-b": "user-a", "session-c": "user-b"}
    monkeypatch.setattr("jiuwenswarm.symphony.service.trusted_session_user", lambda session: owners.get(session, ""))
    for index, session in enumerate(("session-a", "session-b", "session-a")):
        await service.submit_evolution_and_notify(
            None, _execution(f"trace-{index}"), session_id=session, capture_mode="agent", channel_id="web",
        )
    assert len((await service.prepare_skillpack_candidates(session_id="session-b"))["candidates"]) == 1
    assert (await service.prepare_skillpack_candidates(session_id="session-b", scope="current_session"))["candidates"] == []
    assert (await service.prepare_skillpack_candidates(session_id="session-c"))["candidates"] == []
    assert (await service.prepare_skillpack_candidates(session_id="session-a", source_refs=["forged-trace"]))["candidates"] == []


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
    monkeypatch.setattr("jiuwenswarm.symphony.service.trusted_session_user", lambda session: owners.get(session, ""))
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
    monkeypatch.setattr("jiuwenswarm.symphony.service.trusted_session_user", lambda session: "user-a")
    await _collect(service)
    candidate = (await service.prepare_skillpack_candidates(session_id="session-a"))["candidates"][0]
    request = await service.request_skillpack_candidate(
        recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"], session_id="session-a", channel_id="web",
    )
    monkeypatch.setattr("jiuwenswarm.symphony.service.trusted_session_user", lambda session: "user-b")
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
    monkeypatch.setattr("jiuwenswarm.symphony.service.trusted_session_user", lambda session: "user-a")
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
        monkeypatch.setattr("jiuwenswarm.symphony.service.trusted_session_user", lambda session: "user-b")
        return result

    monkeypatch.setattr(scoped, "review_and_prepare_install", change_owner)
    if phase == "request":
        result = await service.request_skillpack_candidate(**arguments)
        assert not result["success"]
        assert CandidateScopeStore(source.store.root)._read().get("approvals", {}) == {}
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
    result = await restarted.install_candidate(**arguments)
    assert result["installed"]
    assert result["replayed"] is installed_before_restart
    assert result["newly_installed"] is not installed_before_restart
    assert (skills / "writer-pack/SKILL.md").is_file()
    await restarted.close()
