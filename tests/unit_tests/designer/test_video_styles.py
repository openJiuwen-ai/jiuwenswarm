# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
    infer_shot_budget,
)
from jiuwenswarm.server.runtime.designer.skills_loader import (
    attach_skills_metadata,
    load_style_skill,
)
from jiuwenswarm.server.runtime.designer.video_styles import (
    VIDEO_STYLE_DEFAULT_CINEMATIC,
    VIDEO_STYLE_FINAL_FRAME_REVERSE,
    detect_video_style,
    enforce_style_shot_floor,
    list_video_styles,
    resolve_video_style,
    stamp_video_style_on_graph,
    video_style_min_shots,
)


def test_final_frame_reverse_is_in_catalog():
    ids = {s["id"] for s in list_video_styles()}
    assert VIDEO_STYLE_FINAL_FRAME_REVERSE in ids
    assert VIDEO_STYLE_DEFAULT_CINEMATIC in ids


def test_detect_final_frame_reverse_from_chinese_cues():
    assert (
        detect_video_style("把这张名画定格图做成15秒短视频，终帧对齐经典构图")
        == VIDEO_STYLE_FINAL_FRAME_REVERSE
    )


def test_detect_default_without_cues():
    assert detect_video_style("两个人在咖啡店对话的短片") == VIDEO_STYLE_DEFAULT_CINEMATIC


def test_forced_metadata_overrides_cues():
    graph = {
        "description": "普通对话短片",
        "metadata": {"video_style": VIDEO_STYLE_FINAL_FRAME_REVERSE},
        "nodes": [],
    }
    assert resolve_video_style(graph) == VIDEO_STYLE_FINAL_FRAME_REVERSE


def test_style_skill_file_loads():
    text = load_style_skill(VIDEO_STYLE_FINAL_FRAME_REVERSE)
    assert "终帧" in text or "final_frame" in text.lower()


def test_attach_skills_stamps_video_style():
    graph = {
        "description": "经典名画定格，倒推出形成之前的最后几秒",
        "metadata": {"scenario": "video"},
        "nodes": [
            {"id": "n_clip_1", "type": "video", "config": {"pipeline": "clip", "shot_index": 1}},
            {"id": "n_storyboard", "type": "table", "config": {"pipeline": "storyboard"}},
        ],
    }
    out = attach_skills_metadata(graph, graph["description"])
    assert out["metadata"]["video_style"] == VIDEO_STYLE_FINAL_FRAME_REVERSE
    clip = next(n for n in out["nodes"] if n["id"] == "n_clip_1")
    assert clip["config"].get("video_style") == VIDEO_STYLE_FINAL_FRAME_REVERSE
    excerpt = str(clip["config"].get("skill_excerpt") or "")
    assert "final_frame_reverse" in excerpt or "终帧" in excerpt


def test_director_method_alias_works():
    graph = {
        "description": "普通对话",
        "metadata": {"director_method": VIDEO_STYLE_FINAL_FRAME_REVERSE},
        "nodes": [],
    }
    assert resolve_video_style(graph) == VIDEO_STYLE_FINAL_FRAME_REVERSE


def test_stamp_persists_lock_record():
    graph = {"description": "freeze frame as endpoint still", "metadata": {}, "nodes": []}
    style_id = stamp_video_style_on_graph(graph)
    assert style_id == VIDEO_STYLE_FINAL_FRAME_REVERSE
    assert graph["metadata"]["video_style_lock"]["id"] == style_id


PAINTING_REMAKE = (
    "《奶破龙翻越阿尔卑斯山》（致敬达维特《跨越阿尔卑斯山圣伯纳隘口的拿破仑》）原画精髓："
    "法兰西皇帝骑着烈马，披风在狂风中猎猎作响，手指前方。奶蛙魔改：皇帝变成奶破龙。"
    "画面保留原作巴洛克式厚重油画质感、雪山寒风与戏剧性逆光。"
)


def test_painting_remake_activates_style():
    """原画/致敬/魔改/油画 also mean 'classic still as endpoint'."""
    assert detect_video_style(PAINTING_REMAKE) == VIDEO_STYLE_FINAL_FRAME_REVERSE


def test_style_floor_grows_single_shot_analysis():
    analysis = {
        "source": "llm",
        "target_shot_count": 1,
        "shots": [{"shot_index": 1, "action": "奶破龙骑马指向前方", "character_ids": ["char_1"]}],
    }
    out = enforce_style_shot_floor(analysis, PAINTING_REMAKE)
    assert len(out["shots"]) == video_style_min_shots(VIDEO_STYLE_FINAL_FRAME_REVERSE) >= 4
    assert out["target_shot_count"] == len(out["shots"])
    # Original beat survives as the opening shot.
    assert "奶破龙骑马指向前方" in str(out["shots"][0]["action"])
    assert out["shots"][-1]["style_beat_role"]


def test_style_floor_respects_explicit_shot_count():
    analysis = {"source": "llm", "target_shot_count": 1, "shots": [{"shot_index": 1}]}
    out = enforce_style_shot_floor(analysis, PAINTING_REMAKE + " 只要1个分镜。")
    assert len(out["shots"]) == 1


def test_style_floor_lifts_shot_budget():
    analysis = {"source": "llm", "target_shot_count": 1, "shots": [{"shot_index": 1}]}
    assert infer_shot_budget(PAINTING_REMAKE, analysis) >= 4


def test_default_style_keeps_llm_shot_count():
    analysis = {"source": "llm", "target_shot_count": 1, "shots": [{"shot_index": 1}]}
    assert infer_shot_budget("两个人在咖啡店对话的短片", analysis) == 1
