import asyncio
import ast
import json
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.symphony import (
    FINGERPRINT_ARTIFACT_FILENAME,
    CapabilityDescriptor,
    CapabilityFingerprint,
    CapabilityIO,
    FingerprintArtifact,
    OrchestrationPlan,
    ScanResult,
    SkillFolderScanner,
    SymphonyRuntime,
)

from jiuwenswarm.symphony.adapter import (
    candidate_ids_from_skill_ids,
    graph_build_orchestration_config_from_swarm,
    llm_config_signature,
    orchestration_config_from_swarm,
)
from jiuwenswarm.symphony.build import (
    _copy_fingerprint_artifact,
    _relation_cache_counts,
    build_graph,
    graph_status,
)
from jiuwenswarm.symphony.config import symphony_config_from_dict
from jiuwenswarm.symphony.graph_state import load_graph_state
from jiuwenswarm.symphony.llm import LLMConfig
from jiuwenswarm.symphony.service import (
    SwarmSymphonyService,
    _BuildProcessLogger,
    _OrderedProgressDispatcher,
    _build_progress,
)
from jiuwenswarm.symphony.graph_storage import latest_incomplete_build, graph_exists


class _FakeGraphModel:
    def __init__(self, model="fake-model", confidences=()):
        self.model = model
        self.confidences = list(confidences)
        self.call_count = 0
        self.model_config = SimpleNamespace(
            model_name=model,
            temperature=0.0,
            top_p=1.0,
            max_tokens=None,
            stop=None,
        )
        self.model_client_config = SimpleNamespace(
            client_provider="fake",
            api_base="https://fake.example/v1",
            api_key="fake-secret",
        )

    async def invoke(self, messages, **kwargs):
        del kwargs
        self.call_count += 1
        payload = json.loads(messages[-1]["content"])
        matches = []
        for candidate, confidence in zip(
            payload.get("candidates", []),
            self.confidences,
        ):
            directions = candidate.get("directions") or {}
            direction = "forward" if "forward" in directions else next(iter(directions))
            matches.append(
                {
                    "id": candidate["id"],
                    "direction": direction,
                    "confidence": confidence,
                }
            )
        return SimpleNamespace(content=json.dumps({"matches": matches}))


class _CountingGraphModel(_FakeGraphModel):
    """Use the production identity contract while keeping tests offline."""

    def __init__(self, config):
        super().__init__(config.model, confidences=[0.95] * 100)
        self.config = config
        request_config = config.model_request_kwargs()
        self.model_config = SimpleNamespace(
            model_name=config.model,
            **{key: value for key, value in request_config.items() if key != "model"},
        )
        self.model_client_config = SimpleNamespace(**config.model_client_kwargs())


