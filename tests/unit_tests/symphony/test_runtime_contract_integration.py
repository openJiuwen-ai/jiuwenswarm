"""Host YAML to installed Core runtime, Discovery, planning and Flow evidence."""

import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from openjiuwen.symphony import GraphArtifactStore, SymphonyRuntime

from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import SkillRetrievalToolkit
from jiuwenswarm.common.config import migrate_config_from_template
from jiuwenswarm.common.symphony_config import load_symphony_config
from jiuwenswarm.common.utils import ensure_config_migrated_from_template
from jiuwenswarm.runtime.symphony.llm import LLMConfig
from jiuwenswarm.runtime.symphony.service import SwarmSymphonyService


def _write_active_upgrade_config(monkeypatch, tmp_path, raw, *, current_version="0.2.10"):
    template = tmp_path / "template.yaml"
    template.write_text("{}\n", encoding="utf-8")
    config_path = tmp_path / "config" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr("jiuwenswarm.common.config.get_config_file", lambda: config_path)
    monkeypatch.setattr("jiuwenswarm.common.utils._find_config_template_path", lambda: template)
    monkeypatch.setattr("jiuwenswarm.common.version.get_runtime_version_label", lambda: current_version)
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: load_symphony_config(yaml.safe_load(config_path.read_text(encoding="utf-8"))),
    )
    return template, config_path


def _write_legacy_skill_graph(graph_dir):
    version = "before-upgrade"
    version_dir = graph_dir / "versions" / version
    version_dir.mkdir(parents=True)
    (graph_dir / "current.json").write_text(json.dumps({
        "schema_version": "1.0", "version": version, "path": f"versions/{version}",
    }), encoding="utf-8")
    payload = {
        "schema_version": "1.0",
        "generated_at": "2026-11-18T00:00:00Z",
        "source_snapshot": {"schema_version": "JiuwenSwarm-symphony-graph-source-v2"},
        "capabilities": [{"capability_id": name, "capability_type": "skill", "name": name}
                         for name in ("extract", "summarize")],
        "nodes": [{"id": f"capability:{name}", "type": "capability", "label": name}
                  for name in ("extract", "summarize")],
        "edges": [{"source": "capability:extract", "target": "capability:summarize",
                   "type": "can_feed", "confidence": 0.97}],
        "lookup": {},
        "config": {"thresholds": {"can_feed": 0.7}},
    }
    (version_dir / "graph.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", ["default", "custom", "already-copied"])
async def test_upgrade_publishes_legacy_skill_graph_before_web_reads(monkeypatch, tmp_path, layout):
    skills = tmp_path / "skills"
    skills.mkdir()
    symphony_dir = tmp_path / "symphony"
    monkeypatch.setattr("jiuwenswarm.common.paths.get_symphony_dir", lambda: symphony_dir)
    legacy_dir = symphony_dir / "graph"
    paths = {"skills_root": str(skills)}
    if layout == "custom":
        legacy_dir = tmp_path / "custom-graph"
        paths["graph_dir"] = str(legacy_dir)
    _, config_path = _write_active_upgrade_config(monkeypatch, tmp_path, {
        "config_version": "0.2.9.beta5 · 2026-11-17",
        "symphony": {"enabled": True, "paths": paths},
    }, current_version="0.2.9.beta5 · 2026-11-28")
    runtime_root = tmp_path / "custom-graph" / "runtime" if layout == "custom" else symphony_dir / "runtime"
    graph_dir = runtime_root / "graph"
    if layout == "already-copied":
        legacy_dir = graph_dir
    prior_graph = _write_legacy_skill_graph(legacy_dir)
    service = SwarmSymphonyService()

    assert (await service.graph())["success"] is False
    assert (await service.graph_status())["exists"] is False
    assert not (runtime_root / "runtime.json").exists()
    assert ensure_config_migrated_from_template(tmp_path) is True
    assert ensure_config_migrated_from_template(tmp_path) is False
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["config_version"] == "0.2.9.beta5 · 2026-11-28"
    config = load_symphony_config(saved)

    result = await service.graph()
    status = await service.graph_status()
    assert status["success"] is True
    assert status["exists"] is True
    assert status["stale"] is True
    assert result["success"] is True
    assert [node["id"] for node in result["graph"]["nodes"]] == ["skill:extract", "skill:summarize"]
    assert result["graph"]["edges"] == [{"source": "skill:extract", "target": "skill:summarize",
                                         "type": "can_feed", "confidence": 0.97}]
    assert SymphonyRuntime(None, config).orchestration.read().to_dict() == prior_graph
    store = GraphArtifactStore(graph_dir)
    assert store.current_artifact().version == status["version"]
    assert len(list((graph_dir / "versions").iterdir())) == 1
    if layout == "already-copied":
        legacy_dir = symphony_dir / ".runtime-legacy-graph"
    assert GraphArtifactStore(legacy_dir).read() == prior_graph

    current = {**prior_graph, "source_snapshot": {"runtime_schema": "symphony.runtime.v1"}, "edges": []}
    store.publish(current, version="new-build")
    assert (await service.graph())["graph"]["edges"] == []
    assert store.current_artifact().version == "new-build"


