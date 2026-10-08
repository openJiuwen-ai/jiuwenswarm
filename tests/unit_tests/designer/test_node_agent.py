# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Node Agent host, template loading, and agent-owned graph scheduler."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_STATUS_COMPLETED,
    NODE_STATUS_PENDING,
    NODE_TYPE_TEXT,
    ROLE_DEFAULT_TEMPLATES,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_COMPLETED,
    DesignerGraphNode,
    node_agent_template,
    node_delegate,
    normalize_node,
)
from tests.unit_tests.designer.graph_fixtures import make_pipeline_graph
from jiuwenswarm.server.runtime.designer.executor import GraphExecutor
from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
from jiuwenswarm.server.runtime.designer.handlers.types import NodeResult
from jiuwenswarm.server.runtime.designer.node_agent import (
    DesignerGraphToolkit,
    parse_agent_template_ref,
)


@pytest.fixture()
def designer_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_director_llm: None
) -> DesignerGraphStore:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.graph_store.get_agent_root_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.get_agent_workspace_dir",
        lambda: tmp_path,
    )

    async def fake_image(prompt: str, **kwargs) -> dict[str, str]:
        path = tmp_path / "agent_stub.png"
        path.write_bytes(b"png")
        return {"image_path": str(path)}

    async def fake_video(prompt: str, **kwargs) -> dict[str, str]:
        path = tmp_path / "agent_stub.mp4"
        path.write_bytes(b"mp4" * 200)
        return {"video_path": str(path), "revised_prompt": prompt}

    def fake_concat(paths: list[Path], dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"mp4-merged" * 80)
        return dest.resolve()

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.generate_designer_image",
        fake_image,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_video,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.compose.concatenate_clip_videos",
        fake_concat,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.require_llm",
        lambda: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.llm_available",
        lambda: True,
    )
    return DesignerGraphStore()


async def _complete_with_dummy_media(
    tmp_path: Path,
    node: DesignerGraphNode,
    toolkit: DesignerGraphToolkit,
) -> NodeResult:
    node_type = str(node.get("type") or "text")
    if node_type in {"image", "video", "audio"}:
        suffix = { "image": ".png", "video": ".mp4", "audio": ".m4a" }[node_type]
        mime = {
            "image": "image/png",
            "video": "video/mp4",
            "audio": "audio/mp4",
        }[node_type]
        path = tmp_path / f"{node['id']}{suffix}"
        path.write_bytes(b"x" * 16)
        await toolkit.node_complete(
            uri=path.resolve().as_uri(),
            kind=node_type,
            mime_type=mime,
        )
    else:
        await toolkit.node_complete(text=f"done {node['id']}")
    assert toolkit.completed is not None
    return toolkit.completed


async def _await_executor_task(executor: GraphExecutor, run_id: str) -> None:
    task = executor._tasks.get(run_id)
    if task is not None:
        await task


def test_parse_agent_template_ref() -> None:
    assert parse_agent_template_ref("designer/leader") == ("designer", "leader")
    assert parse_agent_template_ref("my_template") == (None, "my_template")


def test_node_agent_template_uses_role_default() -> None:
    node = normalize_node(
        {
            "id": "n_brief",
            "type": NODE_TYPE_TEXT,
            "label": "brief",
            "config": {"role": NODE_ROLE_BRIEF},
        }
    )
    assert node_delegate(node) == "agent"
    assert node_agent_template(node) == ROLE_DEFAULT_TEMPLATES[NODE_ROLE_BRIEF]
    explicit = normalize_node(
        {
            "id": "n_brief",
            "type": NODE_TYPE_TEXT,
            "label": "brief",
            "config": {
                "role": NODE_ROLE_BRIEF,
                "agent_template": "custom/leader",
            },
        }
    )
    assert node_agent_template(explicit) == "custom/leader"



@pytest.mark.asyncio
async def test_agent_scheduler_starts_only_ready_root(
    designer_store: DesignerGraphStore,
    tmp_path: Path,
) -> None:
    started: list[str] = []

    async def runner(node, ctx, toolkit: DesignerGraphToolkit) -> NodeResult:
        if not started:
            assert node["id"] == "n_brief"
        started.append(node["id"])
        return await _complete_with_dummy_media(tmp_path, node, toolkit)

    graph = designer_store.save_graph(
        make_pipeline_graph(project_id="proj_agent_root", prompt="only root"),
    )
    executor = GraphExecutor(designer_store, runner=runner)
    run = executor.create_run(graph)
    await executor.start_run(run["run_id"])
    await _await_executor_task(executor, run["run_id"])
    finished = designer_store.get_run(run["run_id"])
    assert finished is not None
    assert finished["status"] == RUN_STATUS_COMPLETED
    assert started[0] == "n_brief"
    assert finished["node_states"]["n_brief"]["status"] == NODE_STATUS_COMPLETED
    assert "n_character" in started


