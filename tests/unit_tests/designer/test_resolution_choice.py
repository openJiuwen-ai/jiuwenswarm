# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Video resolution comes from the user, then the director, then the model default."""

from jiuwenswarm.server.runtime.designer.media_model_playbook import (
    MANAGER_CORRECTION_HINTS,
    SUPERVISOR_CORRECTION_HINTS,
)
from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import (
    apply_axis_locks_to_graph,
    resolve_clip_video_format,
    video_format_for_node,
)
from jiuwenswarm.server.runtime.designer.pipeline.model_capacity import (
    capacity_for_model,
    snap_resolution,
)


def _use_wan(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.pipeline.model_capacity.configured_video_model_id",
        lambda: "wan3.0-video",
    )


def _graph(prompt: str, *, director: str = "") -> dict:
    analysis: dict = {"characters": [], "shots": []}
    if director:
        analysis["video_resolution"] = director
    return {
        "description": prompt,
        "metadata": {"user_prompt": prompt, "script_analysis": analysis},
        "nodes": [
            {"id": "char", "type": "image", "config": {"role": "character_design"}},
            {"id": "scene", "type": "image", "config": {"role": "scene"}},
            {"id": "frame", "type": "image", "config": {"role": "frame"}},
            {"id": "keyframe", "type": "image", "config": {"role": "keyframe"}},
            {"id": "clip", "type": "video", "config": {"role": "clip"}},
            {"id": "video", "type": "video", "config": {"role": "video"}},
        ],
    }


def _cfg(graph: dict, node_id: str) -> dict:
    return next(node["config"] for node in graph["nodes"] if node["id"] == node_id)


def test_resolution_follows_user_then_director_then_model_default(monkeypatch) -> None:
    _use_wan(monkeypatch)
    size, tier = resolve_clip_video_format(
        ratio="16:9",
        user_resolution="720P",
        director_resolution="1080P",
    )
    assert tier == "720P"
    assert size == "1280*720"
    size, tier = resolve_clip_video_format(ratio="9:16", director_resolution="4K")
    assert tier == "1080P"
    assert size == "1080*1920"
    assert snap_resolution("", capacity_for_model("MiniMax-H3")) == "768P"
    assert snap_resolution("", capacity_for_model("wan3.0-video")) == "1080P"


def test_duration_snaps_like_resolution_per_model() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.model_capacity import snap_duration

    h3 = capacity_for_model("MiniMax-H3")
    h3_max = capacity_for_model("MiniMax-H3-Max")
    wan = capacity_for_model("wan3.0-video")
    seedance25 = capacity_for_model("doubao-seedance-2-5-260628")
    seedance20 = capacity_for_model("doubao-seedance-2-0-260128")
    assert snap_duration(4, h3) == 4
    assert snap_duration(4, h3_max) == 5
    assert snap_duration(None, h3_max) == 6
    assert snap_duration(2, wan) == 2
    assert snap_duration(1, wan) == 2
    assert snap_duration(99, seedance20) == 15
    assert snap_duration(99, seedance25) == 30
    assert snap_duration(4, seedance25) == 4


def test_resolve_clip_video_duration_uses_active_model(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import (
        resolve_clip_video_duration,
    )

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.pipeline.model_capacity.configured_video_model_id",
        lambda: "MiniMax-H3-Max",
    )
    assert resolve_clip_video_duration(4) == 5
    assert resolve_clip_video_duration(None, default=4) == 5
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.pipeline.model_capacity.configured_video_model_id",
        lambda: "wan3.0-video",
    )
    assert resolve_clip_video_duration(2) == 2


