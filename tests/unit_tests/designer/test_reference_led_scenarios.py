"""Reference-led and text-film contracts.

Each scenario is a role bundle, not a scene-specific prompt. One thousand
cases per scenario vary shot count, duration, style, light, and crowd, and
check that later shots continue from the previous end without repeating it.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

try:
    import openjiuwen.core.kv_cache  # noqa: F401
except ImportError:
    _package = types.ModuleType("openjiuwen")
    _core = types.ModuleType("openjiuwen.core")
    _kv = types.ModuleType("openjiuwen.core.kv_cache")

    class KVCacheAffinityConfig:
        pass

    _kv.KVCacheAffinityConfig = KVCacheAffinityConfig
    _package.core = _core
    _core.kv_cache = _kv
    sys.modules.setdefault("openjiuwen", _package)
    sys.modules.setdefault("openjiuwen.core", _core)
    sys.modules["openjiuwen.core.kv_cache"] = _kv

import pytest

from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
    ROLE_CHARACTER,
    ROLE_MOTION,
    ROLE_PRODUCT,
    ROLE_SCENE,
    ROLE_STYLE,
    VIDEO_ANIMATE_KEYFRAME,
    VIDEO_MULTI_REF,
    compose_reference_clip_prompt,
    stamp_creative_intent,
    video_generation_overrides,
)
from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph

_ROOT = Path(__file__).resolve().parents[3]
_REFERENCE_LED = (
    _ROOT
    / "jiuwenswarm"
    / "server"
    / "runtime"
    / "designer"
    / "pipeline"
    / "reference_led.py"
)
_FORBIDDEN = ("moon cake", "月饼", "climb a wall", "as a painting")
# 40 scenario kinds × 1000 = 40000 parametric topology/packing cases (+ Fix tests).
_CASES_PER_SCENARIO = 1000
_SCENARIO_KINDS = (
    "product",
    "motion",
    "scene",
    "character",
    "text",
    "character_family",
    "character_condition_family",
    "product_cast",
    "scene_cast",
    "motion_cast",
    "id_reconcile",
    "five_stills",
    "stale_solo_flags",
    "mixed_roles",
    "two_character_stills",
    "all_cast_covered",
    "scene_condition_extra",
    "product_and_scene",
    "style_and_character",
    "motion_stale_flags",
    # video_binding / packing expansion (R1–R7)
    "story_motion_role_ignored",
    "advertise_product_story",
    "act_character_family",
    "dinner_scene_story",
    "animate_keyframe_solo",
    "animate_keyframe_cast",
    "packing_resolve_merge",
    "image_n_labels",
    "overflow_combined_cast",
    "motion_role_without_binding",
    "scene_product_cast_story",
    "character_family_no_i2v",
    "product_cast_image_labels",
    "keyframe_condition_solo",
    "keyframe_condition_cast",
    "multi_ref_default_stamp",
    "companion_edge_fallback",
    "locked_scene_with_cast",
    "style_authority_family",
    "cap_prefers_uploads_and_combined",
)


@pytest.fixture(autouse=True)
def _no_configured_audio_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.capabilities.detect_audio_backends",
        lambda: {
            "can_speech": False,
            "can_music": False,
            "can_video_audio": False,
            "video_audio_model": "",
        },
    )


def _cases(kind: str) -> list[dict]:
    rows = []
    for index in range(_CASES_PER_SCENARIO):
        shot_count = 2 + (index % 3)
        durations = [2 + ((index + shot) % 5) for shot in range(shot_count)]
        rows.append(
            {
                "id": f"{kind}-{index:04d}",
                "kind": kind,
                "index": index,
                "shot_count": shot_count,
                "durations": durations,
                "look": f"look-{index % 17}",
                "medium": f"medium-{index % 13}",
                "lighting": f"light-{index % 11}",
                "crowd": f"crowd-{index % 7}",
                "binding": "condition" if index % 2 else "verbatim",
            }
        )
    return rows


def _shots(case: dict) -> list[dict]:
    shots = []
    for shot in range(case["shot_count"]):
        action = f"action-{case['kind']}-{case['index']}-{shot}"
        shots.append(
            {
                "shot_index": shot + 1,
                "action": action,
                "end_state": f"end-{case['kind']}-{case['index']}-{shot}",
                "camera": "medium",
                "duration_sec": case["durations"][shot],
                "lighting": case["lighting"],
                "crowd": case["crowd"],
                "setting_id": "set_1",
                "character_ids": ["char_1"],
                "on_screen": ["char_1"],
            }
        )
    return shots


def _family_cast(case: dict) -> list[dict]:
    return [
        {"id": "char_1", "name": "Subject", "description": "lead person"},
        {"id": "char_2", "name": f"Companion-A-{case['index'] % 9}", "description": "family adult"},
        {"id": "char_3", "name": f"Companion-B-{case['index'] % 5}", "description": "family elder"},
    ]


def _analysis(case: dict) -> dict:
    kind = case["kind"]
    style = {"look": case["look"], "medium": case["medium"]}
    # Solo fixtures: product/scene/motion do not list incidental cast/set that
    # would mint companion sheets/plates. Character keeps a solo covered cast.
    # *_family / *_cast / id_reconcile / five_stills exercise companions + plates.
    if kind == "text":
        characters = [{"id": "char_1", "name": "Subject", "description": "a person"}]
        scenes = [{"id": "set_1", "name": "Place", "description": f"place-{case['index']}"}]
    elif kind in {
        "character",
        "character_family",
        "character_condition_family",
        "id_reconcile",
    }:
        if kind == "id_reconcile":
            characters = [
                {
                    "id": "char_1",
                    "name": "小月",
                    "description": "young woman",
                    "match_terms": ["xiaoyue", "小月"],
                },
                {
                    "id": "char_2",
                    "name": f"Companion-A-{case['index'] % 9}",
                    "description": "family adult",
                },
                {
                    "id": "char_3",
                    "name": f"Companion-B-{case['index'] % 5}",
                    "description": "family elder",
                },
            ]
        else:
            characters = (
                _family_cast(case)
                if kind.endswith("family")
                else [{"id": "char_1", "name": "Subject", "description": "a person"}]
            )
        if kind.endswith("family") or kind == "id_reconcile":
            scenes = [
                {"id": "set_1", "name": "Dining", "description": f"dining-{case['index']}"},
                {
                    "id": "set_2",
                    "name": f"Kitchen-{case['index'] % 3}",
                    "description": "kitchen",
                },
            ]
        else:
            scenes = [{"id": "set_1", "name": "Place", "description": f"place-{case['index']}"}]
    elif kind == "five_stills":
        characters = [
            {"id": f"char_{i}", "name": f"Person-{i}", "description": f"cast {i}"}
            for i in range(1, 7)
        ]
        scenes = [{"id": "set_1", "name": "Place", "description": f"place-{case['index']}"}]
    elif kind in {
        "stale_solo_flags",
        "two_character_stills",
        "all_cast_covered",
        "style_and_character",
        "motion_stale_flags",
        "mixed_roles",
    }:
        characters = _family_cast(case)
        scenes = [
            {"id": "set_1", "name": "Dining", "description": f"dining-{case['index']}"},
            {"id": "set_2", "name": "Kitchen", "description": "kitchen"},
        ]
        if kind == "motion_stale_flags":
            scenes = []
    elif kind == "scene_condition_extra":
        characters = []
        scenes = [
            {"id": "set_1", "name": "Place", "description": f"place-{case['index']}"},
            {"id": "set_2", "name": "Alt", "description": "second"},
        ]
    elif kind == "product_and_scene":
        characters = _family_cast(case)
        scenes = [
            {"id": "set_1", "name": "Store", "description": f"store-{case['index']}"},
            {"id": "set_2", "name": "Street", "description": "street"},
        ]
    elif kind == "scene":
        characters = []
        scenes = [{"id": "set_1", "name": "Place", "description": f"place-{case['index']}"}]
    elif kind == "scene_cast":
        characters = _family_cast(case)
        scenes = [
            {"id": "set_1", "name": "Place", "description": f"place-{case['index']}"},
            {"id": "set_2", "name": f"Alt-{case['index'] % 3}", "description": "second room"},
        ]
    elif kind in {
        "motion",
        "motion_cast",
        "animate_keyframe_solo",
        "animate_keyframe_cast",
        "keyframe_condition_solo",
        "keyframe_condition_cast",
        "motion_role_without_binding",
    }:
        characters = (
            _family_cast(case)
            if kind
            in {
                "motion_cast",
                "animate_keyframe_cast",
                "keyframe_condition_cast",
                "motion_role_without_binding",
            }
            else [{"id": "char_1", "name": "Subject", "description": "a person"}]
        )
        scenes = []
    elif kind in {
        "product_cast",
        "advertise_product_story",
        "product_cast_image_labels",
        "scene_product_cast_story",
    }:
        characters = _family_cast(case)
        scenes = [{"id": "set_1", "name": "Store", "description": f"store-{case['index']}"}]
        if kind == "scene_product_cast_story":
            scenes.append({"id": "set_2", "name": "Street", "description": "street"})
    elif kind in {
        "dinner_scene_story",
        "locked_scene_with_cast",
        "story_motion_role_ignored",
    }:
        characters = _family_cast(case)
        scenes = [
            {"id": "set_1", "name": "Dining", "description": f"dining-{case['index']}"},
            {"id": "set_2", "name": "Kitchen", "description": "kitchen"},
        ]
    elif kind in {
        "act_character_family",
        "character_family_no_i2v",
        "image_n_labels",
        "packing_resolve_merge",
        "overflow_combined_cast",
        "companion_edge_fallback",
        "style_authority_family",
        "cap_prefers_uploads_and_combined",
        "multi_ref_default_stamp",
    }:
        if kind == "overflow_combined_cast":
            characters = [
                {"id": f"char_{i}", "name": f"Person-{i}", "description": f"cast {i}"}
                for i in range(1, 8)
            ]
        else:
            characters = _family_cast(case)
        scenes = [
            {"id": "set_1", "name": "Dining", "description": f"dining-{case['index']}"},
            {"id": "set_2", "name": "Kitchen", "description": "kitchen"},
        ]
    else:
        # product
        characters = []
        scenes = []
    base = {
        "source": "llm",
        "style_lock": style,
        "characters": characters,
        "scenes": scenes,
        "shots": _shots(case),
        "audio": {"policy": "silent", "include_speech": False, "include_music": False},
    }
    if kind == "text":
        return base
    if kind == "five_stills":
        slots = [
            {
                "slot": i,
                "path": f"/refs/five_stills-{case['index']}-{i}.png",
                "roles": [ROLE_CHARACTER],
                "bindings": {ROLE_CHARACTER: "verbatim"},
                "character_id": f"char_{i}",
                "setting_id": "",
                "node_id": f"n_ref_{i:02d}",
            }
            for i in range(1, 6)
        ]
        base["creative_intent"] = {"mode": "reference_led", "slots": slots}
        return base
    if kind == "two_character_stills":
        slots = [
            {
                "slot": i,
                "path": f"/refs/two_character_stills-{case['index']}-{i}.png",
                "roles": [ROLE_CHARACTER],
                "bindings": {ROLE_CHARACTER: "verbatim"},
                "character_id": f"char_{i}",
                "setting_id": "",
                "node_id": f"n_ref_{i:02d}",
            }
            for i in range(1, 3)
        ]
        base["creative_intent"] = {"mode": "reference_led", "slots": slots}
        return base
    if kind == "all_cast_covered":
        slots = [
            {
                "slot": i,
                "path": f"/refs/all_cast_covered-{case['index']}-{i}.png",
                "roles": [ROLE_CHARACTER],
                "bindings": {ROLE_CHARACTER: "verbatim"},
                "character_id": f"char_{i}",
                "setting_id": "",
                "node_id": f"n_ref_{i:02d}",
            }
            for i in range(1, 4)
        ]
        base["creative_intent"] = {"mode": "reference_led", "slots": slots}
        return base
    if kind == "mixed_roles":
        slots = [
            {
                "slot": 1,
                "path": f"/refs/mixed-{case['index']}-char.png",
                "roles": [ROLE_CHARACTER],
                "bindings": {ROLE_CHARACTER: "verbatim"},
                "character_id": "char_1",
                "setting_id": "",
                "node_id": "n_ref_01",
            },
            {
                "slot": 2,
                "path": f"/refs/mixed-{case['index']}-scene.png",
                "roles": [ROLE_SCENE],
                "bindings": {ROLE_SCENE: "verbatim"},
                "character_id": "",
                "setting_id": "set_1",
                "node_id": "n_ref_02",
            },
            {
                "slot": 3,
                "path": f"/refs/mixed-{case['index']}-product.png",
                "roles": [ROLE_PRODUCT],
                "bindings": {ROLE_PRODUCT: "verbatim"},
                "character_id": "",
                "setting_id": "",
                "node_id": "n_ref_03",
            },
        ]
        base["creative_intent"] = {"mode": "reference_led", "slots": slots}
        return base
    if kind == "product_and_scene":
        slots = [
            {
                "slot": 1,
                "path": f"/refs/product_and_scene-{case['index']}-sku.png",
                "roles": [ROLE_PRODUCT],
                "bindings": {ROLE_PRODUCT: "verbatim"},
                "character_id": "",
                "setting_id": "",
                "node_id": "n_ref_01",
            },
            {
                "slot": 2,
                "path": f"/refs/product_and_scene-{case['index']}-room.png",
                "roles": [ROLE_SCENE],
                "bindings": {ROLE_SCENE: "verbatim"},
                "character_id": "",
                "setting_id": "set_1",
                "node_id": "n_ref_02",
            },
        ]
        base["creative_intent"] = {"mode": "reference_led", "slots": slots}
        return base
    if kind == "style_and_character":
        slots = [
            {
                "slot": 1,
                "path": f"/refs/style_and_character-{case['index']}-char.png",
                "roles": [ROLE_CHARACTER],
                "bindings": {ROLE_CHARACTER: "verbatim"},
                "character_id": "char_1",
                "setting_id": "",
                "node_id": "n_ref_01",
            },
            {
                "slot": 2,
                "path": f"/refs/style_and_character-{case['index']}-style.png",
                "roles": [ROLE_STYLE],
                "bindings": {ROLE_STYLE: "verbatim"},
                "character_id": "",
                "setting_id": "",
                "node_id": "n_ref_02",
            },
        ]
        base["creative_intent"] = {"mode": "reference_led", "slots": slots}
        return base
    if kind == "cap_prefers_uploads_and_combined":
        slots = [
            {
                "slot": i,
                "path": f"/refs/cap-{case['index']}-{i}.png",
                "roles": [ROLE_CHARACTER],
                "bindings": {ROLE_CHARACTER: "verbatim"},
                "character_id": f"char_{i}",
                "setting_id": "",
                "node_id": f"n_ref_{i:02d}",
            }
            for i in range(1, 5)
        ]
        base["creative_intent"] = {
            "mode": "reference_led",
            "video_binding": VIDEO_MULTI_REF,
            "slots": slots,
        }
        return base
    if kind == "scene_product_cast_story":
        slots = [
            {
                "slot": 1,
                "path": f"/refs/spc-{case['index']}-sku.png",
                "roles": [ROLE_PRODUCT],
                "bindings": {ROLE_PRODUCT: "verbatim"},
                "character_id": "",
                "setting_id": "",
                "node_id": "n_ref_01",
            },
            {
                "slot": 2,
                "path": f"/refs/spc-{case['index']}-room.png",
                "roles": [ROLE_SCENE],
                "bindings": {ROLE_SCENE: "verbatim"},
                "character_id": "",
                "setting_id": "set_1",
                "node_id": "n_ref_02",
                "set_lock": True,
            },
        ]
        base["creative_intent"] = {
            "mode": "reference_led",
            "video_binding": VIDEO_MULTI_REF,
            "slots": slots,
        }
        return base

    path = f"/refs/{kind}-{case['index']}.png"
    video_binding = VIDEO_MULTI_REF
    set_lock = False
    style_authority = False
    if kind in {
        "product",
        "product_cast",
        "advertise_product_story",
        "product_cast_image_labels",
    }:
        roles = [ROLE_PRODUCT]
        binding = "verbatim"
        character_id = ""
        setting_id = ""
    elif kind in {
        "motion",
        "motion_cast",
        "animate_keyframe_solo",
        "animate_keyframe_cast",
        "keyframe_condition_solo",
        "keyframe_condition_cast",
        "motion_stale_flags",
    }:
        roles = [ROLE_MOTION]
        binding = (
            "condition"
            if kind in {"keyframe_condition_solo", "keyframe_condition_cast"}
            else case["binding"]
            if kind in {"motion", "motion_cast"}
            else "verbatim"
        )
        character_id = "char_1"
        setting_id = ""
        video_binding = VIDEO_ANIMATE_KEYFRAME
    elif kind == "motion_role_without_binding":
        # ROLE_MOTION present but no video_binding → must stay multi_ref / R2V.
        roles = [ROLE_MOTION]
        binding = "verbatim"
        character_id = "char_1"
        setting_id = ""
        video_binding = ""  # unset → default multi_ref_story
    elif kind in {
        "scene",
        "scene_cast",
        "dinner_scene_story",
        "locked_scene_with_cast",
    }:
        roles = [ROLE_SCENE]
        binding = "verbatim"
        character_id = ""
        setting_id = "set_1"
        set_lock = kind in {"dinner_scene_story", "locked_scene_with_cast"}
    elif kind == "story_motion_role_ignored":
        # Dinner bug class: scene + motion roles must NOT force I2V.
        roles = [ROLE_SCENE, ROLE_MOTION]
        binding = "verbatim"
        character_id = ""
        setting_id = "set_1"
        set_lock = True
        video_binding = VIDEO_MULTI_REF
    elif kind == "character_condition_family":
        roles = [ROLE_CHARACTER]
        binding = "condition"
        character_id = "char_1"
        setting_id = ""
    elif kind == "id_reconcile":
        roles = [ROLE_CHARACTER]
        binding = "verbatim"
        character_id = "xiaoyue"
        setting_id = ""
    elif kind == "scene_condition_extra":
        roles = [ROLE_SCENE]
        binding = "condition"
        character_id = ""
        setting_id = "set_1"
    elif kind == "stale_solo_flags":
        roles = [ROLE_CHARACTER]
        binding = "verbatim"
        character_id = "char_1"
        setting_id = ""
    else:
        # character / character_family / act_* / packing_* — default use-as-is
        roles = [ROLE_CHARACTER]
        binding = "verbatim"
        character_id = "char_1"
        setting_id = ""
        style_authority = False
    bindings = {role: binding for role in roles}
    slot: dict = {
        "slot": 1,
        "path": path,
        "roles": roles,
        "bindings": bindings,
        "character_id": character_id,
        "setting_id": setting_id,
        "node_id": "n_ref_01",
    }
    if set_lock:
        slot["set_lock"] = True
    if style_authority:
        slot["style_authority"] = True
    intent: dict = {"mode": "reference_led", "slots": [slot]}
    if video_binding:
        intent["video_binding"] = video_binding
    base["creative_intent"] = intent
    if kind in {"stale_solo_flags", "motion_stale_flags"}:
        # Leftover classify flags must NOT wipe companions (WI-1: flags unused).
        base["creative_intent"]["solo_subject"] = True
        base["creative_intent"]["suppress_companions"] = True
        base["creative_intent"]["keyframe_complete"] = True
        slot["solo_subject"] = True
    return base


def _graph(case: dict) -> dict:
    return build_smart_video_graph(
        project_id=f"proj_{case['id']}",
        prompt=f"reference case {case['id']}",
        analysis=_analysis(case),
    )


def _pipeline(node: dict) -> str:
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    return str(cfg.get("pipeline") or cfg.get("role") or "")


def _clips(graph: dict) -> list[dict]:
    clips = [node for node in graph.get("nodes") or [] if _pipeline(node) == "clip"]
    return sorted(clips, key=lambda node: int(node["config"]["shot_index"]))


def _tasks(graph: dict, task: str) -> list[dict]:
    found = []
    for node in graph.get("nodes") or []:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        if str(cfg.get("reference_still_task") or "") == task:
            found.append(node)
    return found


def _scene_nodes(graph: dict) -> list[dict]:
    return [node for node in graph.get("nodes") or [] if _pipeline(node) == "scene"]


def _assert_shared_contract(case: dict, graph: dict) -> None:
    clips = _clips(graph)
    assert len(clips) == case["shot_count"]
    actions = []
    cursor = 0.0
    total = 0
    for clip, duration in zip(clips, case["durations"], strict=True):
        cfg = clip["config"]
        prompt = str((cfg.get("generate") or {}).get("prompt") or "")
        action = str(cfg.get("shot_action") or "")
        actions.append(action)
        assert action in prompt
        assert case["lighting"] in prompt
        assert case["crowd"] in prompt
        # set_lock / style_authority may rewrite the film lock to match_reference_still.
        from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
            MATCH_REFERENCE_LOOK,
            MATCH_REFERENCE_MEDIUM,
        )

        if cfg["style_lock"].get("medium") == MATCH_REFERENCE_MEDIUM:
            assert MATCH_REFERENCE_LOOK in prompt or "match the rendering" in prompt.lower()
        else:
            assert case["look"] in prompt
            assert cfg["style_lock"]["look"] == case["look"]
            assert cfg["style_lock"]["medium"] == case["medium"]
        assert cfg["lighting"] == case["lighting"]
        assert cfg["crowd"] == case["crowd"]
        assert int(cfg["duration_sec"]) == duration
        start, end = str(cfg["timeline"]).replace("s", "").split("-")
        assert float(start) == pytest.approx(cursor)
        assert float(end) == pytest.approx(cursor + duration)
        cursor += duration
        total += duration
        index = int(cfg["shot_index"])
        if index == 1:
            assert "continues from" not in prompt.lower()
        else:
            assert "continues from" in prompt.lower()
            assert cfg["previous_end_state"] in prompt
            assert f"Action: {cfg['previous_action']}" not in prompt
            assert cfg["previous_action"] in cfg["already_done"]
            assert action != cfg["previous_action"]
    assert len(set(actions)) == len(actions)
    assert int(graph["metadata"]["film_duration_sec"]) == total


def _companion_sheets(graph: dict) -> list[dict]:
    found = []
    for node in graph.get("nodes") or []:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        if cfg.get("companion_cast") is True:
            found.append(node)
    return found


def _assert_reference_case(case: dict) -> None:
    graph = _graph(case)
    _assert_shared_contract(case, graph)
    ref = next(node for node in graph["nodes"] if node["id"] == "n_ref_01")
    assert ref["config"].get("user_reference_id")
    assert ref["config"].get("immutable_source") is True
    clips = _clips(graph)
    kind = case["kind"]
    if kind == "product":
        assert not _tasks(graph, "identity_sheet")
        assert not _tasks(graph, "medium_change")
        assert not _scene_nodes(graph)
        for clip in clips:
            cfg = clip["config"]
            assert cfg["reference_call_mode"] == "r2v"
            assert cfg["reference_prompt_contract"] == "product"
            assert cfg["reference_image_plan"][0]["role"] == ROLE_PRODUCT
            assert "Image 1" in cfg["generate"]["prompt"]
    elif kind == "product_cast":
        # Product still covers no cast ids → all analysis characters are companions.
        companions = _companion_sheets(graph)
        assert len(companions) == 3
        assert all(c["config"]["style_lock"]["medium"] == case["medium"] for c in companions)
        assert len(_scene_nodes(graph)) == 1  # uncovered store plate
        for clip in clips:
            cfg = clip["config"]
            assert cfg["reference_call_mode"] == "r2v"
            assert cfg["reference_image_plan"][0]["role"] == ROLE_PRODUCT
    elif kind == "motion":
        assert not _tasks(graph, "identity_sheet")
        assert not _scene_nodes(graph)
        for clip in clips:
            cfg = clip["config"]
            assert cfg["reference_call_mode"] == "i2v"
            assert "first frame" in cfg["generate"]["prompt"].lower()
        if case["binding"] == "condition":
            assert len(_tasks(graph, "medium_change")) == 1
            assert clips[0]["config"]["reference_first_frame_node"] == "n_restyle_01"
            assert case["medium"] in _tasks(graph, "medium_change")[0]["config"]["prompt"]
        else:
            assert not _tasks(graph, "medium_change")
            assert clips[0]["config"]["reference_first_frame"].endswith(
                f"motion-{case['index']}.png"
            )
    elif kind == "motion_cast":
        # animate_keyframe + extra cast → R2V so companions pack as refs.
        for clip in clips:
            assert clip["config"]["reference_call_mode"] == "r2v"
            assert clip["config"].get("reference_video_binding") == VIDEO_ANIMATE_KEYFRAME
            plan = clip["config"]["reference_image_plan"]
            assert plan[0]["role"] == ROLE_MOTION
            assert any(e["node_id"].startswith("n_character_") for e in plan)
        companions = _companion_sheets(graph)
        assert len(companions) == 2
        assert all(c["config"]["style_lock"]["medium"] == case["medium"] for c in companions)
        assert not _scene_nodes(graph)
    elif kind == "scene":
        assert not _tasks(graph, "identity_sheet")
        assert not _scene_nodes(graph)
        for clip in clips:
            cfg = clip["config"]
            plan = cfg["reference_image_plan"]
            assert cfg["reference_call_mode"] == "r2v"
            assert cfg["reference_prompt_contract"] == "scene"
            assert plan[-1]["role"] == ROLE_SCENE
            assert plan[-1]["path"].endswith(f"scene-{case['index']}.png")
            assert "last reference" in cfg["generate"]["prompt"].lower()
    elif kind == "scene_cast":
        # Scene still covers set_1 only; all three analysis characters are companions.
        companions = _companion_sheets(graph)
        assert len(companions) == 3
        # Locked set_1 stays upload; uncovered set_2 becomes a plate.
        assert len(_scene_nodes(graph)) == 1
        assert _scene_nodes(graph)[0]["config"]["setting_id"] == "set_2"
        for clip in clips:
            plan = clip["config"]["reference_image_plan"]
            assert any(e["node_id"] == "n_ref_01" and e["role"] == ROLE_SCENE for e in plan)
    elif kind == "character_family":
        assert ref["config"].get("reference_card_role") == "character_design"
        companions = _companion_sheets(graph)
        assert len(companions) == 2
        assert len(_scene_nodes(graph)) >= 1
        for clip in clips:
            plan = clip["config"]["reference_image_plan"]
            assert plan[0]["node_id"] == "n_ref_01"
            assert plan[0]["path"].endswith(f"character_family-{case['index']}.png")
            sheet_ids = [e["node_id"] for e in plan if e["node_id"].startswith("n_character_")]
            assert len(sheet_ids) == 2
    elif kind == "character_condition_family":
        assert "reference_card_role" not in ref["config"]
        sheets = _tasks(graph, "identity_sheet")
        assert len(sheets) == 3  # self condition sheet + 2 companions
        companions = _companion_sheets(graph)
        assert len(companions) == 2
        assert len(_scene_nodes(graph)) >= 1
        for clip in clips:
            plan = clip["config"]["reference_image_plan"]
            assert plan[0]["node_id"].startswith("n_character_")
            assert plan[0]["path"] == ""
    elif kind == "id_reconcile":
        companions = _companion_sheets(graph)
        assert len(companions) == 2
        ids = {str(n["config"].get("character_id") or "") for n in companions}
        assert ids == {"char_2", "char_3"}
        assert not any(n["config"].get("character_id") in {"xiaoyue", "char_1"} for n in companions)
        assert len(_scene_nodes(graph)) >= 1
        slot = graph["metadata"]["script_analysis"]["creative_intent"]["slots"][0]
        assert slot["character_id"] == "char_1"
    elif kind == "five_stills":
        companions = _companion_sheets(graph)
        assert len(companions) == 1
        assert companions[0]["config"]["character_id"] == "char_6"
        assert len(_scene_nodes(graph)) >= 1
        for clip in clips:
            plan = clip["config"]["reference_image_plan"]
            upload_ids = {e["node_id"] for e in plan if e["path"]}
            assert upload_ids == {f"n_ref_{i:02d}" for i in range(1, 6)}
            assert len(plan) <= 5
    elif kind == "stale_solo_flags":
        companions = _companion_sheets(graph)
        assert len(companions) == 2
        assert len(_scene_nodes(graph)) >= 1
        assert ref["config"].get("reference_card_role") == "character_design"
    elif kind == "motion_stale_flags":
        companions = _companion_sheets(graph)
        assert len(companions) == 2
        assert not _scene_nodes(graph)
        for clip in clips:
            assert clip["config"]["reference_call_mode"] == "r2v"
            assert clip["config"].get("reference_video_binding") == VIDEO_ANIMATE_KEYFRAME
    elif kind in {
        "story_motion_role_ignored",
        "dinner_scene_story",
        "locked_scene_with_cast",
    }:
        companions = _companion_sheets(graph)
        assert len(companions) == 3
        for clip in clips:
            cfg = clip["config"]
            assert cfg["reference_call_mode"] == "r2v"
            assert cfg.get("reference_video_binding") == VIDEO_MULTI_REF
            assert any(e["node_id"] == "n_ref_01" for e in cfg["reference_image_plan"])
            assert "Image 1" in cfg["generate"]["prompt"]
    elif kind in {
        "advertise_product_story",
        "product_cast_image_labels",
    }:
        companions = _companion_sheets(graph)
        assert len(companions) == 3
        for clip in clips:
            cfg = clip["config"]
            assert cfg["reference_call_mode"] == "r2v"
            assert cfg["reference_image_plan"][0]["role"] == ROLE_PRODUCT
            assert "Image 1" in cfg["generate"]["prompt"]
    elif kind in {
        "act_character_family",
        "character_family_no_i2v",
        "image_n_labels",
        "packing_resolve_merge",
        "companion_edge_fallback",
        "style_authority_family",
        "multi_ref_default_stamp",
    }:
        companions = _companion_sheets(graph)
        assert len(companions) == 2
        for clip in clips:
            cfg = clip["config"]
            assert cfg["reference_call_mode"] == "r2v"
            assert cfg.get("reference_video_binding") == VIDEO_MULTI_REF
            prompt = cfg["generate"]["prompt"]
            assert "Image 1" in prompt
            assert "Image 2" in prompt
    elif kind == "animate_keyframe_solo":
        for clip in clips:
            assert clip["config"]["reference_call_mode"] == "i2v"
            assert "first frame" in clip["config"]["generate"]["prompt"].lower()
        assert _companion_sheets(graph) == []
    elif kind == "animate_keyframe_cast":
        companions = _companion_sheets(graph)
        assert len(companions) == 2
        for clip in clips:
            assert clip["config"]["reference_call_mode"] == "r2v"
            plan = clip["config"]["reference_image_plan"]
            assert plan[0]["role"] == ROLE_MOTION
            assert any(e["node_id"].startswith("n_character_") for e in plan)
    elif kind == "keyframe_condition_solo":
        assert len(_tasks(graph, "medium_change")) == 1
        for clip in clips:
            assert clip["config"]["reference_call_mode"] == "i2v"
            assert clip["config"]["reference_first_frame_node"] == "n_restyle_01"
    elif kind == "keyframe_condition_cast":
        companions = _companion_sheets(graph)
        assert len(companions) == 2
        assert len(_tasks(graph, "medium_change")) == 1
        for clip in clips:
            assert clip["config"]["reference_call_mode"] == "r2v"
    elif kind == "motion_role_without_binding":
        companions = _companion_sheets(graph)
        assert len(companions) == 2
        for clip in clips:
            assert clip["config"]["reference_call_mode"] == "r2v"
            assert clip["config"].get("reference_video_binding") == VIDEO_MULTI_REF
    elif kind == "overflow_combined_cast":
        combined = [
            n
            for n in _companion_sheets(graph)
            if n["config"].get("combined_cast") is True
        ]
        assert combined, "expected a combined secondary cast card under Wan cap"
        for clip in clips:
            plan = clip["config"]["reference_image_plan"]
            assert len(plan) <= 5
            assert any(e["node_id"] == combined[0]["id"] for e in plan)
    elif kind == "scene_product_cast_story":
        companions = _companion_sheets(graph)
        # Product+scene+plate reserve 3 slots → leftover cast may be combined.
        assert len(companions) >= 2
        assert any(c["config"].get("combined_cast") for c in companions) or len(companions) == 3
        for clip in clips:
            assert clip["config"]["reference_call_mode"] == "r2v"
            roles = {e["role"] for e in clip["config"]["reference_image_plan"]}
            assert ROLE_PRODUCT in roles
            assert ROLE_SCENE in roles
    elif kind == "cap_prefers_uploads_and_combined":
        for clip in clips:
            plan = clip["config"]["reference_image_plan"]
            assert len(plan) <= 5
            upload_ids = {e["node_id"] for e in plan if e.get("path")}
            assert upload_ids <= {f"n_ref_{i:02d}" for i in range(1, 5)}
    elif kind == "two_character_stills":
        companions = _companion_sheets(graph)
        assert len(companions) == 1
        assert companions[0]["config"]["character_id"] == "char_3"
        assert len(_scene_nodes(graph)) >= 1
        ids = {n["id"] for n in graph["nodes"] if n["id"].startswith("n_ref_")}
        assert {"n_ref_01", "n_ref_02"} <= ids
    elif kind == "all_cast_covered":
        assert _companion_sheets(graph) == []
        assert len(_scene_nodes(graph)) >= 1
        ids = {n["id"] for n in graph["nodes"] if n["id"].startswith("n_ref_")}
        assert ids == {"n_ref_01", "n_ref_02", "n_ref_03"}
    elif kind == "mixed_roles":
        companions = _companion_sheets(graph)
        # Product+char+scene uploads + plate leave little room → secondaries combine.
        assert len(companions) >= 1
        assert any(c["config"].get("combined_cast") for c in companions) or len(companions) == 2
        assert len(_scene_nodes(graph)) == 1
        assert _scene_nodes(graph)[0]["config"]["setting_id"] == "set_2"
        for clip in clips:
            plan = clip["config"]["reference_image_plan"]
            assert plan[0]["role"] == ROLE_PRODUCT
            assert any(e["node_id"] == "n_ref_01" and e["role"] == ROLE_CHARACTER for e in plan)
    elif kind == "product_and_scene":
        companions = _companion_sheets(graph)
        assert len(companions) >= 2
        assert any(c["config"].get("combined_cast") for c in companions) or len(companions) == 3
        assert len(_scene_nodes(graph)) == 1
        assert _scene_nodes(graph)[0]["config"]["setting_id"] == "set_2"
        for clip in clips:
            plan = clip["config"]["reference_image_plan"]
            assert plan[0]["role"] == ROLE_PRODUCT
    elif kind == "style_and_character":
        companions = _companion_sheets(graph)
        assert len(companions) == 2
        assert len(_scene_nodes(graph)) >= 1
        assert ref["config"].get("reference_card_role") == "character_design"
        assert any(n["id"] == "n_ref_02" for n in graph["nodes"])
    elif kind == "scene_condition_extra":
        assert len(_scene_nodes(graph)) >= 1
        setting_ids = {n["config"].get("setting_id") for n in _scene_nodes(graph)}
        assert "set_1" in setting_ids or "set_2" in setting_ids
        for clip in clips:
            assert clip["config"]["reference_call_mode"] == "r2v"
    else:
        # Default character path: upload card as-is, no sheet; uncovered set is plated.
        assert not _tasks(graph, "identity_sheet")
        assert len(_scene_nodes(graph)) >= 1
        ref = next(node for node in graph["nodes"] if node["id"] == "n_ref_01")
        assert ref["config"].get("reference_card_role") == "character_design"
        for clip in clips:
            cfg = clip["config"]
            plan = cfg["reference_image_plan"]
            roles = [item["role"] for item in plan]
            assert cfg["reference_call_mode"] == "r2v"
            assert cfg["reference_prompt_contract"] == "character"
            assert roles[0] == ROLE_CHARACTER
            assert plan[0]["node_id"] == "n_ref_01"
            assert plan[0]["path"].endswith(f"character-{case['index']}.png")
            assert ROLE_SCENE in roles
            assert "Image 1" in cfg["generate"]["prompt"]


def _assert_text_case(case: dict) -> None:
    graph = _graph(case)
    clips = _clips(graph)
    assert len(clips) == case["shot_count"]
    assert any(_pipeline(node) == "character_design" for node in graph["nodes"])
    assert _scene_nodes(graph)
    actions = []
    for clip in clips:
        cfg = clip["config"]
        assert "reference_call_mode" not in cfg
        assert cfg["style_lock"]["look"] == case["look"]
        actions.append(cfg["shot_action"])
    assert len(set(actions)) == len(actions)
    assert clips[-1]["config"].get("first_of_setting") is False
    intent = (graph["metadata"].get("script_analysis") or {}).get("creative_intent")
    assert not intent or intent.get("mode") != "reference_led"
    overrides = video_generation_overrides(clips[0]["config"], graph, ["fallback.png"])
    assert overrides["force_reference_mode"] is True
    assert overrides["first_frame"] is None
    assert overrides["reference_images"] == ["fallback.png"]


@pytest.mark.parametrize("case", _cases("product"), ids=lambda case: case["id"])
def test_product_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("motion"), ids=lambda case: case["id"])
def test_motion_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("scene"), ids=lambda case: case["id"])
def test_scene_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("character"), ids=lambda case: case["id"])
def test_character_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("text"), ids=lambda case: case["id"])
def test_text_film_scenario(case: dict) -> None:
    _assert_text_case(case)


@pytest.mark.parametrize("case", _cases("character_family"), ids=lambda case: case["id"])
def test_character_family_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize(
    "case", _cases("character_condition_family"), ids=lambda case: case["id"]
)
def test_character_condition_family_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("product_cast"), ids=lambda case: case["id"])
def test_product_cast_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("scene_cast"), ids=lambda case: case["id"])
def test_scene_cast_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("motion_cast"), ids=lambda case: case["id"])
def test_motion_cast_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("id_reconcile"), ids=lambda case: case["id"])
def test_id_reconcile_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("five_stills"), ids=lambda case: case["id"])
def test_five_stills_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("stale_solo_flags"), ids=lambda case: case["id"])
def test_stale_solo_flags_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("mixed_roles"), ids=lambda case: case["id"])
def test_mixed_roles_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("two_character_stills"), ids=lambda case: case["id"])
def test_two_character_stills_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("all_cast_covered"), ids=lambda case: case["id"])
def test_all_cast_covered_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("scene_condition_extra"), ids=lambda case: case["id"])
def test_scene_condition_extra_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("product_and_scene"), ids=lambda case: case["id"])
def test_product_and_scene_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("style_and_character"), ids=lambda case: case["id"])
def test_style_and_character_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize("case", _cases("motion_stale_flags"), ids=lambda case: case["id"])
def test_motion_stale_flags_scenario(case: dict) -> None:
    _assert_reference_case(case)


@pytest.mark.parametrize(
    "kind",
    [
        "story_motion_role_ignored",
        "advertise_product_story",
        "act_character_family",
        "dinner_scene_story",
        "animate_keyframe_solo",
        "animate_keyframe_cast",
        "packing_resolve_merge",
        "image_n_labels",
        "overflow_combined_cast",
        "motion_role_without_binding",
        "scene_product_cast_story",
        "character_family_no_i2v",
        "product_cast_image_labels",
        "keyframe_condition_solo",
        "keyframe_condition_cast",
        "multi_ref_default_stamp",
        "companion_edge_fallback",
        "locked_scene_with_cast",
        "style_authority_family",
        "cap_prefers_uploads_and_combined",
    ],
)
@pytest.mark.parametrize("case", range(_CASES_PER_SCENARIO), ids=lambda i: f"idx-{i:04d}")
def test_video_binding_packing_scenarios(kind: str, case: int) -> None:
    row = _cases(kind)[case]
    _assert_reference_case(row)


def test_reference_led_source_has_no_scene_specific_rules() -> None:
    text = _REFERENCE_LED.read_text(encoding="utf-8").lower()
    for phrase in _FORBIDDEN:
        assert phrase not in text


def test_text_call_shape_is_unchanged_without_a_mode() -> None:
    overrides = video_generation_overrides({}, {}, ["a.png"])
    assert overrides == {
        "first_frame": None,
        "reference_images": ["a.png"],
        "force_reference_mode": True,
    }


def _chat_edit(graph: dict, candidate: dict) -> dict:
    from jiuwenswarm.server.runtime.designer.chat_document_sync import prepare_document_update

    edited, _texts, changed = prepare_document_update(graph, candidate, {}, [])
    assert changed
    return edited


def _remove_node(graph: dict, node_id: str) -> None:
    graph["nodes"] = [node for node in graph["nodes"] if node["id"] != node_id]
    graph["edges"] = [
        edge for edge in graph["edges"] if node_id not in {edge["source"], edge["target"]}
    ]


def test_chat_action_edit_refreshes_reference_led_handoff() -> None:
    from copy import deepcopy

    graph = _graph(_cases("character")[0])
    candidate = deepcopy(graph)
    first = _clips(candidate)[0]["config"]
    first["shot_action"] = "new opening action"
    first["end_state"] = "new opening end"

    edited = _chat_edit(graph, candidate)

    second = _clips(edited)[1]["config"]
    assert second["previous_action"] == "new opening action"
    assert second["previous_end_state"] == "new opening end"
    assert second["already_done"] == ["new opening action"]
    assert "new opening end" in second["generate"]["prompt"]
    assert "new opening action" in _clips(edited)[0]["config"]["generate"]["prompt"]


def test_chat_action_edit_keeps_default_end_state_in_step() -> None:
    from copy import deepcopy

    graph = _graph(_cases("product")[0])
    first = _clips(graph)[0]["config"]
    first["end_state"] = f"completed: {first['shot_action']}"
    candidate = deepcopy(graph)
    _clips(candidate)[0]["config"]["shot_action"] = "new opening action"

    edited = _chat_edit(graph, candidate)

    assert _clips(edited)[0]["config"]["end_state"] == "completed: new opening action"
    assert _clips(edited)[1]["config"]["previous_end_state"] == "completed: new opening action"


def test_chat_shot_removal_rechains_reference_led_clips() -> None:
    from copy import deepcopy

    case = next(case for case in _cases("product") if case["shot_count"] == 3)
    graph = _graph(case)
    candidate = deepcopy(graph)
    first, middle, last = _clips(candidate)
    _remove_node(candidate, middle["id"])
    last["config"]["shot_index"] = 2

    edited = _chat_edit(graph, candidate)

    cfg = _clips(edited)[1]["config"]
    assert cfg["previous_action"] == first["config"]["shot_action"]
    assert cfg["previous_end_state"] == first["config"]["end_state"]
    assert cfg["already_done"] == [first["config"]["shot_action"]]
    assert cfg["generate"]["prompt"].startswith("Shot 2,")


def test_chat_cannot_remove_a_still_a_reference_led_clip_uses() -> None:
    from copy import deepcopy

    from jiuwenswarm.common.schema.designer_graph import DesignerGraphValidationError

    # Default character is use-as-is (no sheet). Exercise the restyle path that
    # still builds an identity_sheet the clip depends on.
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="condition", character_id="char_1")]
    )
    candidate = deepcopy(graph)
    sheet_id = _tasks(candidate, "identity_sheet")[0]["id"]
    _remove_node(candidate, sheet_id)

    with pytest.raises(DesignerGraphValidationError, match=sheet_id):
        _chat_edit(graph, candidate)


@pytest.mark.asyncio
async def test_director_does_not_rebuild_a_reference_led_graph() -> None:
    from jiuwenswarm.server.runtime.designer.orchestration import Director

    case = _cases("product")[0]
    graph = _graph(case)
    graph_id = graph["graph_id"]
    ack = await Director().design_execution_graph(graph)
    assert ack["source"] == "reference_led"
    assert graph["graph_id"] == graph_id
    assert _clips(graph)


# ---------------------------------------------------------------------------
# Intent-driven topology: inject the classify JSON the LLM would return and
# assert graph structure. No production code reads user-prompt phrases; the
# bindings / set_lock / style_authority / medium fields alone drive these.
# ---------------------------------------------------------------------------


def _base_analysis(
    slots: list[dict],
    *,
    style_lock: dict | None = None,
    characters: list[dict] | None = None,
    scenes: list[dict] | None = None,
    video_binding: str | None = None,
) -> dict:
    intent: dict = {"mode": "reference_led", "slots": slots}
    if video_binding:
        intent["video_binding"] = video_binding
    return {
        "source": "llm",
        "style_lock": style_lock if style_lock is not None else {"look": "base-look", "medium": "base-medium"},
        "characters": (
            characters
            if characters is not None
            else [{"id": "char_1", "name": "Subject", "description": "a person"}]
        ),
        "scenes": (
            scenes
            if scenes is not None
            else [{"id": "set_1", "name": "Place", "description": "a-room"}]
        ),
        "shots": [
            {
                "shot_index": 1,
                "action": "beat-one",
                "end_state": "end-one",
                "camera": "medium",
                "duration_sec": 4,
                "setting_id": "set_1",
            },
            {
                "shot_index": 2,
                "action": "beat-two",
                "end_state": "end-two",
                "camera": "medium",
                "duration_sec": 4,
                "setting_id": "set_1",
            },
        ],
        "audio": {"policy": "silent", "include_speech": False, "include_music": False},
        "creative_intent": intent,
    }


def _slot(
    roles: list[str],
    *,
    binding: str | None = None,
    bindings: dict | None = None,
    path: str = "/refs/upload.png",
    node_id: str = "n_ref_01",
    slot: int = 1,
    character_id: str = "",
    setting_id: str = "",
    **extra,
) -> dict:
    entry: dict = {"slot": slot, "path": path, "roles": list(roles), "node_id": node_id}
    if bindings is not None:
        entry["bindings"] = bindings
    elif binding is not None:
        entry["bindings"] = {role: binding for role in roles}
    if character_id:
        entry["character_id"] = character_id
    if setting_id:
        entry["setting_id"] = setting_id
    entry.update(extra)
    return entry


def _intent_graph(
    slots: list[dict],
    *,
    style_lock: dict | None = None,
    characters: list[dict] | None = None,
    scenes: list[dict] | None = None,
    video_binding: str | None = None,
) -> dict:
    return build_smart_video_graph(
        project_id="proj_intent",
        prompt="reference intent case",
        analysis=_base_analysis(
            slots,
            style_lock=style_lock,
            characters=characters,
            scenes=scenes,
            video_binding=video_binding,
        ),
    )


def _node(graph: dict, node_id: str) -> dict:
    return next(node for node in graph["nodes"] if node["id"] == node_id)


# --- Fix 1: character verbatim skips the identity sheet ---------------------


def test_character_verbatim_fills_card_and_skips_sheet() -> None:
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="char_1")],
        scenes=[],
    )
    assert not _tasks(graph, "identity_sheet")
    assert not _scene_nodes(graph)
    ref = _node(graph, "n_ref_01")
    assert ref["config"]["reference_card_role"] == "character_design"
    assert ref["output_ref"]["uri"].endswith("upload.png")
    for clip in _clips(graph):
        cfg = clip["config"]
        assert cfg["reference_prompt_contract"] == "character"
        char_entries = [e for e in cfg["reference_image_plan"] if e["role"] == ROLE_CHARACTER]
        assert char_entries
        assert char_entries[0]["node_id"] == "n_ref_01"
        assert char_entries[0]["path"].endswith("upload.png")
        assert not any(e["role"] == ROLE_SCENE for e in cfg["reference_image_plan"])


def test_character_omitted_binding_defaults_to_verbatim() -> None:
    """Missing / invalid binding must not invent a sheet or plate."""
    from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
        BINDING_VERBATIM,
        ROLE_CHARACTER,
        _bindings_for,
    )

    bindings = _bindings_for({"roles": [ROLE_CHARACTER]}, [ROLE_CHARACTER])
    assert bindings[ROLE_CHARACTER] == BINDING_VERBATIM

    graph = _intent_graph(
        [
            {
                "slot": 1,
                "path": "/refs/upload.png",
                "roles": [ROLE_CHARACTER],
                "bindings": {},
                "character_id": "char_1",
                "setting_id": "",
                "node_id": "n_ref_01",
            }
        ],
        scenes=[],
    )
    assert not _tasks(graph, "identity_sheet")
    assert not _scene_nodes(graph)
    ref = _node(graph, "n_ref_01")
    assert ref["config"]["reference_card_role"] == "character_design"


def test_character_condition_still_builds_identity_sheet() -> None:
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="condition", character_id="char_1")],
        scenes=[],
    )
    assert len(_tasks(graph, "identity_sheet")) == 1
    # Restyle character alone must not invent a text set plate.
    assert not _scene_nodes(graph)
    ref = _node(graph, "n_ref_01")
    assert "reference_card_role" not in ref["config"]
    for clip in _clips(graph):
        char_entries = [
            e for e in clip["config"]["reference_image_plan"] if e["role"] == ROLE_CHARACTER
        ]
        assert char_entries and char_entries[0]["node_id"].startswith("n_character_")
        assert char_entries[0]["path"] == ""
        assert not any(e["role"] == ROLE_SCENE for e in clip["config"]["reference_image_plan"])


def test_scene_condition_without_lock_may_invent_plate() -> None:
    """Only an explicit scene restyle invents a T2I plate."""
    graph = _intent_graph(
        [
            _slot(
                [ROLE_CHARACTER],
                binding="verbatim",
                character_id="char_1",
                slot=1,
                node_id="n_ref_01",
                path="/refs/person.png",
            ),
            _slot(
                [ROLE_SCENE],
                binding="condition",
                setting_id="set_1",
                slot=2,
                node_id="n_ref_02",
                path="/refs/room.png",
            ),
        ]
    )
    assert not _tasks(graph, "identity_sheet")
    assert len(_scene_nodes(graph)) == 1
    for clip in _clips(graph):
        scene_entries = [
            e for e in clip["config"]["reference_image_plan"] if e["role"] == ROLE_SCENE
        ]
        assert scene_entries and scene_entries[-1]["node_id"].startswith("n_scene_")
        assert scene_entries[-1]["path"] == ""


# --- Fix 3: set_lock / scene verbatim locks the set, no invented plate ------


def test_scene_set_lock_locks_set_and_skips_plate() -> None:
    graph = _intent_graph(
        [
            _slot(
                [ROLE_CHARACTER],
                binding="condition",
                character_id="char_1",
                slot=1,
                node_id="n_ref_01",
                path="/refs/person.png",
            ),
            _slot(
                [ROLE_SCENE],
                binding="condition",
                setting_id="set_1",
                slot=2,
                node_id="n_ref_02",
                path="/refs/room.png",
                set_lock=True,
            ),
        ]
    )
    # Character still needs its sheet, but the locked scene is never invented.
    assert len(_tasks(graph, "identity_sheet")) == 1
    assert not _scene_nodes(graph)
    scene_ref = _node(graph, "n_ref_02")
    assert scene_ref["config"]["reference_card_role"] == "scene"
    assert scene_ref["output_ref"]["uri"].endswith("room.png")
    for clip in _clips(graph):
        plan = clip["config"]["reference_image_plan"]
        scene_entries = [e for e in plan if e["role"] == ROLE_SCENE]
        assert scene_entries and scene_entries[-1]["node_id"] == "n_ref_02"
        assert scene_entries[-1]["path"].endswith("room.png")


# --- Fix 4/5: medium inheritance from an authority still --------------------


def test_style_authority_without_vision_locks_to_reference() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
        MATCH_REFERENCE_MEDIUM,
    )

    graph = _intent_graph(
        [
            _slot(
                [ROLE_SCENE],
                binding="verbatim",
                setting_id="set_1",
                set_lock=True,
                style_authority=True,
            )
        ],
        style_lock={"look": "photoreal commercial", "medium": "photoreal"},
    )
    lock = graph["metadata"]["style_lock"]
    assert lock["medium"] == MATCH_REFERENCE_MEDIUM
    for clip in _clips(graph):
        prompt = clip["config"]["generate"]["prompt"]
        assert "match the rendering" in prompt.lower()
        assert clip["config"]["style_lock"]["medium"] == MATCH_REFERENCE_MEDIUM


def test_style_authority_with_vision_inherits_named_medium() -> None:
    graph = _intent_graph(
        [
            _slot(
                [ROLE_SCENE],
                binding="verbatim",
                setting_id="set_1",
                set_lock=True,
                style_authority=True,
                style_read={"medium": "anime", "look": "cel-shaded", "palette": "pastel"},
            )
        ],
        style_lock={"look": "photoreal commercial", "medium": "photoreal"},
    )
    lock = graph["metadata"]["style_lock"]
    assert lock["medium"] == "anime"
    assert lock["look"] == "cel-shaded"
    for clip in _clips(graph):
        prompt = clip["config"]["generate"]["prompt"]
        assert "Medium: anime." in prompt
        assert "cel-shaded" in prompt


def test_user_medium_wins_when_no_style_authority() -> None:
    graph = _intent_graph(
        [_slot([ROLE_SCENE], binding="verbatim", setting_id="set_1")],
        style_lock={"look": "live-action", "medium": "photoreal"},
    )
    lock = graph["metadata"]["style_lock"]
    assert lock["medium"] == "photoreal"
    assert lock["look"] == "live-action"


# --- Fix 6: product / motion verbatim --------------------------------------


def test_product_verbatim_keeps_plan_path_and_card() -> None:
    graph = _intent_graph(
        [_slot([ROLE_PRODUCT], binding="verbatim", path="/refs/sku.png")],
        characters=[],
        scenes=[],
    )
    assert not _tasks(graph, "identity_sheet")
    assert not _tasks(graph, "medium_change")
    assert not _scene_nodes(graph)
    ref = _node(graph, "n_ref_01")
    assert ref["config"]["reference_card_role"] == "product"
    for clip in _clips(graph):
        plan = clip["config"]["reference_image_plan"]
        assert plan[0]["role"] == ROLE_PRODUCT
        assert plan[0]["path"].endswith("sku.png")


def test_motion_verbatim_i2v_without_restyle() -> None:
    graph = _intent_graph(
        [_slot([ROLE_MOTION], binding="verbatim", path="/refs/frame.png")],
        video_binding=VIDEO_ANIMATE_KEYFRAME,
        scenes=[],
    )
    assert not _tasks(graph, "medium_change")
    ref = _node(graph, "n_ref_01")
    assert ref["config"]["reference_card_role"] == "motion"
    for clip in _clips(graph):
        cfg = clip["config"]
        assert cfg["reference_call_mode"] == "i2v"
        assert cfg["reference_first_frame"].endswith("frame.png")


def test_motion_condition_builds_restyle_node() -> None:
    graph = _intent_graph(
        [_slot([ROLE_MOTION], binding="condition", path="/refs/frame.png")],
        video_binding=VIDEO_ANIMATE_KEYFRAME,
        scenes=[],
    )
    assert len(_tasks(graph, "medium_change")) == 1
    ref = _node(graph, "n_ref_01")
    assert "reference_card_role" not in ref["config"]
    for clip in _clips(graph):
        assert clip["config"]["reference_first_frame_node"] == "n_restyle_01"


# --- Fix 2: classifier fields persist through stamp_creative_intent ---------


def test_enriched_classify_fields_persist_into_slots() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
        absorb_reference_read,
        stamp_creative_intent,
    )

    item = {
        "slot": 1,
        "subject": "scene",
        "roles": [ROLE_SCENE],
        "binding": "verbatim",
        "set_lock": True,
        "style_authority": True,
        "medium": "anime",
        "look": "cel-shaded",
        "palette": "pastel",
        "rationale": "locked cartoon set",
    }
    read = absorb_reference_read(item, 1, "scene")
    assert read["set_lock"] is True
    assert read["style_authority"] is True
    assert "keyframe_complete" not in read
    assert "solo_subject" not in read
    assert "suppress_companions" not in read
    assert read["style_read"] == {"medium": "anime", "look": "cel-shaded", "palette": "pastel"}

    analysis = stamp_creative_intent(
        {"style_lock": {}}, [read], [{"path": "/refs/room.png"}]
    )
    slot = analysis["creative_intent"]["slots"][0]
    assert slot["set_lock"] is True
    assert slot["style_authority"] is True
    assert slot["style_read"]["medium"] == "anime"
    assert slot["bindings"][ROLE_SCENE] == "verbatim"
    assert "keyframe_complete" not in analysis["creative_intent"]


# --- Companion cast / set: uncovered analysis ids get sheets/plates ---------


_FAMILY = [
    {"id": "xiaoyue", "name": "Xiaoyue", "description": "young woman"},
    {"id": "father", "name": "Father", "description": "middle-aged man"},
    {"id": "mother", "name": "Mother", "description": "middle-aged woman"},
]

_PLACES = [
    {"id": "set_1", "name": "Dining room", "description": "family dining room"},
    {"id": "set_2", "name": "Kitchen", "description": "warm kitchen"},
]


def test_verbatim_character_multi_cast_builds_companion_sheets() -> None:
    """Verbatim still covers that id; other analysis characters get sheets."""
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="xiaoyue")],
        characters=_FAMILY,
        scenes=[_PLACES[0]],
        style_lock={"look": "ink wash", "medium": "anime"},
    )
    sheets = _tasks(graph, "identity_sheet")
    assert len(sheets) == 2
    sheet_ids = {str(n["config"].get("character_id") or "") for n in sheets}
    assert sheet_ids == {"father", "mother"}
    assert not any(n["config"].get("character_id") == "xiaoyue" for n in sheets)
    for sheet in sheets:
        assert sheet["config"]["style_lock"]["medium"] == "anime"
        assert sheet["config"].get("companion_cast") is True
        assert "n_ref_01" not in (sheet["config"].get("inputs") or [])
    ref = _node(graph, "n_ref_01")
    assert ref["config"]["force_handler"] is True
    assert ref["config"]["reference_card_role"] == "character_design"
    for clip in _clips(graph):
        plan = clip["config"]["reference_image_plan"]
        char_entries = [e for e in plan if e["role"] == ROLE_CHARACTER]
        assert char_entries[0]["node_id"] == "n_ref_01"
        assert char_entries[0]["path"].endswith("upload.png")
        assert {e["node_id"] for e in char_entries[1:]} == {
            sheets[0]["id"],
            sheets[1]["id"],
        }
        assert any(e["role"] == ROLE_SCENE for e in plan)
    assert len(_scene_nodes(graph)) == 1


def test_verbatim_character_solo_cast_skips_companion_sheets() -> None:
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="xiaoyue")],
        characters=[{"id": "xiaoyue", "name": "Xiaoyue", "description": "young woman"}],
        scenes=[],
    )
    assert not _tasks(graph, "identity_sheet")
    assert not _scene_nodes(graph)


def test_condition_character_sheets_self_plus_companions() -> None:
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="condition", character_id="xiaoyue")],
        characters=_FAMILY,
    )
    sheets = _tasks(graph, "identity_sheet")
    assert len(sheets) == 3
    by_id = {str(n["config"].get("character_id") or ""): n for n in sheets}
    assert set(by_id) == {"xiaoyue", "father", "mother"}
    # Condition still feeds its own sheet; companions do not.
    assert "n_ref_01" in (by_id["xiaoyue"]["config"].get("inputs") or [])
    assert by_id["xiaoyue"].get("config", {}).get("companion_cast") is not True
    assert by_id["father"]["config"].get("companion_cast") is True
    assert "n_ref_01" not in (by_id["father"]["config"].get("inputs") or [])


def test_scene_verbatim_builds_plates_for_uncovered_settings() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
        MATCH_REFERENCE_MEDIUM,
    )

    graph = _intent_graph(
        [
            _slot(
                [ROLE_SCENE],
                binding="verbatim",
                setting_id="set_1",
                set_lock=True,
                style_authority=True,
            )
        ],
        characters=[],
        scenes=_PLACES,
        style_lock={"look": "photoreal", "medium": "photoreal"},
    )
    plates = _scene_nodes(graph)
    assert len(plates) == 1
    assert plates[0]["config"]["setting_id"] == "set_2"
    # Authority still without vision inherits match-reference medium.
    assert plates[0]["config"]["style_lock"]["medium"] == MATCH_REFERENCE_MEDIUM
    ref = _node(graph, "n_ref_01")
    assert ref["config"]["reference_card_role"] == "scene"
    for clip in _clips(graph):
        scene_entries = [
            e for e in clip["config"]["reference_image_plan"] if e["role"] == ROLE_SCENE
        ]
        assert scene_entries[0]["node_id"] == "n_ref_01"
        assert scene_entries[0]["path"].endswith("upload.png")
        assert scene_entries[-1]["node_id"] == plates[0]["id"]
        assert scene_entries[-1]["path"] == ""


def test_scene_verbatim_with_cast_builds_companion_sheets() -> None:
    """Locked set + multi-cast analysis → companion sheets, no plate for locked set."""
    graph = _intent_graph(
        [
            _slot(
                [ROLE_SCENE],
                binding="verbatim",
                setting_id="set_1",
                set_lock=True,
                style_authority=True,
                style_read={"medium": "anime", "look": "cel-shaded"},
            )
        ],
        characters=_FAMILY,
        scenes=[_PLACES[0]],
    )
    assert not _scene_nodes(graph)
    sheets = _tasks(graph, "identity_sheet")
    assert len(sheets) == 3
    for sheet in sheets:
        assert sheet["config"]["style_lock"]["medium"] == "anime"


def test_product_with_cast_builds_companion_sheets() -> None:
    graph = _intent_graph(
        [_slot([ROLE_PRODUCT], binding="verbatim", path="/refs/sku.png")],
        characters=_FAMILY[:2],
        scenes=[],
    )
    sheets = _tasks(graph, "identity_sheet")
    assert len(sheets) == 2
    ref = _node(graph, "n_ref_01")
    assert ref["config"]["reference_card_role"] == "product"
    for clip in _clips(graph):
        plan = clip["config"]["reference_image_plan"]
        assert plan[0]["role"] == ROLE_PRODUCT
        assert any(e["node_id"].startswith("n_character_") for e in plan)


def test_motion_multi_cast_mints_companions_ignoring_stale_flags() -> None:
    graph = _intent_graph(
        [_slot([ROLE_MOTION], binding="verbatim", path="/refs/frame.png", character_id="xiaoyue")],
        characters=_FAMILY,
        scenes=[],
        video_binding=VIDEO_ANIMATE_KEYFRAME,
    )
    sheets = _tasks(graph, "identity_sheet")
    assert len(sheets) == 2
    assert {n["config"]["character_id"] for n in sheets} == {"father", "mother"}
    for clip in _clips(graph):
        # Extra cast upgrades animate_keyframe to R2V so companions pack as refs.
        assert clip["config"]["reference_call_mode"] == "r2v"
        plan = clip["config"]["reference_image_plan"]
        assert plan[0]["path"].endswith("frame.png")
        assert any(e["node_id"].startswith("n_character_") for e in plan)

    stale = _intent_graph(
        [
            _slot(
                [ROLE_MOTION],
                binding="verbatim",
                path="/refs/frame.png",
                character_id="xiaoyue",
                keyframe_complete=True,
                solo_subject=True,
                suppress_companions=True,
            )
        ],
        characters=_FAMILY,
        scenes=[],
        video_binding=VIDEO_ANIMATE_KEYFRAME,
    )
    assert len(_tasks(stale, "identity_sheet")) == 2


def test_multi_still_covers_each_id_only_uncovered_get_companions() -> None:
    graph = _intent_graph(
        [
            _slot(
                [ROLE_CHARACTER],
                binding="verbatim",
                character_id="xiaoyue",
                slot=1,
                node_id="n_ref_01",
                path="/refs/xiaoyue.png",
            ),
            _slot(
                [ROLE_CHARACTER],
                binding="verbatim",
                character_id="father",
                slot=2,
                node_id="n_ref_02",
                path="/refs/father.png",
            ),
        ],
        characters=_FAMILY,
    )
    sheets = _tasks(graph, "identity_sheet")
    assert len(sheets) == 1
    assert sheets[0]["config"]["character_id"] == "mother"
    for clip in _clips(graph):
        char_entries = [
            e for e in clip["config"]["reference_image_plan"] if e["role"] == ROLE_CHARACTER
        ]
        assert {e["node_id"] for e in char_entries} == {
            "n_ref_01",
            "n_ref_02",
            sheets[0]["id"],
        }


def test_slot_character_id_matches_analysis_name() -> None:
    """Normalize so slot character_id can match analysis name/id."""
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="Xiaoyue")],
        characters=[
            {"id": "char_xy", "name": "Xiaoyue", "description": "young woman"},
            {"id": "father", "name": "Father", "description": "dad"},
        ],
    )
    sheets = _tasks(graph, "identity_sheet")
    assert len(sheets) == 1
    assert sheets[0]["config"]["character_id"] == "father"


def test_stale_solo_subject_does_not_wipe_uncovered_companions() -> None:
    """T1: leftover solo_subject=true is ignored; uncovered cast still gets sheets."""
    graph = _intent_graph(
        [
            _slot(
                [ROLE_CHARACTER],
                binding="verbatim",
                character_id="char_1",
                solo_subject=True,
                suppress_companions=True,
                keyframe_complete=True,
            )
        ],
        characters=[
            {"id": "char_1", "name": "Lead", "description": "lead"},
            {"id": "char_2", "name": "Two", "description": "two"},
            {"id": "char_3", "name": "Three", "description": "three"},
            {"id": "char_4", "name": "Four", "description": "four"},
        ],
        scenes=[],
    )
    sheets = _companion_sheets(graph)
    assert len(sheets) == 3
    assert {n["config"]["character_id"] for n in sheets} == {"char_2", "char_3", "char_4"}
    assert not any(n["config"].get("character_id") == "char_1" for n in _tasks(graph, "identity_sheet"))


def test_xiaoyue_alias_covers_char_1_and_parents_are_companions() -> None:
    """T3: classify id xiaoyue reconciles to char_1 / 小月; parents only are companions."""
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="xiaoyue")],
        characters=[
            {
                "id": "char_1",
                "name": "小月",
                "description": "young woman",
                "match_terms": ["xiaoyue", "小月"],
            },
            {"id": "father", "name": "Father", "description": "dad"},
            {"id": "mother", "name": "Mother", "description": "mom"},
        ],
        scenes=[],
    )
    sheets = _companion_sheets(graph)
    assert {n["config"]["character_id"] for n in sheets} == {"father", "mother"}
    assert not any(n["config"].get("character_id") in {"xiaoyue", "char_1"} for n in sheets)
    slot = graph["metadata"]["script_analysis"]["creative_intent"]["slots"][0]
    assert slot["character_id"] == "char_1"


def test_shared_generic_match_terms_do_not_skip_companion_sheets() -> None:
    """#7725: shared wardrobe match_terms (无外套) must not mark other cast covered."""
    shared = "无外套"
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="char_1")],
        characters=[
            {
                "id": "char_1",
                "name": "小月",
                "description": "child lead",
                "match_terms": [shared, "小月"],
            },
            {
                "id": "char_2",
                "name": "妈妈",
                "description": "mom",
                "match_terms": ["围裙"],
            },
            {
                "id": "char_3",
                "name": "爸爸",
                "description": "dad",
                "match_terms": [shared],
            },
            {
                "id": "char_4",
                "name": "奶奶",
                "description": "grandma",
                "match_terms": [shared, "灰发"],
            },
        ],
        scenes=[],
    )
    sheets = _companion_sheets(graph)
    assert {n["config"]["character_id"] for n in sheets} == {"char_2", "char_3", "char_4"}
    assert not any(n["config"].get("character_id") == "char_1" for n in sheets)


