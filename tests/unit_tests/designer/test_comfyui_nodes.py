# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Nodes imported from a ComfyUI vLLM-Omni workflow."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.server.runtime.designer.executor import GraphExecutor
from tests.unit_tests.designer.graph_fixtures import make_pipeline_graph
from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
from jiuwenswarm.server.runtime.designer.handlers import comfyui_nodes, resolve_handler_key
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
from jiuwenswarm.server.runtime.designer.orchestration import Director


def _video_node(**comfyui: Any) -> dict:
    return {
        "id": "n_comfy_video",
        "type": "video",
        "label": "Generate Video",
        "config": {
            "is_comfyui": True,
            "user_added": True,
            "force_handler": True,
            "skip_llm": True,
            "delegate": "handler",
            "role": "video",
            "generate": {"prompt": "a red boat at dawn", "prompt_origin": "user"},
            "comfyui": {
                "class_type": "VLLMOmniGenerateVideo",
                "fields": {
                    "url": "http://omni:8091/v1",
                    "model": "Wan-AI/Wan2.2-T2V-A14B-Diffusers",
                    "negative_prompt": "blurry",
                    "width": 832,
                    "height": 480,
                    "fps": 16,
                    "duration": 5,
                },
                **comfyui,
            },
        },
    }


def _upload_node(node_id: str, kind: str, path: Path | None) -> dict:
    config: dict[str, Any] = {"role": kind, "interaction_mode": "upload"}
    if path is not None:
        config["user_replaced_output"] = True
        config["upload"] = {"filename": path.name, "uri": path.resolve().as_uri()}
    return {"id": node_id, "type": kind, "label": f"ref {node_id}", "config": config}


def test_request_translates_fields_sampling_and_h3_model_params() -> None:
    node = _video_node(
        sampling_params={
            "num_inference_steps": 30,
            "guidance_scale": 5.0,
            "vae_use_tiling": True,
            "seed": -1,
        },
        model_params={"type": "minimax_h3", "flow_shift": 10.0, "audio_flow_shift": 2.5},
    )

    request = comfyui_nodes.comfyui_request(node)

    assert request.api_base == "http://omni:8091/v1"
    assert request.model == "Wan-AI/Wan2.2-T2V-A14B-Diffusers"
    assert request.size == "832x480"
    assert request.fps == 16
    assert request.duration == 5.0
    assert request.negative_prompt == "blurry"
    # seed -1 means "no seed" in ComfyUI.
    assert request.extra_fields == {
        "num_inference_steps": 30,
        "guidance_scale": 5.0,
        "vae_use_tiling": True,
        "flow_shift": 10.0,
    }
    assert request.extra_params == {"audio_flow_shift": 2.5}


def test_wan_model_params_are_all_top_level() -> None:
    node = _video_node(
        sampling_params={"seed": 42},
        model_params={"type": "wan", "guidance_scale_2": 4.0, "boundary_ratio": 0.875},
    )

    request = comfyui_nodes.comfyui_request(node)

    assert request.extra_fields == {"seed": 42, "guidance_scale_2": 4.0, "boundary_ratio": 0.875}
    assert request.extra_params == {}


def test_comfyui_nodes_dispatch_to_their_own_handlers() -> None:
    video = _video_node()
    image = {**video, "type": "image", "config": {**video["config"], "role": "image"}}
    plain = {"id": "n_plain", "type": "video", "config": {"role": "video"}}

    assert resolve_handler_key(video) == "comfyui_video"
    assert resolve_handler_key(image) == "comfyui_image"
    assert resolve_handler_key(plain) == "video"


def test_onboard_keeps_comfyui_nodes_on_the_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.llm_available",
        lambda: False,
    )
    node = _video_node()
    node["config"]["force_handler"] = False
    node["config"]["delegate"] = "agent"
    graph = {"nodes": [node], "edges": []}

    Director().onboard_user_added_nodes(graph)

    cfg = graph["nodes"][0]["config"]
    assert cfg["force_handler"] is True
    assert cfg["skip_llm"] is True
    assert cfg["delegate"] == "handler"
    assert cfg["generate"]["prompt"] == "a red boat at dawn"


def _clear_video_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("VIDEO_GEN_API_KEY", "VIDEO_GEN_API_BASE", "VIDEO_GEN_MODEL_NAME", "VIDEO_GEN_PROTOCOL"):
        monkeypatch.delenv(name, raising=False)