@pytest.mark.asyncio
async def test_agent_node_run_starts_companion(
    designer_store: DesignerGraphStore,
    tmp_path: Path,
) -> None:
    started: list[str] = []

    async def runner(node, ctx, toolkit: DesignerGraphToolkit) -> NodeResult:
        started.append(node["id"])
        if node["id"] == "n_brief":
            await toolkit.node_run("n_character")
        return await _complete_with_dummy_media(tmp_path, node, toolkit)

    graph = designer_store.save_graph(
        make_pipeline_graph(project_id="proj_agent_run", prompt="pull companion"),
    )
    executor = GraphExecutor(designer_store, runner=runner)
    run = executor.create_run(graph)
    await executor.start_run(run["run_id"])
    await _await_executor_task(executor, run["run_id"])
    finished = designer_store.get_run(run["run_id"])
    assert finished is not None
    assert finished["status"] == RUN_STATUS_COMPLETED
    assert started[:2] == ["n_brief", "n_character"]
    assert finished["node_states"]["n_character"]["status"] == NODE_STATUS_COMPLETED
    assert finished["node_states"]["n_storyboard"]["status"] == NODE_STATUS_COMPLETED


@pytest.mark.asyncio
async def test_agent_patch_cannot_add_nodes_to_frozen_topology(
    designer_store: DesignerGraphStore,
    tmp_path: Path,
) -> None:
    started: list[str] = []

    async def runner(node, ctx, toolkit: DesignerGraphToolkit) -> NodeResult:
        started.append(node["id"])
        if node["id"] == "n_brief":
            toolkit.graph_patch(
                {
                    "upsert_nodes": [
                        {
                            "id": "n_extra",
                            "type": NODE_TYPE_TEXT,
                            "label": "备注",
                            "config": {"role": NODE_ROLE_BRIEF, "prompt": "extra"},
                        }
                    ]
                }
            )
            spawned.append(await toolkit.node_run("n_extra"))
        return await _complete_with_dummy_media(tmp_path, node, toolkit)

    spawned: list[str] = []
    graph = designer_store.save_graph(
        make_pipeline_graph(project_id="proj_agent_patch", prompt="patch then run"),
    )
    executor = GraphExecutor(designer_store, runner=runner)
    run = executor.create_run(graph)
    await executor.start_run(run["run_id"])
    await _await_executor_task(executor, run["run_id"])
    finished = designer_store.get_run(run["run_id"])
    assert finished is not None
    assert finished["status"] == RUN_STATUS_COMPLETED
    assert "n_extra" not in started
    assert spawned == ["node not found: n_extra"]
    patched = designer_store.get_graph(graph["graph_id"])
    assert patched is not None
    assert (patched.get("metadata") or {}).get("freeze_shot_topology") is True
    assert not any(node["id"] == "n_extra" for node in patched["nodes"])


@pytest.mark.asyncio
async def test_cancel_stops_node_agent_host(
    designer_store: DesignerGraphStore,
) -> None:
    started = asyncio.Event()

    async def runner(node, ctx, toolkit: DesignerGraphToolkit) -> NodeResult:
        started.set()
        await asyncio.sleep(30)
        toolkit.node_complete(text="late")
        assert toolkit.completed is not None
        return toolkit.completed

    graph = designer_store.save_graph(
        make_pipeline_graph(project_id="proj_agent_cancel", prompt="cancel me"),
    )
    executor = GraphExecutor(designer_store, runner=runner)
    run = executor.create_run(graph)
    await executor.start_run(run["run_id"])
    await asyncio.wait_for(started.wait(), timeout=2)
    cancelled = executor.cancel_run(run["run_id"])
    assert cancelled["status"] == RUN_STATUS_CANCELLED
    assert executor._host._agents == {}
    task = executor._tasks.get(run["run_id"])
    if task is not None:
        with pytest.raises(asyncio.CancelledError):
            await task
        return
    workers = executor._node_workers.get(run["run_id"]) or {}
    assert not workers