def test_character_still_plates_uncovered_analysis_scenes() -> None:
    """T6: character still + two storyboard rooms → companion plates."""
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="char_1")],
        characters=[{"id": "char_1", "name": "Lead", "description": "lead"}],
        scenes=_PLACES,
    )
    plates = _scene_nodes(graph)
    assert len(plates) >= 1
    assert {p["config"]["setting_id"] for p in plates} == {"set_1", "set_2"}


def test_scene_still_plates_only_uncovered_setting() -> None:
    """T7: locked set_1 of two analysis sets → plate set_2 only."""
    graph = _intent_graph(
        [
            _slot(
                [ROLE_SCENE],
                binding="verbatim",
                setting_id="set_1",
                set_lock=True,
            )
        ],
        characters=[],
        scenes=_PLACES,
    )
    plates = _scene_nodes(graph)
    assert len(plates) == 1
    assert plates[0]["config"]["setting_id"] == "set_2"


def test_plan_cap_keeps_uploads_over_generated_companions() -> None:
    """T8: 2 uploads + 3 companions + plate → ≤5 and both uploads stay."""
    graph = _intent_graph(
        [
            _slot(
                [ROLE_CHARACTER],
                binding="verbatim",
                character_id="char_1",
                slot=1,
                node_id="n_ref_01",
                path="/refs/a.png",
            ),
            _slot(
                [ROLE_CHARACTER],
                binding="verbatim",
                character_id="char_2",
                slot=2,
                node_id="n_ref_02",
                path="/refs/b.png",
            ),
        ],
        characters=[
            {"id": "char_1", "name": "One", "description": "one"},
            {"id": "char_2", "name": "Two", "description": "two"},
            {"id": "char_3", "name": "Three", "description": "three"},
            {"id": "char_4", "name": "Four", "description": "four"},
            {"id": "char_5", "name": "Five", "description": "five"},
        ],
        scenes=_PLACES[:1],
    )
    companions = _companion_sheets(graph)
    assert len(companions) >= 2
    assert any(c["config"].get("combined_cast") for c in companions) or len(companions) == 3
    assert _scene_nodes(graph)
    for clip in _clips(graph):
        plan = clip["config"]["reference_image_plan"]
        assert len(plan) <= 5
        upload_ids = {e["node_id"] for e in plan if e.get("path")}
        assert {"n_ref_01", "n_ref_02"} <= upload_ids