def test_video_handler_pins_vllm_omni_with_wired_references(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_video_slot(monkeypatch)
    still = tmp_path / "still.png"
    still.write_bytes(b"png")
    motion = tmp_path / "motion.mp4"
    motion.write_bytes(b"mp4")
    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"mp3")
    produced = tmp_path / "out" / "generated.mp4"
    produced.parent.mkdir()
    produced.write_bytes(b"video")
    workspace = tmp_path / "workspace"

    node = _video_node(sampling_params={"num_inference_steps": 20})
    graph = {
        "nodes": [
            _upload_node("n_ref_image", "image", still),
            _upload_node("n_ref_video", "video", motion),
            _upload_node("n_ref_audio", "audio", voice),
            node,
        ],
        "edges": [
            {"source": "n_ref_image", "target": node["id"], "kind": "data"},
            {"source": "n_ref_video", "target": node["id"], "kind": "data"},
            {"source": "n_ref_audio", "target": node["id"], "kind": "data"},
        ],
    }
    calls: list[dict[str, Any]] = []

    def fake_video(prompt: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"prompt": prompt, **kwargs})
        return {"video_path": str(produced)}

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.vllm_omni_gen.invoke_vllm_omni_video_generation_sync",
        fake_video,
    )
    monkeypatch.setattr(comfyui_nodes, "graph_workspace_dir", lambda _graph: workspace)

    ctx = NodeExecutionContext(graph=graph, run_id="run_c", node_id=node["id"], run={})
    result = asyncio.run(comfyui_nodes.COMFYUI_VIDEO_HANDLER.execute(node, ctx))

    call = calls[0]
    assert call["prompt"] == "a red boat at dawn"
    assert call["api_base"] == "http://omni:8091/v1"
    assert call["api_key"] == ""  # no vLLM-Omni slot in Settings, so no key reaches the node's server
    assert call["model"] == "Wan-AI/Wan2.2-T2V-A14B-Diffusers"
    assert call["size"] == "832x480"
    assert call["duration"] == 5.0
    assert call["fps"] == 16
    assert call["negative_prompt"] == "blurry"
    assert call["num_inference_steps"] == 20
    assert call["reference_images"] == [str(still.resolve())]
    assert call["reference_videos"] == [str(motion.resolve())]
    assert call["reference_audios"] == [str(voice.resolve())]
    assert result.output_ref is not None
    assert result.output_ref["uri"] == (workspace / "generated.mp4").resolve().as_uri()


@pytest.mark.parametrize(
    "protocol,expected_key,expected_base",
    [
        # a vLLM-Omni Settings slot fills in what the node leaves out
        ("vllm-omni", "sk-omni", "http://settings-omni:8091/v1"),
        # another vendor's key and URL never reach a vLLM-Omni node
        ("minimax", "", ""),
    ],
)
def test_node_endpoint_reuses_only_a_vllm_omni_settings_slot(
    monkeypatch: pytest.MonkeyPatch, protocol: str, expected_key: str, expected_base: str
) -> None:
    from jiuwenswarm.server.runtime.designer.media_generation import vllm_omni_endpoint

    monkeypatch.setenv("VIDEO_GEN_API_KEY", "sk-omni")
    monkeypatch.setenv("VIDEO_GEN_API_BASE", "http://settings-omni:8091/v1")
    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "")
    monkeypatch.setenv("VIDEO_GEN_PROTOCOL", protocol)
    assert vllm_omni_endpoint("video", None, None) == (expected_key, expected_base, "")
    assert vllm_omni_endpoint("video", "http://node:8091/v1", "Wan")[1:] == ("http://node:8091/v1", "Wan")


def test_wired_reference_without_file_fails_the_node(tmp_path: Path) -> None:
    node = _video_node()
    graph = {
        "nodes": [_upload_node("n_ref_image", "image", None), node],
        "edges": [{"source": "n_ref_image", "target": node["id"], "kind": "data"}],
    }
    ctx = NodeExecutionContext(graph=graph, run_id="run_c", node_id=node["id"], run={})

    with pytest.raises(RuntimeError, match="ref n_ref_image"):
        asyncio.run(comfyui_nodes.COMFYUI_VIDEO_HANDLER.execute(node, ctx))


@pytest.fixture()
def designer_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DesignerGraphStore:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.graph_store.get_agent_root_dir",
        lambda: tmp_path,
    )
    return DesignerGraphStore()


def _imported_graph(store: DesignerGraphStore, reference: Path | None) -> dict:
    """A Director-built graph plus an imported image-to-video ComfyUI pair."""
    graph = make_pipeline_graph(project_id="proj_comfy", prompt="a harbour film")
    node = _video_node()
    graph["nodes"].extend([_upload_node("n_ref_image", "image", reference), node])
    graph["edges"].append(
        {"id": "e_ref", "source": "n_ref_image", "target": node["id"], "kind": "data"}
    )
    return store.save_graph(graph)


