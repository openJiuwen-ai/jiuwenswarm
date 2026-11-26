import asyncio
import ast
import json
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.symphony import OrchestrationPlan, SymphonyRuntime

from jiuwenswarm.runtime.symphony.adapter import (
    llm_config_signature,
    normalize_candidate_ids,
)
from jiuwenswarm.runtime.symphony.build import build_graph
from jiuwenswarm.common.symphony_config import symphony_config_from_dict
from jiuwenswarm.runtime.symphony.llm import LLMConfig
from jiuwenswarm.runtime.symphony.service import (
    SwarmSymphonyService,
    _BuildProcessLogger,
    _OrderedProgressDispatcher,
    _build_progress,
    _build_log_path,
    _graph_needs_build,
)
from jiuwenswarm.runtime.symphony.graph_storage import graph_exists


_ALLOWED_SYMPHONY_FACADE_SYMBOLS = {
    "openjiuwen.symphony.flow": frozenset({"FlowStore", "RecipeEvidence"}),
    "openjiuwen.symphony.config": frozenset({"RetrievalConfig"}),
}


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








def _forbidden_symphony_import(
    node,
    *,
    allowed_symbols,
    allowed_modules=frozenset(),
):
    if isinstance(node, ast.Import):
        imported_modules = {
            alias.name
            for alias in node.names
            if alias.name.startswith("openjiuwen.symphony")
        }
        forbidden = imported_modules - {"openjiuwen.symphony", *allowed_modules}
        return ",".join(sorted(forbidden))
    if not isinstance(node, ast.ImportFrom):
        return ""
    imported = {alias.name for alias in node.names}
    if node.module == "openjiuwen":
        return "openjiuwen:symphony" if "symphony" in imported else ""
    if not (node.module or "").startswith("openjiuwen.symphony"):
        return ""
    if node.module in allowed_modules:
        return ""
    if imported <= _ALLOWED_SYMPHONY_FACADE_SYMBOLS.get(node.module, frozenset()):
        return ""
    if node.module == "openjiuwen.symphony" and imported <= allowed_symbols:
        return ""
    return f"{node.module}:{','.join(sorted(imported))}"


@pytest.fixture
def fake_graph_llm(monkeypatch):
    client = _FakeGraphModel()
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.build.model_from_config",
        lambda _config: client,
    )
    return client


@pytest.fixture(autouse=True)
def stub_model_connection_probe(monkeypatch):
    async def probe(_config):
        return None

    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.probe_model_connection",
        probe,
    )


def _config(tmp_path, *, evolution=True):
    return symphony_config_from_dict({
        "capability": {"sources": [{"type": "skill", "root": str(tmp_path / "skills")}]},
        "storage": {"artifact_root": str(tmp_path / "runtime")},
        "orchestration": {
            "enabled": True, "build": {"matching": {"batch_size": 4}},
            "experience": {"enabled": evolution},
        },
    })


def _minimal_planned_graph(status="ready"):
    nodes = (
        {}
        if status == "no_plan"
        else {
            "writer": {"label": "Writer", "metadata": {"type": "skill"}},
            "reviewer": {"label": "Reviewer", "metadata": {"type": "skill"}},
        }
    )
    return {
        "graph": {
            "id": "plan-1",
            "type": "planned_graph",
            "directed": True,
            "metadata": {"status": status},
            "nodes": nodes,
            "edges": (
                []
                if status == "no_plan"
                else [
                    {
                        "source": "writer",
                        "target": "reviewer",
                        "relation": "can_feed",
                    }
                ]
            ),
        }
    }


def test_adapter_deduplicates_candidate_skill_ids():
    assert normalize_candidate_ids(["writer", "writer", "reviewer"]) == [
        "writer",
        "reviewer",
    ]






@pytest.mark.asyncio
@pytest.mark.parametrize("edge_weights", [{}, {"runtime_weight": 0.75}, {"runtime_weight": 0.0}])
async def test_service_graph_adapts_public_artifact_for_skill_graph_panel(
    monkeypatch,
    tmp_path,
    edge_weights,
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
                **edge_weights,
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
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(service, "_read_graph_artifact", lambda _graph_dir: artifact)
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_execution_disabled_skills",
        lambda **_kwargs: {"disabled"},
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
            **edge_weights,
        }
    ]
    skill_node_ids = {item["id"] for item in result["graph"]["nodes"]}
    assert all(
        edge[endpoint] in skill_node_ids
        for edge in result["graph"]["edges"]
        for endpoint in ("source", "target")
    )


