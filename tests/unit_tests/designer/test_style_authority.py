# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations


def _analysis() -> dict:
    return {
        "source": "llm",
        "characters": [
            {
                "id": "char_1",
                "name": "小熊",
                "description": "圆脸小熊，蓝色背带裤",
            }
        ],
        "scenes": [
            {
                "id": "set_1",
                "name": "森林空地",
                "description": "柔和晨光下的森林空地",
            }
        ],
        "shots": [
            {
                "shot_index": 1,
                "title": "挥手",
                "action": "小熊在森林空地挥手。",
                "camera": "medium / eye-level",
                "character_ids": ["char_1"],
                "on_screen": ["char_1"],
                "setting_id": "set_1",
                "keyframe_prompt": "小熊在森林空地挥手。",
                "timeline": "0-5s",
            }
        ],
        "audio": {"policy": "silent", "include_speech": False, "include_music": False},
    }


def test_unspecified_style_defaults_to_cartoonish() -> None:
    from jiuwenswarm.server.runtime.designer.media_model_playbook import (
        default_style_lock,
    )

    style = default_style_lock("A bear waves in a forest.")
    assert style["medium"] == "stylized_animation"
    assert "cartoonish" in style["look"]
    assert "photoreal" not in style["look"].lower()


def test_chinese_cartoon_style_survives_graph_and_media_prompts(
    monkeypatch,
) -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        compose_practice_prompt,
    )
    from jiuwenswarm.common.schema.designer_graph import node_pipeline
    from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.capabilities.detect_audio_backends",
        lambda: {
            "can_speech": False,
            "can_music": False,
            "can_video_audio": False,
            "video_audio_model": "",
        },
    )
    graph = build_smart_video_graph(
        project_id="style-cn",
        prompt="制作一段卡通画风短片：扁平、柔和渲染、圆润造型，小熊在森林里挥手。",
        analysis=_analysis(),
    )

    analysis_style = graph["metadata"]["script_analysis"]["style_lock"]
    assert analysis_style["medium"] == "stylized_animation"
    assert "卡通画风" in analysis_style["look"]
    assert "flat shapes" in analysis_style["look"]

    media_nodes = [
        node
        for node in graph["nodes"]
        if node_pipeline(node) in {"character_design", "scene", "clip"}
    ]
    assert media_nodes
    for node in media_nodes:
        cfg = node["config"]
        assert cfg["style_lock"]["medium"] == "stylized_animation"
        prompt = str(cfg.get("prompt") or "") + str((cfg.get("generate") or {}).get("prompt") or "")
        assert "photoreal" not in prompt.lower()

    clip = next(node for node in media_nodes if node_pipeline(node) == "clip")
    video_prompt = compose_practice_prompt(
        cfg=clip["config"],
        graph=graph,
        action=str(clip["config"].get("shot_action") or ""),
    )
    assert "cartoonish animated feature look" in video_prompt
    assert "photoreal" not in video_prompt.lower()

    # A later artifact cannot overwrite an explicit user medium.
    from jiuwenswarm.server.runtime.designer.media_model_playbook import (
        synchronize_graph_style_from_brief,
    )

    resynced = synchronize_graph_style_from_brief(
        graph,
        "Visual style: photoreal live-action documentary",
    )
    assert resynced["medium"] == "stylized_animation"


def test_explicit_photoreal_style_is_still_honored() -> None:
    from jiuwenswarm.server.runtime.designer.media_model_playbook import (
        default_style_lock,
    )

    style = default_style_lock("真人实拍、写实纪录片风格")
    assert style["medium"] == "photoreal_cinematic"
    assert "photoreal" in style["look"].lower()


def test_brief_style_replaces_graph_style_for_every_media_leaf() -> None:
    from jiuwenswarm.server.runtime.designer.media_model_playbook import (
        default_style_lock,
        synchronize_graph_style_from_brief,
    )

    old = default_style_lock("")
    graph = {
        "description": "A quiet walk.",
        "metadata": {"script_analysis": {"style_lock": old}},
        "nodes": [
            {"id": "char", "config": {"role": "character", "style_lock": old}},
            {"id": "scene", "config": {"role": "scene", "style_lock": old}},
            {"id": "clip", "config": {"role": "clip", "style_lock": old}},
        ],
    }
    style = synchronize_graph_style_from_brief(
        graph,
        "## Brief\nVisual style: delicate watercolor storybook with loose blue washes",
    )

    assert style["medium"] == "watercolor_illustration"
    assert "watercolor" in style["look"]
    assert graph["metadata"]["script_analysis"]["style_lock"] == style
    assert all(node["config"]["style_lock"] == style for node in graph["nodes"])


def test_leaf_prompt_helpers_do_not_invent_a_medium() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
        compose_character_sheet_prompt,
    )
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        compose_practice_prompt,
    )

    image_prompt = compose_character_sheet_prompt(
        cfg={"character_name": "Alex", "costume_lock": "blue coat"},
    )
    video_prompt = compose_practice_prompt(
        cfg={
            "cast_names": ["Alex"],
            "on_screen": ["Alex"],
            "shot_action": "Alex waves.",
            "scene_specs": {"scene_name": "a park"},
        },
        graph={},
        action="Alex waves.",
    )

    for prompt in (image_prompt, video_prompt):
        assert "photoreal" not in prompt.lower()
        assert "cartoonish" not in prompt.lower()


def test_generated_storyboard_visibly_carries_style_authority() -> None:
    from jiuwenswarm.server.runtime.designer.smart_graph import (
        _write_storyboard_markdown,
    )

    markdown = _write_storyboard_markdown(
        _analysis()["shots"],
        _analysis()["characters"],
        style_lock={
            "look": "cartoonish animation with flat shapes and rounded forms",
            "medium": "stylized_animation",
        },
    )

    assert "Visual style: cartoonish animation with flat shapes and rounded forms" in markdown
