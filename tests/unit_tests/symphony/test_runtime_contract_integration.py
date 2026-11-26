"""Host YAML to installed Core runtime, Discovery, planning and Flow evidence."""

import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import SkillRetrievalToolkit
from jiuwenswarm.common.symphony_config import load_symphony_config
from jiuwenswarm.runtime.symphony.llm import LLMConfig
from jiuwenswarm.runtime.symphony.service import SwarmSymphonyService


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
