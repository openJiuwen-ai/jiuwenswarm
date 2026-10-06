from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.designer.handlers import common
from jiuwenswarm.server.runtime.designer import model_tools
from jiuwenswarm.server.runtime.gateway_adapter import designer_adapter
from jiuwenswarm.server.runtime.session.project_store import Project


def _workspace_payload(project_dir: str) -> dict:
    return {
        "project": {
            "project_id": "proj_design",
            "project_dir": project_dir,
            "name": "Design",
            "work_mode": "design",
        },
        "session": {
            "session_id": "design_session",
            "project_id": "proj_design",
            "project_dir": project_dir,
            "title": "Design",
            "work_mode": "design",
        },
        "graph": {
            "graph_id": "graph_design",
            "project_id": "proj_design",
            "nodes": [],
            "edges": [],
        },
        "messages": [],
    }


def test_graph_workspace_uses_authoritative_project_directory(tmp_path, monkeypatch):
    root = tmp_path / "agent"
    project_dir = root / "workspace" / "design" / "managed-project"
    project_dir.mkdir(parents=True)
    project = Project(
        project_id="proj_design",
        name="Design",
        project_dir=str(project_dir),
        work_mode="design",
    )
    monkeypatch.setattr(common, "get_agent_root_dir", lambda: root)
    monkeypatch.setattr(
        common.project_store,
        "get_project_by_id",
        lambda project_id, cache_bust=False: project if project_id == project.project_id else None,
    )

    directory = common.graph_workspace_dir(
        {
            "project_id": project.project_id,
            "metadata": {"project_dir": str(tmp_path / "attacker-controlled")},
        }
    )

    assert directory == project_dir / "assets"
    assert directory.is_dir()


def test_graph_workspace_rejects_design_project_outside_managed_root(tmp_path, monkeypatch):
    root = tmp_path / "agent"
    project = Project(
        project_id="proj_design",
        name="Design",
        project_dir=str(tmp_path / "outside"),
        work_mode="design",
    )
    monkeypatch.setattr(common, "get_agent_root_dir", lambda: root)
    monkeypatch.setattr(
        common.project_store,
        "get_project_by_id",
        lambda project_id, cache_bust=False: project,
    )

    with pytest.raises(ValueError, match="outside the managed root"):
        common.graph_workspace_dir({"project_id": project.project_id})


