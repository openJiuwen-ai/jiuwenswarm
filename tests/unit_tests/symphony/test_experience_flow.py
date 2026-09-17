"""JiuwenSwarm wiring tests for Symphony experience candidates."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import openjiuwen.symphony as core_symphony
import pytest
from openjiuwen.agent_evolving.trajectory.model import Trajectory
from openjiuwen.agent_evolving.trajectory.schema import SESSION_ID, TRAJECTORY_ID
from openjiuwen.agent_evolving.trajectory.spans import attributes_from_map
from openjiuwen.extensions.observability import semconv
from openjiuwen.harness.rails.evolution import (
    SymphonyEdgeDecision,
    build_symphony_edge_candidates,
    build_symphony_execution_graph,
    project_symphony_execution_fragments,
)

from jiuwenswarm.runtime.host_services import (
    install_runtime_push_handler,
    restore_runtime_push_handler,
)
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
    _FORWARD_NO_LOCAL_HANDLER_METHODS,
    _FORWARD_REQ_METHODS,
)
from jiuwenswarm.server.runtime.agent_adapter.interface import _SKILL_ROUTES
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.skill.skill_manager import (
    ERROR_SKILL_INVALID_PACKAGE,
    SkillManager,
    SkillRpcError,
)
from jiuwenswarm.server.runtime.skill.skillpack import load_skillpack
from jiuwenswarm.symphony.config import symphony_config_from_dict
from jiuwenswarm.symphony.experience import (
    JiuwenSwarmSkillAdapter,
    PublishedCapabilitySnapshotProvider,
    SkillPackNotInstallableError,
)
from jiuwenswarm.symphony.llm import LLMConfig
from jiuwenswarm.symphony.service import (
    SwarmSymphonyService,
    experience_request_id,
)
from jiuwenswarm.agents.swarm import config_specs
from jiuwenswarm.agents.swarm.providers import evolution_rails


def _candidate() -> core_symphony.CombinationCandidate:
    return core_symphony.CombinationCandidate(
        recipe_id="recipe-1",
        version=2,
        name="研究组合",
        applicability="适合检索后生成报告",
        structure=(("search", "writer", "can_feed"),),
        execution_count=2,
        success_count=2,
        success_rate=1.0,
    )


def _enable_evolution_config(monkeypatch, tmp_path: Path):
    config = symphony_config_from_dict(
        {
            "enabled": True,
            "paths": {"graph_dir": str(tmp_path / "graph")},
            "evolution": {"enabled": True},
        }
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config", lambda: config
    )
    return config


def _install_service(monkeypatch, flow):
    service = SwarmSymphonyService()
    service._runtime = SimpleNamespace(flow_engine=flow)
    monkeypatch.setattr(service, "runtime", lambda: service._runtime)
    return service


def _review_flow(tmp_path, *, package=None, verdict="approved", artifact=None):
    return SimpleNamespace(
        store=SimpleNamespace(
            root=tmp_path,
            artifact_dir=lambda package_id, target_kind: (
                tmp_path / "packages" / package_id / target_kind
            ),
        ),
        review_and_prepare_install=AsyncMock(
            return_value=SimpleNamespace(
                verdict=verdict,
                package=package,
                artifact_dir=str(artifact) if artifact is not None else None,
                reasons=[] if verdict == "approved" else ["manual review required"],
            )
        ),
    )


def _server_package(monkeypatch, package_id, integrity="sha256:value"):
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.CapabilityPackager.verify_package_integrity",
        lambda _package: True,
    )
    return {
        "package_id": package_id,
        "integrity": integrity,
        "meta_name": "search-writer-pack",
        "materials": {
            "recipe": {
                "applicability": {"task_description": "先检索再生成报告"},
                "combination_structure": {
                    "nodes": {
                        "search": {"metadata": {"capability_type": "skill"}},
                        "writer": {"metadata": {"capability_type": "skill"}},
                    },
                    "edges": [
                        {
                            "source": "search",
                            "target": "writer",
                            "relation": "can_feed",
                        }
                    ],
                },
                "execution_narrative": "先检索，再生成报告。",
            }
        },
    }


def _write_member_skills(workspace: Path) -> None:
    for name in ("search", "writer"):
        skill_dir = workspace / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} skill\n---\n# {name}\n",
            encoding="utf-8",
        )


def _write_skillpack_artifact(root: Path, name: str, *, marker: str = "") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "kind: skillpack\n"
        "description: combined\n"
        "skills:\n"
        "  - search\n"
        "  - writer\n"
        "---\n"
        f"# {name}\n\n"
        "## When to use\n\nComplete tasks.\n\n"
        "## Do not use\n\nSingle tasks.\n\n"
        "## Required inputs\n\nUser goal.\n\n"
        "## Side effects and confirmation\n\nFollow permissions.\n\n"
        "## Included Skills\n\n- `search`\n- `writer`\n\n"
        "## Execution Process\n\nSearch then write.\n\n"
        "## Failure handling\n\nReturn partial results.\n\n"
        "## Final output\n\nReturn the report.\n"
        f"{marker}",
        encoding="utf-8",
    )


def _reject_install_manager(message):
    def reject(*args, **kwargs):
        pytest.fail(message)

    return SimpleNamespace(install_symphony_skill_artifact=Mock(side_effect=reject))


def _patch_runtime_construction(monkeypatch, flow, runtime, model):
    monkeypatch.setattr("jiuwenswarm.symphony.service.SymphonyFlowEngine", flow)
    monkeypatch.setattr("jiuwenswarm.symphony.service.SymphonyRuntime", runtime)
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.model_from_config", lambda _: model
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.model_response_observer_from_config",
        lambda _: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.LLMConfig.from_default_model",
        lambda: LLMConfig(model="default"),
    )


@pytest.fixture
def rail_capture(monkeypatch):
    submissions = []
    captured = {}

    async def submit(planned_graph, execution_graph, **kwargs):
        submissions.append(kwargs)

    service = SimpleNamespace(
        runtime=lambda: SimpleNamespace(
            capture_graph_snapshot=lambda: {"static_revision": "r"}
        ),
        submit_evolution_and_notify=submit,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.get_swarm_symphony_service", lambda: service
    )

    def rail_factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    def install(name):
        monkeypatch.setattr(f"openjiuwen.harness.rails.evolution.{name}", rail_factory)
        return captured, submissions

    return install


def test_evolution_is_the_only_core_experience_switch(tmp_path: Path) -> None:
    config = symphony_config_from_dict({"paths": {"graph_dir": str(tmp_path)}})

    assert config.evolution.enabled is False
    assert not hasattr(config.evolution, "backend")
    assert not hasattr(config, "flow")


def test_experience_candidate_server_methods_are_routable() -> None:
    expected = {
        ReqMethod.SKILLS_EXPERIENCE_LIST: "handle_skills_experience_list",
        ReqMethod.SKILLS_EXPERIENCE_REQUEST: "handle_skills_experience_request",
    }

    for method, handler in expected.items():
        assert _SKILL_ROUTES[method] == handler
        assert method.value in _FORWARD_REQ_METHODS
        assert method.value in _FORWARD_NO_LOCAL_HANDLER_METHODS


def test_target_adapter_adds_installable_frontmatter(tmp_path: Path) -> None:
    package = {
        "target_kind": "skill",
        "meta_name": "research-writer",
        "materials": {
            "recipe": {
                "applicability": {"task_description": "先检索再生成报告"},
                "combination_structure": {
                    "nodes": {
                        "search": {"metadata": {"capability_type": "skill"}},
                        "writer": {"metadata": {"capability_type": "skill"}},
                    },
                    "edges": [
                        {
                            "source": "search",
                            "target": "writer",
                            "relation": "can_feed",
                        }
                    ],
                },
                "execution_narrative": "按顺序执行。",
            },
            "swarmflow_script": "def run():\n    return None\n",
        },
    }

    JiuwenSwarmSkillAdapter.render(package, tmp_path)

    text = (tmp_path / "SKILL.md").read_text(encoding="utf-8")
    definition = load_skillpack(tmp_path, expected_name="research-writer")
    assert text.startswith("---\n")
    assert "name: research-writer" in text
    assert "kind: skillpack" in text
    assert "skills:\n- search\n- writer" in text
    assert "description:" in text
    assert "## Execution Process" in text
    assert '"type": "skillpack_workflow"' in text
    assert definition.members == ("search", "writer")
    assert definition.workflow_graph is not None
    assert not (tmp_path / "swarmflow").exists()
    assert not (tmp_path / "dependencies.yaml").exists()


def test_target_adapter_rejects_non_skill_nodes(monkeypatch, tmp_path: Path) -> None:
    package = _server_package(
        monkeypatch,
        "cap-tool",
    )
    package["materials"]["recipe"]["combination_structure"]["nodes"]["search"][
        "metadata"
    ]["capability_type"] = "tool"

    with pytest.raises(SkillPackNotInstallableError):
        JiuwenSwarmSkillAdapter.render(package, tmp_path)


@pytest.mark.parametrize(
    "edges",
    [
        [
            {"source": "search", "target": "writer", "relation": "can_feed"},
            {"source": "search", "target": "reviewer", "relation": "can_feed"},
        ],
        [
            {"source": "search", "target": "writer", "relation": "can_feed"},
            {"source": "writer", "target": "search", "relation": "can_feed"},
        ],
    ],
)
def test_target_adapter_rejects_branch_and_loop_structures(
    monkeypatch,
    tmp_path: Path,
    edges: list[dict[str, str]],
) -> None:
    package = _server_package(monkeypatch, "cap-graph")
    nodes = package["materials"]["recipe"]["combination_structure"]["nodes"]
    if any(edge["target"] == "reviewer" for edge in edges):
        nodes["reviewer"] = {"metadata": {"capability_type": "skill"}}
    package["materials"]["recipe"]["combination_structure"]["edges"] = edges

    with pytest.raises(SkillPackNotInstallableError):
        JiuwenSwarmSkillAdapter.render(package, tmp_path)


def _otlp_span(name: str, span_id: int, *, skill: str | None = None) -> dict:
    attributes = {}
    if skill is not None:
        attributes = {
            semconv.GEN_AI_TOOL_NAME: "skill_tool",
            semconv.GEN_AI_TOOL_CALL_ARGUMENTS: {
                "skill_name": skill,
                "relative_file_path": "SKILL.md",
            },
            semconv.GEN_AI_TOOL_CALL_RESULT: {"success": True},
        }
    return {
        "traceId": f"{1:032x}",
        "spanId": f"{span_id:016x}",
        "parentSpanId": f"{1:016x}" if span_id != 1 else None,
        "name": name,
        "attributes": attributes_from_map(attributes),
        "status": {"code": "STATUS_CODE_OK"},
        "startTimeUnixNano": str(span_id),
        "endTimeUnixNano": str(span_id + 1),
    }


def test_published_capability_snapshot_builds_nonempty_execution_edge(
    tmp_path: Path,
) -> None:
    graph_dir = tmp_path / "graph"
    graph_dir.mkdir()
    capabilities = [
        {
            "capability_id": "search",
            "capability_type": "skill",
            "name": "search",
            "version": "1.0.0",
            "outputs": [{"name": "result"}],
        },
        {
            "capability_id": "writer",
            "capability_type": "skill",
            "name": "writer",
            "version": "2.0.0",
            "inputs": [{"name": "result"}],
        },
    ]
    (graph_dir / "graph.json").write_text(
        json.dumps(
            {
                "capabilities": capabilities,
                "capability_hashes": {
                    "skill:search": "hash-search",
                    "skill:writer": "hash-writer",
                },
            }
        ),
        encoding="utf-8",
    )
    (graph_dir / "fingerprint.json").write_text(
        json.dumps({"fingerprints": capabilities}),
        encoding="utf-8",
    )
    spans = [
        _otlp_span("agent.main", 1),
        _otlp_span("tool.skill_tool", 2, skill="search"),
        _otlp_span("tool.lookup", 3),
        _otlp_span("tool.skill_tool", 4, skill="writer"),
    ]
    spans[0].pop("parentSpanId")
    trajectory = Trajectory.from_otlp(
        {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": attributes_from_map(
                            {TRAJECTORY_ID: "trajectory-1", SESSION_ID: "session-1"}
                        )
                    },
                    "scopeSpans": [{"scope": {"name": "test"}, "spans": spans}],
                }
            ]
        }
    )
    continuities = ((0, trajectory),)
    fragments = project_symphony_execution_fragments(continuities)
    candidates = build_symphony_edge_candidates(fragments, continuities)
    assert candidates
    candidate = candidates[0]
    decision = SymphonyEdgeDecision(
        candidate_id=candidate.candidate_id,
        source_fragment_id=candidate.source_fragment.fragment_id,
        target_fragment_id=candidate.target_fragment.fragment_id,
        status="success",
        reason="target consumed the source artifact",
        evidence_refs=candidate.evidence_refs,
        evidence_method="model_assisted",
        evidence_strength="low",
    )
    identities = PublishedCapabilitySnapshotProvider(graph_dir).snapshot_capabilities()
    execution_graph = build_symphony_execution_graph(
        trace_id=f"{1:032x}",
        query="search then write",
        outcome="success",
        candidates=candidates,
        decisions=(decision,),
        capability_snapshot=identities,
    )

    assert {item.capability_id for item in identities} == {"search", "writer"}
    assert execution_graph["graph"]["edges"]


def test_core_flow_ignores_legacy_distill_switch(monkeypatch, tmp_path: Path) -> None:
    config = symphony_config_from_dict(
        {
            "enabled": True,
            "paths": {"graph_dir": str(tmp_path / "graph")},
            "evolution": {"enabled": True, "flow": {"enabled": False}},
        }
    )
    service = SwarmSymphonyService()
    model = object()
    created: dict[str, object] = {}

    class ReviewAgent:
        def __init__(self, value) -> None:
            created["review_model"] = value

    class Gate:
        def __init__(self, agent) -> None:
            created["review_agent"] = agent

    class Flow:
        def __init__(self, root, **kwargs) -> None:
            created["flow_root"] = root
            created.update(kwargs)

    class Runtime:
        def __init__(self, **kwargs) -> None:
            created["runtime"] = kwargs

    _patch_runtime_construction(monkeypatch, Flow, Runtime, model)
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.LLMPackageReviewAgent", ReviewAgent
    )
    monkeypatch.setattr("jiuwenswarm.symphony.service.PackageReviewGate", Gate)
    runtime = service._runtime_for(config)

    assert runtime is service._runtime
    assert created["review_model"] is model
    assert created["review_agent"] is not None
    assert created["flow_root"] == tmp_path / "flow"
    assert created["llm_client"] is model
    assert isinstance(created["skill_adapter"], JiuwenSwarmSkillAdapter)
    assert isinstance(created["runtime"]["flow_engine"], Flow)


@pytest.mark.asyncio
async def test_service_start_recovers_candidates_when_flow_is_enabled(
    monkeypatch, tmp_path: Path
) -> None:
    _enable_evolution_config(monkeypatch, tmp_path)
    service = SwarmSymphonyService()
    starts = 0

    class Flow:
        async def start(self):
            nonlocal starts
            starts += 1
            return (_candidate(),)

    runtime = SimpleNamespace(flow_engine=Flow())
    monkeypatch.setattr(service, "_runtime_for", lambda _config: runtime)

    await service.start()
    await service.start()

    assert starts == 1
    assert service._recovered_candidates == (_candidate(),)


@pytest.mark.asyncio
async def test_candidate_push_is_concurrency_idempotent(monkeypatch) -> None:
    service = SwarmSymphonyService()
    pushes: list[dict] = []

    async def push(message: dict) -> bool:
        pushes.append(message)
        return True

    class Flow:
        def __init__(self) -> None:
            self.acknowledged: list[tuple[str, int]] = []

        def acknowledge_candidate(self, recipe_id: str, version: int) -> bool:
            self.acknowledged.append((recipe_id, version))
            return True

    flow = Flow()
    service._runtime = SimpleNamespace(flow_engine=flow)
    previous = install_runtime_push_handler(push)
    try:
        await asyncio.gather(
            service._notify_candidate(_candidate(), session_id="s1", channel_id="web"),
            service._notify_candidate(_candidate(), session_id="s1", channel_id="web"),
        )
    finally:
        restore_runtime_push_handler(push, previous)

    assert len(pushes) == 1
    assert flow.acknowledged == [("recipe-1", 2)]
    payload = pushes[0]["payload"]
    assert payload["event_type"] == "chat.ask_user_question"
    assert [item["label"] for item in payload["questions"][0]["options"]] == [
        "安装",
        "稍后",
    ]


@pytest.mark.asyncio
async def test_successful_candidate_push_stays_deduplicated_when_ack_fails(
    monkeypatch,
) -> None:
    service = SwarmSymphonyService()
    pushes: list[dict] = []

    async def push(message: dict) -> bool:
        pushes.append(message)
        return True

    class Flow:
        @staticmethod
        def acknowledge_candidate(recipe_id: str, version: int) -> bool:
            raise OSError("store unavailable")

    service._runtime = SimpleNamespace(flow_engine=Flow())
    previous = install_runtime_push_handler(push)
    try:
        await service._notify_candidate(_candidate(), session_id="s1", channel_id="web")
        await service._notify_candidate(_candidate(), session_id="s1", channel_id="web")
    finally:
        restore_runtime_push_handler(push, previous)

    assert len(pushes) == 1


@pytest.mark.asyncio
async def test_deferred_candidate_can_be_listed_and_requested_again(
    monkeypatch, tmp_path: Path
) -> None:
    _enable_evolution_config(monkeypatch, tmp_path)
    candidate = _candidate()

    class Flow:
        @staticmethod
        def list_candidates():
            return (candidate,)

        @staticmethod
        def get_candidate(recipe_id, *, version):
            return candidate if (recipe_id, version) == ("recipe-1", 2) else None

        @staticmethod
        def acknowledge_candidate(*args):
            return True

    service = SwarmSymphonyService()
    service._runtime = SimpleNamespace(flow_engine=Flow())
    service._runtime_key = ("stable",)
    monkeypatch.setattr(service, "_runtime_for", lambda _config: service._runtime)
    pushes: list[dict] = []

    async def push(message: dict) -> bool:
        pushes.append(message)
        return True

    previous = install_runtime_push_handler(push)
    try:
        listed = service.list_experience_candidates()
        requested = await service.request_experience_candidate(
            recipe_id="recipe-1",
            recipe_version=2,
            session_id="session-1",
            channel_id="web",
        )
    finally:
        restore_runtime_push_handler(push, previous)

    assert listed["candidates"][0]["recipe_id"] == "recipe-1"
    assert requested["success"] is True
    assert pushes[0]["payload"]["event_type"] == "chat.ask_user_question"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"path": "/tmp/client-controlled"},
        {"target_dir": "/tmp/client-controlled"},
        {"nested": {"artifact_path": "/tmp/client-controlled"}},
        {"nested": [{"output-root": "/tmp/client-controlled"}]},
    ],
)
async def test_answer_rejects_any_client_path_field_without_installing(extra) -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    params = {
        "evolution_meta": {"recipe_id": "recipe-1", "recipe_version": 2},
        **extra,
    }

    result = await adapter._handle_symphony_experience_answer(
        experience_request_id("recipe-1", 2),
        [{"selected_options": ["安装"]}],
        params,
    )

    assert result == {
        "accepted": False,
        "resolved": False,
        "reason": "client_path_rejected",
    }


@pytest.mark.asyncio
async def test_later_keeps_candidate_without_preparing_install() -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    request_id = experience_request_id("recipe-1", 2)

    result = await adapter._handle_symphony_experience_answer(
        request_id,
        [{"selected_options": ["稍后"]}],
        {"evolution_meta": {"recipe_id": "recipe-1", "recipe_version": 2}},
    )

    assert result["deferred"] is True
    assert result["installed"] is False
    assert result["request_id"] == request_id


@pytest.mark.asyncio
async def test_web_transport_project_dir_does_not_reject_later_answer() -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    request_id = experience_request_id("recipe-1", 2)

    result = await adapter._handle_symphony_experience_answer(
        request_id,
        [{"selected_options": ["稍后"]}],
        {
            "project_dir": "/workspace/project",
            "cwd": "/workspace/project",
            "trusted_dirs": ["/workspace/project"],
            "evolution_meta": {"recipe_id": "recipe-1", "recipe_version": 2},
        },
    )

    assert result["accepted"] is True
    assert result["deferred"] is True


@pytest.mark.asyncio
async def test_install_requires_approved_server_artifact(
    monkeypatch, tmp_path: Path
) -> None:
    _enable_evolution_config(monkeypatch, tmp_path)
    package = _server_package(monkeypatch, "cap-123")
    artifact = tmp_path / "packages" / "cap-123" / "skill"
    artifact.mkdir(parents=True)
    (artifact / "SKILL.md").write_text("MALICIOUS CACHE", encoding="utf-8")

    flow = _review_flow(tmp_path, package=package, artifact=artifact)
    service = _install_service(monkeypatch, flow)
    calls: list[Path] = []

    class Manager:
        def install_symphony_skill_artifact(self, artifact_dir, **kwargs):
            assert "MALICIOUS CACHE" not in (Path(artifact_dir) / "SKILL.md").read_text(
                encoding="utf-8"
            )
            calls.append(Path(artifact_dir))
            return {"success": True, "skill": {"name": "combo"}}

    request_id = experience_request_id("recipe-1", 2)
    receipt = await service.install_candidate(
        request_id=request_id,
        recipe_id="recipe-1",
        recipe_version=2,
        package_id=None,
        integrity=None,
        skill_manager=Manager(),
    )
    duplicate = await service.install_candidate(
        request_id=request_id,
        recipe_id="recipe-1",
        recipe_version=2,
        package_id="cap-123",
        integrity="sha256:value",
        skill_manager=Manager(),
    )

    assert receipt["installed"] is True
    assert duplicate == {**receipt, "newly_installed": False, "replayed": True}
    assert len(calls) == 1
    assert calls[0].parent == tmp_path
    assert calls[0].name.startswith(".symphony-install-")
    assert not calls[0].exists()

    restarted = _install_service(
        monkeypatch, _review_flow(tmp_path, package=package, artifact=artifact)
    )
    persisted = await restarted.install_candidate(
        request_id=request_id,
        recipe_id="recipe-1",
        recipe_version=2,
        package_id="cap-123",
        integrity="sha256:value",
        skill_manager=Manager(),
    )
    assert persisted == {**receipt, "newly_installed": False, "replayed": True}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_receipt_write_crash_recovers_installed_skill_after_restart(
    monkeypatch, tmp_path: Path, allow_macos_pytest_temp_sources
) -> None:
    _enable_evolution_config(monkeypatch, tmp_path)
    package = _server_package(monkeypatch, "cap-crash")
    from jiuwenswarm.symphony import service as service_module

    real_save = service_module._save_install_receipt
    first = _install_service(monkeypatch, _review_flow(tmp_path, package=package))
    workspace = tmp_path / "workspace"
    _write_member_skills(workspace)
    manager = SkillManager(workspace_dir=str(workspace))
    monkeypatch.setattr(
        service_module,
        "_save_install_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("receipt crash")),
    )
    request_id = experience_request_id("recipe-1", 2)

    with pytest.raises(OSError, match="receipt crash"):
        await first.install_candidate(
            request_id=request_id,
            recipe_id="recipe-1",
            recipe_version=2,
            package_id=None,
            integrity=None,
            skill_manager=manager,
        )

    monkeypatch.setattr(service_module, "_save_install_receipt", real_save)
    restarted = _install_service(monkeypatch, _review_flow(tmp_path, package=package))
    restarted_manager = SkillManager(workspace_dir=str(workspace))
    monkeypatch.setattr(
        restarted_manager,
        "install_symphony_skill_artifact",
        lambda *args, **kwargs: pytest.fail("recovery must not copy twice"),
    )

    receipt = await restarted.install_candidate(
        request_id=request_id,
        recipe_id="recipe-1",
        recipe_version=2,
        package_id=None,
        integrity=None,
        skill_manager=restarted_manager,
    )

    assert receipt["installed"] is True
    assert receipt["newly_installed"] is True
    assert receipt["skill"]["name"] == "search-writer-pack"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict", ["rejected", "needs_human_review", "not_installable"]
)
async def test_non_approved_review_is_persistently_idempotent(
    monkeypatch, tmp_path: Path, verdict: str
) -> None:
    _enable_evolution_config(monkeypatch, tmp_path)
    flow = _review_flow(tmp_path, verdict=verdict)
    service = _install_service(monkeypatch, flow)
    manager = _reject_install_manager("non-approved package must not be installed")

    result = await service.install_candidate(
        request_id=experience_request_id("recipe-1", 2),
        recipe_id="recipe-1",
        recipe_version=2,
        package_id=None,
        integrity=None,
        skill_manager=manager,
    )
    duplicate = await service.install_candidate(
        request_id=experience_request_id("recipe-1", 2),
        recipe_id="recipe-1",
        recipe_version=2,
        package_id=None,
        integrity=None,
        skill_manager=manager,
    )

    restarted_flow = _review_flow(tmp_path, verdict=verdict)
    restarted = _install_service(monkeypatch, restarted_flow)
    persisted = await restarted.install_candidate(
        request_id=experience_request_id("recipe-1", 2),
        recipe_id="recipe-1",
        recipe_version=2,
        package_id=None,
        integrity=None,
        skill_manager=manager,
    )

    assert result["installed"] is False
    assert result["reason"] == verdict
    assert duplicate == {**result, "replayed": True}
    assert persisted == {**result, "replayed": True}
    assert flow.review_and_prepare_install.await_count == 1
    restarted_flow.review_and_prepare_install.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_package_credentials_must_match_server_package(
    monkeypatch, tmp_path: Path
) -> None:
    _enable_evolution_config(monkeypatch, tmp_path)
    package = _server_package(monkeypatch, "server-package", "sha256:server")
    artifact = tmp_path / "packages" / "server-package" / "skill"
    artifact.mkdir(parents=True)

    service = _install_service(
        monkeypatch, _review_flow(tmp_path, package=package, artifact=artifact)
    )

    result = await service.install_candidate(
        request_id=experience_request_id("recipe-1", 2),
        recipe_id="recipe-1",
        recipe_version=2,
        package_id="client-package",
        integrity="sha256:client",
        skill_manager=_reject_install_manager(
            "mismatched client package credentials must not install"
        ),
    )

    assert result == {"installed": False, "reason": "package_id_mismatch"}


def test_skill_manager_installs_only_server_owned_artifact(
    tmp_path: Path,
    allow_macos_pytest_temp_sources,
) -> None:
    root = tmp_path / "flow"
    artifact = root / "packages" / "cap-1" / "skill"
    _write_skillpack_artifact(artifact, "combo-skill")
    workspace = tmp_path / "workspace"
    _write_member_skills(workspace)
    manager = SkillManager(workspace_dir=str(workspace))

    result = manager.install_symphony_skill_artifact(
        artifact,
        expected_root=root,
        package_id="cap-1",
        integrity="sha256:value",
    )

    assert result["success"] is True
    assert (tmp_path / "workspace" / "skills" / "combo-skill" / "SKILL.md").is_file()


def test_skill_manager_does_not_install_pack_with_missing_member(
    tmp_path: Path,
    allow_macos_pytest_temp_sources,
) -> None:
    root = tmp_path / "flow"
    artifact = root / "packages" / "cap-missing" / "skill"
    _write_skillpack_artifact(artifact, "missing-member-pack")
    workspace = tmp_path / "workspace"
    _write_member_skills(workspace)
    (workspace / "skills" / "writer" / "SKILL.md").unlink()
    manager = SkillManager(workspace_dir=str(workspace))

    with pytest.raises(SkillRpcError) as exc:
        manager.install_symphony_skill_artifact(
            artifact,
            expected_root=root,
            package_id="cap-missing",
            integrity="sha256:value",
        )

    assert exc.value.code == ERROR_SKILL_INVALID_PACKAGE
    assert not (workspace / "skills" / "missing-member-pack").exists()


def test_skill_manager_installs_pack_with_disabled_member_as_unavailable(
    tmp_path: Path,
    allow_macos_pytest_temp_sources,
) -> None:
    root = tmp_path / "flow"
    artifact = root / "packages" / "cap-disabled" / "skill"
    _write_skillpack_artifact(artifact, "disabled-member-pack")
    workspace = tmp_path / "workspace"
    _write_member_skills(workspace)
    manager = SkillManager(workspace_dir=str(workspace))
    manager.set_skill_enabled("search", False)

    result = manager.install_symphony_skill_artifact(
        artifact,
        expected_root=root,
        package_id="cap-disabled",
        integrity="sha256:value",
    )

    assert result["success"] is True
    assert result["skill"]["skill_type"] == "skillpack"
    assert manager.get_skill_enabled("disabled-member-pack") is True
    assert "disabled-member-pack" in manager.list_execution_disabled_skills()


@pytest.mark.asyncio
async def test_install_candidate_reports_missing_member_as_not_installable(
    monkeypatch,
    tmp_path: Path,
    allow_macos_pytest_temp_sources,
) -> None:
    _enable_evolution_config(monkeypatch, tmp_path)
    package = _server_package(monkeypatch, "cap-missing")
    service = _install_service(monkeypatch, _review_flow(tmp_path, package=package))
    workspace = tmp_path / "workspace"
    _write_member_skills(workspace)
    (workspace / "skills" / "writer" / "SKILL.md").unlink()
    manager = SkillManager(workspace_dir=str(workspace))

    result = await service.install_candidate(
        request_id=experience_request_id("recipe-1", 2),
        recipe_id="recipe-1",
        recipe_version=2,
        package_id=None,
        integrity=None,
        skill_manager=manager,
    )

    assert result["installed"] is False
    assert result["reason"] == "not_installable"
    assert not (workspace / "skills" / "search-writer-pack").exists()


def test_skill_install_recovers_copy_before_state_crash(
    monkeypatch, tmp_path: Path, allow_macos_pytest_temp_sources
) -> None:
    root = tmp_path / "flow"
    artifact = root / "staging"
    _write_skillpack_artifact(artifact, "recovered-combo")
    workspace = tmp_path / "workspace"
    _write_member_skills(workspace)
    first = SkillManager(workspace_dir=str(workspace))
    monkeypatch.setattr(
        first,
        "_add_local_skill",
        lambda _record: (_ for _ in ()).throw(SystemExit("crash after copy")),
    )

    with pytest.raises(SystemExit, match="crash after copy"):
        first.install_symphony_skill_artifact(
            artifact,
            expected_root=root,
            package_id="cap-recovered",
            integrity="sha256:value",
        )

    restarted = SkillManager(workspace_dir=str(workspace))
    recovered = restarted.recover_symphony_skill_install(
        artifact,
        package_id="cap-recovered",
        integrity="sha256:value",
    )

    assert recovered is not None and recovered["success"] is True
    record = next(
        item
        for item in restarted.get_local_skills()
        if item["name"] == "recovered-combo"
    )
    assert record["origin"] == "symphony:cap-recovered"


def test_skill_install_does_not_rebind_different_local_content(
    tmp_path: Path, allow_macos_pytest_temp_sources
) -> None:
    root = tmp_path / "flow"
    artifact = root / "staging"
    _write_skillpack_artifact(artifact, "local-combo", marker="expected\n")
    workspace = tmp_path / "workspace"
    _write_member_skills(workspace)
    local = workspace / "skills" / "local-combo"
    _write_skillpack_artifact(local, "local-combo", marker="local\n")
    manager = SkillManager(workspace_dir=str(workspace))

    assert (
        manager.recover_symphony_skill_install(
            artifact,
            package_id="cap-foreign",
            integrity="sha256:value",
        )
        is None
    )
    record = next(
        item for item in manager.get_local_skills() if item["name"] == "local-combo"
    )
    assert record["origin"] != "symphony:cap-foreign"


def test_team_graph_rail_spec_is_leader_only() -> None:
    config = {
        "symphony": {
            "enabled": True,
            "evolution": {"enabled": True},
        },
        "react": {"evolution": {"skill_evolution": False}},
    }

    leader = config_specs._role_evolution_rails(config, "leader")
    teammate = config_specs._role_evolution_rails(config, "teammate")

    assert [rail.type for rail in leader] == ["swarm.symphony_graph_evolution"]
    assert teammate == []


def test_team_graph_rail_provider_skips_members(monkeypatch) -> None:
    context = SimpleNamespace(
        role="teammate",
        config={
            "symphony": {
                "enabled": True,
                "evolution": {"enabled": True},
            }
        },
    )

    assert evolution_rails.build_symphony_graph_evolution_rail({}, context) is None


@pytest.mark.asyncio
async def test_core_graph_plan_does_not_load_legacy_overlay(
    monkeypatch, tmp_path: Path
) -> None:
    config = symphony_config_from_dict(
        {
            "enabled": True,
            "paths": {"graph_dir": str(tmp_path)},
            "evolution": {"enabled": True},
        }
    )
    service = SwarmSymphonyService()
    calls: list[dict] = []

    class GraphEngine:
        async def plan(self, query: str, **kwargs):
            calls.append({"query": query, **kwargs})
            return SimpleNamespace(
                to_dict=lambda: {
                    "planned_graph": {
                        "graph": {"type": "planned_graph", "nodes": {}, "edges": []}
                    }
                }
            )

    runtime = SimpleNamespace(graph_engine=GraphEngine(), graph_scope_id="scope")
    monkeypatch.setattr(service, "_runtime_for", lambda _config: runtime)
    monkeypatch.setattr(
        service, "graph_status", lambda: _async_value({"success": True, "exists": True})
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.evolution.service.load_dynamic_overlay",
        lambda _path: pytest.fail("legacy overlay must not be loaded"),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.get_config",
        lambda: {"preferred_language": "zh"},
    )

    result = await service.plan("query")

    assert result["success"] is True
    assert calls[0]["graph_scope_id"] == "scope"


@pytest.mark.asyncio
async def test_evolution_disabled_gates_flow_service_operations(
    monkeypatch, tmp_path: Path
) -> None:
    config = symphony_config_from_dict(
        {
            "enabled": True,
            "paths": {"graph_dir": str(tmp_path)},
            "evolution": {"enabled": False},
        }
    )
    service = SwarmSymphonyService()
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config", lambda: config
    )
    monkeypatch.setattr(
        service,
        "_runtime_for",
        lambda _config: pytest.fail("disabled evolution must not create a runtime"),
    )

    await service.submit_evolution_and_notify(
        None,
        {"graph": {}},
        session_id="session-1",
        capture_mode="agent",
        channel_id="web",
    )

    listed = service.list_experience_candidates()
    requested = await service.request_experience_candidate(
        recipe_id="recipe-1",
        recipe_version=2,
        session_id="session-1",
        channel_id="web",
    )
    installed = await service.install_candidate(
        request_id=experience_request_id("recipe-1", 2),
        recipe_id="recipe-1",
        recipe_version=2,
        package_id=None,
        integrity=None,
        skill_manager=object(),
    )

    assert listed == {"success": True, "enabled": False, "candidates": []}
    assert requested == {"success": False, "reason": "flow_disabled"}
    assert installed == {"installed": False, "reason": "flow_disabled"}


@pytest.mark.asyncio
async def test_flow_start_failure_does_not_block_graph_submission(
    monkeypatch, tmp_path: Path
) -> None:
    _enable_evolution_config(monkeypatch, tmp_path)
    service = SwarmSymphonyService()
    calls: list[str] = []

    class Runtime:
        flow_engine = object()

        async def submit_evolution(self, *args, **kwargs):
            calls.append("graph_submit")
            return SimpleNamespace(new_candidates=())

    async def fail_start(_runtime):
        calls.append("flow_start")
        raise OSError("broken recovery")

    monkeypatch.setattr(service, "_runtime_for", lambda _config: Runtime())
    monkeypatch.setattr(service, "_start_flow", fail_start)

    await service.submit_evolution_and_notify(
        None,
        {"graph": {}},
        session_id="session-1",
        capture_mode="agent",
        channel_id="web",
    )

    assert calls == ["graph_submit", "flow_start"]


@pytest.mark.asyncio
async def test_agent_server_symphony_recovery_is_fail_soft(monkeypatch) -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    class Service:
        @staticmethod
        async def start():
            raise OSError("flow store unavailable")

    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.get_swarm_symphony_service",
        lambda: Service(),
    )

    await AgentWebSocketServer._start_symphony_recovery()


@pytest.mark.asyncio
async def test_request_model_plan_keeps_rail_runtime_and_single_flow_owner(
    monkeypatch, tmp_path: Path
) -> None:
    _enable_evolution_config(monkeypatch, tmp_path)
    flow_instances: list[object] = []
    runtimes: list[object] = []
    submitted: list[object] = []

    class Flow:
        def __init__(self, *args, **kwargs):
            flow_instances.append(self)

    class Model:
        async def invoke(self, messages):
            del messages
            return "{}"

    class PlanResult:
        @staticmethod
        def to_dict():
            return {"planned_graph": {"graph": {"nodes": [], "edges": []}}}

    class GraphEngine:
        async def plan(self, *args, **kwargs):
            return PlanResult()

    class Runtime:
        def __init__(self, **kwargs):
            self.flow_engine = kwargs["flow_engine"]
            self.graph_engine = GraphEngine()
            self.graph_scope_id = "scope"
            self.closed = False
            runtimes.append(self)

        async def submit_evolution(self, *args, **kwargs):
            submitted.append(self)
            return SimpleNamespace(new_candidates=())

        async def aclose(self):
            self.closed = True

    service = SwarmSymphonyService()
    _patch_runtime_construction(monkeypatch, Flow, Runtime, Model())
    monkeypatch.setattr(
        service,
        "graph_status",
        lambda: _async_value({"success": True, "exists": True, "stale": False}),
    )
    rail_runtime = service.runtime()

    result = await service.plan("query", llm_config=LLMConfig(model="request"))
    await service.submit_evolution_and_notify(
        None,
        {"graph": {}},
        session_id="session",
        capture_mode="agent",
        channel_id="web",
    )

    assert result["success"] is True
    assert service.runtime() is rail_runtime
    assert submitted == [rail_runtime]
    assert len(flow_instances) == 1
    assert runtimes[1].closed is True


@pytest.mark.asyncio
async def test_service_close_awaits_current_and_retired_runtimes() -> None:
    service = SwarmSymphonyService()
    closed: list[str] = []

    class Runtime:
        async def aclose(self) -> None:
            closed.append("current")

    async def retired() -> None:
        closed.append("retired")

    service._runtime = Runtime()
    task = asyncio.create_task(retired())
    service._retired_runtime_tasks.add(task)

    await service.close()

    assert set(closed) == {"current", "retired"}


@pytest.mark.asyncio
async def test_service_close_drains_retired_runtime_when_current_close_fails() -> None:
    service = SwarmSymphonyService()
    closed: list[str] = []

    class Runtime:
        async def aclose(self) -> None:
            closed.append("current")
            raise OSError("current close failed")

    async def retired() -> None:
        closed.append("retired")

    service._runtime = Runtime()
    task = asyncio.create_task(retired())
    service._retired_runtime_tasks.add(task)

    with pytest.raises(OSError, match="current close failed"):
        await service.close()

    assert set(closed) == {"current", "retired"}


@pytest.mark.asyncio
async def test_flow_owner_is_not_replaced_and_close_blocks_runtime_creation(
    monkeypatch, tmp_path: Path
) -> None:
    config = _enable_evolution_config(monkeypatch, tmp_path)
    changed = symphony_config_from_dict(
        {
            "enabled": True,
            "paths": {"graph_dir": str(tmp_path / "graph")},
            "orchestration": {"max_depth": 9},
            "evolution": {"enabled": True},
        }
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    flow_count = 0

    class Flow:
        def __init__(self, *args, **kwargs):
            nonlocal flow_count
            flow_count += 1

    class Model:
        async def invoke(self, messages):
            return "{}"

    class Runtime:
        def __init__(self, **kwargs):
            self.flow_engine = kwargs["flow_engine"]

        async def aclose(self):
            entered.set()
            await release.wait()

    service = SwarmSymphonyService()
    _patch_runtime_construction(monkeypatch, Flow, Runtime, Model())
    first = service._runtime_for(config)

    assert service._runtime_for(changed) is first
    assert flow_count == 1
    closing = asyncio.create_task(service.close())
    await entered.wait()
    with pytest.raises(RuntimeError, match="closing"):
        service.runtime()
    release.set()
    await closing


@pytest.mark.asyncio
async def test_single_agent_rail_forwards_agent_capture(
    monkeypatch, rail_capture
) -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._config_base_cache = {
        "symphony": {
            "enabled": True,
            "evolution": {"enabled": True},
        }
    }
    adapter._model = object()
    adapter._channel_id = "web"
    captured, submissions = rail_capture("SymphonyGraphEvolutionRail")
    monkeypatch.setattr(
        "openjiuwen.extensions.observability.demand.get_trajectory_span_processor",
        lambda: "processor",
    )

    rail = adapter._build_symphony_graph_evolution_rail()
    await captured["submit_evolution"](
        None,
        {"graph": {}},
        session_id="session-1",
        capture_mode="forged",
    )

    assert rail is not None
    assert isinstance(
        captured["capability_snapshot_provider"], PublishedCapabilitySnapshotProvider
    )
    assert submissions == [
        {"session_id": "session-1", "capture_mode": "agent", "channel_id": "web"}
    ]
    adapter._channel_id = "new-channel"
    await captured["submit_evolution"](
        None, {"graph": {}}, session_id="session-2", capture_mode="forged"
    )
    assert submissions[-1] == {
        "session_id": "session-2",
        "capture_mode": "agent",
        "channel_id": "new-channel",
    }


@pytest.mark.asyncio
async def test_team_leader_rail_forwards_team_capture(
    monkeypatch, rail_capture
) -> None:
    context = SimpleNamespace(
        role="leader",
        channel_id="web",
        trajectory_span_processor="team-processor",
        config={
            "symphony": {
                "enabled": True,
                "evolution": {"enabled": True},
            }
        },
    )
    captured, submissions = rail_capture("TeamSymphonyGraphEvolutionRail")
    monkeypatch.setattr(
        "jiuwenswarm.symphony.llm.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.adapter.model_from_config",
        lambda _config: "model",
    )

    rail = evolution_rails.build_symphony_graph_evolution_rail({}, context)
    await captured["submit_evolution"](
        None,
        {"graph": {}},
        session_id="session-1",
        capture_mode="forged",
    )

    assert rail is not None
    assert isinstance(
        captured["capability_snapshot_provider"], PublishedCapabilitySnapshotProvider
    )
    assert submissions == [
        {"session_id": "session-1", "capture_mode": "team", "channel_id": "web"}
    ]


@pytest.mark.asyncio
async def test_approved_answer_refreshes_skills_and_static_graph(monkeypatch) -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._skill_manager = object()
    calls: list[str] = []

    class Service:
        @staticmethod
        async def install_candidate(**kwargs):
            calls.append("install")
            return {
                "installed": True,
                "newly_installed": True,
                "package_id": "cap-1",
            }

        @staticmethod
        async def start_refresh_graph(*, force: bool = False):
            calls.append(f"graph:{force}")

    async def refresh() -> None:
        calls.append("rails")

    async def refresh_team() -> int:
        calls.append("team")
        return 1

    adapter.refresh_skill_rails = refresh
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.get_swarm_symphony_service",
        lambda: Service(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.reload_team_skill_views_across_managers",
        refresh_team,
    )
    request_id = experience_request_id("recipe-1", 2)

    result = await adapter._handle_symphony_experience_answer(
        request_id,
        [{"selected_options": ["安装"]}],
        {"evolution_meta": {"recipe_id": "recipe-1", "recipe_version": 2}},
    )

    assert result["installed"] is True
    assert calls == ["install", "rails", "team", "graph:False"]


@pytest.mark.asyncio
async def test_replayed_install_receipt_does_not_refresh(monkeypatch) -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._skill_manager = object()

    class Service:
        @staticmethod
        async def install_candidate(**kwargs):
            return {
                "installed": True,
                "newly_installed": False,
                "replayed": True,
            }

    async def unexpected_refresh():
        pytest.fail("replayed receipt must not refresh runtime state")

    adapter.refresh_skill_rails = unexpected_refresh
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.get_swarm_symphony_service",
        lambda: Service(),
    )

    result = await adapter._handle_symphony_experience_answer(
        experience_request_id("recipe-1", 2),
        [{"selected_options": ["安装"]}],
        {"evolution_meta": {"recipe_id": "recipe-1", "recipe_version": 2}},
    )

    assert result["installed"] is True
    assert result["replayed"] is True


@pytest.mark.asyncio
async def test_installed_receipt_survives_all_refresh_failures(monkeypatch) -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._skill_manager = object()

    class Service:
        @staticmethod
        async def install_candidate(**kwargs):
            return {"installed": True, "newly_installed": True}

        @staticmethod
        async def start_refresh_graph(*, force=False):
            raise OSError("graph refresh failed")

    async def fail_agent_refresh():
        raise OSError("agent refresh failed")

    async def fail_team_refresh():
        raise OSError("team refresh failed")

    adapter.refresh_skill_rails = fail_agent_refresh
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.get_swarm_symphony_service",
        lambda: Service(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.reload_team_skill_views_across_managers",
        fail_team_refresh,
    )

    result = await adapter._handle_symphony_experience_answer(
        experience_request_id("recipe-1", 2),
        [{"selected_options": ["安装"]}],
        {"evolution_meta": {"recipe_id": "recipe-1", "recipe_version": 2}},
    )

    assert result["installed"] is True
    assert result["refresh_warnings"] == [
        "agent_skill_rails",
        "team_skill_views",
        "static_skill_graph",
    ]


async def _async_value(value):
    return value