# --- R1–R7: video_binding, packing resolve, Image-N, combined cast ----------


def test_stamp_persists_video_binding_default_multi_ref() -> None:
    analysis = stamp_creative_intent(
        {"characters": [], "scenes": []},
        [
            {
                "slot": 1,
                "subject": "character",
                "roles": [ROLE_CHARACTER],
                "binding": "verbatim",
                "video_binding": VIDEO_MULTI_REF,
            }
        ],
        [{"path": "/refs/a.png"}],
    )
    assert analysis["creative_intent"]["video_binding"] == VIDEO_MULTI_REF


def test_motion_role_alone_does_not_force_i2v() -> None:
    graph = _intent_graph(
        [_slot([ROLE_MOTION], binding="verbatim", path="/refs/frame.png", character_id="char_1")],
        characters=_FAMILY,
        scenes=[],
        # no video_binding → multi_ref_story
    )
    for clip in _clips(graph):
        assert clip["config"]["reference_call_mode"] == "r2v"
        assert clip["config"].get("reference_video_binding") == VIDEO_MULTI_REF


def test_dinner_scene_plus_motion_role_stays_r2v() -> None:
    graph = _intent_graph(
        [
            _slot(
                [ROLE_SCENE, ROLE_MOTION],
                binding="verbatim",
                setting_id="set_1",
                set_lock=True,
                path="/refs/room.png",
            )
        ],
        characters=_FAMILY,
        scenes=_PLACES,
        video_binding=VIDEO_MULTI_REF,
    )
    for clip in _clips(graph):
        assert clip["config"]["reference_call_mode"] == "r2v"
        plan = clip["config"]["reference_image_plan"]
        assert sum(1 for e in plan if e["node_id"] == "n_ref_01") == 1
        assert any(e["node_id"].startswith("n_character_") for e in plan)