class _FakeFingerprintService:
    def __init__(self, scan_result, artifact_root, fingerprints, progress_events=()):
        self.scan_result = scan_result
        self.artifact_root = Path(artifact_root)
        self.fingerprints = fingerprints
        self.progress_events = tuple(progress_events)

    async def build(self, *, force=False, progress_callback=None):
        del force
        if progress_callback is not None:
            for event in self.progress_events:
                progress_callback(event)
        content_hashes = self.scan_result.content_hashes
        fingerprints = tuple(
            item.model_copy(
                update={
                    "content_hash": content_hashes.get(
                        item.capability_id,
                        item.content_hash,
                    )
                }
            )
            for item in self.fingerprints
        )
        artifact = FingerprintArtifact(
            source_snapshot=self.scan_result.source_snapshot,
            fingerprints=fingerprints,
        )
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        (self.artifact_root / FINGERPRINT_ARTIFACT_FILENAME).write_text(
            artifact.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return artifact


class _FakeGraphBuildRuntimeFactory:
    """Keep graph integration tests on the public core fingerprint boundary."""

    def __init__(
        self,
        fingerprint_batches,
        *,
        scan_real_root=False,
        fingerprint_progress=(),
    ):
        self.fingerprint_batches = [tuple(batch) for batch in fingerprint_batches]
        self.scan_real_root = scan_real_root
        self.fingerprint_progress = tuple(fingerprint_progress)
        self.build_index = 0
        self.last_scan_result = None

    def scan(self, skills_root, *, max_depth):
        if self.scan_real_root:
            result = SkillFolderScanner(
                skills_root,
                max_depth=max_depth,
            ).scan()
        else:
            result = _scan_result(self._current_batch())
        self.last_scan_result = result
        return result

    def fingerprint_service(
        self,
        scan_result,
        artifact_root,
        *,
        llm_config,
        runtime_config,
    ):
        del llm_config, runtime_config
        batch = self._current_batch()
        self.build_index += 1
        return _FakeFingerprintService(
            scan_result,
            artifact_root,
            batch,
            self.fingerprint_progress,
        )

    def _current_batch(self):
        return self.fingerprint_batches[
            min(self.build_index, len(self.fingerprint_batches) - 1)
        ]


def _forbidden_symphony_import(
    node,
    *,
    allowed_symbols,
):
    if isinstance(node, ast.Import):
        imported_modules = {
            alias.name
            for alias in node.names
            if alias.name.startswith("openjiuwen.symphony")
        }
        forbidden = imported_modules - {"openjiuwen.symphony"}
        return ",".join(sorted(forbidden))
    if not isinstance(node, ast.ImportFrom):
        return ""
    imported = {alias.name for alias in node.names}
    if node.module == "openjiuwen":
        return "openjiuwen:symphony" if "symphony" in imported else ""
    if not (node.module or "").startswith("openjiuwen.symphony"):
        return ""
    if node.module == "openjiuwen.symphony" and imported <= allowed_symbols:
        return ""
    return f"{node.module}:{','.join(sorted(imported))}"


@pytest.fixture
def fake_graph_llm(monkeypatch):
    client = _FakeGraphModel()
    monkeypatch.setattr(
        "jiuwenswarm.symphony.build.model_from_config",
        lambda _config: client,
    )
    return client


def _config(tmp_path, *, evolution=True):
    return SimpleNamespace(
        paths=SimpleNamespace(
            skills_root=tmp_path / "skills",
            graph_dir=tmp_path / "graph",
        ),
        build=SimpleNamespace(
            workers=1,
            batch_size=4,
            max_candidates_per_skill_relation=8,
            require_consensus=False,
            min_edge_confidence=0.5,
        ),
        orchestration=SimpleNamespace(
            mode="fast",
            top_k=3,
            max_depth=4,
            min_edge_confidence=0.5,
        ),
        evolution=SimpleNamespace(enabled=evolution),
    )


def test_adapter_deduplicates_candidate_skill_ids():
    assert candidate_ids_from_skill_ids(["writer", "writer", "reviewer"]) == [
        "writer",
        "reviewer",
    ]


def test_adapter_maps_dynamic_graph_config(tmp_path):
    public = orchestration_config_from_swarm(_config(tmp_path), mode="beam")

    assert public.mode == "beam"
    assert public.dynamic_graph_enabled is True


def test_build_and_planning_configs_use_distinct_relation_thresholds(tmp_path):
    config = symphony_config_from_dict(
        {
            "paths": {
                "skills_root": str(tmp_path / "skills"),
                "graph_dir": str(tmp_path / "graph"),
            },
            "build": {"min_edge_confidence": 0.9},
            "orchestration": {"min_edge_confidence": 0.1},
        }
    )

    assert (
        graph_build_orchestration_config_from_swarm(config).min_edge_confidence == 0.9
    )
    assert orchestration_config_from_swarm(config).min_edge_confidence == 0.1


@pytest.mark.asyncio
async def test_service_graph_adapts_public_artifact_for_skill_graph_panel(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)

    artifact = {
        "config": {"thresholds": {"can_feed": 0.5}},
        "capabilities": [
            {"capability_id": "writer", "capability_type": "skill", "name": "Writer"},
            {
                "capability_id": "reviewer",
                "capability_type": "skill",
                "name": "Reviewer",
            },
            {
                "capability_id": "disabled",
                "capability_type": "skill",
                "name": "Disabled",
            },
        ],
        "nodes": [
            {
                "id": "capability:writer",
                "type": "capability",
                "label": "Writer",
                "properties": {},
            },
            {
                "id": "capability:reviewer",
                "type": "capability",
                "label": "Reviewer",
                "properties": {},
            },
            {
                "id": "capability:disabled",
                "type": "capability",
                "label": "Disabled",
                "properties": {},
            },
        ],
        "edges": [
            {
                "source": "capability:writer",
                "target": "capability:reviewer",
                "type": "can_feed",
            },
            {
                "source": "capability:writer",
                "target": "capability:disabled",
                "type": "can_feed",
            },
        ],
        "diagnostics": [{"code": "sample"}],
    }

    service = SwarmSymphonyService()
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(service, "_read_graph_artifact", lambda _graph_dir: artifact)
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_execution_disabled_skills",
        lambda: {"disabled"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_dynamic_overlay",
        lambda _graph_dir: {
            "edges": {"writer->reviewer:can_feed": {"runtime_weight": 0.7}}
        },
    )

    result = await service.graph()

    assert result["success"] is True
    assert [item["id"] for item in result["skills"]] == ["writer", "reviewer"]
    assert [item["id"] for item in result["graph"]["nodes"]] == [
        "skill:writer",
        "skill:reviewer",
    ]
    assert [item["type"] for item in result["graph"]["nodes"]] == [
        "skill",
        "skill",
    ]
    assert result["graph"]["edges"] == [
        {
            "source": "skill:writer",
            "target": "skill:reviewer",
            "type": "can_feed",
            "runtime_weight": 0.7,
        }
    ]
    skill_node_ids = {item["id"] for item in result["graph"]["nodes"]}
    assert all(
        edge[endpoint] in skill_node_ids
        for edge in result["graph"]["edges"]
        for endpoint in ("source", "target")
    )


@pytest.mark.asyncio
async def test_service_plans_through_public_runtime_and_restores_skill_fields(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    captured = {}

    class FakeOrchestration:
        async def plan(self, query, candidate_ids=None, **kwargs):
            captured.update(query=query, candidate_ids=candidate_ids, **kwargs)
            return OrchestrationPlan(
                {
                    "plan_id": "plan-1",
                    "dynamic_graph_enabled": True,
                    "recommended_plans": [
                        {
                            "title": "Plan",
                            "status": "ready",
                            "steps": [
                                {
                                    "step": 1,
                                    "capability_id": "writer",
                                    "name": "Writer",
                                }
                            ],
                            "can_feed_edges": [],
                            "missing_inputs": [],
                        }
                    ],
                    "execution_graph": {
                        "nodes": [{"id": "writer"}],
                        "edges": [],
                    },
                }
            )

    service = SwarmSymphonyService()

    async def fresh_status():
        return {"success": True, "exists": True, "stale": False}

    service.graph_status = fresh_status
    service._runtime_for = lambda _config: SimpleNamespace(
        orchestration=FakeOrchestration()
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config", lambda: config
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.get_config",
        lambda: {"preferred_language": "zh"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_execution_disabled_skills",
        lambda: {"disabled"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_dynamic_overlay",
        lambda _graph_dir: {"edges": {"writer->reviewer": {}}},
    )

    progress = object()
    result = await service.plan(
        "write",
        mode="beam",
        candidate_skill_ids=["writer", "writer"],
        progress=progress,
    )

    assert result["success"] is True
    assert result["direct_display"] is True
    assert result["result"]["recommended_plans"][0]["steps"][0]["skill_id"] == "writer"
    assert captured["candidate_ids"] == ["writer"]
    assert captured["disabled_capability_ids"] == {"disabled"}
    assert captured["dynamic_overlay"]["edges"]
    assert captured["language"] == "cn"
    assert captured["mode"] == "beam"
    assert captured["progress"] is progress


@pytest.mark.asyncio
async def test_service_rebuilds_stale_graph_before_planning(monkeypatch, tmp_path):
    config = _config(tmp_path, evolution=False)
    calls = []
    plan_kwargs = {}

    class FakeOrchestration:
        async def plan(self, *args, **kwargs):
            del args
            plan_kwargs.update(kwargs)
            return OrchestrationPlan(
                {"recommended_plans": [], "execution_graph": {"nodes": [], "edges": []}}
            )

    service = SwarmSymphonyService()

    async def stale_status():
        return {"success": True, "exists": True, "stale": True}

    async def refresh_graph(*, force=False, progress=None):
        calls.append((force, progress))
        return {"success": True}

    service.graph_status = stale_status
    service.refresh_graph = refresh_graph
    service._runtime_for = lambda _config: SimpleNamespace(
        orchestration=FakeOrchestration()
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config", lambda: config
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.get_config",
        lambda: {"preferred_language": "en"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_execution_disabled_skills", set
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_dynamic_overlay",
        lambda _graph_dir: pytest.fail("overlay must stay disabled"),
    )

    progress = object()
    result = await service.plan("write", progress=progress)

    assert calls == [(False, progress)]
    assert result["graph_build"]["rebuilt"] is True
    assert result["language"] == "en"
    assert plan_kwargs["dynamic_overlay"] is None
    assert "No Symphony plan" in result["content"]
    assert "Would you like to proceed" not in result["content"]


@pytest.mark.asyncio
async def test_swarm_build_publishes_public_graph_artifact(
    monkeypatch,
    tmp_path,
    fake_graph_llm,
):
    fake_graph_llm.confidences = [0.95, 0.1]
    fingerprint = CapabilityFingerprint(
        capability_type="skill",
        capability_id="writer",
        name="Writer",
        description="Write markdown.",
        version="1.0.0",
        outputs=(CapabilityIO(name="document", type="markdown"),),
        content_hash="writer-hash",
    )
    reviewer = CapabilityFingerprint(
        capability_type="skill",
        capability_id="reviewer",
        name="Reviewer",
        description="Review markdown.",
        version="1.0.0",
        inputs=(CapabilityIO(name="document", type="markdown"),),
        content_hash="reviewer-hash",
    )
    archiver = CapabilityFingerprint(
        capability_type="skill",
        capability_id="archiver",
        name="Archiver",
        description="Archive markdown.",
        version="1.0.0",
        inputs=(CapabilityIO(name="document", type="markdown"),),
        content_hash="archiver-hash",
    )
    runtime_factory = _FakeGraphBuildRuntimeFactory(
        [(fingerprint, reviewer, archiver)],
        fingerprint_progress=(
            {"event": "fingerprint.extract.progress", "current": 0, "total": 3},
            {"event": "fingerprint.extract.progress", "current": 1, "total": 3},
            {"event": "fingerprint.extract.progress", "current": 2, "total": 3},
            {"event": "fingerprint.extract.progress", "current": 3, "total": 3},
        ),
    )
    (tmp_path / "skills").mkdir()
    graph_dir = tmp_path / "graph"
    config = symphony_config_from_dict(
        {
            "paths": {
                "skills_root": str(tmp_path / "skills"),
                "graph_dir": str(graph_dir),
            },
            "build": {"min_edge_confidence": 0.9},
            "orchestration": {"min_edge_confidence": 0.1},
        }
    )
    build_events = []

    result = await build_graph(
        tmp_path / "skills",
        graph_dir,
        llm_config=LLMConfig(model="test-model"),
        force=True,
        build_log=lambda stage, **details: build_events.append((stage, details)),
        symphony_config=config,
        runtime_factory=runtime_factory,
    )

    current = json.loads((graph_dir / "current.json").read_text(encoding="utf-8"))
    graph_path = graph_dir / current["artifact"]
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    assert result.success is True
    assert result.edge_count == 1
    assert result.relation_reused_count == 0
    assert result.relation_resolved_count == 2
    assert graph["config"]["llm"]["relation_cache"]["resolved_count"] == 2
    assert graph["config"]["thresholds"]["can_feed"] == 0.9
    assert graph["config"]["orchestration"]["min_edge_confidence"] == 0.9
    assert (
        graph["source_snapshot"]["symphony_graph_build"]["matcher"]["thresholds"][
            "can_feed"
        ]
        == 0.9
    )
    assert orchestration_config_from_swarm(config).min_edge_confidence == 0.1
    assert current["schema_version"] == "1.0"
    assert {item["capability_id"] for item in graph["capabilities"]} == {
        "archiver",
        "reviewer",
        "writer",
    }
    resolve_events = [
        details for stage, details in build_events if stage == "graph.resolve.done"
    ]
    assert resolve_events == [
        {
            "candidate_count": 2,
            "match_count": 2,
            "accepted_match_count": 1,
            "diagnostics_count": 1,
        }
    ]
    build_done = next(
        details for stage, details in build_events if stage == "graph.build.done"
    )
    assert build_done["candidate_count"] == 2
    assert build_done["match_count"] == 2
    assert build_done["accepted_match_count"] == 1
    assert build_done["match_count"] > build_done["edge_count"] == 1
    stages = [stage for stage, _details in build_events]
    assert "build_started" not in stages
    assert "build_published" not in stages
    assert stages.count("graph.resolve.done") == 1
    fingerprint_progress = [
        details
        for stage, details in build_events
        if stage == "fingerprint.extract.start" and "current" in details
    ]
    assert [(item["current"], item["total"]) for item in fingerprint_progress] == [
        (0, 3),
        (1, 3),
        (2, 3),
        (3, 3),
    ]
    assert _build_progress(
        [{"stage": "fingerprint.extract.start", **item} for item in fingerprint_progress[:3]]
    )["percent"] < 48
    assert _build_progress(
        [{"stage": "fingerprint.extract.start", **item} for item in fingerprint_progress]
    ) == {
        "stage": "fingerprint.extract.start",
        "label": "提取技能指纹",
        "percent": 48,
        "status": "running",
        "current": 3,
        "total": 3,
        "ts": None,
    }
    progress_entries = []
    percents = []
    for stage, details in build_events:
        progress_entries.append({"stage": stage, **details})
        percents.append(_build_progress(progress_entries)["percent"])
    assert percents == sorted(percents)
    assert (
        graph_status(
            tmp_path / "skills",
            graph_dir,
            symphony_config=config,
            runtime_factory=runtime_factory,
        ).stale
        is False
    )
    assert (graph_path.parent / "graph_state.json").is_file()
    assert (graph_path.parent / FINGERPRINT_ARTIFACT_FILENAME).is_file()
    assert graph_exists(graph_dir) is True
    assert not list((graph_dir / ".build_runs").glob("*/artifacts"))


@pytest.mark.asyncio
async def test_swarm_build_relation_cache_reuses_unchanged_candidates(
    monkeypatch,
    tmp_path,
    fake_graph_llm,
):
    writer = CapabilityFingerprint(
        capability_type="skill",
        capability_id="writer",
        name="Writer",
        description="Write markdown.",
        version="1.0.0",
        outputs=(CapabilityIO(name="document", type="markdown"),),
        content_hash="writer-hash",
    )
    reviewer = CapabilityFingerprint(
        capability_type="skill",
        capability_id="reviewer",
        name="Reviewer",
        description="Review markdown.",
        version="1.0.0",
        inputs=(CapabilityIO(name="document", type="markdown"),),
        content_hash="reviewer-hash",
    )
    unrelated = CapabilityFingerprint(
        capability_type="skill",
        capability_id="unrelated",
        name="Unrelated",
        description="Unrelated capability.",
        version="1.0.0",
        content_hash="unrelated-hash",
    )
    runtime_factory = _FakeGraphBuildRuntimeFactory(
        [(writer, reviewer), (writer, reviewer, unrelated)]
    )
    graph_dir = tmp_path / "graph"
    config = symphony_config_from_dict(
        {
            "paths": {
                "skills_root": str(tmp_path / "skills"),
                "graph_dir": str(graph_dir),
            }
        }
    )
    first = await build_graph(
        tmp_path / "skills",
        graph_dir,
        llm_config=LLMConfig(model="test-model"),
        symphony_config=config,
        runtime_factory=runtime_factory,
    )
    first_call_count = fake_graph_llm.call_count
    second = await build_graph(
        tmp_path / "skills",
        graph_dir,
        llm_config=LLMConfig(model="test-model"),
        symphony_config=config,
        runtime_factory=runtime_factory,
    )

    assert first.relation_resolved_count > 0
    assert second.relation_reused_count == first.relation_resolved_count
    assert fake_graph_llm.call_count == first_call_count
    assert (graph_dir / "cache" / "relation_matches.json").is_file()


@pytest.mark.asyncio
async def test_real_core_relation_cache_invalidates_for_complete_llm_identity(
    monkeypatch,
    tmp_path,
):
    writer = CapabilityFingerprint(
        capability_type="skill",
        capability_id="writer",
        name="Writer",
        description="Write markdown.",
        version="1.0.0",
        outputs=(CapabilityIO(name="document", type="markdown"),),
        content_hash="writer-hash",
    )
    reviewer = CapabilityFingerprint(
        capability_type="skill",
        capability_id="reviewer",
        name="Reviewer",
        description="Review markdown.",
        version="1.0.0",
        inputs=(CapabilityIO(name="document", type="markdown"),),
        content_hash="reviewer-hash",
    )
    runtime_factory = _FakeGraphBuildRuntimeFactory([(writer, reviewer)])
    clients = []

    def create_counting_model(config):
        client = _CountingGraphModel(config)
        clients.append(client)
        return client

    monkeypatch.setattr(
        "jiuwenswarm.symphony.build.model_from_config",
        create_counting_model,
    )
    skills_root = tmp_path / "skills"
    graph_dir = tmp_path / "graph"
    skills_root.mkdir()
    config = symphony_config_from_dict(
        {
            "paths": {
                "skills_root": str(skills_root),
                "graph_dir": str(graph_dir),
            }
        }
    )
    raw_endpoint = "https://private-endpoint.example/v1"
    sensitive_values = (
        "secret-api-key",
        raw_endpoint,
        "private-credential",
        "private-token",
    )

    def llm_config(*, top_p, routing, request_route):
        return LLMConfig(
            model="model-a",
            temperature=0.0,
            top_p=top_p,
            model_client_config={
                "api_key": sensitive_values[0],
                "api_base": raw_endpoint,
                "client_provider": "openai",
                "routing": {
                    "region": routing,
                    "credential": sensitive_values[2],
                },
            },
            model_config_obj={
                "max_tokens": 99,
                "extra_body": {
                    "request_route": request_route,
                    "token": sensitive_values[3],
                },
            },
        )

    configs = [
        llm_config(top_p=0.2, routing="route-a", request_route="request-a"),
        llm_config(top_p=0.9, routing="route-a", request_route="request-a"),
        llm_config(top_p=0.9, routing="route-b", request_route="request-a"),
        llm_config(top_p=0.9, routing="route-b", request_route="request-b"),
    ]
    results = []
    artifacts = []
    for llm_config_value in configs:
        result = await build_graph(
            skills_root,
            graph_dir,
            llm_config=llm_config_value,
            symphony_config=config,
            runtime_factory=runtime_factory,
        )
        results.append(result)
        artifacts.append(
            json.loads(
                (graph_dir / "versions" / result.version / "graph.json").read_text(
                    encoding="utf-8"
                )
            )
        )

    assert len(clients) == 4
    assert all(client.call_count > 0 for client in clients)
    assert all(result.relation_resolved_count > 0 for result in results)
    assert all(result.relation_reused_count == 0 for result in results[1:])
    assert [artifact["source_snapshot"]["llm_sha256"] for artifact in artifacts] == [
        client.config.identity_digest() for client in clients
    ]
    cache_path = graph_dir / "cache" / "relation_matches.json"
    assert cache_path.is_file()
    serialized_outputs = "\n".join(
        path.read_text(encoding="utf-8") for path in graph_dir.rglob("*.json")
    )
    for sensitive_value in sensitive_values:
        assert sensitive_value not in serialized_outputs


@pytest.mark.asyncio
async def test_swarm_refresh_publishes_state_when_file_changes_but_fingerprint_does_not(
    monkeypatch,
    tmp_path,
    fake_graph_llm,
):
    del fake_graph_llm
    skills_root = tmp_path / "skills"
    skill_md = skills_root / "writer" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: writer\n---\nWrite markdown.\n", encoding="utf-8")
    fingerprint = CapabilityFingerprint(
        capability_type="skill",
        capability_id="writer",
        name="Writer",
        description="Write markdown.",
        version="1.0.0",
        outputs=(CapabilityIO(name="document", type="markdown"),),
        content_hash="replaced-from-scan",
    )
    runtime_factory = _FakeGraphBuildRuntimeFactory(
        [(fingerprint,)],
        scan_real_root=True,
    )
    graph_dir = tmp_path / "graph"
    config = symphony_config_from_dict(
        {"paths": {"skills_root": str(skills_root), "graph_dir": str(graph_dir)}}
    )
    first = await build_graph(
        skills_root,
        graph_dir,
        llm_config=LLMConfig(model="test-model"),
        symphony_config=config,
        runtime_factory=runtime_factory,
    )
    old_hash = load_graph_state(graph_dir).active_entries()["writer"].content_hash
    skill_md.write_text(
        "---\nname: writer\n---\nWrite markdown with revised guidance.\n",
        encoding="utf-8",
    )

    assert (
        graph_status(
            skills_root,
            graph_dir,
            symphony_config=config,
            runtime_factory=runtime_factory,
        ).stale
        is True
    )
    second = await build_graph(
        skills_root,
        graph_dir,
        llm_config=LLMConfig(model="test-model"),
        symphony_config=config,
        runtime_factory=runtime_factory,
    )

    new_hash = load_graph_state(graph_dir).active_entries()["writer"].content_hash
    assert second.version != first.version
    assert new_hash != old_hash
    assert new_hash == runtime_factory.last_scan_result.capabilities[0].content_hash
    current = json.loads((graph_dir / "current.json").read_text(encoding="utf-8"))
    artifact = json.loads((graph_dir / current["artifact"]).read_text(encoding="utf-8"))
    captured_status_snapshots = []
    real_runtime = SymphonyRuntime

    def capture_status_runtime(**kwargs):
        if kwargs.get("model") is None and "source_snapshot" in kwargs:
            captured_status_snapshots.append(dict(kwargs["source_snapshot"]))
        return real_runtime(**kwargs)

    monkeypatch.setattr(
        "jiuwenswarm.symphony.build.SymphonyRuntime",
        capture_status_runtime,
    )
    assert (
        graph_status(
            skills_root,
            graph_dir,
            symphony_config=config,
            runtime_factory=runtime_factory,
        ).stale
        is False
    )
    assert len(captured_status_snapshots) == 1
    expected_snapshot = captured_status_snapshots[0]
    assert set(expected_snapshot) == {
        "schema_version",
        "capabilities_sha256",
        "current_hashes",
        "fingerprint_schema_version",
        "fingerprint_source_snapshot",
        "fingerprint_sha256",
        "fingerprint_config_sha256",
        "graph_config",
        "llm_sha256",
    }
    assert "symphony_graph_build" not in expected_snapshot
    assert (
        expected_snapshot["fingerprint_sha256"]
        == artifact["source_snapshot"]["fingerprint_sha256"]
    )
    assert expected_snapshot["llm_sha256"] == artifact["source_snapshot"]["llm_sha256"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["build_config", "llm_config"])
async def test_swarm_graph_identity_rebuilds_for_build_or_llm_config_change(
    monkeypatch,
    tmp_path,
    change,
):
    fingerprint = CapabilityFingerprint(
        capability_type="skill",
        capability_id="writer",
        name="Writer",
        description="Write markdown.",
        version="1.0.0",
        content_hash="writer-hash",
    )
    runtime_factory = _FakeGraphBuildRuntimeFactory([(fingerprint,)])
    graph_dir = tmp_path / "graph"
    skills_root = tmp_path / "skills"
    skills_root.mkdir()

    def config(max_candidates):
        return symphony_config_from_dict(
            {
                "paths": {
                    "skills_root": str(skills_root),
                    "graph_dir": str(graph_dir),
                },
                "build": {
                    "max_candidates_per_skill_relation": max_candidates,
                },
            }
        )

    first_config = config(8)
    second_config = config(9 if change == "build_config" else 8)
    first_llm_config = LLMConfig(model="model-v1")
    second_llm_config = LLMConfig(
        model="model-v2" if change == "llm_config" else "model-v1"
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.build.model_from_config",
        lambda llm_config: _FakeGraphModel(llm_config.model),
    )
    captured_status_snapshots = []
    real_runtime = SymphonyRuntime

    def capture_status_runtime(**kwargs):
        if kwargs.get("model") is None and "source_snapshot" in kwargs:
            captured_status_snapshots.append(dict(kwargs["source_snapshot"]))
        return real_runtime(**kwargs)

    monkeypatch.setattr(
        "jiuwenswarm.symphony.build.SymphonyRuntime",
        capture_status_runtime,
    )
    first = await build_graph(
        skills_root,
        graph_dir,
        llm_config=first_llm_config,
        symphony_config=first_config,
        runtime_factory=runtime_factory,
    )
    first_graph = json.loads(
        (graph_dir / "versions" / first.version / "graph.json").read_text()
    )

    assert (
        graph_status(
            skills_root,
            graph_dir,
            llm_config=second_llm_config,
            symphony_config=second_config,
            runtime_factory=runtime_factory,
        ).stale
        is True
    )
    assert "symphony_graph_build" not in captured_status_snapshots[-1]

    second = await build_graph(
        skills_root,
        graph_dir,
        llm_config=second_llm_config,
        symphony_config=second_config,
        runtime_factory=runtime_factory,
    )
    second_graph = json.loads(
        (graph_dir / "versions" / second.version / "graph.json").read_text()
    )

    assert second.version != first.version
    assert second_graph["source_snapshot"] != first_graph["source_snapshot"]
    assert (
        graph_status(
            skills_root,
            graph_dir,
            llm_config=second_llm_config,
            symphony_config=second_config,
            runtime_factory=runtime_factory,
        ).stale
        is False
    )
    assert "symphony_graph_build" not in captured_status_snapshots[-1]
    if change == "build_config":
        assert second_graph["config"]["graph"]["max_candidates_per_skill_relation"] == 9
    else:
        assert (
            second_graph["source_snapshot"]["llm_sha256"]
            != first_graph["source_snapshot"]["llm_sha256"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("prepare failed"), asyncio.CancelledError()]
)
async def test_swarm_auxiliary_prepare_failure_preserves_current(
    monkeypatch,
    tmp_path,
    failure,
    fake_graph_llm,
):
    del fake_graph_llm
    writer = CapabilityFingerprint(
        capability_type="skill",
        capability_id="writer",
        name="Writer",
        description="Write markdown.",
        version="1.0.0",
        outputs=(CapabilityIO(name="document", type="markdown"),),
        content_hash="writer-hash",
    )
    reviewer = CapabilityFingerprint(
        capability_type="skill",
        capability_id="reviewer",
        name="Reviewer",
        description="Review markdown.",
        version="1.0.0",
        inputs=(CapabilityIO(name="document", type="markdown"),),
        content_hash="reviewer-hash",
    )
    runtime_factory = _FakeGraphBuildRuntimeFactory(
        [
            (writer,),
            (writer, reviewer),
        ]
    )
    graph_dir = tmp_path / "graph"
    config = symphony_config_from_dict(
        {
            "paths": {
                "skills_root": str(tmp_path / "skills"),
                "graph_dir": str(graph_dir),
            }
        }
    )

    await build_graph(
        tmp_path / "skills",
        graph_dir,
        llm_config=LLMConfig(model="test-model"),
        force=True,
        symphony_config=config,
        runtime_factory=runtime_factory,
    )
    current_before = (graph_dir / "current.json").read_bytes()

    def fail_staged_copy(*args, **kwargs):
        del args, kwargs
        raise failure

    monkeypatch.setattr(
        "jiuwenswarm.symphony.build._copy_fingerprint_artifact",
        fail_staged_copy,
    )
    with pytest.raises(type(failure), match="prepare failed" if str(failure) else None):
        await build_graph(
            tmp_path / "skills",
            graph_dir,
            llm_config=LLMConfig(model="test-model"),
            force=True,
            symphony_config=config,
            runtime_factory=runtime_factory,
        )

    assert (graph_dir / "current.json").read_bytes() == current_before
    resume_from = latest_incomplete_build(graph_dir)
    assert resume_from is not None
    assert (resume_from / "artifacts").is_dir()


@pytest.mark.asyncio
async def test_swarm_graph_version_uses_run_snapshot_after_root_fingerprint_changes(
    monkeypatch,
    tmp_path,
    fake_graph_llm,
):
    del fake_graph_llm
    writer = CapabilityFingerprint(
        capability_type="skill",
        capability_id="writer",
        name="Writer",
        description="Write markdown.",
        version="1.0.0",
        content_hash="writer-hash",
    )
    concurrent = CapabilityFingerprint(
        capability_type="skill",
        capability_id="concurrent",
        name="Concurrent",
        description="Published by another fingerprint build.",
        version="1.0.0",
        content_hash="concurrent-hash",
    )
    runtime_factory = _FakeGraphBuildRuntimeFactory([(writer,)])
    skills_root = tmp_path / "skills"
    graph_dir = tmp_path / "graph"
    config = symphony_config_from_dict(
        {
            "paths": {
                "skills_root": str(skills_root),
                "graph_dir": str(graph_dir),
            }
        }
    )

    def overwrite_root_before_copy(source_dir, target_dir):
        assert source_dir.parent.parent == graph_dir / ".build_runs"
        concurrent_artifact = FingerprintArtifact(
            source_snapshot=runtime_factory.last_scan_result.source_snapshot,
            fingerprints=(concurrent,),
        )
        (graph_dir / FINGERPRINT_ARTIFACT_FILENAME).write_text(
            concurrent_artifact.model_dump_json(indent=2),
            encoding="utf-8",
        )
        _copy_fingerprint_artifact(source_dir, target_dir)

    monkeypatch.setattr(
        "jiuwenswarm.symphony.build._copy_fingerprint_artifact",
        overwrite_root_before_copy,
    )
    result = await build_graph(
        skills_root,
        graph_dir,
        llm_config=LLMConfig(model="test-model"),
        force=True,
        symphony_config=config,
        runtime_factory=runtime_factory,
    )

    version_payload = json.loads(
        (
            graph_dir / "versions" / result.version / FINGERPRINT_ARTIFACT_FILENAME
        ).read_text(encoding="utf-8")
    )
    root_payload = json.loads(
        (graph_dir / FINGERPRINT_ARTIFACT_FILENAME).read_text(encoding="utf-8")
    )
    assert version_payload["fingerprints"][0]["capability_id"] == "writer"
    assert root_payload["fingerprints"][0]["capability_id"] == "concurrent"


def test_incompatible_graph_pointer_forces_first_public_rebuild(tmp_path):
    graph_dir = tmp_path / "graph"
    version_dir = graph_dir / "versions" / "old"
    version_dir.mkdir(parents=True)
    (version_dir / "graph.json").write_text("{}", encoding="utf-8")
    (graph_dir / "current.json").write_text(
        json.dumps(
            {
                "schema_version": "Symphony-graph-pointer-v0",
                "version": "old",
                "path": "versions/old",
            }
        ),
        encoding="utf-8",
    )

    assert graph_exists(graph_dir) is False


@pytest.mark.parametrize(
    "resolved_count",
    [float("nan"), float("inf"), float("-inf"), "not-a-number"],
)
def test_force_relation_cache_counts_fall_back_for_invalid_metadata(resolved_count):
    graph_payload = {
        "edges": [{"source": "writer", "target": "reviewer"}],
        "config": {
            "llm": {
                "relation_cache": {
                    "reused_count": float("inf"),
                    "resolved_count": resolved_count,
                }
            }
        },
    }

    assert _relation_cache_counts(graph_payload, force=True) == (0, 1)


def test_runtime_cache_rebuilds_when_default_llm_changes(monkeypatch, tmp_path):
    config = _config(tmp_path)
    current = {
        "value": LLMConfig(
            model="model-a",
            model_client_config={
                "api_key": "secret-a",
                "api_base": "https://a.example/v1",
                "client_provider": "openai",
            },
        )
    }
    created = []

    class FakeRuntime:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.LLMConfig.from_default_model",
        lambda: current["value"],
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.model_from_config",
        lambda value: ("model", value.model),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.SymphonyRuntime",
        FakeRuntime,
    )
    service = SwarmSymphonyService()

    first = service._runtime_for(config)
    assert service._runtime_for(config) is first
    current["value"] = LLMConfig(
        model="model-b",
        model_client_config={
            "api_key": "secret-b",
            "api_base": "https://b.example/v1",
            "client_provider": "openai",
        },
    )
    second = service._runtime_for(config)

    assert second is not first
    assert len(created) == 2
    current["value"] = LLMConfig(
        model="model-b",
        model_client_config={
            "api_key": "secret-c",
            "api_base": "https://b.example/v1",
            "client_provider": "openai",
        },
    )
    third = service._runtime_for(config)

    assert third is not second
    assert len(created) == 3
    signature_text = repr(llm_config_signature(current["value"]))
    assert "secret-c" not in signature_text
    assert "https://b.example" not in signature_text


def test_production_uses_only_stable_openjiuwen_symphony_imports():
    package_root = Path(__file__).parents[3] / "jiuwenswarm"
    allowed_symbols = {
        "FINGERPRINT_ARTIFACT_FILENAME",
        "CapabilityDescriptor",
        "CapabilityFingerprint",
        "FingerprintArtifact",
        "FingerprintService",
        "FingerprintSettings",
        "OrchestrationConfig",
        "ScanResult",
        "SkillFolderScanner",
        "SourceSnapshot",
        "SymphonyRuntime",
    }
    offenders = []
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            violation = _forbidden_symphony_import(
                node,
                allowed_symbols=allowed_symbols,
            )
            if violation:
                offenders.append(
                    f"{path.relative_to(package_root)}:{node.lineno}:{violation}"
                )

    assert offenders == []


def test_static_import_guard_detects_openjiuwen_symphony_alias_bypass():
    bypass = ast.parse("from openjiuwen import symphony as hidden").body[0]
    dotted = ast.parse("import openjiuwen.symphony.orchestration.graph as hidden").body[
        0
    ]
    legal = ast.parse("from openjiuwen import core as public_core").body[0]
    stable_module = ast.parse("import openjiuwen.symphony as stable").body[0]

    assert _forbidden_symphony_import(bypass, allowed_symbols=set())
    assert _forbidden_symphony_import(dotted, allowed_symbols=set())
    assert not _forbidden_symphony_import(legal, allowed_symbols=set())
    assert not _forbidden_symphony_import(stable_module, allowed_symbols=set())


@pytest.mark.asyncio
async def test_build_progress_is_ordered_and_drained_before_close(tmp_path):
    events = []

    async def progress(event):
        await asyncio.sleep(0)
        events.append(event["event"])

    dispatcher = _OrderedProgressDispatcher(progress)
    dispatcher.start()
    build_logger = _BuildProcessLogger(
        tmp_path / "build_log.jsonl",
        progress=dispatcher,
    )
    build_logger.record("scan.start")
    build_logger.record("scan.done")
    await dispatcher.close()

    assert events == ["scan.start", "scan.done"]
    assert dispatcher.worker is None


@pytest.mark.asyncio
async def test_build_progress_callback_failure_does_not_drop_later_events(tmp_path):
    attempted = []

    async def progress(event):
        attempted.append(event["event"])
        if event["event"] == "scan.start":
            raise RuntimeError("progress transport failed")

    dispatcher = _OrderedProgressDispatcher(progress)
    dispatcher.start()
    build_logger = _BuildProcessLogger(
        tmp_path / "build_log.jsonl",
        progress=dispatcher,
    )
    build_logger.record("scan.start")
    build_logger.record("scan.done")

    await dispatcher.close()

    assert attempted == ["scan.start", "scan.done"]
    assert dispatcher.worker is None


@pytest.mark.asyncio
async def test_threaded_progress_enqueue_is_acknowledged_before_worker_shutdown():
    events = []

    async def progress(event):
        events.append(event["event"])

    dispatcher = _OrderedProgressDispatcher(progress)
    dispatcher.start()
    enqueued = Event()

    def send_from_thread():
        dispatcher.enqueue({"event": "thread.event"})
        enqueued.set()

    thread = Thread(target=send_from_thread)
    thread.start()
    assert enqueued.wait(timeout=1.0)

    await dispatcher.close()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert events == ["thread.event"]
    assert dispatcher.queue.empty()
    assert dispatcher.worker is None

    late_thread = Thread(target=lambda: dispatcher.enqueue({"event": "late.event"}))
    late_thread.start()
    late_thread.join(timeout=1.0)
    await asyncio.sleep(0)

    assert events == ["thread.event"]
    assert dispatcher.queue.empty()


@pytest.mark.asyncio
async def test_abort_cancels_pending_thread_delivery_and_clears_queue():
    async def blocking_progress(_event):
        await asyncio.Event().wait()

    dispatcher = _OrderedProgressDispatcher(blocking_progress)
    dispatcher.start()
    enqueued = Event()

    def send_from_thread():
        dispatcher.enqueue({"event": "thread.event"})
        enqueued.set()

    thread = Thread(target=send_from_thread)
    thread.start()
    assert enqueued.wait(timeout=1.0)

    await asyncio.wait_for(dispatcher.abort(), timeout=0.5)
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert dispatcher.worker is None
    assert dispatcher.queue.empty()
    assert not _named_progress_tasks()


@pytest.mark.asyncio
async def test_refresh_keeps_build_guard_until_slow_progress_is_drained(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    callback_entered = asyncio.Event()
    release_callback = asyncio.Event()
    first_events = []
    second_events = []

    async def first_progress(event):
        first_events.append(event["event"])
        if event["event"] == "update.start":
            callback_entered.set()
            await release_callback.wait()

    async def second_progress(event):
        second_events.append(event["event"])

    async def fake_build_graph(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace(to_dict=lambda: {"success": True, "version": "v1"})

    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.service_build_graph",
        fake_build_graph,
    )
    service = SwarmSymphonyService()

    first = asyncio.create_task(service.refresh_graph(progress=first_progress))
    await callback_entered.wait()
    await asyncio.sleep(0)

    second = await service.refresh_graph(progress=second_progress)

    assert second["success"] is False
    assert "正在运行" in second["detail"]
    assert second_events == []
    assert not first.done()

    release_callback.set()
    first_result = await first

    assert first_result["success"] is True
    assert first_events == ["update.start", "update.done"]


@pytest.mark.asyncio
async def test_start_refresh_graph_runs_in_background_and_reuses_active_task(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    _BuildProcessLogger(config.paths.graph_dir / "build_log.jsonl").record(
        "update.done",
        success=True,
        version="old",
    )
    build_entered = asyncio.Event()
    release_build = asyncio.Event()

    async def fake_build_graph(*args, **kwargs):
        del args, kwargs
        build_entered.set()
        await release_build.wait()
        return SimpleNamespace(to_dict=lambda: {"success": True, "version": "v1"})

    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.service_build_graph",
        fake_build_graph,
    )
    service = SwarmSymphonyService()

    started = await service.start_refresh_graph(force=True)
    first_task = service._active_build_task

    assert started["success"] is True
    assert started["background"] is True
    assert started["build_status"] == "running"
    assert started["build_progress"]["status"] == "running"
    assert [entry["stage"] for entry in started["build_log"]] == ["update.start"]
    assert first_task is not None
    assert build_entered.is_set() is False

    await build_entered.wait()
    reused = await service.start_refresh_graph(force=False)

    assert reused["success"] is True
    assert reused["background"] is True
    assert reused["build_status"] == "running"
    assert [entry["stage"] for entry in reused["build_log"]] == ["update.start"]
    assert service._active_build_task is first_task

    release_build.set()
    result = await asyncio.wait_for(first_task, timeout=0.5)

    assert result["success"] is True
    assert service._active_build_task is None


@pytest.mark.asyncio
async def test_start_refresh_graph_status_is_running_before_background_task_enters(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    _BuildProcessLogger(config.paths.graph_dir / "build_log.jsonl").record(
        "update.done",
        success=True,
        version="old",
    )
    build_entered = asyncio.Event()
    release_build = asyncio.Event()

    async def fake_build_graph(*args, **kwargs):
        del args, kwargs
        build_entered.set()
        await release_build.wait()
        return SimpleNamespace(to_dict=lambda: {"success": True, "version": "new"})

    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.service_build_graph",
        fake_build_graph,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.graph_status",
        lambda *args, **kwargs: SimpleNamespace(to_dict=lambda: {"success": True}),
    )
    service = SwarmSymphonyService()

    started = await service.start_refresh_graph(force=True)
    status = await service.graph_status()

    assert started["build_progress"] == status["build_progress"]
    assert status["build_progress"]["status"] == "running"
    assert status["build_progress"]["stage"] == "update.start"
    assert status["build_progress"]["percent"] == 3
    assert [entry["stage"] for entry in status["build_log"]] == ["update.start"]

    await build_entered.wait()
    release_build.set()
    result = await asyncio.wait_for(service._active_build_task, timeout=0.5)

    assert result["success"] is True


def test_build_progress_keeps_running_batch_at_relation_stage_start():
    entries = [
        {"stage": "graph.resolve.start"},
        {
            "stage": "graph.resolve.progress",
            "matcher_event": "matching_start",
            "current": 0,
            "total": 1,
        },
        {
            "stage": "graph.resolve.progress",
            "matcher_event": "batch_start",
            "current": 1,
            "total": 1,
        },
    ]

    assert _build_progress(entries)["percent"] == 72


def test_build_progress_uses_completed_batches_and_never_regresses():
    entries = [
        {"stage": "graph.resolve.start"},
        {
            "stage": "graph.resolve.progress",
            "matcher_event": "matching_start",
            "current": 0,
            "total": 4,
        },
        {
            "stage": "graph.resolve.progress",
            "matcher_event": "batch_done",
            "current": 2,
            "total": 4,
        },
        {
            "stage": "graph.resolve.progress",
            "matcher_event": "batch_start",
            "current": 1,
            "total": 4,
        },
    ]

    assert _build_progress(entries)["percent"] == 78

    entries.append(
        {
            "stage": "graph.resolve.progress",
            "matcher_event": "batch_done",
            "current": 1,
            "total": 4,
        }
    )
    assert _build_progress(entries)["percent"] == 78

    entries.append(
        {
            "stage": "graph.resolve.progress",
            "matcher_event": "matching_done",
            "current": 4,
            "total": 4,
        }
    )
    assert _build_progress(entries)["percent"] == 84


def test_build_progress_uses_global_candidate_counts_across_matcher_windows():
    entries = [
        {"stage": "graph.resolve.start", "candidate_count": 143},
        {
            "stage": "graph.resolve.progress",
            "matcher_event": "matching_done",
            "current": 4,
            "total": 4,
            "completed_candidate_count": 16,
            "total_candidate_count": 143,
        },
        {
            "stage": "graph.resolve.progress",
            "matcher_event": "batch_start",
            "current": 1,
            "total": 4,
            "completed_candidate_count": 16,
            "total_candidate_count": 143,
        },
        {
            "stage": "graph.resolve.progress",
            "matcher_event": "batch_done",
            "current": 1,
            "total": 4,
            "completed_candidate_count": 20,
            "total_candidate_count": 143,
        },
    ]

    progress = _build_progress(entries)

    assert progress["percent"] == 74
    assert progress["current"] == 20
    assert progress["total"] == 143


def test_build_progress_invalid_relation_totals_stay_at_stage_start():
    entries = [
        {
            "stage": "graph.resolve.progress",
            "matcher_event": "batch_done",
            "current": 1,
            "total": 0,
        }
    ]

    assert _build_progress(entries)["percent"] == 72


@pytest.mark.asyncio
async def test_cancel_build_without_active_task_returns_idle(monkeypatch, tmp_path):
    config = _config(tmp_path)
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )

    result = await SwarmSymphonyService().cancel_build()

    assert result["success"] is False
    assert result["cancelled"] is False
    assert result["build_status"] == "idle"


@pytest.mark.asyncio
async def test_direct_cancel_aborts_blocked_progress_and_releases_guard(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    callback_entered = asyncio.Event()
    callback_cancelled = asyncio.Event()
    build_entered = asyncio.Event()

    async def blocking_progress(_event):
        callback_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            callback_cancelled.set()

    async def fake_build_graph(*args, **kwargs):
        del args, kwargs
        build_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.service_build_graph",
        fake_build_graph,
    )
    service = SwarmSymphonyService()
    task = asyncio.create_task(service.refresh_graph(progress=blocking_progress))
    await callback_entered.wait()
    await build_entered.wait()

    task.cancel("test.direct_cancel")
    result = await asyncio.wait_for(task, timeout=0.5)

    assert result["success"] is False
    assert result["cancelled"] is True
    assert result["build_status"] == "cancelled"
    assert callback_cancelled.is_set()
    assert service._active_build_task is None
    assert not _named_progress_tasks()


@pytest.mark.asyncio
async def test_cancel_build_aborts_blocked_progress_and_releases_guard(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    callback_entered = asyncio.Event()
    build_entered = asyncio.Event()

    async def blocking_progress(_event):
        callback_entered.set()
        await asyncio.Event().wait()

    async def fake_build_graph(*args, **kwargs):
        del args, kwargs
        build_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.service_build_graph",
        fake_build_graph,
    )
    service = SwarmSymphonyService()
    task = asyncio.create_task(service.refresh_graph(progress=blocking_progress))
    await callback_entered.wait()
    await build_entered.wait()

    cancelled = await asyncio.wait_for(service.cancel_build(), timeout=0.5)
    build_result = await asyncio.wait_for(task, timeout=0.5)

    assert cancelled["success"] is True
    assert cancelled["cancelled"] is True
    assert cancelled["build_status"] == "cancelled"
    assert build_result["success"] is False
    assert build_result["cancelled"] is True
    assert build_result["build_status"] == "cancelled"
    assert service._active_build_task is None
    assert not _named_progress_tasks()


@pytest.mark.asyncio
async def test_graph_status_repairs_interrupted_build_log(monkeypatch, tmp_path):
    config = _config(tmp_path)
    graph_dir = config.paths.graph_dir
    _BuildProcessLogger(graph_dir / "build_log.jsonl").record("update.start")

    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.graph_status",
        lambda *args, **kwargs: SimpleNamespace(
            to_dict=lambda: {"success": True, "exists": False, "stale": True}
        ),
    )

    result = await SwarmSymphonyService().graph_status()

    assert result["build_progress"]["status"] == "cancelled"
    assert result["build_log"][-1]["stage"] == "update.cancelled"
    assert result["build_log"][-1]["reason"] == "process_interrupted"


@pytest.mark.asyncio
async def test_service_graph_status_does_not_bind_freshness_to_current_default_model(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    captured = {}

    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )

    def fake_graph_status(*args, **kwargs):
        del args
        captured.update(kwargs)
        return SimpleNamespace(
            to_dict=lambda: {"success": True, "exists": True, "stale": False}
        )

    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.graph_status",
        fake_graph_status,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.LLMConfig.from_default_model",
        lambda: (_ for _ in ()).throw(
            AssertionError("default model must not affect graph status")
        ),
    )

    result = await SwarmSymphonyService().graph_status()

    assert result["stale"] is False
    assert captured.get("llm_config") is None


@pytest.mark.asyncio
async def test_refresh_build_failure_returns_business_payload(monkeypatch, tmp_path):
    config = _config(tmp_path)

    async def fail_build(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.service_build_graph",
        fail_build,
    )

    result = await SwarmSymphonyService().refresh_graph()

    assert result["success"] is False
    assert "LLM unavailable" in result["detail"]
    assert result["build_progress"]["status"] == "error"


@pytest.mark.asyncio
async def test_refresh_preserves_downstream_failure_result(monkeypatch, tmp_path):
    config = _config(tmp_path)

    async def return_failed_build(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace(
            to_dict=lambda: {
                "success": False,
                "graph_dir": str(config.paths.graph_dir),
                "detail": "fingerprint model failed",
            }
        )

    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.service_build_graph",
        return_failed_build,
    )

    result = await SwarmSymphonyService().refresh_graph()

    assert result["success"] is False
    assert result["detail"] == "fingerprint model failed"
    assert result["build_progress"]["status"] == "error"
    assert result["build_log"][-1]["stage"] == "update.failed"


@pytest.mark.asyncio
async def test_refresh_propagates_framework_model_failure_from_core(monkeypatch, tmp_path):
    config = symphony_config_from_dict(
        {
            "paths": {
                "skills_root": str(tmp_path / "skills"),
                "graph_dir": str(tmp_path / "graph"),
            }
        }
    )
    skill_md = config.paths.skills_root / "writer" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: writer\n---\nWrite markdown.\n", encoding="utf-8")

    class FrameworkFailingModel:
        async def invoke(self, messages, **kwargs):
            del messages, kwargs
            raise build_error(
                StatusCode.MODEL_CALL_FAILED,
                error_msg="model unavailable",
            )

    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.LLMConfig.from_default_model",
        lambda: LLMConfig(model="failing-model"),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.build.model_from_config",
        lambda _config: FrameworkFailingModel(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.adapter.model_from_config",
        lambda _config: FrameworkFailingModel(),
    )

    result = await SwarmSymphonyService().refresh_graph()

    assert result["success"] is False
    assert "model unavailable" in result["detail"]
    assert result["build_progress"]["status"] == "error"
    assert result["build_log"][-1]["stage"] == "update.failed"
    assert not (config.paths.graph_dir / "current.json").exists()


def test_build_progress_treats_false_done_payload_as_error() -> None:
    progress = _build_progress(
        [
            {
                "stage": "update.done",
                "success": False,
                "label": "构建完成",
                "current": 1,
                "total": 1,
            }
        ]
    )

    assert progress["status"] == "error"


def _named_progress_tasks():
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "symphony-build-progress"
    ]


def _scan_result(fingerprints):
    return ScanResult(
        capabilities=tuple(
            CapabilityDescriptor(
                capability_id=item.capability_id,
                capability_type=item.capability_type,
                name=item.name,
                description=item.description,
                source="test",
                inputs=item.inputs,
                outputs=item.outputs,
                classification=item.classification,
                tags=item.tags,
                content_hash=item.content_hash,
                semantic_content=item.semantic_content,
                metadata={"entrypoint": f"{item.capability_id}/SKILL.md"},
            )
            for item in fingerprints
        )
    )