def _forbid_director_and_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("ComfyUI generate must not reach the Director or LLM")

    monkeypatch.setattr("jiuwenswarm.server.runtime.designer.model_tools.require_llm", forbidden)
    monkeypatch.setattr(Director, "decide_capabilities", forbidden)
    monkeypatch.setattr(Director, "finalize", forbidden)


def test_rerun_without_earlier_play_counts_the_upload_as_ready(
    designer_store: DesignerGraphStore, tmp_path: Path
) -> None:
    still = tmp_path / "still.png"
    still.write_bytes(b"png")
    graph = _imported_graph(designer_store, still)
    executor = GraphExecutor(designer_store)

    run = executor.create_rerun(graph, source_run=None, node_id="n_comfy_video")

    assert run["metadata"]["scope_node_ids"] == ["n_comfy_video"]
    reference = run["node_states"]["n_ref_image"]
    assert reference["status"] == "completed"
    assert reference["output_ref"]["uri"] == still.resolve().as_uri()
    assert reference["output_ref"]["kind"] == "image"
    assert run["node_states"]["n_comfy_video"]["status"] == "pending"
    saved = designer_store.get_graph(graph["graph_id"])
    assert saved is not None
    assert not (saved.get("metadata") or {}).get("use_prior_feedback")


def test_rerun_refuses_a_reference_without_a_file(designer_store: DesignerGraphStore) -> None:
    graph = _imported_graph(designer_store, None)
    executor = GraphExecutor(designer_store)

    with pytest.raises(ValueError, match="upstream not ready: n_ref_image"):
        executor.create_rerun(graph, source_run=None, node_id="n_comfy_video")


@pytest.mark.asyncio
async def test_scoped_run_calls_vllm_omni_and_runs_nothing_else(
    designer_store: DesignerGraphStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_director_and_llm(monkeypatch)
    still = tmp_path / "still.png"
    still.write_bytes(b"png")
    produced = tmp_path / "out" / "generated.mp4"
    produced.parent.mkdir()
    produced.write_bytes(b"video")
    calls: list[dict[str, Any]] = []

    def fake_video(prompt: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"prompt": prompt, **kwargs})
        return {"video_path": str(produced)}

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.vllm_omni_gen.invoke_vllm_omni_video_generation_sync",
        fake_video,
    )
    monkeypatch.setattr(comfyui_nodes, "graph_workspace_dir", lambda _graph: tmp_path / "ws")
    graph = _imported_graph(designer_store, still)
    executor = GraphExecutor(designer_store)
    run = executor.create_rerun(graph, source_run=None, node_id="n_comfy_video")
    seen: list[str] = []

    def on_update(updated: dict, node_id: str | None = None) -> None:
        if node_id == "n_comfy_video":
            seen.append(str(updated["node_states"]["n_comfy_video"].get("status")))

    await executor.start_run(run["run_id"], on_update=on_update)
    task = executor._tasks.get(run["run_id"])
    if task is not None:
        await task

    finished = designer_store.get_run(run["run_id"])
    assert finished is not None
    assert finished["status"] == "completed", finished.get("error")
    assert "running" in seen
    assert len(calls) == 1
    assert calls[0]["api_base"] == "http://omni:8091/v1"
    assert calls[0]["reference_images"] == [str(still.resolve())]
    states = finished["node_states"]
    assert states["n_comfy_video"]["status"] == "completed"
    assert states["n_brief"]["status"] == "pending"


@pytest.mark.asyncio
async def test_scoped_run_failure_reaches_the_run_error(
    designer_store: DesignerGraphStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_director_and_llm(monkeypatch)
    still = tmp_path / "still.png"
    still.write_bytes(b"png")

    def unreachable(_prompt: str, **_kwargs: Any) -> dict[str, Any]:
        raise ValueError("vLLM-Omni connection refused")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.vllm_omni_gen.invoke_vllm_omni_video_generation_sync",
        unreachable,
    )
    graph = _imported_graph(designer_store, still)
    executor = GraphExecutor(designer_store)
    run = executor.create_rerun(graph, source_run=None, node_id="n_comfy_video")

    await executor.start_run(run["run_id"])
    task = executor._tasks.get(run["run_id"])
    if task is not None:
        await task

    finished = designer_store.get_run(run["run_id"])
    assert finished is not None
    assert finished["status"] == "failed"
    assert "connection refused" in str(finished.get("error"))
    assert finished["node_states"]["n_comfy_video"]["status"] == "failed"