@pytest.mark.asyncio
async def test_completed_output_survives_agent_timeout(
    designer_store: DesignerGraphStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.executor._node_execute_timeout_sec",
        lambda node: 0.35 if node.get("id") == "n_brief" else 20.0,
    )

    async def runner(node, ctx, toolkit: DesignerGraphToolkit) -> NodeResult:
        await toolkit.node_complete(text=f"done {node['id']}")
        if node["id"] == "n_brief":
            await asyncio.sleep(5)
        assert toolkit.completed is not None
        return toolkit.completed

    graph = designer_store.save_graph(
        make_pipeline_graph(project_id="proj_timeout_keep", prompt="keep completed brief"),
    )
    executor = GraphExecutor(designer_store, runner=runner)
    run = executor.create_run(graph)
    await executor.start_run(run["run_id"])
    await _await_executor_task(executor, run["run_id"])
    finished = designer_store.get_run(run["run_id"])
    assert finished is not None
    assert finished["node_states"]["n_brief"]["status"] == NODE_STATUS_COMPLETED
    assert finished["node_states"]["n_brief"].get("output_ref")


@pytest.mark.asyncio
async def test_node_run_after_complete_does_not_spawn(
    designer_store: DesignerGraphStore,
    tmp_path: Path,
) -> None:
    spawned_from_brief: list[str] = []

    async def runner(node, ctx, toolkit: DesignerGraphToolkit) -> NodeResult:
        if node["id"] == "n_brief":
            await toolkit.node_complete(text="done brief")
            msg = await toolkit.node_run("n_character")
            spawned_from_brief.append(msg)
            assert toolkit.completed is not None
            return toolkit.completed
        return await _complete_with_dummy_media(tmp_path, node, toolkit)

    graph = designer_store.save_graph(
        make_pipeline_graph(project_id="proj_no_spawn", prompt="do not spawn after complete"),
    )
    executor = GraphExecutor(designer_store, runner=runner)
    run = executor.create_run(graph)
    await executor.start_run(run["run_id"])
    await _await_executor_task(executor, run["run_id"])
    assert spawned_from_brief
    assert "already completed" in spawned_from_brief[0]
    finished = designer_store.get_run(run["run_id"])
    assert finished is not None
    assert finished["node_states"]["n_character"]["status"] == NODE_STATUS_COMPLETED


class _DummySpawner:
    async def spawn_node_agent(self, run_id: str, node_id: str) -> str:
        return "ok"

    def apply_agent_graph_patch(self, graph_id: str, patch: dict) -> dict:
        return {}

    def load_graph_snapshot(self, graph_id: str, run_id: str) -> dict:
        return {}


def test_clip_video_tool_timeout_overrides_ability_manager_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_GEN_PROTOCOL", "minimax")
    from openjiuwen.core.single_agent.ability_manager import (
        AbilityManager,
        DEFAULT_TOOL_CALL_TIMEOUT,
    )

    from jiuwenswarm.server.runtime.designer.executor import _node_execute_timeout_sec
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
    from jiuwenswarm.server.runtime.designer.node_agent import build_designer_tools

    node = {
        "id": "n_clip_1",
        "type": "video",
        "config": {"pipeline": "clip", "tools": ["call_video_model"]},
    }
    ctx = NodeExecutionContext(
        graph={"graph_id": "g1", "nodes": [node], "edges": []},
        run_id="r1",
        node_id="n_clip_1",
    )
    tools = build_designer_tools(DesignerGraphToolkit(_DummySpawner(), ctx))
    video = next(tool for tool in tools if tool.card.name == "call_video_model")
    timeout = AbilityManager._resolve_call_timeout(video.card)
    assert timeout == 1500.0
    assert timeout > DEFAULT_TOOL_CALL_TIMEOUT
    assert _node_execute_timeout_sec(node) >= timeout

    mgr = AbilityManager(owner_id="designer:r1:n_clip_1")
    mgr.add_ability(video.card, video)
    registered = mgr._tools["call_video_model"]
    assert AbilityManager._resolve_call_timeout(registered) == 1500.0


def test_vllm_omni_clip_waits_as_long_as_the_video_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    from openjiuwen.core.single_agent.ability_manager import AbilityManager

    from jiuwenswarm.server.runtime.designer.executor import _node_execute_timeout_sec
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
    from jiuwenswarm.server.runtime.designer.node_agent import build_designer_tools

    monkeypatch.setenv("VIDEO_GEN_PROTOCOL", "vllm-omni")
    monkeypatch.setenv("VIDEO_GEN_API_BASE", "http://127.0.0.1:8091/v1")
    node = {
        "id": "n_clip_1",
        "type": "video",
        "config": {"pipeline": "clip", "tools": ["call_video_model"]},
    }
    compose = {
        "id": "n_compose",
        "type": "video",
        "config": {"pipeline": "compose"},
    }
    ctx = NodeExecutionContext(
        graph={"graph_id": "g1", "nodes": [node, compose], "edges": []},
        run_id="r1",
        node_id="n_clip_1",
    )
    tools = build_designer_tools(DesignerGraphToolkit(_DummySpawner(), ctx))
    video = next(tool for tool in tools if tool.card.name == "call_video_model")
    assert AbilityManager._resolve_call_timeout(video.card) == 7200.0
    assert _node_execute_timeout_sec(node) == 7200.0
    assert _node_execute_timeout_sec(compose) == 1800.0