@pytest.mark.asyncio
async def test_service_graph_disabled_skill_keeps_distinct_unicode_name(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    artifact = {
        "capabilities": [
            {"capability_id": "ppt", "capability_type": "skill", "name": "ppt"},
            {
                "capability_id": "ppt-master",
                "capability_type": "skill",
                "name": "ppt大师",
            },
            {
                "capability_id": "reviewer",
                "capability_type": "skill",
                "name": "Reviewer",
            },
        ],
        "nodes": [
            {"id": "capability:ppt", "type": "capability", "label": "ppt"},
            {
                "id": "capability:ppt-master",
                "type": "capability",
                "label": "ppt大师",
            },
            {
                "id": "capability:reviewer",
                "type": "capability",
                "label": "Reviewer",
            },
        ],
        "edges": [
            {
                "source": "capability:ppt",
                "target": "capability:ppt-master",
                "type": "can_feed",
            },
            {
                "source": "capability:ppt-master",
                "target": "capability:reviewer",
                "type": "can_feed",
            },
        ],
    }
    service = SwarmSymphonyService()
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(service, "_read_graph_artifact", lambda _graph_dir: artifact)
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_execution_disabled_skills",
        lambda **_kwargs: {"ppt"},
    )

    result = await service.graph()

    assert [item["id"] for item in result["skills"]] == ["ppt-master", "reviewer"]
    assert [item["id"] for item in result["graph"]["nodes"]] == [
        "skill:ppt-master",
        "skill:reviewer",
    ]
    assert result["graph"]["edges"] == [
        {
            "source": "skill:ppt-master",
            "target": "skill:reviewer",
            "type": "can_feed",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("capability_type", ["skill", "expert"])
async def test_service_plans_through_public_runtime_with_minimal_jgf(
    monkeypatch,
    tmp_path,
    capability_type,
):
    config = _config(tmp_path)
    captured = {}

    class FakeGraphEngine:
        async def plan(self, query, candidate_ids=None, **kwargs):
            captured.update(query=query, candidate_ids=candidate_ids, **kwargs)
            return OrchestrationPlan({"planned_graph": _minimal_planned_graph()})

    service = SwarmSymphonyService()

    async def fresh_status():
        return {"success": True, "exists": True, "stale": False}

    service.graph_status = fresh_status
    service._runtime_for = lambda _config: SimpleNamespace(
        orchestration=FakeGraphEngine(), aclose=AsyncMock()
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_symphony_config", lambda: config
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.resolve_preferred_language",
        lambda: "zh",
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_execution_disabled_skills",
        lambda **_kwargs: {"disabled"},
    )
    progress = object()
    result = await service.plan(
        "write",
        mode="beam",
        capability_type=capability_type,
        candidate_ids=["writer", "writer"],
        progress=progress,
    )

    assert result == {
        "success": True,
        "planned_graph": _minimal_planned_graph(),
    }
    assert captured["candidate_ids"] == ["writer"]
    assert captured["type"] == capability_type
    if capability_type == "skill":
        assert captured["disabled_capability_ids"] == {"disabled"}
    else:
        assert captured["expert_manifest_paths"] == []
        assert "disabled_capability_ids" not in captured
    assert "graph_scope_id" not in captured
    assert captured["language"] == "cn"
    assert captured["mode"] == "beam"
    assert captured["progress"] is progress


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fast", "beam"])
async def test_skill_compose_uses_public_core_plan(monkeypatch, tmp_path, mode):
    from jiuwenswarm.agents.harness.common.tools.symphony_toolkits import SymphonyToolkit

    config = _config(tmp_path, evolution=False)
    skill = tmp_path / "skills" / "writer" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: writer\ndescription: Write a brief\n---\nWrite the requested brief.\n")
    requests = []

    async def invoke(messages, **kwargs):
        request = json.loads(messages[-1]["content"])
        requests.append(request)
        response = {"status": "ready", "steps": [{"skill_id": "writer"}]} if mode == "fast" else {
            "selected_plan_index": 1,
        }
        return SimpleNamespace(content=json.dumps(response))

    runtime = SymphonyRuntime(SimpleNamespace(invoke=invoke), config)
    await runtime.orchestration.build()
    service = SwarmSymphonyService()
    monkeypatch.setattr(service, "_runtime_for", lambda *_: runtime)
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.load_symphony_config", lambda: config)
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.resolve_preferred_language",
        lambda: "zh",
    )
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.load_execution_disabled_skills", lambda **_: set())
    monkeypatch.setattr("jiuwenswarm.agents.harness.common.tools.symphony_toolkits.load_symphony_config", lambda *_: config)
    compose = next(tool for tool in SymphonyToolkit(service, llm_config_provider=lambda: None).get_tools()
                   if tool.card.name == "symphony_compose_graph")
    try:
        result = await compose.invoke({
            "query": "Write a brief from confirmed facts.", "type": "skill", "mode": mode,
            "candidate_ids": ["writer"], "inputs": [{"name": "facts", "kind": "text", "value": "Approved facts"}],
        })
        assert result["success"], result
        graph = result["planned_graph"]["graph"]
        assert graph["metadata"]["status"] == "ready"
        assert graph["nodes"]["writer"]["metadata"]["type"] == "skill"
        assert "plan_kind" not in graph["metadata"]
        assert requests[-1]["query"] == "Write a brief from confirmed facts."
        assert requests[-1]["inputs"] == [{"name": "facts", "kind": "text", "value": "Approved facts"}]
    finally:
        await service.close()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_expert_compose_uses_live_catalog_and_shares_the_web_graph(monkeypatch, tmp_path):
    from openjiuwen.agent_teams.context import reset_session_id, set_session_id
    from openjiuwen.agent_teams.experts import PackageLocation
    from openjiuwen.agent_teams.paths import get_agent_teams_home, reset_task_openjiuwen_home, set_task_openjiuwen_home
    from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase

    from jiuwenswarm.agents.harness.common.tools.symphony_toolkits import SymphonyToolkit
    from jiuwenswarm.agents.harness.team.experts import JiuwenExpertCatalog

    skills = tmp_path / "skills"
    (skills / "research").mkdir(parents=True)
    (skills / "research" / "SKILL.md").write_text("---\nname: research\ndescription: Find invoices\n---\nSearch.\n")
    experts = tmp_path / "experts"
    descriptions = {"research": "Produces an evidence table as CSV", "report": "Accepts an evidence table as CSV"}
    for name, description in descriptions.items():
        package = experts / f"installed-{name}"
        package.mkdir(parents=True)
        (package / "manifest.json").write_text(json.dumps({
            "name": name, "description": description, "package_type": "agent_template",
        }))
    installed = [PackageLocation(name=f"installed-{name}", path=experts / f"installed-{name}", source="local")
                 for name in descriptions]

    async def list_packages(_self):
        return list(installed)

    class HandoffModel(_FakeGraphModel):
        async def invoke(self, messages, **kwargs):
            data = json.loads(messages[-1]["content"])
            table = {"name": "evidence_table", "type": "csv", "description": "Source evidence table"}
            if "declared_inputs" in data:
                result = {}
                if data["capability_type"] == "expert":
                    if data["capability_id"] == "research":
                        result = {"outputs": [table]}
                    else:
                        result = {"inputs": [{**table, "required": True}], "outputs": [{"name": "brief", "type": "text"}]}
            elif "current_skill" in data:
                result = {"judgements": [
                    {"candidate_id": item["candidate_id"], "score": 0.95} for item in data["candidates"]
                ]}
            elif "candidate_plans" in data:
                result = {"selected_plan_index": 1, "steps": [{"skill_id": name} for name in descriptions]}
            elif "experts" in data:
                result = self._task_plan(data)
            elif "query" in data and "can_feed_edges" in data:
                ids = {item["id"] for item in data["skills"]}
                result = {"status": "ready", "steps": [{"skill_id": name} for name in descriptions if name in ids]}
            else:
                return await super().invoke(messages, **kwargs)
            return SimpleNamespace(content=json.dumps(result), finish_reason="stop")

        @staticmethod
        def _task_plan(data):
            assert all(set(expert) == {"id", "name", "description"} for expert in data["experts"])
            assert "published_expert_handoffs" not in data
            available = {item["id"] for item in data["experts"]}
            if data["query"] == "Use the installed report expert" and "report" not in available:
                return {"status": "no_plan", "reason": "The required report expert is unavailable."}
            notes = {"name": "source_notes", "type": "markdown"}
            checked = {"name": "checked_notes", "type": "markdown"}
            tasks = [
                {"id": "research", "type": "expert", "expert_id": "research", "title": "Research",
                 "task": "Collect source notes.", "outputs": [notes]},
                {"id": "verify", "type": "inline", "title": "Verification", "task": "Check each citation.",
                 "role": {"name": "Citation verifier", "description": "Independently check source evidence."},
                 "outputs": [checked]},
            ]
            handoffs = [{"source": "research", "target": "verify", "source_output": "source_notes"}]
            if "report" in available:
                tasks.append({"id": "report", "type": "expert", "expert_id": "report", "title": "Report",
                              "task": "Write the brief using checked notes.",
                              "outputs": [{"name": "brief", "type": "markdown"}]})
                handoffs.append({"source": "verify", "target": "report", "source_output": "checked_notes"})
            return {"status": "ready", "tasks": tasks, "handoffs": handoffs}

    config = symphony_config_from_dict({
        "capability": {"sources": [{"type": "skill", "root": skills}, {"type": "expert", "root": experts}]},
        "storage": {"artifact_root": str(tmp_path / "runtime")},
        "orchestration": {"enabled": True},
    })
    monkeypatch.setattr(JiuwenExpertCatalog, "list_expert_packages", list_packages)
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.load_symphony_config", lambda: config)
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.resolve_preferred_language",
        lambda: "zh",
    )
    monkeypatch.setattr("jiuwenswarm.agents.harness.common.tools.symphony_toolkits.load_symphony_config", lambda *_: config)
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.load_execution_disabled_skills", lambda **_: set())
    model = HandoffModel(confidences=[0.95])
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.build.model_from_config", lambda _: model)
    result = await build_graph(config, LLMConfig(model="fake-model"))
    assert result.success and (result.skill_count, result.expert_count) == (1, 2)

    runtime = SymphonyRuntime(model, config)
    service = SwarmSymphonyService()
    monkeypatch.setattr(service, "_runtime_for", lambda *_: runtime)
    published = runtime.orchestration.read().to_dict()
    compose = next(tool for tool in SymphonyToolkit(service).get_tools() if tool.card.name == "symphony_compose_graph")
    team_home = set_task_openjiuwen_home(tmp_path / "team-runtime")
    session = set_session_id("expert-plan-test")
    database = TeamDatabase(DatabaseConfig(connection_string=str(get_agent_teams_home() / "team.db")))
    try:
        await database.initialize()
        assert await database.team.create_team("planning-team", "Planning Team", "leader")
        monkeypatch.setattr(service, "refresh_graph", AsyncMock(side_effect=AssertionError("Planning rebuilt graph")))
        for mode in ("fast", "beam"):
            planned = await compose.invoke({"query": "Write and verify a Markdown brief.", "type": "expert", "mode": mode,
                                            "candidate_ids": ["research"]})
            assert planned["success"], planned
            graph = planned["planned_graph"]["graph"]
            assert graph["metadata"]["status"] == "ready"
            assert graph["metadata"]["plan_kind"] == "expert_tasks"
            assert [node["metadata"]["type"] for node in graph["nodes"].values()] == ["expert", "inline", "expert"]
            assert graph["nodes"]["task-1"]["metadata"]["expert_id"] == "research"
            assert graph["nodes"]["task-2"]["metadata"]["role"]["name"] == "Citation verifier"
            assert graph["nodes"]["task-3"]["metadata"]["expert_id"] == "report"
            assert graph["nodes"]["task-3"]["metadata"]["inputs"] == [{"name": "checked_notes", "type": "markdown"}]
            assert graph["edges"] == [
                {"source": "task-1", "target": "task-2", "relation": "can_feed",
                 "source_output": "source_notes", "target_input": "source_notes"},
                {"source": "task-2", "target": "task-3", "relation": "can_feed",
                 "source_output": "checked_notes", "target_input": "checked_notes"},
            ]

        web = await service.graph()
        assert {node["id"]: node["type"] for node in web["graph"]["nodes"]} == {
            "skill:research": "skill", "expert:research": "expert", "expert:report": "expert",
        }
        assert [(edge["source"], edge["target"]) for edge in web["graph"]["edges"]] == [
            ("expert:research", "expert:report"),
        ]
        assert [item["id"] for item in web["skills"]] == ["research"]

        installed.pop()
        missing = await compose.invoke({"query": "Use the installed report expert", "type": "expert",
                                        "candidate_ids": ["report"]})
        assert missing["planned_graph"]["graph"]["metadata"]["status"] == "no_plan"
        for mode in ("fast", "beam"):
            projected = await compose.invoke({"query": "write a brief", "type": "expert", "mode": mode,
                                              "candidate_ids": ["research"]})
            graph = projected["planned_graph"]["graph"]
            assert all(node["metadata"].get("expert_id") != "report" for node in graph["nodes"].values())
            assert len(graph["edges"]) == 1
        assert runtime.orchestration.read().to_dict() == published
        assert await database.member.get_team_members("planning-team") == []
        assert await database.task.get_team_tasks("planning-team") == []
    finally:
        await database.close()
        reset_session_id(session)
        reset_task_openjiuwen_home(team_home)
        await service.close()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_service_plan_uses_request_selected_model_instead_of_default(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    selected = LLMConfig(model="selected-model")
    runtime_calls = []

    class FakeGraphEngine:
        async def plan(self, *args, **kwargs):
            del args, kwargs
            return OrchestrationPlan({"planned_graph": _minimal_planned_graph()})

    service = SwarmSymphonyService()

    async def fresh_status():
        return {"success": True, "exists": True, "stale": False}

    def runtime_for(_config, *, llm_config=None):
        runtime_calls.append(llm_config)
        return SimpleNamespace(orchestration=FakeGraphEngine(), aclose=AsyncMock())

    service.graph_status = fresh_status
    service._runtime_for = runtime_for
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_symphony_config", lambda: config
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.resolve_preferred_language",
        lambda: "zh",
    )

    result = await service.plan("compose", llm_config=selected)

    assert result["success"] is True
    assert runtime_calls == [selected]


