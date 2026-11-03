from __future__ import annotations

import asyncio
from pathlib import Path

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


def test_pair_design_canvases_keeps_chat_canvases_separate():
    sessions = [
        {"session_id": "s1", "created_at": 1, "title": "One"},
        {"session_id": "s2", "created_at": 2, "title": "Two"},
    ]
    graphs = [
        {"graph_id": "g1", "updated_at": 10, "metadata": {"session_id": "s1"}},
        {"graph_id": "g1-old", "updated_at": 5, "metadata": {"session_id": "s1"}},
        {"graph_id": "g2", "updated_at": 30, "metadata": {"session_id": "s2"}},
    ]

    pairs = designer_adapter._pair_design_canvases(sessions, graphs)

    assert [
        (item["session"]["session_id"], item["graph"]["graph_id"]) for item in pairs
    ] == [("s1", "g1"), ("s2", "g2")]


def test_pair_design_canvases_adopts_a_legacy_graph():
    pairs = designer_adapter._pair_design_canvases(
        [{"session_id": "s1", "created_at": 1, "title": "Legacy"}],
        [{"graph_id": "g-legacy", "created_at": 4, "updated_at": 4, "metadata": {}}],
    )

    assert pairs[0]["graph"]["graph_id"] == "g-legacy"


def test_get_design_workspace_returns_only_the_requested_session(monkeypatch):
    project = Project(
        project_id="proj_design",
        name="Design",
        project_dir="D:/design",
        work_mode="design",
    )
    sessions = [
        {
            "session_id": "s1",
            "project_id": "proj_design",
            "work_mode": "design",
            "created_at": 1,
            "title": "One",
        },
        {
            "session_id": "s2",
            "project_id": "proj_design",
            "work_mode": "design",
            "created_at": 2,
            "title": "Two",
        },
    ]
    graphs = [
        {
            "graph_id": "g1",
            "project_id": "proj_design",
            "title": "One",
            "updated_at": 10,
            "nodes": [],
            "metadata": {"session_id": "s1"},
        },
        {
            "graph_id": "g2",
            "project_id": "proj_design",
            "title": "Two",
            "updated_at": 20,
            "nodes": [],
            "metadata": {"session_id": "s2"},
        },
    ]
    monkeypatch.setattr(
        designer_adapter.project_store,
        "get_project_by_id",
        lambda project_id, cache_bust=False: project if project_id == project.project_id else None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.collect_all_sessions_metadata",
        lambda: sessions,
    )
    monkeypatch.setattr(designer_adapter._store, "list_graphs_for_project", lambda project_id: graphs)
    monkeypatch.setattr(designer_adapter._store, "get_latest_run_for_graph", lambda graph_id: None)
    monkeypatch.setattr(
        designer_adapter,
        "_design_workspace_messages",
        lambda session_id: [{"id": session_id, "content": session_id}],
    )

    payload, error, code = designer_adapter._get_design_workspace(
        {"project_id": "proj_design", "session_id": "s2"}
    )

    assert error is None
    assert code is None
    assert payload is not None
    assert payload["session"]["session_id"] == "s2"
    assert payload["graph"]["graph_id"] == "g2"
    assert payload["messages"] == [{"id": "s2", "content": "s2"}]
    assert [item["session_id"] for item in payload["sessions"]] == ["s1", "s2"]


def test_get_design_workspace_retitles_a_canvas_still_named_after_its_prompt(monkeypatch):
    prompt = "制作一个 5 秒的写实风格短片，16:9 画幅，只有一个镜头：女店员推开咖啡店的门"
    project = Project(project_id="proj_design", name="Design", project_dir="D:/design", work_mode="design")
    session = {
        "session_id": "s1",
        "project_id": "proj_design",
        "work_mode": "design",
        "created_at": 1,
        "title": "Morning Shift Start",
    }
    graph = {
        "graph_id": "g1",
        "project_id": "proj_design",
        "title": prompt[:80],
        "description": prompt,
        "updated_at": 10,
        "nodes": [],
        "metadata": {
            "session_id": "s1",
            "script_analysis": {"story_name": "Morning Shift Start"},
        },
    }
    saved: list[dict] = []

    def save_graph(item: dict) -> dict:
        saved.append(item)
        return item

    monkeypatch.setattr(
        designer_adapter.project_store,
        "get_project_by_id",
        lambda project_id, cache_bust=False: project if project_id == project.project_id else None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.collect_all_sessions_metadata",
        lambda: [session],
    )
    monkeypatch.setattr(designer_adapter._store, "list_graphs_for_project", lambda project_id: [graph])
    monkeypatch.setattr(designer_adapter._store, "get_latest_run_for_graph", lambda graph_id: None)
    monkeypatch.setattr(designer_adapter._store, "save_graph", save_graph)
    monkeypatch.setattr(designer_adapter, "_design_workspace_messages", lambda session_id: [])

    payload, error, _ = designer_adapter._get_design_workspace(
        {"project_id": "proj_design", "session_id": "s1"}
    )

    assert error is None
    assert payload["graph"]["title"] == "Morning Shift Start"
    assert [item["title"] for item in saved] == ["Morning Shift Start"]

    graph["title"] = "我的咖啡短片"
    payload, error, _ = designer_adapter._get_design_workspace(
        {"project_id": "proj_design", "session_id": "s1"}
    )
    assert error is None
    assert payload["graph"]["title"] == "我的咖啡短片"
    assert len(saved) == 1


