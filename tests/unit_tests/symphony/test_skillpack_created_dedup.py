"""Created SkillPacks must suppress equivalent automatic creation offers."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from openjiuwen.symphony.flow import PackageReviewGate, SkillPackAdapter, SymphonyFlowEngine
from openjiuwen.symphony.orchestration import SymphonyFlowConfig

from jiuwenswarm.common.host_services import install_runtime_push_handler, restore_runtime_push_handler
from jiuwenswarm.common.symphony_config import symphony_config_from_dict
from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.runtime.symphony.service import SwarmSymphonyService
from jiuwenswarm.runtime.symphony.skillpack_candidates import CandidateEvidenceScope, CandidateScopeStore
from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata, update_session_metadata
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


class _Approver:
    async def review(self, package):
        return "approved"


def _execution(trace_id):
    return {
        "trace_id": trace_id, "query": "Search sources and write a report", "outcome": "success",
        "graph": {
            "id": trace_id, "type": "execution_graph", "directed": True,
            "nodes": {
                f"skill:{name}": {"label": "skill", "metadata": {"capability_type": "skill", "version": "1.0.0"}}
                for name in ("search", "writer")
            },
            "edges": [{
                "source": "skill:search", "target": "skill:writer", "relation": "can_feed",
                "metadata": {"success": True},
            }],
        },
    }


@pytest.fixture
async def created_pack_environment(tmp_path, monkeypatch, allow_macos_pytest_temp_sources):
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_PROFILE", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"symphony": {"evolution": {"flow": {"enabled": True}}}},
    )
    skills = tmp_path / "skills"
    for name in ("search", "writer"):
        directory = skills / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: ordinary member\n---\nDo the task.\n", encoding="utf-8",
        )
    config = symphony_config_from_dict({
        "enabled": True, "paths": {"skills_root": str(skills), "graph_dir": str(tmp_path / "graph")},
        "evolution": {"enabled": True},
    })
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.load_symphony_config", lambda: config)
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.load_execution_disabled_skills", lambda: [])
    for session in ("local-a", "local-b"):
        init_session_metadata(session_id=session, channel_id="web", user_id="")
    services = []
    flows = []

    def start():
        flow = SymphonyFlowEngine(
            get_agent_workspace_dir() / "symphony" / "flow", config=SymphonyFlowConfig(min_successes=3),
            gate=PackageReviewGate(review_agent=_Approver()), skill_adapter=SkillPackAdapter(),
        )

        async def submit(planned, execution, *, session_id, capture_mode):
            return SimpleNamespace(new_candidates=await flow.submit(execution))

        runtime = SimpleNamespace(flow_engine=flow, submit_evolution=submit)
        service = SwarmSymphonyService()
        monkeypatch.setattr(service, "_runtime_for", lambda _config: runtime)
        monkeypatch.setattr(service, "runtime", lambda: runtime)
        services.append(service)
        flows.append(flow)
        return service, flow

    pushes = []

    async def push(message):
        pushes.append(message)
        return True

    previous = install_runtime_push_handler(push)
    try:
        yield SimpleNamespace(start=start, skills=skills, pushes=pushes)
    finally:
        restore_runtime_push_handler(push, previous)
        for service in services:
            await service.close()
        for flow in flows:
            await flow.close()


async def _submit(service, trace_id, session="local-a"):
    return await service.submit_evolution_and_notify(
        None, _execution(trace_id), session_id=session, capture_mode="agent", channel_id="web",
    )


async def _offer(environment):
    service, flow = environment.start()
    for index in range(3):
        await _submit(service, f"trace-{index}")
    assert len(environment.pushes) == 1
    request_id = environment.pushes[0]["payload"]["request_id"]
    approval = CandidateScopeStore(flow.store.root).approval(request_id)
    assert approval["evidence_scope"]["local_workspace"]
    return service, flow, request_id, approval


async def _create(environment):
    service, flow, request_id, approval = await _offer(environment)
    manager = SkillManager(workspace_dir=str(environment.skills.parent))
    receipt = await service.install_candidate(
        request_id=request_id, recipe_id=approval["recipe_id"], recipe_version=approval["recipe_version"],
        package_id=None, integrity=None, skill_manager=manager, session_id="local-a",
    )
    assert receipt["installed"] and receipt["newly_installed"]
    document = environment.skills / approval["name"] / "SKILL.md"
    assert document.is_file()
    return service, flow, document


@pytest.mark.asyncio
@pytest.mark.parametrize("next_session", ["local-a", "local-b"])
@pytest.mark.parametrize("restart", [False, True])
async def test_created_pack_is_not_automatically_offered_again(created_pack_environment, next_session, restart):
    environment = created_pack_environment
    service, flow, document = await _create(environment)
    installed_bytes = document.read_bytes()
    if restart:
        await service.close()
        await flow.close()
        service, flow = environment.start()
    await _submit(service, "trace-after-create", next_session)
    assert len(flow.store.read_evidence()) == 4
    assert document.read_bytes() == installed_bytes
    assert len(environment.pushes) == 1, "The installed Pack was offered for creation again"


@pytest.mark.asyncio
async def test_unconfirmed_creation_is_still_offered_in_new_session(created_pack_environment):
    environment = created_pack_environment
    service, flow, _, approval = await _offer(environment)
    assert not (environment.skills / approval["name"]).exists()
    await _submit(service, "trace-after-unconfirmed", "local-b")
    assert len(environment.pushes) == 2
    assert len(flow.store.read_evidence()) == 4


@pytest.mark.asyncio
async def test_created_candidate_can_still_be_inspected_and_explicitly_requested(created_pack_environment):
    environment = created_pack_environment
    service, _, _ = await _create(environment)
    prepared = await service.prepare_skillpack_candidates(session_id="local-b")
    assert prepared["success"]
    assert len(prepared["candidates"]) == 1
    candidate = prepared["candidates"][0]
    requested = await service.request_skillpack_candidate(
        recipe_id=candidate["recipe_id"], recipe_version=candidate["recipe_version"],
        session_id="local-b", channel_id="web",
    )
    assert requested["awaiting_user_confirmation"]
    assert len(environment.pushes) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "renamed"])
async def test_same_composition_not_offered_when_existing_pack_disabled_or_renamed(
    created_pack_environment, monkeypatch, change,
):
    environment = created_pack_environment
    service, _, document = await _create(environment)
    if change == "disabled":
        monkeypatch.setattr(
            "jiuwenswarm.runtime.symphony.service.load_execution_disabled_skills", lambda: ["writer-pack"],
        )
    else:
        document.write_text(document.read_text(encoding="utf-8").replace("writer-pack", "renamed-pack"), encoding="utf-8")
        document.parent.rename(environment.skills / "renamed-pack")
    await _submit(service, "trace-existing-equivalent", "local-b")
    assert len(environment.pushes) == 1


def _replace_workflow(document, change):
    text = document.read_text(encoding="utf-8")
    before, section = text.split("## Workflow Graph\n", 1)
    graph = json.loads(section.split("```json\n", 1)[1].split("```", 1)[0])
    change(graph["graph"])
    document.write_text(before + "## Workflow Graph\n\n```json\n" + json.dumps(graph) + "\n```\n", encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "reverse", "contradictory_order", "parallel", "different_members", "extra_member", "missing_graph", "ordinary",
    "malformed", "deleted", "symlink_root", "symlink_directory",
])
async def test_only_valid_equivalent_installed_pack_suppresses_creation(created_pack_environment, change):
    environment = created_pack_environment
    service, _, document = await _create(environment)
    if change in {"reverse", "contradictory_order"}:
        _replace_workflow(document, lambda graph: graph.update(edges=[{
            "source": "writer", "target": "search", "relation": "can_feed",
        }]))
        if change == "reverse":
            text = document.read_text(encoding="utf-8")
            document.write_text(text.replace("- search\n- writer\n", "- writer\n- search\n", 1), encoding="utf-8")
    elif change == "parallel":
        _replace_workflow(document, lambda graph: graph.update(edges=[]))
    elif change in {"different_members", "extra_member"}:
        other = environment.skills / "publisher"
        other.mkdir()
        (other / "SKILL.md").write_text("---\nname: publisher\ndescription: ordinary member\n---\nPublish.\n")
        text = document.read_text(encoding="utf-8")
        if change == "different_members":
            text = text.replace("writer", "publisher").replace("publisher-pack", "writer-pack")
            document.write_text(text, encoding="utf-8")
        else:
            document.write_text(text.replace("- writer\n", "- writer\n- publisher\n", 1), encoding="utf-8")
            _replace_workflow(document, lambda graph: graph["nodes"].update(
                publisher={"label": "publisher", "metadata": {"skill": "publisher"}},
            ))
    elif change == "missing_graph":
        text = document.read_text(encoding="utf-8").split("## Workflow Graph\n", 1)[0]
        document.write_text(text, encoding="utf-8")
    elif change == "ordinary":
        document.write_text("---\nname: writer-pack\ndescription: ordinary member\n---\nDo the task.\n", encoding="utf-8")
    elif change == "malformed":
        document.write_text("---\nkind: skillpack\nname: [invalid\n---\n", encoding="utf-8")
    elif change == "symlink_root":
        target = environment.skills.parent / "outside-SKILL.md"
        document.rename(target)
        document.symlink_to(target)
    elif change == "symlink_directory":
        target = environment.skills.parent / "outside-pack"
        document.parent.rename(target)
        document.parent.symlink_to(target, target_is_directory=True)
    else:
        document.unlink()
    await _submit(service, "trace-not-equivalent", "local-b")
    assert len(environment.pushes) == 2


@pytest.mark.asyncio
async def test_workflow_node_ids_are_resolved_to_member_ids(created_pack_environment):
    environment = created_pack_environment
    service, _, document = await _create(environment)

    def rename_nodes(graph):
        graph["nodes"] = {
            "step-one": graph["nodes"]["search"], "step-two": graph["nodes"]["writer"],
        }
        graph["edges"] = [{"source": "step-one", "target": "step-two", "relation": "can_feed"}]

    _replace_workflow(document, rename_nodes)
    await _submit(service, "trace-mapped-node-ids", "local-b")
    assert len(environment.pushes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["duplicate_member", "missing_member", "duplicate_edge", "cycle"])
async def test_incomplete_or_ambiguous_workflow_does_not_suppress_creation(created_pack_environment, change):
    environment = created_pack_environment
    service, _, document = await _create(environment)

    def change_graph(graph):
        if change == "duplicate_member":
            graph["nodes"]["another-search"] = {"metadata": {"skill": "search"}}
        elif change == "missing_member":
            graph["nodes"].pop("writer")
            graph["edges"] = []
        elif change == "duplicate_edge":
            graph["edges"].append(dict(graph["edges"][0]))
        else:
            graph["edges"].append({"source": "writer", "target": "search", "relation": "can_feed"})

    _replace_workflow(document, change_graph)
    await _submit(service, "trace-ambiguous-graph", "local-b")
    assert len(environment.pushes) == 2


@pytest.mark.asyncio
async def test_install_during_review_suppresses_card_before_delivery(created_pack_environment, monkeypatch):
    environment = created_pack_environment
    service, flow, _, approval = await _offer(environment)
    original_flow = next(iter(service._skillpack_candidate_flows.values()))
    artifact = original_flow.store.root / "packages" / approval["package_id"] / "skill"
    manager = SkillManager(workspace_dir=str(environment.skills.parent))
    completed = []

    async def install_during_review(package):
        await asyncio.sleep(0)
        # A second installer commits while the candidate review yields.
        receipt = manager.install_symphony_skill_artifact(
            artifact, expected_root=original_flow.store.root,
            package_id=approval["package_id"], integrity=approval["integrity"],
        )
        completed.append(receipt["success"])
        return "approved"

    monkeypatch.setattr(flow.gate._review_agent, "review", install_during_review)
    await _submit(service, "trace-install-during-review", "local-b")
    assert completed == [True]
    assert (environment.skills / approval["name"] / "SKILL.md").is_file()
    assert len(environment.pushes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "channel"])
async def test_inventory_scan_cannot_preserve_a_changed_approval_owner(created_pack_environment, monkeypatch, change):
    from jiuwenswarm.runtime.symphony import service as service_module
    from jiuwenswarm.server.runtime.session import lifecycle

    environment = created_pack_environment
    service, flow, _, _ = await _offer(environment)
    prepared = await service.prepare_skillpack_candidates(session_id="local-b")
    candidate = prepared["candidates"][0]
    identity = CandidateEvidenceScope.resolve("local-b", "", "current_user", None, flow_root=flow.store.root)
    approvals_before = CandidateScopeStore(flow.store.root).path.read_bytes()
    original_scan = service_module.installed_candidate_structures
    scans = []

    def change_during_scan(skills):
        scans.append(skills)
        if change == "owner":
            update_session_metadata(session_id="local-b", user_id="different-owner", sync=True)
        else:
            metadata_path = lifecycle.resolve_session("local-b") / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["channel_id"] = "feishu"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        return original_scan(skills)

    monkeypatch.setattr(service_module, "installed_candidate_structures", change_during_scan)
    result = await service._offer_reviewed_skillpack_candidate(
        candidate, identity, session_id="local-b", channel_id="web", force=False,
    )
    assert result == {"success": False, "reason": "evidence_scope_changed_reconfirm"}
    assert len(scans) == 1
    assert len(environment.pushes) == 1
    assert CandidateScopeStore(flow.store.root).path.read_bytes() == approvals_before


@pytest.mark.asyncio
async def test_no_eligible_candidate_does_not_scan_installed_inventory(created_pack_environment, monkeypatch):
    from jiuwenswarm.runtime.symphony import service as service_module

    environment = created_pack_environment
    service, flow = environment.start()
    scans = []
    original_scan = service_module.installed_candidate_structures

    def scan(skills):
        scans.append(skills)
        return original_scan(skills)

    monkeypatch.setattr(service_module, "installed_candidate_structures", scan)
    await _submit(service, "only-one-success")
    assert len(flow.store.read_evidence()) == 1
    assert scans == []
    assert environment.pushes == []
