# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.designer.handlers.clip import collect_clip_reference_slots
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
from jiuwenswarm.server.runtime.designer.pipeline.reference_binding import (
    REFERENCE_BINDING_KEY,
    ClipReferenceError,
    ReferenceSlot,
    append_reference_binding,
    reindex_slots,
)


def _image_ref(path: Path) -> dict[str, str]:
    return {"kind": "image", "uri": path.resolve().as_uri(), "mime_type": "image/png"}


def _wired_clip_graph(character: Path, scene: Path | None) -> dict:
    scene_node: dict = {
        "id": "n_scene_1",
        "type": "image",
        "label": "Scene 1",
        "config": {
            "role": "scene",
            "setting_id": "set_1",
            "scene_specs": {"scene_name": "the old library"},
        },
    }
    if scene is not None:
        scene_node["output_ref"] = _image_ref(scene)
    return {
        "nodes": [
            {
                "id": "n_character_1",
                "type": "image",
                "label": "Character 1",
                "config": {"role": "character", "character_name": "Ada"},
                "output_ref": _image_ref(character),
            },
            scene_node,
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {"role": "clip", "clip_index": 1, "setting_id": "set_1", "on_screen": ["Ada"]},
            },
        ],
        "edges": [
            {"id": "e_char", "source": "n_character_1", "target": "n_clip_1", "kind": "data"},
            {"id": "e_scene", "source": "n_scene_1", "target": "n_clip_1", "kind": "data"},
        ],
    }


def _ctx(graph: dict) -> NodeExecutionContext:
    states = {
        node["id"]: {"status": "completed", "output_ref": node["output_ref"]}
        for node in graph["nodes"]
        if "output_ref" in node
    }
    return NodeExecutionContext(graph=graph, run_id="run_1", node_id="n_clip_1", run={"node_states": states})


def test_slots_follow_attach_order_with_cast_and_scene_labels(tmp_path: Path) -> None:
    character = tmp_path / "ada.png"
    scene = tmp_path / "library.png"
    character.write_bytes(b"img")
    scene.write_bytes(b"img")
    graph = _wired_clip_graph(character, scene)

    slots = collect_clip_reference_slots(_ctx(graph), 1, graph["nodes"][2])

    assert [(s.index, s.role, s.label) for s in slots] == [
        (1, "character", "Ada"),
        (2, "scene", "the old library"),
    ]


def test_wired_scene_without_image_stops_the_clip(tmp_path: Path) -> None:
    character = tmp_path / "ada.png"
    character.write_bytes(b"img")
    graph = _wired_clip_graph(character, None)

    with pytest.raises(ClipReferenceError, match="n_scene_1"):
        collect_clip_reference_slots(_ctx(graph), 1, graph["nodes"][2])


def test_declared_scene_without_image_stops_the_clip(tmp_path: Path) -> None:
    character = tmp_path / "ada.png"
    character.write_bytes(b"img")
    graph = _wired_clip_graph(character, None)
    graph["edges"] = []
    graph["nodes"][2]["config"]["scene_node_id"] = "n_scene_1"

    with pytest.raises(ClipReferenceError, match="n_scene_1"):
        collect_clip_reference_slots(_ctx(graph), 1, graph["nodes"][2])


def test_several_scenes_without_relation_bind_no_scene(tmp_path: Path) -> None:
    character = tmp_path / "ada.png"
    scene = tmp_path / "library.png"
    other = tmp_path / "street.png"
    for path in (character, scene, other):
        path.write_bytes(b"img")
    graph = _wired_clip_graph(character, scene)
    graph["edges"] = []
    graph["nodes"][2]["config"].pop("setting_id")
    graph["nodes"].append(
        {
            "id": "n_scene_2",
            "type": "image",
            "config": {"role": "scene", "setting_id": "set_2"},
            "output_ref": _image_ref(other),
        }
    )

    slots = collect_clip_reference_slots(_ctx(graph), 1, graph["nodes"][2])

    assert all(slot.role != "scene" for slot in slots)


def test_binding_is_appended_once_and_replaced_on_resend(tmp_path: Path) -> None:
    cfg: dict = {}
    slots = [
        ReferenceSlot(1, "character", "Ada", tmp_path / "ada.png"),
        ReferenceSlot(2, "scene", "the old library", tmp_path / "library.png"),
    ]
    first = append_reference_binding("Ada reads by the window.", slots, cfg=cfg, family="wan", lang="en")
    assert first.endswith("Image 2 is the scene: the old library.")

    resent = append_reference_binding(first, slots[1:], cfg=cfg, family="wan", lang="en")
    assert resent.count("is the scene") == 1
    assert "identity reference for Ada" not in resent
    assert cfg[REFERENCE_BINDING_KEY] == "Image 2 is the scene: the old library."