@pytest.mark.asyncio
async def test_service_plan_keeps_host_default_for_stale_graph_refresh(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    selected = LLMConfig(model="selected-model")
    refresh_calls = []

    class FakeGraphEngine:
        async def plan(self, *args, **kwargs):
            del args, kwargs
            return OrchestrationPlan({"planned_graph": _minimal_planned_graph()})

    service = SwarmSymphonyService()

    async def stale_status():
        return {"success": True, "exists": True, "stale": True}

    async def refresh_graph(*, progress=None, llm_config=None):
        refresh_calls.append((progress, llm_config))
        return {"success": True}

    service.graph_status = stale_status
    service.refresh_graph = refresh_graph
    service._runtime_for = lambda _config, **_kwargs: SimpleNamespace(
        orchestration=FakeGraphEngine(), aclose=AsyncMock()
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_symphony_config", lambda: config
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.resolve_preferred_language",
        lambda: "zh",
    )

    progress = object()
    result = await service.plan(
        "compose",
        progress=progress,
        llm_config=selected,
        capability_type="skill",
    )

    assert result["success"] is True
    assert refresh_calls == [(progress, None)]


@pytest.mark.asyncio
async def test_service_rebuilds_stale_graph_before_planning(monkeypatch, tmp_path):
    config = _config(tmp_path, evolution=False)
    calls = []
    plan_kwargs = {}

    class FakeGraphEngine:
        async def plan(self, *args, **kwargs):
            del args
            plan_kwargs.update(kwargs)
            return OrchestrationPlan(
                {"planned_graph": _minimal_planned_graph("no_plan")}
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
        orchestration=FakeGraphEngine(), aclose=AsyncMock()
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_symphony_config", lambda: config
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.resolve_preferred_language",
        lambda: "en",
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_execution_disabled_skills", lambda **_kwargs: set()
    )
    progress = object()
    result = await service.plan("write", capability_type="skill", progress=progress)

    assert calls == [(False, progress)]
    assert result["graph_build"]["rebuilt"] is True
    assert result["planned_graph"]["graph"]["metadata"]["status"] == "no_plan"
    assert "graph_scope_id" not in plan_kwargs


@pytest.mark.asyncio
async def test_service_promotes_graph_preparing_before_planning(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path, evolution=False)
    planner_called = False
    service = SwarmSymphonyService()

    async def stale_status():
        return {"success": True, "exists": True, "stale": True}

    async def refresh_graph(*, force=False, progress=None):
        del force, progress
        return {
            "success": False,
            "reason": "graph_preparing",
            "retryable": False,
            "build_status": "running",
            "detail": "已有技能总谱构建正在运行，请等待完成或先取消当前构建。",
        }

    async def unexpected_plan(*args, **kwargs):
        nonlocal planner_called
        del args, kwargs
        planner_called = True
        raise AssertionError("planner must not run while the graph is building")

    service.graph_status = stale_status
    service.refresh_graph = refresh_graph
    service._runtime_for = lambda _config: SimpleNamespace(
        orchestration=SimpleNamespace(plan=unexpected_plan)
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_symphony_config", lambda: config
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.resolve_preferred_language",
        lambda: "zh",
    )

    result = await service.plan("write", capability_type="skill")

    assert result["success"] is False
    assert result["reason"] == "graph_preparing"
    assert result["retryable"] is False
    assert result["build_status"] == "running"
    assert "正在运行" in result["detail"]
    assert planner_called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("planned_graph", [None, [], "not-a-graph"])
async def test_service_rejects_non_dict_planned_graph(
    monkeypatch,
    tmp_path,
    planned_graph,
):
    config = _config(tmp_path)

    class FakeGraphEngine:
        async def plan(self, *args, **kwargs):
            del args, kwargs
            return OrchestrationPlan({"planned_graph": planned_graph})

    service = SwarmSymphonyService()

    async def fresh_status():
        return {"success": True, "exists": True, "stale": False}

    service.graph_status = fresh_status
    service._runtime_for = lambda _config: SimpleNamespace(
        orchestration=FakeGraphEngine(), aclose=AsyncMock()
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_symphony_config", lambda: config
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.resolve_preferred_language",
        lambda: "zh",
    )

    result = await service.plan("compose")

    assert result["success"] is False
    assert result["detail"] == "Symphony orchestration returned no planned_graph"






















def test_runtime_cache_rebuilds_when_default_llm_changes(monkeypatch, tmp_path):
    config = _config(tmp_path, evolution=False)
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
            self.flow = SimpleNamespace(status=lambda: {"started": False})
            self.aclose = AsyncMock()

    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.LLMConfig.from_default_model",
        lambda: current["value"],
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.model_from_config",
        lambda value: ("model", value.model),
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.SymphonyRuntime",
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
    import openjiuwen.symphony.flow as flow_facade

    assert _ALLOWED_SYMPHONY_FACADE_SYMBOLS["openjiuwen.symphony.flow"] <= set(flow_facade.__all__)
    package_root = Path(__file__).parents[3] / "jiuwenswarm"
    allowed_symbols = {
        "FINGERPRINT_ARTIFACT_FILENAME",
        "CapabilityDescriptor",
        "CapabilityFingerprint",
        "FingerprintArtifact",
        "FingerprintArtifactStore",
        "FingerprintService",
        "FingerprintSettings",
        "GraphArtifactStore",
        "IONameVocabulary",
        "OrchestrationConfig",
        "PlanningInput",
        "ScanResult",
        "SkillFolderScanner",
        "SourceSnapshot",
        "SymphonyRuntime",
        "SymphonyConfig",
        "capability_content_hash",
        "normalize_name_key",
    }
    allowed_modules = {
        "openjiuwen.symphony.agent",
        "openjiuwen.symphony.discovery",
        "openjiuwen.symphony.flow.models",
        "openjiuwen.symphony.flow.distill",
        "openjiuwen.symphony.flow.narrative",
    }
    offenders = []
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            violation = _forbidden_symphony_import(
                node,
                allowed_symbols=allowed_symbols,
                allowed_modules=allowed_modules,
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


def test_flow_facade_import_guard_keeps_deep_imports_forbidden():
    public = ast.parse("from openjiuwen.symphony.flow import FlowStore, RecipeEvidence").body[0]
    assert not _forbidden_symphony_import(public, allowed_symbols=set())
    for statement in (
        "from openjiuwen.symphony.flow.store import FlowStore",
        "from openjiuwen.symphony.flow.engine import SymphonyFlowEngine",
        "import openjiuwen.symphony.flow.store as hidden",
        "from openjiuwen.symphony.flow import InternalStore",
        "from openjiuwen.symphony.flow import *",
    ):
        assert _forbidden_symphony_import(ast.parse(statement).body[0], allowed_symbols=set())


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
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.service_build_graph",
        fake_build_graph,
    )
    service = SwarmSymphonyService()

    first = asyncio.create_task(service.refresh_graph(progress=first_progress))
    await callback_entered.wait()
    await asyncio.sleep(0)

    second = await service.refresh_graph(progress=second_progress)

    assert second["success"] is False
    assert second["reason"] == "graph_preparing"
    assert second["retryable"] is False
    assert second["build_status"] == "running"
    assert "正在运行" in second["detail"]
    assert second_events == []
    assert not first.done()

    release_callback.set()
    first_result = await first

    assert first_result["success"] is True
    assert first_events == [
        "update.start",
        "model.probe.start",
        "model.probe.done",
        "update.done",
    ]


@pytest.mark.asyncio
async def test_start_refresh_graph_runs_in_background_and_reuses_active_task(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    _BuildProcessLogger(_build_log_path(Path(config.storage.artifact_root) / "graph")).record(
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
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.service_build_graph",
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
    assert [entry["stage"] for entry in reused["build_log"]] == [
        "update.start",
        "model.probe.start",
        "model.probe.done",
    ]
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
    _BuildProcessLogger(_build_log_path(Path(config.storage.artifact_root) / "graph")).record(
        "update.done",
        success=True,
        version="old",
    )
    probe_entered = asyncio.Event()
    release_probe = asyncio.Event()
    build_entered = asyncio.Event()
    release_build = asyncio.Event()

    async def blocking_probe(_config):
        probe_entered.set()
        await release_probe.wait()

    async def fake_build_graph(*args, **kwargs):
        del args, kwargs
        build_entered.set()
        await release_build.wait()
        return SimpleNamespace(to_dict=lambda: {"success": True, "version": "new"})

    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.probe_model_connection",
        blocking_probe,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.service_build_graph",
        fake_build_graph,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.graph_status",
        lambda *args, **kwargs: SimpleNamespace(to_dict=lambda: {"success": True}),
    )
    service = SwarmSymphonyService()

    started = await service.start_refresh_graph(force=True)
    build_task = service._active_build_task

    assert started["build_progress"]["status"] == "running"
    assert started["build_progress"]["stage"] == "update.start"
    assert build_task is not None

    await probe_entered.wait()
    try:
        status = await service.graph_status()

        assert status["build_progress"]["status"] == "running"
        assert status["build_progress"]["stage"] == "model.probe.start"
        assert status["build_progress"]["percent"] == 5
        assert [entry["stage"] for entry in status["build_log"]] == [
            "update.start",
            "model.probe.start",
        ]
        assert build_entered.is_set() is False
    finally:
        release_probe.set()
        release_build.set()

    result = await asyncio.wait_for(build_task, timeout=0.5)

    assert build_entered.is_set()
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
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
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
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.service_build_graph",
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
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.service_build_graph",
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




def test_cancelled_graph_build_keeps_the_last_published_graph_for_planning():
    assert _graph_needs_build(
        {
            "exists": True,
            "stale": True,
            "added_count": 1,
            "changed_count": 0,
            "removed_count": 0,
            "build_progress": {"status": "cancelled"},
        }
    ) is False
    assert _graph_needs_build(
        {
            "exists": True,
            "stale": True,
            "added_count": 1,
            "changed_count": 0,
            "removed_count": 0,
            "build_progress": {"status": "running"},
        }
    ) is True


@pytest.mark.asyncio
async def test_graph_status_repairs_interrupted_build_log(monkeypatch, tmp_path):
    config = _config(tmp_path)
    graph_dir = (Path(config.storage.artifact_root) / "graph")
    _BuildProcessLogger(_build_log_path(graph_dir)).record("update.start")

    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.graph_status",
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
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: config,
    )

    def fake_graph_status(*args, **kwargs):
        del args
        captured.update(kwargs)
        return SimpleNamespace(
            to_dict=lambda: {"success": True, "exists": True, "stale": False}
        )

    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.graph_status",
        fake_graph_status,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.LLMConfig.from_default_model",
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
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.service_build_graph",
        fail_build,
    )

    result = await SwarmSymphonyService().refresh_graph()

    assert result["success"] is False
    assert "LLM unavailable" in result["detail"]
    assert result["build_error"] == result["detail"]
    assert result["build_progress"]["detail"] == result["detail"]
    assert result["build_progress"]["error"] == "LLM unavailable"
    assert result["build_progress"]["status"] == "error"