def test_r2v_merge_keeps_companion_paths_with_upload() -> None:
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="xiaoyue")],
        characters=_FAMILY,
        scenes=_PLACES[:1],
        video_binding=VIDEO_MULTI_REF,
    )
    for node in graph["nodes"]:
        cfg = node.get("config") or {}
        if cfg.get("companion_cast"):
            node["output_ref"] = {"kind": "image", "uri": f"/out/{node['id']}.png"}
    clip = _clips(graph)[0]
    overrides = video_generation_overrides(
        clip["config"], graph, ["/edge/from_inputs.png"]
    )
    refs = overrides["reference_images"] or []
    assert any(str(p).endswith("upload.png") or "upload" in str(p) for p in refs) or any(
        str(p).endswith(".png") for p in refs
    )
    assert any("/out/n_character_" in str(p) for p in refs)
    assert "/edge/from_inputs.png" in refs


def test_compose_prompt_labels_every_plan_image() -> None:
    prompt = compose_reference_clip_prompt(
        {
            "shot_index": 1,
            "shot_action": "wave",
            "camera": "medium",
            "timeline": "0.0-4.0s",
            "duration_sec": 4,
            "style_lock": {"look": "anime", "medium": "anime"},
            "reference_call_mode": "r2v",
            "reference_prompt_contract": "character",
            "reference_image_plan": [
                {"role": ROLE_CHARACTER, "path": "/a.png", "node_id": "n_ref_01"},
                {
                    "role": ROLE_CHARACTER,
                    "path": "",
                    "node_id": "n_character_1",
                    "names": ["Mom", "Dad"],
                },
                {"role": ROLE_SCENE, "path": "", "node_id": "n_scene_1"},
            ],
        }
    )
    assert "Image 1 is the person" in prompt
    assert "Image 2 is Mom and Dad" in prompt
    assert "Image 3 is the place" in prompt