def test_reindex_binds_restored_files_as_connected_references(tmp_path: Path) -> None:
    ada = tmp_path / "ada.png"
    extra = tmp_path / "restored.png"
    slots = [ReferenceSlot(1, "character", "Ada", ada)]

    out = reindex_slots(slots, [str(extra), str(ada)])

    assert [(s.index, s.role, s.label) for s in out] == [(1, "other", "restored"), (2, "character", "Ada")]


def test_rewrite_with_slots_numbers_cast_and_never_invents_a_room(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        compose_practice_prompt,
    )

    cfg = {
        "cast_names": ["Ada"],
        "on_screen": ["Ada"],
        "clip_action": "Ada reads by the window.",
        "costume_lock": "Ada: green cardigan",
        "style_lock": {"look": "photoreal cinematic"},
    }
    slots = [
        ReferenceSlot(1, "character", "Ada", tmp_path / "ada.png"),
        ReferenceSlot(2, "scene", "the old library", tmp_path / "library.png"),
    ]

    text = compose_practice_prompt(cfg=cfg, graph={}, action=cfg["clip_action"], slots=slots)

    assert "Ada from Image 1" in text
    assert "in the scene from Image 2" in text
    assert "empty room" not in text.lower()


def test_rewrite_without_scene_slot_does_not_reference_a_scene_image(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        compose_practice_prompt,
    )

    cfg = {"cast_names": ["Ada"], "on_screen": ["Ada"], "clip_action": "Ada waves."}
    slots = [ReferenceSlot(1, "character", "Ada", tmp_path / "ada.png")]

    text = compose_practice_prompt(cfg=cfg, graph={}, action="Ada waves.", slots=slots)

    assert "Image 2" not in text
    assert "empty room" not in text.lower()


def test_call_locks_append_binding_from_attached_slots(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.wan_call_locks import apply_wan_call_locks

    cfg = {"cast_names": ["Ada"], "on_screen": ["Ada"], "clip_action": "Ada reads by the window."}
    slots = [
        ReferenceSlot(1, "character", "Ada", tmp_path / "ada.png"),
        ReferenceSlot(2, "scene", "the old library", tmp_path / "library.png"),
    ]

    text = apply_wan_call_locks("Ada reads by the window.", cfg=cfg, graph={}, clip_index=1, slots=slots)

    assert text.endswith("Image 2 is the scene: the old library.")
    assert cfg["last_wan_prompt"] == text
    again = apply_wan_call_locks(text, cfg=cfg, graph={}, clip_index=1, slots=slots)
    assert again.count("Image 2 is the scene") == 1


def test_reference_led_call_gets_no_appended_binding(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.wan_call_locks import apply_wan_call_locks

    cfg = {"reference_call_mode": "character", "clip_action": "Ada waves."}
    slots = [ReferenceSlot(1, "character", "Ada", tmp_path / "ada.png")]

    text = apply_wan_call_locks("Ada waves.", cfg=cfg, graph={}, clip_index=1, slots=slots)

    assert "identity reference" not in text


def test_shot_brief_names_the_scene_as_plain_text() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        clip_scene_label,
        shot_brief_prompt,
    )

    cfg = {"scene_specs": {"scene_name": "the old library"}}
    text = shot_brief_prompt(1, camera="static", action="Ada reads.", scene=clip_scene_label(cfg), lang="en")

    assert "Scene: the old library." in text
    assert "Image" not in text


def test_analysis_without_named_scenes_is_rejected() -> None:
    from jiuwenswarm.server.runtime.designer.script_analysis import (
        _normalize_llm_analysis,
        heuristic_analysis,
    )

    base = heuristic_analysis("Ada reads in a library.")
    parsed = {
        "characters": [{"id": "char_1", "name": "Ada", "description": "green cardigan"}],
        "clips": [{"clip_index": 1, "action": "Ada reads.", "on_screen": ["char_1"], "setting_id": "set_1"}],
    }
    assert _normalize_llm_analysis(parsed, base) is None

    parsed["scenes"] = [{"id": "set_1", "name": "the old library", "description": "tall shelves"}]
    out = _normalize_llm_analysis(parsed, base)
    assert out is not None
    assert out["scenes"][0]["name"] == "the old library"


def test_scene_plate_without_place_raises() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
        compose_scene_specs_prompt,
    )

    with pytest.raises(ValueError, match="no place"):
        compose_scene_specs_prompt(cfg={"role": "scene"}, seed="")
