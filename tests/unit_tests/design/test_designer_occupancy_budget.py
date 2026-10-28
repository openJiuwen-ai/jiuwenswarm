"""Occupancy + shot-budget guards (domain-agnostic)."""

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
    _cid_list,
    _explicit_clip_count_from_prompt,
    enrich_analysis_heuristically,
    infer_shot_budget,
)
from jiuwenswarm.server.runtime.designer.script_analysis import (
    heuristic_analysis,
    resolve_cast_token_list,
)


PROMPT_3 = (
    "帮我创作一个三分镜15秒的竖屏视频。内容是一位年轻人在即将下班时突然意识到"
    "当天是情人节，不得不匆忙安排约会。第一镜他自白；第二镜用手机订餐厅；"
    "视频结尾是他和他的伴侣烛光晚餐碰杯。"
)


def test_explicit_shot_count_cn_三分镜():
    assert _explicit_clip_count_from_prompt(PROMPT_3) == 3
    assert infer_shot_budget(PROMPT_3, {}) == 3


def test_explicit_shot_count_en():
    assert _explicit_clip_count_from_prompt("Make a 3-shot vertical video") == 3
    assert _explicit_clip_count_from_prompt("four-shot sequence") == 4


def test_resolve_cast_names_to_ids():
    valid = {"char_1", "char_2"}
    by_name = {"年轻人": "char_1", "伴侣": "char_2", "char_1": "char_1", "char_2": "char_2"}
    assert resolve_cast_token_list(["年轻人"], valid_ids=valid, by_name=by_name) == ["char_1"]
    assert resolve_cast_token_list(["伴侣", "char_1"], valid_ids=valid, by_name=by_name) == [
        "char_2",
        "char_1",
    ]
    assert _cid_list(["年轻人"], valid, by_name) == ["char_1"]


def test_heuristic_respects_三分镜_ceiling():
    analysis = heuristic_analysis(PROMPT_3)
    assert int(analysis.get("target_clip_count") or 0) == 3
    assert len(analysis.get("clips") or []) <= 3


def test_heuristic_leans_to_one_shot_without_explicit_count():
    prompt = (
        "I want the video of a man standing in a church explaining something from the Bible "
        "and the crowd listened while another man gets up and leaves the church while the man "
        "at the pulpit is still speaking, this man is sitting in the front, and then the camera "
        "pans to a woman whose tears begin to flow as she nods gently agreeing with what the "
        "man is saying and a child next to her listening profusely."
    )
    analysis = heuristic_analysis(prompt)
    assert analysis.get("source") == "heuristic"
    assert int(analysis.get("target_clip_count") or 0) == 1
    shots = analysis.get("clips") or []
    assert len(shots) == 1
    shot = shots[0]
    char_ids = [str(c.get("id")) for c in (analysis.get("characters") or []) if c.get("id")]
    assert set(shot.get("on_screen") or []) == set(char_ids)
    assert set(shot.get("character_ids") or []) == set(char_ids)
    assert len(str(shot.get("clip_prompt") or "")) > 80


def test_enrich_keeps_on_screen_names_mapped():
    base = {
        "characters": [
            {"id": "char_1", "name": "年轻人", "description": "young man"},
            {"id": "char_2", "name": "伴侣", "description": "partner"},
        ],
        "clips": [
            {
                "clip_index": 1,
                "action": "年轻人意识到今天是情人节",
                "on_screen": ["年轻人"],
                "character_ids": ["年轻人"],
            },
            {
                "clip_index": 2,
                "action": "年轻人用手机订餐厅",
                "on_screen": ["年轻人"],
            },
            {
                "clip_index": 3,
                "action": "年轻人和伴侣烛光晚餐碰杯",
                "on_screen": ["年轻人", "伴侣"],
            },
        ],
        "target_clip_count": 3,
    }
    out = enrich_analysis_heuristically(PROMPT_3, base)
    assert len(out["clips"]) == 3
    assert out["clips"][0]["on_screen"] == ["char_1"]
    assert out["clips"][1]["on_screen"] == ["char_1"]
    assert set(out["clips"][2]["on_screen"]) == {"char_1", "char_2"}
    # Early beats must not pull later-meet cast just because character specs exist.
    assert "char_2" not in out["clips"][0]["on_screen"]
    assert "char_2" not in out["clips"][1]["on_screen"]


def test_smart_graph_shot_budget_ceiling():
    from jiuwenswarm.server.runtime.designer.smart_graph import _shot_budget

    analysis = {"target_clip_count": 3, "user_prompt": PROMPT_3}
    shots = [{"clip_index": i} for i in range(1, 6)]
    assert _shot_budget(analysis, shots) == 3


def test_infer_shot_budget_trusts_llm_above_four():
    """LLM-owned N must not be clamped back to the old 2–4 heuristic ceiling."""
    analysis = {
        "target_clip_count": 6,
        "clips": [{"clip_index": i} for i in range(1, 7)],
    }
    assert infer_shot_budget("A multi-beat story with several settings.", analysis) == 6


def test_normalize_llm_owns_shot_count_not_heuristic_ceiling():
    from jiuwenswarm.server.runtime.designer.script_analysis import _normalize_llm_analysis

    base = heuristic_analysis(
        "A short film about two friends walking through a market then sitting by a lake."
    )
    parsed = {
        "story_name": "Market Lake",
        "characters": [
            {"id": "char_1", "name": "Alex", "description": "blue jacket; jeans; sneakers"},
            {"id": "char_2", "name": "Sam", "description": "green coat; dark trousers; boots"},
        ],
        "target_clip_count": 5,
        "clips": [
            {
                "clip_index": i,
                "title": f"Shot {i}",
                "action": f"action {i}",
                "camera": "medium / eye-level",
                "on_screen": ["char_1", "char_2"],
                "setting_id": "set_1" if i < 4 else "set_2",
                "clip_prompt": f"frame {i}",
                "timeline": f"{(i - 1) * 3}-{i * 3}s",
            }
            for i in range(1, 6)
        ],
    }
    out = _normalize_llm_analysis(parsed, base)
    assert out is not None
    assert int(out["target_clip_count"]) == 5
    assert len(out["clips"]) == 5
