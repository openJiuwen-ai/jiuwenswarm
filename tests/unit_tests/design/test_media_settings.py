# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.tools.gen_toolkits import MODELARK, VLLM_OMNI
from jiuwenswarm.common.schema.designer_graph import NODE_TYPE_TABLE, NODE_TYPE_VIDEO
from jiuwenswarm.server.runtime.designer import media_generation
from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref
from jiuwenswarm.server.runtime.designer.media_settings import (
    MediaSettingsError,
    image_settings,
    media_capabilities,
    update_media_settings,
    video_settings,
)
from jiuwenswarm.server.runtime.gateway_adapter import designer_adapter as adapter

from tests.unit_tests.design.graph_fixtures import make_pipeline_graph

_MODEL_ID = "jiuwenswarm.server.runtime.designer.pipeline.model_capacity.configured_video_model_id"


def _use_model(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    monkeypatch.setattr(_MODEL_ID, lambda: model)


def _use_image_model(monkeypatch: pytest.MonkeyPatch, model: str, backend: str | None) -> None:
    real = media_generation.slot_settings

    def fake(kind):
        if kind != "image":
            return real(kind)
        return media_generation.SlotSettings(api_key="k", api_base="https://x", model=model, backend=backend)

    monkeypatch.setattr(media_generation, "slot_settings", fake)


@pytest.fixture(autouse=True)
def _wan(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_model(monkeypatch, "wan3.0-video")
    _use_image_model(monkeypatch, "seedream-4-0-250828", MODELARK)


@pytest.fixture()
def designer_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DesignerGraphStore:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.graph_store.get_agent_root_dir",
        lambda: tmp_path,
    )
    store = DesignerGraphStore()
    monkeypatch.setattr(adapter, "_store", store)
    return store


def _graph(video: dict | None = None, image: dict | None = None) -> dict:
    meta: dict = {}
    if video is not None:
        meta["video_settings"] = video
    if image is not None:
        meta["image_settings"] = image
    return {"metadata": meta, "nodes": []}


def test_unset_settings_take_the_model_defaults() -> None:
    assert video_settings(_graph()).as_dict() == {"aspect_ratio": "16:9", "resolution": "1080P"}
    still = image_settings(_graph())
    assert (still.aspect_ratio, still.resolution, still.size) == ("16:9", "1K", "1424x800")


def test_stored_settings_snap_onto_the_active_model(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _graph({"aspect_ratio": "21:9", "resolution": "1080p"})
    assert video_settings(graph).as_dict() == {"aspect_ratio": "21:9", "resolution": "1080P"}

    _use_model(monkeypatch, "dreamina-seedance-2-0-mini-260615")
    assert video_settings(graph).as_dict() == {"aspect_ratio": "21:9", "resolution": "720P"}

    _use_model(monkeypatch, "MiniMax-H3")
    assert video_settings(graph).as_dict() == {"aspect_ratio": "21:9", "resolution": "768P"}

    stills = _graph(image={"aspect_ratio": "4:3", "resolution": "1.5K"})
    assert image_settings(stills).resolution == "1K"
    _use_image_model(monkeypatch, "seedream-4-5-251128", MODELARK)
    assert image_settings(stills).resolution == "2K"
    _use_image_model(monkeypatch, "image-01", "minimax")
    assert image_settings(stills).size == "1792x1344"
    _use_image_model(monkeypatch, "Qwen/Qwen-Image-2512", VLLM_OMNI)
    assert image_settings(_graph()).resolution == "1K"
    _use_image_model(monkeypatch, "google/gemini-3.1-flash-image", None)
    assert image_settings(stills).resolution == "1K"


def test_image_settings_size_follows_ratio_and_tier() -> None:
    assert image_settings(_graph(image={"aspect_ratio": "9:16"})).size == "800x1424"
    assert image_settings(_graph(image={"aspect_ratio": "3:4", "resolution": "4K"})).size == "3520x4704"
    assert image_settings(_graph(image={"aspect_ratio": "2.39:1"})).aspect_ratio == "16:9"


def test_update_is_partial_and_keeps_the_other_field() -> None:
    graph = _graph({"aspect_ratio": "9:16", "resolution": "720P"})
    update_media_settings(graph, video={"resolution": "480p"})
    assert graph["metadata"]["video_settings"] == {"aspect_ratio": "9:16", "resolution": "480P"}
    assert "image_settings" not in graph["metadata"]

    update_media_settings(graph, image={"aspect_ratio": "1:1"})
    assert graph["metadata"]["image_settings"] == {"aspect_ratio": "1:1", "resolution": "1K"}
    assert graph["metadata"]["video_settings"]["aspect_ratio"] == "9:16"

    update_media_settings(graph, image={"resolution": "2K"})
    assert graph["metadata"]["image_settings"] == {"aspect_ratio": "1:1", "resolution": "2K"}


@pytest.mark.parametrize(
    ("video", "image", "message"),
    [
        ({"resolution": "2K"}, None, "video resolution"),
        ({"aspect_ratio": "5:4"}, None, "video aspect_ratio"),
        ({"duration": 8}, None, "unknown video setting"),
        (None, {"aspect_ratio": "21:9"}, "image aspect_ratio"),
        (None, {"resolution": "1.5K"}, "image resolution"),
        (None, {"size": "1024x1024"}, "unknown image setting"),
        ("720P", None, "must be an object"),
    ],
)
def test_update_rejects_values_the_model_does_not_accept(video, image, message) -> None:
    graph = _graph()
    with pytest.raises(MediaSettingsError, match=message):
        update_media_settings(graph, video=video, image=image)
    assert graph["metadata"] == {}


def test_capabilities_list_the_active_model_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_model(monkeypatch, "MiniMax-H3-Max")
    caps = media_capabilities()
    assert caps["video"]["model_id"] == "MiniMax-H3-Max"
    assert caps["video"]["resolutions"] == ["480P", "768P"]
    assert caps["video"]["default_resolution"] == "768P"
    assert (caps["video"]["min_sec"], caps["video"]["max_sec"]) == (5, 15)
    assert caps["video"]["ratios"] == ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]
    assert caps["image"]["ratios"] == ["16:9", "4:3", "1:1", "3:4", "9:16"]
    assert caps["image"]["resolutions"] == ["1K", "2K", "4K"]
    assert (caps["image"]["default_ratio"], caps["image"]["default_resolution"]) == ("16:9", "1K")
    assert caps["image"]["backend"] == MODELARK
    assert "settings" not in caps

    with_graph = media_capabilities(_graph({"resolution": "1080P"}))
    assert with_graph["settings"]["video"] == {"aspect_ratio": "16:9", "resolution": "768P"}
    assert with_graph["settings"]["image"] == {"aspect_ratio": "16:9", "resolution": "1K"}


def test_graph_patch_updates_settings_for_the_whole_canvas(
    designer_store: DesignerGraphStore,
) -> None:
    graph = designer_store.save_graph(make_pipeline_graph(project_id="proj_media", prompt="rain"))
    payload, error, code = adapter._patch_graph(
        {
            "graph_id": graph["graph_id"],
            "patch": {
                "video_settings": {"aspect_ratio": "9:16"},
                "image_settings": {"aspect_ratio": "3:4"},
            },
        }
    )
    assert (error, code) == (None, None)
    meta = payload["graph"]["metadata"]
    assert meta["video_settings"] == {"aspect_ratio": "9:16", "resolution": "1080P"}
    assert meta["image_settings"] == {"aspect_ratio": "3:4", "resolution": "1K"}
    stored = designer_store.get_graph(graph["graph_id"])
    assert stored["metadata"]["video_settings"]["aspect_ratio"] == "9:16"

    payload, error, code = adapter._patch_graph(
        {"graph_id": graph["graph_id"], "video_settings": {"resolution": "720P"}}
    )
    assert error is None
    assert payload["graph"]["metadata"]["video_settings"] == {
        "aspect_ratio": "9:16",
        "resolution": "720P",
    }


def test_graph_patch_rejects_unsupported_settings(designer_store: DesignerGraphStore) -> None:
    graph = designer_store.save_graph(make_pipeline_graph(project_id="proj_media", prompt="rain"))
    payload, error, code = adapter._patch_graph(
        {"graph_id": graph["graph_id"], "patch": {"video_settings": {"resolution": "4K"}}}
    )
    assert payload is None
    assert code == "BAD_REQUEST"
    assert "video resolution" in (error or "")
    assert "video_settings" not in designer_store.get_graph(graph["graph_id"])["metadata"]


def test_capabilities_endpoint_reads_the_graph(designer_store: DesignerGraphStore) -> None:
    graph = make_pipeline_graph(project_id="proj_media", prompt="rain")
    graph["metadata"] = {**graph.get("metadata", {}), "video_settings": {"aspect_ratio": "1:1"}}
    graph = designer_store.save_graph(graph)

    payload, error, _ = adapter._media_capabilities({"graph_id": graph["graph_id"]})
    assert error is None
    assert payload["settings"]["video"]["aspect_ratio"] == "1:1"
    assert payload["video"]["resolutions"] == ["480P", "720P", "1080P"]

    payload, error, _ = adapter._media_capabilities({})
    assert error is None and "settings" not in payload

    payload, error, code = adapter._media_capabilities({"graph_id": "graph_missing"})
    assert payload is None and code == "NOT_FOUND"


def _storyboard_graph(tmp_path: Path) -> dict:
    graph = make_pipeline_graph(project_id="proj_media", prompt="rain")
    nodes = {node["id"]: node for node in graph["nodes"]}
    storyboard = tmp_path / "storyboard.md"
    storyboard.write_text(
        "| Shot | Timeline | Camera | Move | On screen | Character action | Speech |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| 1 | 0-8s | wide | static | She | She waits | |\n",
        encoding="utf-8",
    )
    nodes["n_storyboard"]["output_ref"] = file_output_ref(
        storyboard, kind=NODE_TYPE_TABLE, mime_type="text/markdown"
    )
    return graph


def test_clip_duration_is_the_manual_value_else_the_storyboard(tmp_path: Path) -> None:
    graph = _storyboard_graph(tmp_path)
    nodes = {node["id"]: node for node in graph["nodes"]}
    nodes["n_clip_1"]["config"]["timeline"] = "0-3s"
    assert media_capabilities(graph)["video_durations"] == {"n_clip_1": 8}

    nodes["n_clip_1"]["config"]["duration_sec"] = 12
    assert media_capabilities(graph)["video_durations"] == {"n_clip_1": 12}
    nodes["n_clip_1"]["config"]["duration_sec"] = 99
    assert media_capabilities(graph)["video_durations"] == {"n_clip_1": 30}

    del nodes["n_clip_1"]["config"]["duration_sec"]
    del nodes["n_storyboard"]["output_ref"]
    assert media_capabilities(graph)["video_durations"] == {"n_clip_1": 5}


def test_manual_video_nodes_use_the_model_default_and_uploads_are_untimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_model(monkeypatch, "MiniMax-H3-Max")
    graph = {
        "metadata": {},
        "nodes": [
            {"id": "v_gen", "type": NODE_TYPE_VIDEO, "config": {"prompt": "a fox"}},
            {"id": "v_set", "type": NODE_TYPE_VIDEO, "config": {"prompt": "a fox", "duration_sec": 9}},
            {
                "id": "v_up",
                "type": NODE_TYPE_VIDEO,
                "config": {"interaction_mode": "upload", "upload": {"uri": "file:///tmp/a.mp4"}},
            },
        ],
        "edges": [],
    }
    assert media_capabilities(graph)["video_durations"] == {"v_gen": 6, "v_set": 9}