@pytest.mark.asyncio
async def test_call_image_model_uses_designer_helper_not_localfunction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common.tools.image_tools import generate_image
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
    from jiuwenswarm.server.runtime.designer.node_agent import DesignerGraphToolkit
    from openjiuwen.core.foundation.tool import LocalFunction

    # @tool wraps generate_image as LocalFunction — awaiting it raises TypeError.
    assert isinstance(generate_image, LocalFunction)

    saved = tmp_path / "sheet.png"
    saved.write_bytes(b"png")
    calls: list[str] = []

    async def fake_image(prompt: str, **kwargs) -> dict[str, str]:
        calls.append(prompt)
        return {"image_path": str(saved)}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.generate_designer_image",
        fake_image,
    )
    node = {
        "id": "n_character_1",
        "type": "image",
        "label": "character",
        "config": {"pipeline": "character_design", "prompt": "a hero"},
    }
    ctx = NodeExecutionContext(
        graph={"nodes": [node]},
        run_id="run_img",
        node_id="n_character_1",
    )
    toolkit = DesignerGraphToolkit(_DummySpawner(), ctx)
    out = await toolkit.call_image_model(prompt="a hero")
    assert out.startswith("image_ready")
    assert calls == ["a hero"]
    assert toolkit.completed is not None


@pytest.mark.asyncio
async def test_call_image_model_attaches_scene_character_sheets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
    from jiuwenswarm.server.runtime.designer.node_agent import DesignerGraphToolkit

    young = tmp_path / "young.png"
    partner = tmp_path / "partner.png"
    young.write_bytes(b"png1")
    partner.write_bytes(b"png2")
    out = tmp_path / "frame.png"
    out.write_bytes(b"png3")
    seen: dict[str, object] = {}

    async def fake_image(prompt: str, **kwargs) -> dict[str, str]:
        seen["prompt"] = prompt
        seen["reference_images"] = kwargs.get("reference_images")
        return {"image_path": str(out)}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.generate_designer_image",
        fake_image,
    )
    char_1 = {
        "id": "n_character_1",
        "type": "image",
        "label": "character: young",
        "config": {
            "pipeline": "character_design",
            "character_ids": ["char_1"],
            "character_name": "young",
        },
    }
    char_2 = {
        "id": "n_character_2",
        "type": "image",
        "label": "character: partner",
        "config": {
            "pipeline": "character_design",
            "character_ids": ["char_2"],
            "character_name": "partner",
        },
    }
    frame = {
        "id": "n_scene_2",
        "type": "image",
        "label": "scene 2",
        "config": {
            "pipeline": "scene",
            "character_ids": ["char_1", "char_2"],
            "character_node_ids": ["n_character_1", "n_character_2"],
            "cast_names": ["young", "partner"],
            "identity_refs": {
                "character_ids": ["char_1", "char_2"],
                "character_node_ids": ["n_character_1", "n_character_2"],
            },
        },
    }
    ctx = NodeExecutionContext(
        graph={"nodes": [char_1, char_2, frame]},
        run_id="run_img_refs",
        node_id="n_scene_2",
        run={
            "node_states": {
                "n_character_1": {
                    "output_ref": {
                        "kind": "image",
                        "uri": young.resolve().as_uri(),
                        "mime_type": "image/png",
                    }
                },
                "n_character_2": {
                    "output_ref": {
                        "kind": "image",
                        "uri": partner.resolve().as_uri(),
                        "mime_type": "image/png",
                    }
                },
            }
        },
    )
    toolkit = DesignerGraphToolkit(_DummySpawner(), ctx)
    result = await toolkit.call_image_model(prompt="compose dinner")
    assert result.startswith("image_ready")
    assert seen["reference_images"] == [str(young.resolve()), str(partner.resolve())]
    assert seen["prompt"] == "compose dinner"