def test_design_workspace_messages_keep_leader_finals(monkeypatch):
    records = [
        {"id": "r1:user", "role": "user", "content": "make a clip", "timestamp": 1.0},
        {"id": "r1:assistant", "role": "assistant", "content": "on it", "timestamp": 2.0},
        {"id": "r1:assistant", "role": "leader", "content": "", "timestamp": 2.5},
        {"id": "r1:assistant", "role": "leader", "content": "graph ready", "timestamp": 3.0},
        {"id": "r1:assistant", "role": "teammate", "content": "member note", "timestamp": 3.5},
        {"id": "r1:assistant", "role": "leader", "content": "run failed", "timestamp": 4.0},
    ]
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.load_history_records",
        lambda session_id: records,
    )

    messages = designer_adapter._design_workspace_messages("design_session")

    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "make a clip"),
        ("assistant", "on it"),
        ("assistant", "graph ready"),
        ("assistant", "run failed"),
    ]
    assert len({m["id"] for m in messages}) == len(messages)


def test_default_project_can_host_a_design_canvas(monkeypatch):
    sessions = [
        {
            "session_id": "design_default",
            "project_id": "default",
            "work_mode": "design",
            "created_at": 1,
            "title": "画布 1",
        }
    ]
    graphs = [
        {
            "graph_id": "g-default",
            "project_id": "default",
            "title": "画布 1",
            "updated_at": 10,
            "nodes": [],
            "metadata": {"session_id": "design_default"},
        }
    ]
    monkeypatch.setattr(
        designer_adapter.project_store,
        "get_project_by_id",
        lambda project_id, cache_bust=False: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.collect_all_sessions_metadata",
        lambda: sessions,
    )
    monkeypatch.setattr(
        designer_adapter._store,
        "list_graphs_for_project",
        lambda project_id: graphs if project_id == "default" else [],
    )
    monkeypatch.setattr(designer_adapter._store, "get_latest_run_for_graph", lambda graph_id: None)
    monkeypatch.setattr(designer_adapter, "_design_workspace_messages", lambda session_id: [])

    payload, error, code = designer_adapter._get_design_workspace(
        {"project_id": "default", "session_id": "design_default"}
    )

    assert error is None
    assert code is None
    assert payload is not None
    assert payload["project"]["project_id"] == "default"
    assert payload["session"]["session_id"] == "design_default"
    assert payload["graph"]["graph_id"] == "g-default"


def test_default_session_rehomes_a_graph_saved_under_another_project(monkeypatch):
    sessions = [
        {
            "session_id": "design_default",
            "project_id": "default",
            "work_mode": "design",
            "created_at": 1,
            "title": "晨光咖啡",
        }
    ]
    foreign = {
        "graph_id": "g-foreign",
        "project_id": "proj_stray",
        "title": "晨光咖啡",
        "updated_at": 10,
        "nodes": [{"id": "n1"}],
        "metadata": {"session_id": "design_default"},
    }
    saved: list[dict] = []
    hidden: list[str] = []
    stray = Project(
        project_id="proj_stray",
        name="stray",
        project_dir="",
        work_mode="design",
    )

    monkeypatch.setattr(
        designer_adapter.project_store,
        "get_project_by_id",
        lambda project_id, cache_bust=False: stray if project_id == "proj_stray" else None,
    )
    monkeypatch.setattr(
        designer_adapter.project_store,
        "hide_project",
        lambda project_id: hidden.append(project_id),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.collect_all_sessions_metadata",
        lambda: sessions,
    )
    monkeypatch.setattr(
        designer_adapter._store,
        "list_graphs_for_project",
        lambda project_id: [],
    )
    monkeypatch.setattr(designer_adapter._store, "list_graphs", lambda: [foreign])
    monkeypatch.setattr(
        designer_adapter._store,
        "save_graph",
        lambda graph: saved.append(dict(graph)) or dict(graph),
    )
    monkeypatch.setattr(designer_adapter._store, "get_latest_run_for_graph", lambda graph_id: None)
    monkeypatch.setattr(designer_adapter, "_design_workspace_messages", lambda session_id: [])

    payload, error, code = designer_adapter._get_design_workspace(
        {"project_id": "default", "session_id": "design_default"}
    )

    assert error is None
    assert code is None
    assert payload is not None
    assert payload["graph"]["graph_id"] == "g-foreign"
    assert payload["graph"]["project_id"] == "default"
    assert saved and saved[0]["project_id"] == "default"
    assert hidden == ["proj_stray"]


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

    assert directory == project_dir.resolve()
    assert directory.is_dir()