@pytest.mark.asyncio
async def test_model_preflight_failure_is_preserved_by_status_and_graph_get(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    model_error = build_error(
        StatusCode.MODEL_CALL_FAILED,
        error_msg="model unavailable",
    )
    build_called = False

    async def fail_probe(_config):
        raise model_error

    async def unexpected_build(*args, **kwargs):
        nonlocal build_called
        del args, kwargs
        build_called = True
        raise AssertionError(
            "graph build must not start after a failed model preflight"
        )

    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.LLMConfig.from_default_model",
        lambda: LLMConfig(model="failing-model"),
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.probe_model_connection",
        fail_probe,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.service_build_graph",
        unexpected_build,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.graph_status",
        lambda *args, **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "success": True,
                "exists": False,
                "stale": True,
                "detail": "Symphony graph is missing",
            }
        ),
    )
    service = SwarmSymphonyService()
    monkeypatch.setattr(
        service,
        "_read_graph_artifact",
        lambda _graph_dir: (_ for _ in ()).throw(FileNotFoundError("graph missing")),
    )

    result = await service.refresh_graph()
    status = await service.graph_status()
    graph = await service.graph()

    expected = (
        "主模型连接测试未通过：[181001] model call failed, reason: model unavailable"
    )
    assert build_called is False
    assert result["success"] is False
    assert result["detail"] == expected
    assert result["build_error"] == expected
    assert result["build_progress"]["error"] == str(model_error)
    assert status["detail"] == expected
    assert status["build_error"] == expected
    assert graph["success"] is False
    assert graph["detail"] == expected
    assert graph["build_error"] == expected
    assert [item["stage"] for item in graph["build_log"]] == [
        "update.start",
        "model.probe.start",
        "update.failed",
    ]