def test_agent_failure_message_reads_task_loop_timeout() -> None:
    from jiuwenswarm.server.runtime.designer.node_agent import _agent_failure_message

    timeout = {
        "output": "Task loop round timed out after 600 seconds.",
        "result_type": "error",
        "error": "completion_timeout",
    }
    assert _agent_failure_message(timeout) == timeout["output"]
    assert _agent_failure_message({"output": json.dumps(timeout)}) == timeout["output"]
    assert _agent_failure_message({"output": "红色陶瓷杯静置", "result_type": "answer"}) is None
    assert _agent_failure_message("The scene is as in Image 1.") is None


class _FakeNodeAgent:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.card = None

    def ensure_initialized(self) -> None:
        return None

    async def invoke(self, _payload: dict, session: object = None) -> dict:
        return self.result


def _install_fake_deep_agent(monkeypatch: pytest.MonkeyPatch, result: dict) -> dict:
    captured: dict = {}

    def fake_create(model: object, **kwargs: object) -> _FakeNodeAgent:
        captured["completion_timeout"] = kwargs.get("completion_timeout")
        return _FakeNodeAgent(result)

    monkeypatch.setattr("openjiuwen.harness.factory.create_deep_agent", fake_create)
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_default_models",
        lambda _config: [
            {
                "is_default": True,
                "model_client_config": {
                    "api_key": "test-key",
                    "model_name": "test-model",
                    "api_base": "http://127.0.0.1",
                    "client_provider": "OpenAI",
                },
                "model_config_obj": {},
            }
        ],
    )
    return captured


@pytest.mark.asyncio
async def test_task_loop_error_does_not_submit_another_video(
    designer_store: DesignerGraphStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
    from jiuwenswarm.server.runtime.designer.node_agent import NodeAgentHost

    monkeypatch.setenv("VIDEO_GEN_PROTOCOL", "vllm-omni")
    monkeypatch.setenv("VIDEO_GEN_API_BASE", "http://127.0.0.1:8091/v1")
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_workspace_dir",
        lambda: tmp_path,
    )
    captured = _install_fake_deep_agent(
        monkeypatch,
        {
            "output": "Task loop round timed out after 600 seconds.",
            "result_type": "error",
            "error": "completion_timeout",
        },
    )
    calls: list[str] = []

    async def fake_materialize(self, node):  # noqa: ANN001
        calls.append(str(node.get("id") or ""))
        raise AssertionError("error result must not materialize media")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.node_agent.DesignerGraphToolkit.materialize_media",
        fake_materialize,
    )
    node = {
        "id": "n_clip_1",
        "type": "video",
        "label": "clip",
        "config": {"pipeline": "clip", "tools": ["call_model"]},
    }
    ctx = NodeExecutionContext(
        graph={"graph_id": "g1", "nodes": [node], "edges": [], "metadata": {}},
        run_id="r1",
        node_id="n_clip_1",
        run={"run_id": "r1", "node_states": {}},
    )
    with pytest.raises(RuntimeError, match="timed out after 600 seconds"):
        await NodeAgentHost(_DummySpawner())._run_deep_agent(node, ctx, DesignerGraphToolkit(_DummySpawner(), ctx))
    assert calls == []
    assert captured["completion_timeout"] == 7200.0


@pytest.mark.asyncio
async def test_creative_text_still_materializes_one_video(
    designer_store: DesignerGraphStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
    from jiuwenswarm.server.runtime.designer.node_agent import NodeAgentHost

    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_workspace_dir",
        lambda: tmp_path,
    )
    _install_fake_deep_agent(
        monkeypatch,
        {"output": "The scene is as in Image 1. 红色陶瓷杯静置。", "result_type": "answer"},
    )
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v" * 600)
    calls: list[str] = []

    async def fake_materialize(self, node):  # noqa: ANN001
        calls.append(str(node.get("id") or ""))
        return NodeResult(
            output_ref=file_output_ref(video, kind="video", mime_type="video/mp4"),
            message="one video",
        )

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.node_agent.DesignerGraphToolkit.materialize_media",
        fake_materialize,
    )
    node = {
        "id": "n_clip_1",
        "type": "video",
        "label": "clip",
        "config": {"pipeline": "clip", "tools": ["call_model"]},
    }
    ctx = NodeExecutionContext(
        graph={"graph_id": "g1", "nodes": [node], "edges": [], "metadata": {}},
        run_id="r1",
        node_id="n_clip_1",
        run={"run_id": "r1", "node_states": {}},
    )
    result = await NodeAgentHost(_DummySpawner())._run_deep_agent(
        node, ctx, DesignerGraphToolkit(_DummySpawner(), ctx)
    )
    assert calls == ["n_clip_1"]
    assert result.output_ref is not None
    assert str(result.output_ref.get("uri") or "").endswith("clip.mp4")