def test_overflow_mints_combined_cast_card() -> None:
    cast = [{"id": f"char_{i}", "name": f"P{i}", "description": f"c{i}"} for i in range(1, 8)]
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="char_1")],
        characters=cast,
        scenes=_PLACES[:1],
        video_binding=VIDEO_MULTI_REF,
    )
    combined = [n for n in _companion_sheets(graph) if n["config"].get("combined_cast")]
    assert len(combined) == 1
    assert len(combined[0]["config"].get("character_ids") or []) >= 2
    for clip in _clips(graph):
        assert any(e["node_id"] == combined[0]["id"] for e in clip["config"]["reference_image_plan"])


def test_animate_keyframe_cast_packs_refs_not_null() -> None:
    graph = _intent_graph(
        [_slot([ROLE_MOTION], binding="verbatim", path="/refs/frame.png", character_id="xiaoyue")],
        characters=_FAMILY,
        scenes=[],
        video_binding=VIDEO_ANIMATE_KEYFRAME,
    )
    for node in graph["nodes"]:
        if (node.get("config") or {}).get("companion_cast"):
            node["output_ref"] = {"kind": "image", "uri": f"/out/{node['id']}.png"}
    clip = _clips(graph)[0]
    assert clip["config"]["reference_call_mode"] == "r2v"
    overrides = video_generation_overrides(clip["config"], graph, [])
    refs = overrides["reference_images"] or []
    assert any(str(p).endswith("frame.png") for p in refs)
    assert any("/out/n_character_" in str(p) for p in refs)