@pytest.mark.asyncio
async def test_background_model_preflight_failure_is_visible_without_running_poll(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    model_error = build_error(
        StatusCode.MODEL_CALL_FAILED,
        error_msg="connection refused",
    )

    async def fail_probe(_config):
        raise model_error

    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.LLMConfig.from_default_model",
        lambda: LLMConfig(model="failing-model"),
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.probe_model_connection",
        fail_probe,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.graph_status",
        lambda *args, **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "success": True,
                "exists": False,
                "detail": "Symphony graph is missing",
            }
        ),
    )
    service = SwarmSymphonyService()

    started = await service.start_refresh_graph()
    task = service._active_build_task
    assert task is not None
    result = await asyncio.wait_for(task, timeout=0.5)
    status = await service.graph_status()

    # 连接失败现在走统一的人性化提示；原始异常仍留在 build_progress.error 供诊断。
    assert started["build_progress"]["status"] == "running"
    assert result["detail"].startswith("主模型连接测试未通过：")
    assert "无法连接到模型服务" in result["detail"]
    assert "[181001]" not in result["detail"]
    assert status["build_progress"]["status"] == "error"
    assert status["build_error"] == result["detail"]
    assert status["detail"] == result["detail"]


@pytest.mark.asyncio
async def test_graph_keeps_recent_build_failure_when_old_graph_is_readable(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path, evolution=False)
    detail = "Symphony 总谱构建失败: matcher unavailable"
    _BuildProcessLogger(_build_log_path(Path(config.storage.artifact_root) / "graph")).record(
        "update.failed",
        error="matcher unavailable",
        detail=detail,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_execution_disabled_skills",
        lambda **_kwargs: set(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service._web_graph_payload",
        lambda *args, **kwargs: {"success": True, "graph": {"nodes": [], "edges": []}},
    )
    service = SwarmSymphonyService()
    monkeypatch.setattr(service, "_read_graph_artifact", lambda _graph_dir: {})

    graph = await service.graph()

    assert graph["success"] is True
    assert graph["build_progress"]["status"] == "error"
    assert graph["build_error"] == detail
    assert graph["detail"] == detail


@pytest.mark.asyncio
async def test_refresh_preserves_downstream_failure_result(monkeypatch, tmp_path):
    config = _config(tmp_path)

    async def return_failed_build(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace(
            to_dict=lambda: {
                "success": False,
                "graph_dir": str((Path(config.storage.artifact_root) / "graph")),
                "detail": "fingerprint model failed",
            }
        )

    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.runtime.symphony.service.service_build_graph",
        return_failed_build,
    )

    result = await SwarmSymphonyService().refresh_graph()

    assert result["success"] is False
    assert result["detail"] == "fingerprint model failed"
    assert result["build_progress"]["status"] == "error"
    assert result["build_log"][-1]["stage"] == "update.failed"




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




@pytest.mark.asyncio
async def test_close_blocks_new_builds_until_owned_runtime_is_drained(monkeypatch, tmp_path):
    config = symphony_config_from_dict({
        "capability": {"sources": [{"type": "skill", "root": str(tmp_path / "skills")}]},
        "storage": {"artifact_root": str(tmp_path / "runtime")},
        "orchestration": {"enabled": True},
    })
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.load_symphony_config", lambda: config)
    entered, release = asyncio.Event(), asyncio.Event()

    async def close_runtime():
        entered.set()
        await release.wait()

    service = SwarmSymphonyService()
    service._runtime = SimpleNamespace(aclose=close_runtime)
    closing = asyncio.create_task(service.close())
    await asyncio.wait_for(entered.wait(), 2)
    try:
        for operation in (service.start_refresh_graph, service.refresh_graph):
            result = await operation()
            assert result["success"] is False
            assert result["reason"] == "service_closing"
        assert service._active_build_task is None
        assert not _build_log_path(tmp_path / "runtime" / "graph").exists()
    finally:
        release.set()
        await closing


@pytest.mark.asyncio
async def test_close_drains_other_scopes_and_runtime_after_one_scope_fails():
    async def broken_close():
        raise ValueError("bad scope")

    runtime_close, scope_close = AsyncMock(), AsyncMock()
    service = SwarmSymphonyService()
    service._runtime = SimpleNamespace(aclose=runtime_close)
    service._skillpack_candidate_flows[("user-a", "root")] = SimpleNamespace(close=broken_close)
    service._skillpack_candidate_flows[("user-b", "root")] = SimpleNamespace(close=scope_close)
    with pytest.raises(ValueError, match="bad scope"):
        await service.close()
    runtime_close.assert_awaited_once()
    scope_close.assert_awaited_once()
    assert not service._skillpack_candidate_flows


@pytest.mark.asyncio
async def test_native_core_build_progress_never_resets_at_new_lifecycle_events(monkeypatch, tmp_path):
    root = tmp_path / "skills" / "alpha"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: alpha\ndescription: Process documents\n---\nInstructions")
    config = symphony_config_from_dict({
        "capability": {"sources": [{"type": "skill", "root": str(root.parent)}], "fingerprint": {"extraction": {"llm_enabled": False}}},
        "storage": {"artifact_root": str(tmp_path / "runtime")},
        "orchestration": {"enabled": True},
    })
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.build.model_from_config", lambda *_args, **_kwargs: _FakeGraphModel())
    log = _BuildProcessLogger(tmp_path / "job" / "build_log.jsonl")
    log.reset()
    await build_graph(config, LLMConfig(model="test"), build_log=log.record)
    entries = [json.loads(line) for line in log.path.read_text().splitlines()]
    percents = [_build_progress(entries[:i + 1])["percent"] for i in range(len(entries))]
    assert "build_started" in [entry["stage"] for entry in entries]
    assert "build_published" in [entry["stage"] for entry in entries]
    assert percents == sorted(percents)
    log.reset()
    await build_graph(config, LLMConfig(model="test"), build_log=log.record)
    entries = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert entries[-1]["stage"] == "build_reused"
    assert _build_progress(entries)["percent"] == 98


@pytest.mark.asyncio
async def test_cancel_after_native_publication_preserves_success_and_stops_slow_progress(monkeypatch, tmp_path):
    root = tmp_path / "skills" / "alpha"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: alpha\ndescription: Process documents\n---\nInstructions")
    config = symphony_config_from_dict({
        "capability": {"sources": [{"type": "skill", "root": str(root.parent)}], "fingerprint": {"extraction": {"llm_enabled": False}}},
        "storage": {"artifact_root": str(tmp_path / "runtime")},
        "orchestration": {"enabled": True},
    })
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.load_symphony_config", lambda: config)
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.LLMConfig.from_default_model", lambda: LLMConfig(model="test"))
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.service.probe_model_connection", AsyncMock())
    monkeypatch.setattr("jiuwenswarm.runtime.symphony.build.model_from_config", lambda *_args, **_kwargs: _FakeGraphModel())
    published, callback_closed = asyncio.Event(), asyncio.Event()

    async def slow_progress(event):
        if event.get("event") == "update.done":
            published.set()
            try:
                await asyncio.Event().wait()
            finally:
                callback_closed.set()

    service = SwarmSymphonyService()
    task = asyncio.create_task(service.refresh_graph(progress=slow_progress))
    await asyncio.wait_for(published.wait(), 5)
    cancelled = await asyncio.wait_for(service.cancel_build(), 5)
    result = await asyncio.wait_for(task, 5)
    assert callback_closed.is_set()
    assert cancelled["cancelled"] is False
    assert cancelled["build_status"] == "completed"
    assert cancelled["build_progress"]["status"] == "success"
    assert result["success"] is True
    assert graph_exists(tmp_path / "runtime" / "graph")
    assert not service._active_build_task
    await service.close()