def test_skill_graph_migration_retries_after_import_is_interrupted(monkeypatch, tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    runtime_root = tmp_path / "runtime"
    graph_dir = runtime_root / "graph"
    prior_graph = _write_legacy_skill_graph(graph_dir)
    raw = {"config_version": "0.2.9", "symphony": {
        "capability": {"sources": [{"type": "skill", "root": str(skills)}]},
        "storage": {"artifact_root": str(runtime_root)},
        "orchestration": {"enabled": True},
    }}
    template, config_path = _write_active_upgrade_config(monkeypatch, tmp_path, raw)

    def interrupt_publish(*_args, **_kwargs):
        raise OSError("temporary write failure")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(GraphArtifactStore, "activate_if_current", interrupt_publish)
        assert migrate_config_from_template(template, config_path) is False
        assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == raw
        assert GraphArtifactStore(graph_dir).current_artifact() is None
        assert GraphArtifactStore(tmp_path / ".runtime-legacy-graph").read() == prior_graph

    assert migrate_config_from_template(template, config_path) is True
    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["config_version"] == "0.2.10"
    assert GraphArtifactStore(graph_dir).read() == prior_graph


@pytest.mark.asyncio
async def test_skill_graph_migration_respects_existing_collection_binding(monkeypatch, tmp_path):
    legacy_dir = tmp_path / "legacy-graph"
    prior_graph = _write_legacy_skill_graph(legacy_dir)
    runtime_root = legacy_dir / "runtime"
    runtime_root.mkdir()
    binding = json.dumps({"schema_version": "symphony.runtime.v1", "capability_type": "skill",
                          "capability_root": str(tmp_path / "other-skills")})
    (runtime_root / "runtime.json").write_text(binding, encoding="utf-8")
    template, config_path = _write_active_upgrade_config(monkeypatch, tmp_path, {
        "config_version": "0.2.9",
        "symphony": {"enabled": True, "paths": {"skills_root": str(tmp_path / "skills"), "graph_dir": str(legacy_dir)}},
    })

    assert migrate_config_from_template(template, config_path) is False
    result = await SwarmSymphonyService().graph()

    assert result["success"] is False
    assert "different collection" in result["error"]
    assert not (runtime_root / "graph").exists()
    assert (runtime_root / "runtime.json").read_text(encoding="utf-8") == binding
    assert GraphArtifactStore(legacy_dir).read() == prior_graph


@pytest.mark.asyncio
async def test_native_publish_wins_if_it_completes_during_skill_graph_migration(monkeypatch, tmp_path):
    legacy_dir = tmp_path / "legacy-graph"
    prior_graph = _write_legacy_skill_graph(legacy_dir)
    template, config_path = _write_active_upgrade_config(monkeypatch, tmp_path, {
        "symphony": {"enabled": True, "paths": {"skills_root": str(tmp_path / "skills"), "graph_dir": str(legacy_dir)}},
    })
    activate = GraphArtifactStore.activate_if_current

    def publish_new_graph_first(store, artifact, *, expected_version):
        store.publish({**prior_graph, "source_snapshot": {"runtime_schema": "symphony.runtime.v1"},
                       "edges": []}, version="new-build")
        return activate(store, artifact, expected_version=expected_version)

    monkeypatch.setattr(GraphArtifactStore, "activate_if_current", publish_new_graph_first)

    assert migrate_config_from_template(template, config_path) is True
    result = await SwarmSymphonyService().graph()

    assert result["success"] is True
    assert result["graph"]["edges"] == []
    assert GraphArtifactStore(legacy_dir / "runtime" / "graph").current_artifact().version == "new-build"
    assert GraphArtifactStore(legacy_dir).read() == prior_graph


@pytest.mark.parametrize("previous,current", [
    (None, "0.2.10"),
    ("0.2.10", "0.2.10"),
    ("0.2.11", "0.2.10"),
    ("unknown-build", "0.2.10"),
    ("0.2.9b5.dev20261128", "0.2.9.beta5 · 2026-11-17"),
    ("0.2.9b5.dev20261128", "0.2.9.beta5 · 2026-11-28"),
])
def test_skill_graph_import_skips_startup_without_forward_upgrade(monkeypatch, tmp_path, previous, current):
    legacy = tmp_path / "legacy"
    prior_graph = _write_legacy_skill_graph(legacy)
    runtime_root = legacy / "runtime"
    raw = {"symphony": {
        "capability": {"sources": [{"type": "skill", "root": str(tmp_path / "skills")}]},
        "orchestration": {"enabled": True},
        "storage": {"artifact_root": str(runtime_root)},
    }}
    if previous is not None:
        raw["config_version"] = previous
    template, config_path = _write_active_upgrade_config(monkeypatch, tmp_path, raw, current_version=current)

    migrate_config_from_template(template, config_path)

    assert not runtime_root.exists()
    assert GraphArtifactStore(legacy).read() == prior_graph


@pytest.mark.parametrize("unknown_schema", ["source", "target"])
def test_skill_graph_upgrade_skips_unknown_artifact_schemas(monkeypatch, tmp_path, unknown_schema):
    legacy = tmp_path / "legacy"
    prior_graph = _write_legacy_skill_graph(legacy)
    template, config_path = _write_active_upgrade_config(monkeypatch, tmp_path, {
        "config_version": "0.2.9",
        "symphony": {"enabled": True, "paths": {"skills_root": str(tmp_path / "skills"), "graph_dir": str(legacy)}},
    })
    if unknown_schema == "source":
        prior_graph["source_snapshot"]["schema_version"] = "JiuwenSwarm-symphony-graph-source-v3"
        GraphArtifactStore(legacy).publish(prior_graph, version="different-format")
    else:
        monkeypatch.setattr("openjiuwen.symphony.inventory.LAYOUT_SCHEMA", "symphony.runtime.v2")

    assert migrate_config_from_template(template, config_path) is True
    assert not (legacy / "runtime").exists()
    assert GraphArtifactStore(legacy).read() == prior_graph


def test_upgrade_preserves_legacy_graph_when_orchestration_is_disabled(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy"
    prior_graph = _write_legacy_skill_graph(legacy)
    template, config_path = _write_active_upgrade_config(monkeypatch, tmp_path, {
        "config_version": "0.2.9",
        "symphony": {"enabled": False, "paths": {"graph_dir": str(legacy)}},
    })

    assert migrate_config_from_template(template, config_path) is True

    assert GraphArtifactStore(legacy / "runtime" / "graph").read() == prior_graph
    config = load_symphony_config(yaml.safe_load(config_path.read_text(encoding="utf-8")))
    assert config.orchestration.enabled is False


class LifecycleModel:
    def __init__(self, name):
        self.calls = []
        self.model_config = SimpleNamespace(model_name=name, context_window=8000, temperature=0.0)
        self.aclose = AsyncMock()

    async def invoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        system = messages[0]["content"]
        if "编排经验蒸馏" in system:
            data = {"name": "document-summary", "task_description": "Summarize documents",
                    "trigger_conditions": "A document needs a summary", "example_requests": ["Summarize a report"],
                    "execution_narrative": "Extract text and summarize the result."}
        else:
            try:
                payload = json.loads(messages[-1]["content"])
            except json.JSONDecodeError:
                names = re.findall(r"^### (.+)$", messages[-1]["content"], re.MULTILINE)
                data = {"skills/documents": names}
            else:
                if "candidates" in payload:
                    data = {"matches": [{"id": item["id"], "direction": next(iter(item["directions"])),
                                         "confidence": 0.95} for item in payload["candidates"]]}
                else:
                    data = {"status": "ready", "steps": [{"skill_id": "extract"}, {"skill_id": "summarize"}],
                            "can_feed_edges": [{"source_id": "extract", "target_id": "summarize"}]}
        return SimpleNamespace(content=json.dumps(data), finish_reason="stop")


@pytest.mark.asyncio
async def test_background_graph_build_rejects_multiple_skill_sources_before_writing_state(monkeypatch, tmp_path):
    artifact_root = tmp_path / "runtime"
    config = load_symphony_config({"symphony": {
        "capability": {"sources": [
            {"type": "skill", "root": str(tmp_path / "skills")},
            {"type": "skill", "root": str(tmp_path / "more-skills")},
        ]},
        "storage": {"artifact_root": str(artifact_root)},
        "orchestration": {"enabled": True},
    }})
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.load_symphony_config", lambda: config)
    service = SwarmSymphonyService()
    with pytest.raises(ValueError, match="exactly one skill capability source"):
        await service.start_refresh_graph()
    assert not artifact_root.exists()


@pytest.mark.asyncio
async def test_yaml_build_discovery_plan_and_flow_use_one_runtime_contract(monkeypatch, tmp_path):
    skills = tmp_path / "capabilities"
    for name, input_name, output_name in (("extract", "file", "text"), ("summarize", "text", "summary")):
        directory = skills / name
        directory.mkdir(parents=True)
        metadata = {"name": name, "description": "Extract and summarize documents", "version": "1.0.0",
                    "inputs": [{"name": input_name, "type": "text"}],
                    "outputs": [{"name": output_name, "type": "text"}]}
        (directory / "SKILL.md").write_text("---\n" + yaml.safe_dump(metadata) + "---\nProcess the supplied document.")
    raw = yaml.safe_load(f'''
symphony:
  capability:
    sources:
      - type: skill
        root: {skills}
    fingerprint:
      extraction:
        llm_enabled: false
  storage:
    artifact_root: {tmp_path / "runtime"}
  retrieval:
    enabled: true
    index_build:
      task:
        allow_fallback_tree: false
    prompt:
      context_window_tokens: 1000
  orchestration:
    enabled: true
    min_edge_confidence: 0.7
    plan:
      dynamic_graph_enabled: true
    experience:
      enabled: true
''')
    config = load_symphony_config(raw)
    default_model, session_model = LifecycleModel("build-model"), LifecycleModel("session-model")
    default_llm, session_llm = LLMConfig(model="build-model"), LLMConfig(model="session-model")

    def resolve_model(llm, **_kwargs):
        return session_model if llm.model == "session-model" else default_model

    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.load_symphony_config", lambda: config)
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.resolve_preferred_language",
        lambda: "zh",
    )
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.LLMConfig.from_default_model", lambda: default_llm)
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.model_from_config", resolve_model)
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.build.model_from_config", resolve_model)
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.probe_model_connection", AsyncMock())
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.trusted_session_user", lambda _session: "user-a")
    monkeypatch.setenv("SKILLPACK_PRODUCTIZATION_ENABLED", "true")
    service = SwarmSymphonyService()
    runtime = service.runtime()
    toolkit = None
    try:
        assert (await runtime.retrieval.build())["success"]
        built = await service.refresh_graph()
        assert built["success"] and built["edge_count"] >= 1
        assert session_model.calls == []
        toolkit = SkillRetrievalToolkit(skill_directories=[skills], config_base=raw, model=session_model,
                                        session_scope="cross-repo", auto_build_index=False)
        assert toolkit.index_enabled
        assert toolkit.candidate_budget_tokens == 10
        assert {record.worker_id for record in toolkit.current_records()} == {"extract", "summarize"}
        found = await toolkit.runtime.retrieval.search(operation="search", query="documents")
        assert "extract" in str(found) or "summarize" in str(found)
        calls_before_plan = len(default_model.calls)
        planned = await service.plan("Extract and summarize the document", candidate_skill_ids=["extract", "summarize"],
                                     capability_type="skill", llm_config=session_llm)
        assert planned["success"]
        assert planned["planned_graph"]["graph_snapshot"]["static_revision"]
        assert set(planned["planned_graph"]["graph"]["nodes"]) == {"extract", "summarize"}
        assert session_model.calls and len(default_model.calls) == calls_before_plan
        snapshot = runtime.capture_graph_snapshot()
        for index in range(3):
            trace_id = f"executed-{index}"
            execution = {
                "trace_id": trace_id, "query": "Extract and summarize the document", "outcome": "success",
                "graph_snapshot": snapshot,
                "graph": {"id": trace_id, "type": "execution_graph", "directed": True,
                          "nodes": {f"skill:{name}": {"label": "skill", "metadata": {
                              "capability_type": "skill", "version": "1.0.0"}} for name in ("extract", "summarize")},
                          "edges": [{"source": "skill:extract", "target": "skill:summarize", "relation": "can_feed",
                                     "metadata": {"success": True}}]},
            }
            await service.submit_evolution_and_notify(None, execution, session_id="cross-repo", capture_mode="agent", channel_id="web")
            assert bool(runtime.flow.list_candidates()) == (index == 2)
        assert runtime.capture_graph_snapshot()["observation_revision"] != snapshot["observation_revision"]
        assert len(runtime.flow.store.read_evidence()) == 3
        assert runtime.flow.list_candidates()
        assert all((tmp_path / "runtime" / name).is_dir() for name in ("inventory", "cache", "tree", "graph", "flow"))
    finally:
        if toolkit is not None:
            await toolkit.aclose()
        await service.close()
    default_model.aclose.assert_not_called()
    session_model.aclose.assert_not_called()