@pytest.mark.asyncio
async def test_workspace_create_token_is_serialized_and_reused(tmp_path, monkeypatch):
    payload = _workspace_payload(str(tmp_path / "workspace"))
    create_count = 0

    async def create_once(request, params):
        nonlocal create_count
        create_count += 1
        await asyncio.sleep(0.05)
        return payload, None, None

    monkeypatch.setattr(designer_adapter, "get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setattr(designer_adapter, "_create_design_workspace_once", create_once)
    monkeypatch.setattr(
        designer_adapter,
        "_get_design_workspace",
        lambda params: (payload, None, None),
    )
    request = AgentRequest(request_id="request", channel_id="web")
    params = {
        "prompt": "Build a storyboard",
        "create_token": "stable-token",
        "model_name": "selected-model",
    }

    first, second = await asyncio.gather(
        designer_adapter._create_design_workspace(request, params),
        designer_adapter._create_design_workspace(request, params),
    )

    assert create_count == 1
    assert first == second == (payload, None, None)


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_committed_workspace(tmp_path, monkeypatch):
    payload = _workspace_payload(str(tmp_path / "workspace"))
    rollbacks: list[dict] = []

    async def create_once(request, params):
        return payload, None, None

    def fail_receipt(token, signature, workspace):
        raise OSError("disk full")

    monkeypatch.setattr(designer_adapter, "get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setattr(designer_adapter, "_create_design_workspace_once", create_once)
    monkeypatch.setattr(designer_adapter, "_write_workspace_receipt", fail_receipt)
    monkeypatch.setattr(
        designer_adapter,
        "_rollback_design_workspace",
        lambda **kwargs: rollbacks.append(kwargs),
    )
    request = AgentRequest(request_id="request", channel_id="web")

    result = await designer_adapter._create_design_workspace(
        request,
        {"prompt": "Build a storyboard", "create_token": "receipt-failure"},
    )

    assert result[0] is None
    assert result[2] == "INTERNAL_ERROR"
    assert "disk full" in str(result[1])
    assert rollbacks == [
        {
            "project_id": "proj_design",
            "project_dir": str(tmp_path / "workspace"),
            "session_id": "design_session",
            "graph_id": "graph_design",
        }
    ]


@pytest.mark.asyncio
async def test_selected_model_context_reaches_designer_model_calls(monkeypatch):
    for key in ("API_KEY", "OPENAI_API_KEY", "API_BASE", "OPENAI_API_BASE"):
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(
        model_tools,
        "list_configured_models",
        lambda: [
            {
                "id": "selected-model",
                "model_name": "selected-model",
                "api_base": "",
                "index": 0,
            },
            {
                "id": "default-model",
                "model_name": "default-model",
                "api_base": "",
                "index": 1,
            },
        ],
    )
    monkeypatch.setattr(model_tools, "get_config", lambda: {})

    with model_tools.use_preferred_designer_model("selected-model"):
        result = await model_tools.call_model_tool(
            prompt="prompt",
            system="system",
            optimize_for="quality",
        )

    assert result["model"] == "selected-model"
    assert result["model_name"] == "selected-model"


@pytest.mark.asyncio
async def test_chat_auto_run_failure_is_returned_in_summary(monkeypatch):
    graph = {
        "graph_id": "graph_design",
        "project_id": "proj_design",
        "metadata": {},
        "nodes": [],
        "edges": [],
    }

    async def fake_leader_chat(*args, **kwargs):
        return {
            "graph": graph,
            "changed": False,
            "summary": "Updated workflow",
            "run_node_ids": ["node_1"],
        }

    from jiuwenswarm.server.runtime.designer import leader_chat as leader_chat_module

    monkeypatch.setattr(leader_chat_module, "run_leader_chat", fake_leader_chat)
    monkeypatch.setattr(designer_adapter._store, "get_graph", lambda graph_id: graph)
    monkeypatch.setattr(
        designer_adapter._executor,
        "reconcile_loaded_graph",
        lambda loaded: loaded,
    )
    monkeypatch.setattr(
        designer_adapter,
        "_start_run",
        lambda params: (None, "unable to start", "INTERNAL_ERROR"),
    )
    request = AgentRequest(request_id="request", channel_id="web")

    payload, error, code = await designer_adapter._chat_graph(
        request,
        {"graph_id": graph["graph_id"], "message": "Update it"},
    )

    assert error is None
    assert code is None
    assert payload is not None
    assert payload["summary"] == "Updated workflow (unable to start)"


def test_bootstrap_rejects_project_dir_outside_the_design_root(tmp_path, monkeypatch):
    managed = tmp_path / "agent"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setattr(designer_adapter, "get_agent_root_dir", lambda: managed)
    design_root = managed / "workspace" / "design"
    design_root.mkdir(parents=True)
    good = design_root / "my-film"
    good.mkdir()

    assert designer_adapter._is_managed_design_project_dir(str(good)) is True
    assert designer_adapter._is_managed_design_project_dir(str(design_root)) is False
    assert designer_adapter._is_managed_design_project_dir(str(outside)) is False
    assert designer_adapter._is_managed_design_project_dir("/tmp") is False

    created: list[str] = []
    monkeypatch.setattr(
        designer_adapter.project_store,
        "create_project_checked",
        lambda name, project_dir, work_mode: created.append(project_dir)
        or (SimpleNamespace(project_id="p1", project_dir=project_dir, work_mode=work_mode), False),
    )
    payload, error, code = designer_adapter._bootstrap_graph(
        {"prompt": "a film", "project_dir": str(outside), "work_mode": "design"},
        "web",
        {"source": "llm"},
    )

    assert payload is None
    assert code == "BAD_REQUEST"
    assert "managed workspace" in str(error)
    assert created == []
