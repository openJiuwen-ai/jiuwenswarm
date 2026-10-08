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
    video_generation_overrides,
)
from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph

_ROOT = Path(__file__).resolve().parents[2]
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
_CASES_PER_SCENARIO = 1000


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


def _analysis(case: dict) -> dict:
    kind = case["kind"]
    style = {"look": case["look"], "medium": case["medium"]}
    base = {
        "source": "llm",
        "style_lock": style,
        "characters": [{"id": "char_1", "name": "Subject", "description": "a person"}],
        "scenes": [{"id": "set_1", "name": "Place", "description": f"place-{case['index']}"}],
        "shots": _shots(case),
        "audio": {"policy": "silent", "include_speech": False, "include_music": False},
    }
    if kind == "text":
        return base
    path = f"/refs/{kind}-{case['index']}.png"
    if kind == "product":
        roles = [ROLE_PRODUCT]
        binding = "verbatim"
    elif kind == "motion":
        roles = [ROLE_MOTION]
        binding = case["binding"]
    elif kind == "scene":
        roles = [ROLE_SCENE]
        binding = "verbatim"
    else:
        roles = [ROLE_CHARACTER]
        binding = "condition"
    base["creative_intent"] = {
        "mode": "reference_led",
        "slots": [
            {
                "slot": 1,
                "path": path,
                "roles": roles,
                "bindings": {roles[0]: binding},
                "character_id": "char_1" if kind == "character" else "",
                "setting_id": "set_1" if kind == "scene" else "",
                "node_id": "n_ref_01",
            }
        ],
    }
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
        assert case["look"] in prompt
        assert case["lighting"] in prompt
        assert case["crowd"] in prompt
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
    else:
        assert len(_tasks(graph, "identity_sheet")) == 1
        assert len(_scene_nodes(graph)) == 1
        assert f"place-{case['index']}" in _scene_nodes(graph)[0]["config"]["prompt"]
        for clip in clips:
            cfg = clip["config"]
            roles = [item["role"] for item in cfg["reference_image_plan"]]
            assert cfg["reference_call_mode"] == "r2v"
            assert cfg["reference_prompt_contract"] == "character"
            assert roles[0] == ROLE_CHARACTER
            assert roles[-1] == ROLE_SCENE
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

    graph = _graph(_cases("character")[0])
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