def test_user_resolution_wins_on_every_image_and_video_node(monkeypatch) -> None:
    _use_wan(monkeypatch)
    graph = _graph("请做 720p 横屏短片", director="1080P")
    apply_axis_locks_to_graph(graph)
    clip = _cfg(graph, "clip")
    assert clip["video_resolution"] == "720P"
    assert clip["video_size"] == "1280*720"
    assert "480P" not in str(clip.get("prompt") or "")
    for node_id in ("char", "scene", "frame", "keyframe"):
        cfg = _cfg(graph, node_id)
        assert cfg.get("video_resolution") in {None, "", "720P"}
        assert cfg.get("video_resolution") != "480P"
        assert "480P" not in str(cfg.get("prompt") or cfg.get("axis_clause") or "")
        assert cfg.get("image_size")
    video = _cfg(graph, "video")
    size, tier = video_format_for_node(graph, video)
    assert tier == "720P"
    assert size == "1280*720"


def test_director_resolution_used_when_user_is_silent(monkeypatch) -> None:
    _use_wan(monkeypatch)
    graph = _graph("a short film about a baker", director="720P")
    apply_axis_locks_to_graph(graph)
    clip = _cfg(graph, "clip")
    assert clip["video_resolution"] == "720P"
    assert clip["video_size"] == "1280*720"
    assert clip["video_resolution"] != "480P"


def test_model_default_used_when_user_and_director_are_silent(monkeypatch) -> None:
    _use_wan(monkeypatch)
    graph = _graph("a short film about a baker")
    apply_axis_locks_to_graph(graph)
    clip = _cfg(graph, "clip")
    assert clip["video_resolution"] == "1080P"
    assert clip["video_size"] == "1920*1080"
    for node_id in ("char", "scene", "frame", "keyframe", "clip", "video"):
        text = str(_cfg(graph, node_id).get("prompt") or "") + str(
            _cfg(graph, node_id).get("axis_clause") or ""
        )
        assert "480P" not in text


def test_explicit_480p_is_kept_when_the_user_asks_for_it(monkeypatch) -> None:
    _use_wan(monkeypatch)
    graph = _graph("shoot this at 480p")
    apply_axis_locks_to_graph(graph)
    clip = _cfg(graph, "clip")
    assert clip["video_resolution"] == "480P"
    assert clip["video_size"] == "832*480"


def test_playbook_does_not_tell_the_model_to_stamp_480p() -> None:
    assert "480P" not in SUPERVISOR_CORRECTION_HINTS
    assert "480P" not in MANAGER_CORRECTION_HINTS
    assert "video_resolution=480P" not in MANAGER_CORRECTION_HINTS


def test_smart_graph_shot_character_and_scene_follow_user_resolution(monkeypatch) -> None:
    _use_wan(monkeypatch)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.capabilities.detect_audio_backends",
        lambda: {
            "can_speech": False,
            "can_music": False,
            "can_video_audio": False,
            "video_audio_model": "",
        },
    )
    from jiuwenswarm.common.schema.designer_graph import node_pipeline
    from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph

    graph = build_smart_video_graph(
        project_id="res-choice",
        prompt="A 720p short film. A bear waves in a forest.",
        analysis={
            "source": "llm",
            "video_resolution": "1080P",
            "characters": [
                {"id": "char_1", "name": "Bear", "description": "A round bear in blue overalls"}
            ],
            "scenes": [
                {"id": "set_1", "name": "Clearing", "description": "A forest clearing in morning light"}
            ],
            "shots": [
                {
                    "shot_index": 1,
                    "title": "Wave",
                    "action": "The bear waves.",
                    "camera": "medium",
                    "character_ids": ["char_1"],
                    "on_screen": ["char_1"],
                    "setting_id": "set_1",
                    "timeline": "0-5s",
                }
            ],
        },
    )
    by_role: dict[str, list[dict]] = {}
    for node in graph["nodes"]:
        by_role.setdefault(node_pipeline(node), []).append(node["config"])
    assert by_role["clip"]
    for cfg in by_role["clip"]:
        assert cfg["video_resolution"] == "720P"
        assert cfg["video_size"] == "1280*720"
    for role in ("character_design", "scene"):
        assert by_role[role]
        for cfg in by_role[role]:
            assert cfg.get("video_resolution") != "480P"
            assert str(cfg.get("image_size") or "")
            text = str(cfg.get("prompt") or "") + str((cfg.get("generate") or {}).get("prompt") or "")
            assert "480P" not in text