def test_graph_workspace_uses_a_user_chosen_project_directory(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    project = Project(
        project_id="proj_design",
        name="Design",
        project_dir=str(outside),
        work_mode="design",
    )
    monkeypatch.setattr(
        common.project_store,
        "get_project_by_id",
        lambda project_id, cache_bust=False: project,
    )

    directory = common.graph_workspace_dir({"project_id": project.project_id})

    assert directory == outside.resolve()
    assert directory.is_dir()


@pytest.mark.asyncio
async def test_generated_image_is_saved_in_the_project_directory(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.designer import media_generation
    from jiuwenswarm.server.runtime.designer.handlers import image_nodes
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    project_dir = tmp_path / "film"
    project_dir.mkdir()
    project = Project(
        project_id="proj_design",
        name="Design",
        project_dir=str(project_dir),
        work_mode="design",
    )
    monkeypatch.setattr(
        common.project_store,
        "get_project_by_id",
        lambda project_id, cache_bust=False: project if project_id == project.project_id else None,
    )
    seen: dict[str, str | None] = {}

    async def fake_generate(prompt, *, size=None, reference_images=None, save_dir=None):
        seen["save_dir"] = save_dir
        dest = Path(save_dir) / "image_test.jpg"
        dest.write_bytes(b"jpg")
        return {"image_path": str(dest)}

    monkeypatch.setattr(media_generation, "generation_enabled", lambda _kind: True)
    monkeypatch.setattr(media_generation, "generate_image", fake_generate)
    graph = {
        "project_id": project.project_id,
        "metadata": {"session_id": "design_1"},
        "nodes": [],
        "edges": [],
    }
    result = await image_nodes._require_image(
        prompt="a still",
        stem="designer_image_run_n",
        ctx=NodeExecutionContext(graph=graph, run_id="run", node_id="n_image", run={}),
    )

    assert Path(seen["save_dir"]) == project_dir.resolve()
    assert (project_dir / "image_test.jpg").is_file()
    assert result.output_ref["label"] == "image_test.jpg"


@pytest.mark.asyncio
async def test_generated_image_without_a_project_uses_the_session_workspace(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.designer import media_generation
    from jiuwenswarm.server.runtime.designer.handlers import image_nodes
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    session_dir = tmp_path / "design_abc"
    monkeypatch.setattr(
        common,
        "get_default_project_session_workspace_dir",
        lambda session_id: session_dir,
    )
    seen: dict[str, str | None] = {}

    async def fake_generate(prompt, *, size=None, reference_images=None, save_dir=None):
        seen["save_dir"] = save_dir
        dest = Path(save_dir)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / "image_test.jpg"
        path.write_bytes(b"jpg")
        return {"image_path": str(path)}

    monkeypatch.setattr(media_generation, "generation_enabled", lambda _kind: True)
    monkeypatch.setattr(media_generation, "generate_image", fake_generate)
    graph = {
        "project_id": "default",
        "metadata": {"session_id": "design_abc"},
        "nodes": [],
        "edges": [],
    }
    await image_nodes._require_image(
        prompt="a still",
        stem="designer_image_run_n",
        ctx=NodeExecutionContext(graph=graph, run_id="run", node_id="n_image", run={}),
    )

    assert Path(seen["save_dir"]) == session_dir
    assert (session_dir / "image_test.jpg").is_file()


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
        )

    assert result["model"] == "selected-model"
    assert result["model_name"] == "selected-model"


def test_canvas_title_uses_model_phrase_not_the_prompt():
    from jiuwenswarm.common.schema.designer_node_labels import usable_story_title

    prompt = "帮我做一支雨夜里侦探追凶的短片，要有三个镜头和结尾反转"
    assert usable_story_title("雨夜追凶", prompt) == "雨夜追凶"
    assert usable_story_title(prompt, prompt) == ""
    assert usable_story_title(prompt[:40], prompt) == ""

    graph = {
        "title": prompt[:80],
        "metadata": {"script_analysis": {"story_name": "雨夜追凶"}},
    }
    assert designer_adapter._stamp_canvas_title(graph, prompt) == "雨夜追凶"
    assert graph["title"] == "雨夜追凶"

    echoed = {"title": prompt[:80], "metadata": {"script_analysis": {"story_name": prompt}}}
    assert designer_adapter._stamp_canvas_title(echoed, prompt) == ""
    assert echoed["title"] == "设计项目"
    assert designer_adapter._replacement_display_title(
        "AT10 第3镜首次生成",
        prompt,
        "雨夜追凶",
    ) == ""
    assert designer_adapter._replacement_display_title(prompt, prompt, "雨夜追凶") == "雨夜追凶"
    assert designer_adapter._replacement_display_title("A" * 50, "", "雨夜追凶") == "雨夜追凶"
    assert designer_adapter._replacement_display_title("AT10", "", "雨夜追凶") == ""
