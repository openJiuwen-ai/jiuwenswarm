# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""User-visible Designer text follows preferred_language; locks stay in structured config."""

from __future__ import annotations

import re

import pytest

from jiuwenswarm.server.runtime.designer.pipeline import content_language as cl


@pytest.fixture()
def zh_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cl, "get_config", lambda: {"preferred_language": "zh"})


def _english_words(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_]{3,}", text)) - {"Image"}


@pytest.mark.parametrize(("preferred", "expected"), [("en", "en"), ("zh", "zh"), (None, "zh")])
def test_current_content_language_follows_preferred_language(
    monkeypatch: pytest.MonkeyPatch, preferred: str | None, expected: str
) -> None:
    monkeypatch.setattr(cl, "get_config", lambda: {"preferred_language": preferred})
    assert cl.current_content_language() == expected


@pytest.mark.usefixtures("zh_session")
def test_chinese_character_sheet_prompt() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import compose_character_sheet_prompt

    text = compose_character_sheet_prompt(
        cfg={"character_name": "沈妙（前世）", "costume_lock": "沈妙（前世）: outfit=破旧白色囚衣; footwear=赤脚"}
    )
    assert text.startswith("画面中仅一人：沈妙（前世）")
    assert "身穿破旧白色囚衣，赤脚。" in text
    assert "Wearing" not in text and "outfit=" not in text


def test_llm_clothing_parts_win_over_english_regex() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import enrich_character_clothing

    ch = {"id": "char_1", "name": "沈妙", "description": "white shirt", "clothing_parts": {"outfit": "白色囚衣"}}
    assert enrich_character_clothing(ch) == "沈妙: outfit=白色囚衣"


@pytest.mark.usefixtures("zh_session")
def test_dict_blocking_never_becomes_an_opening_hold() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.storyboard_clip_state import stamp_shot_states_on_clip_cfg

    cfg = stamp_shot_states_on_clip_cfg({}, shot={"start_state": {"blocking": {"landmark": "x"}, "facing": "店门"}})
    assert cfg["pose_holds"] == ["开场朝向：店门。"]


@pytest.mark.usefixtures("zh_session")
def test_chinese_reference_clip_prompt_passes_its_own_checks() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
        compose_reference_clip_prompt,
        reference_prompt_issues,
    )

    cfg = {
        "clip_index": 2,
        "clip_action": "女孩转身挥手",
        "timeline": "4.0-8.0s",
        "duration_sec": 4,
        "style_lock": {"look": "水彩插画"},
        "previous_end_state": "女孩站在花园中央",
        "reference_call_mode": "i2v",
    }
    prompt = compose_reference_clip_prompt(cfg)
    assert "本镜头承接上一镜的结尾：女孩站在花园中央。" in prompt
    assert _english_words(prompt) == set()
    assert reference_prompt_issues(prompt, cfg) == []


def test_chinese_smart_graph_keeps_locks_out_of_visible_and_api_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.server.runtime.designer.orchestration import Director
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import director_approve_video_prompt
    from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph

    monkeypatch.setattr(cl, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.capabilities.detect_audio_backends",
        lambda: {"can_speech": False, "can_music": False, "can_video_audio": True},
    )
    graph = build_smart_video_graph(
        project_id="p",
        prompt="咖啡馆短片：咖啡师阿岚清晨开店",
        analysis={
            "source": "llm",
            "characters": [{"id": "char_1", "name": "阿岚", "clothing_parts": {"outfit": "白衬衫配棕色围裙"}}],
            "scenes": [{"id": "scene_1", "name": "街角咖啡馆"}],
            "clips": [
                {
                    "clip_index": 1,
                    "action": "阿岚擦拭吧台",
                    "camera": "固定中景",
                    "character_ids": ["char_1"],
                    "on_screen": ["char_1"],
                    "setting_id": "set_1",
                    "setting_description": "街角咖啡馆",
                }
            ],
        },
    )
    Director.__new__(Director).validate_plan_structure(graph)
    cfg = next(n["config"] for n in graph["nodes"] if "clip_index" in (n.get("config") or {}))
    assert cfg["generate"]["prompt"] == "仅拍第1镜。镜头：固定中景。动作：阿岚擦拭吧台。出镜角色：阿岚。"
    body, _notes = director_approve_video_prompt(
        "阿岚擦拭吧台", cfg=cfg, graph=graph, clip_index=1, action="阿岚擦拭吧台", camera="固定中景"
    )
    assert "身穿白衬衫配棕色围裙" in body
    assert _english_words(body) == set(), body
    compose = next(n["config"] for n in graph["nodes"] if n["id"] == "n_compose")
    assert compose["director_task"].startswith("使用 ffmpeg_compose 工具，按镜头顺序")
