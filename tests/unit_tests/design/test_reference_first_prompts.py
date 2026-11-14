# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Clips cite the reference slots the call really sends, and trust the plates.

Three canvas-visible defects are covered here:

* the prompt guessed ``Image N`` by counting cast members, so a 5-cast shot
  named an ``Image 7`` that was never attached;
* the style sentence re-listed the film look the identity/scene plates already
  show, instead of pointing the model at those images;
* planner phrasing (``keep dawn across same-setting shots``) leaked into the
  video prompt, and lock banners were stamped onto clip leaves whose plates
  already carry the same information.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_CHARACTER,
    NODE_ROLE_SCENE,
    NODE_TYPE_IMAGE,
)
from jiuwenswarm.server.runtime.designer.handlers import common as handler_common
from jiuwenswarm.server.runtime.designer.handlers.clip import collect_clip_reference_images
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
from jiuwenswarm.server.runtime.designer.orchestration import Director
from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
    _time_of_day_payload,
    _visual_only,
    compose_practice_prompt,
)


def _png(path: Path) -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x")
    return path


def _cast_node(nid: str, cid: str) -> dict:
    return {
        "id": nid,
        "type": NODE_TYPE_IMAGE,
        "config": {"pipeline": NODE_ROLE_CHARACTER, "character_ids": [cid]},
    }


def _clip_node(on_screen: list[str]) -> dict:
    return {
        "id": "n_clip_1",
        "type": "video",
        "config": {
            "pipeline": "clip",
            "on_screen": list(on_screen),
            "character_node_ids": [f"n_char_{cid}" for cid in on_screen],
        },
    }


def _ctx(tmp_path: Path, cast: list[str], *, scene: bool = True) -> tuple[NodeExecutionContext, dict]:
    nodes = [_cast_node(f"n_char_{cid}", cid) for cid in cast]
    nodes.append(
        {
            "id": "n_scene_1",
            "type": NODE_TYPE_IMAGE,
            "config": {"pipeline": NODE_ROLE_SCENE},
        }
    )
    nodes.append(_clip_node(cast))
    states: dict[str, dict] = {}
    for cid in cast:
        states[f"n_char_{cid}"] = {
            "status": "completed",
            "output_ref": handler_common.file_output_ref(
                _png(tmp_path / f"{cid}.png"), kind=NODE_TYPE_IMAGE, mime_type="image/png"
            ),
        }
    if scene:
        states["n_scene_1"] = {
            "status": "completed",
            "output_ref": handler_common.file_output_ref(
                _png(tmp_path / "scene.png"), kind=NODE_TYPE_IMAGE, mime_type="image/png"
            ),
        }
    graph = {"graph_id": "g1", "project_id": "p1", "nodes": nodes, "edges": []}
    ctx = NodeExecutionContext(graph=graph, run_id="run_1", node_id="n_clip_1", run={"node_states": states})
    return ctx, graph


def test_bindings_report_the_slot_each_reference_landed_in(tmp_path: Path) -> None:
    ctx, _graph = _ctx(tmp_path, ["char_1", "char_2"])
    bindings: dict = {}

    refs = collect_clip_reference_images(ctx, bindings=bindings)

    assert [p.name for p in refs] == ["char_1.png", "char_2.png", "scene.png"]
    assert bindings["cast"] == {"char_1": 1, "char_2": 2}
    assert bindings["scene"] == 3
    assert bindings["extra"] == []
    assert bindings["unbound"] == []


def test_capped_cast_is_reported_as_unbound_instead_of_inventing_a_slot(tmp_path: Path) -> None:
    cast = ["char_1", "char_2", "char_3", "char_4", "char_5", "char_6"]
    ctx, _graph = _ctx(tmp_path, cast)
    bindings: dict = {}

    refs = collect_clip_reference_images(ctx, bindings=bindings)

    # Wan keeps the first four character specs plus the scene spec.
    assert len(refs) == 5
    assert bindings["scene"] == 5
    assert set(bindings["cast"]) == {"char_1", "char_2", "char_3", "char_4"}
    assert bindings["unbound"] == ["char_5", "char_6"]


def test_clip_prompt_names_the_slots_from_the_bindings() -> None:
    cfg = {
        "pipeline": "clip",
        "on_screen": ["char_1", "char_2"],
        "scene_specs": {"name": "lighthouse kitchen"},
        "reference_binding": {
            "cast": {"char_1": 1, "char_2": 3},
            "extra": [],
            "scene": 2,
            "unbound": [],
        },
        "clip_action": "The keeper climbs the stair.",
    }

    prompt = compose_practice_prompt(cfg=cfg, graph=None, action="The keeper climbs the stair.")

    assert "Image 1" in prompt
    assert "Image 3" in prompt
    assert "Image 2" in prompt
    assert "Image 4" not in prompt


def test_cast_without_a_plate_is_named_without_a_phantom_image() -> None:
    cfg = {
        "pipeline": "clip",
        "on_screen": ["char_1", "char_2"],
        "reference_binding": {"cast": {"char_1": 1}, "extra": [], "scene": 2, "unbound": ["char_2"]},
        "clip_action": "Both keep watch.",
    }

    prompt = compose_practice_prompt(cfg=cfg, graph=None, action="Both keep watch.")

    assert "Image 1" in prompt
    assert "Image 2" in prompt
    assert "Image 4" not in prompt


def test_connected_still_uses_its_own_slot() -> None:
    cfg = {
        "pipeline": "clip",
        "on_screen": ["char_1"],
        "reference_binding": {"cast": {"char_1": 2}, "extra": [1], "scene": 3, "unbound": []},
    }

    prompt = compose_practice_prompt(
        cfg=cfg,
        graph=None,
        action="",
        extra_image_labels=["lantern (lantern.png)"],
    )

    assert "Image 1" in prompt
    assert "lantern" in prompt


def test_attached_references_replace_the_style_essay() -> None:
    cfg = {
        "pipeline": "clip",
        "style_lock": {"look": "photoreal 35mm film, warm tungsten practicals"},
        "reference_binding": {"cast": {"char_1": 1}, "extra": [], "scene": 2, "unbound": []},
    }

    prompt = compose_practice_prompt(cfg=cfg, graph=None, action="")

    assert "match the attached reference images" in prompt.lower()


def test_without_references_the_style_words_still_lead() -> None:
    cfg = {"pipeline": "clip", "style_lock": {"look": "photoreal 35mm film"}}

    prompt = compose_practice_prompt(cfg=cfg, graph=None, action="")

    assert "photoreal 35mm film" in prompt
    assert "attached reference images" not in prompt.lower()


def test_planner_clause_is_stripped_from_the_lighting_phrase() -> None:
    cfg = {
        "time_of_day_lock": {
            "time_of_day": "dawn",
            "lighting": "low warm dawn light on the stone floor; keep dawn across same-setting shots",
        }
    }

    lighting = _time_of_day_payload(cfg)["lighting"]

    assert "low warm dawn light on the stone floor" in lighting
    assert "same-setting" not in lighting
    assert _visual_only("grey overcast; no relight mid-scene") == "grey overcast"


def test_scene_wardrobe_overrides_the_film_wide_look() -> None:
    cfg = {
        "pipeline": "clip",
        "on_screen": ["char_1"],
        "scene_node_id": "set_2",
        "costume_lock": "Ruth: top=linen headscarf; bottom=blue robe",
        "costume_by_setting": {"set_2": {"char_1": "black oilskin storm coat"}},
    }

    prompt = compose_practice_prompt(cfg=cfg, graph=None, action="Ruth waits.")

    assert "black oilskin storm coat" in prompt


def test_clip_leaf_keeps_lock_banners_off_its_prompt() -> None:
    graph = {
        "nodes": [_clip_node(["char_1"])],
        "metadata": {"spatial_lock": {"axis": "stage left"}},
    }
    node = graph["nodes"][0]
    node["config"].update(
        {
            "user_added": True,
            "costume_lock": "Ruth: top=linen headscarf",
            "identity_refs": {"character_node_ids": ["n_char_char_1"]},
            "generate": {"prompt": "Ruth rolls the barley."},
        }
    )

    Director.review_leaf_media_prompt(graph, node)

    stored = str(node["config"]["generate"]["prompt"])
    assert stored
    for banner in ("Costume lock", "CLOTHING LOCK", "SPATIAL LOCK", "IDENTITY character specs"):
        assert banner not in stored, banner
    # The structured lock is still stamped for downstream coverage.
    assert node["config"]["spatial_lock"] == {"axis": "stage left"}


def test_still_leaf_moves_its_lock_banners_into_director_notes() -> None:
    graph = {
        "nodes": [
            {
                "id": "n_character_1",
                "type": NODE_TYPE_IMAGE,
                "config": {
                    "pipeline": NODE_ROLE_CHARACTER,
                    "user_added": True,
                    "character_ids": ["char_1"],
                    "costume_lock": "Ruth: top=linen headscarf",
                    "generate": {"prompt": "One person on a plain backdrop."},
                },
            }
        ],
        "metadata": {},
    }
    node = graph["nodes"][0]

    Director.review_leaf_media_prompt(graph, node)

    stored = str(node["config"]["generate"]["prompt"])
    notes = str(node["config"].get("director_lock_notes") or "")
    assert "Costume lock" not in stored
    assert "Costume lock" in notes


@pytest.mark.parametrize("lang_cfg", [{"content_language": "en"}, {"content_language": "zh"}])
def test_unbound_cast_sentence_never_cites_an_image(lang_cfg: dict) -> None:
    cfg = {
        "pipeline": "clip",
        "on_screen": ["char_1"],
        "reference_binding": {"cast": {}, "extra": [], "scene": 1, "unbound": ["char_1"]},
        **lang_cfg,
    }

    prompt = compose_practice_prompt(cfg=cfg, graph=None, action="Ruth waits.")

    assert "Image 2" not in prompt


def test_cap_prediction_matches_the_attached_reference_list() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
        resolve_reference_slots,
    )

    assert resolve_reference_slots(2, 0) == {
        "cast": [1, 2],
        "extra": [],
        "scene": 3,
        "cast_dropped": 0,
    }
    assert resolve_reference_slots(2, 1) == {
        "cast": [1, 2],
        "extra": [3],
        "scene": 4,
        "cast_dropped": 0,
    }
    assert resolve_reference_slots(6, 0) == {
        "cast": [1, 2, 3, 4],
        "extra": [],
        "scene": 5,
        "cast_dropped": 2,
    }
    # Uploads are dropped before the setting plate, and before nothing else.
    assert resolve_reference_slots(4, 2) == {
        "cast": [1, 2, 3, 4],
        "extra": [],
        "scene": 5,
        "cast_dropped": 0,
    }


def test_one_cast_without_binding_cites_no_phantom_scene() -> None:
    cfg = {"pipeline": "clip", "on_screen": ["char_1"]}

    prompt = compose_practice_prompt(cfg=cfg, graph=None, action="Ruth waits.")

    assert "Image 1" in prompt
    assert "Image 2" in prompt
    assert "Image 3" not in prompt


def test_six_cast_without_binding_never_cites_an_unattached_image() -> None:
    cast = [f"char_{i}" for i in range(1, 7)]
    cfg = {"pipeline": "clip", "on_screen": cast}

    prompt = compose_practice_prompt(cfg=cfg, graph=None, action="They sit down.")

    cited: set[int] = set()
    for chunk in prompt.split("Image ")[1:]:
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if digits:
            cited.add(int(digits))
    assert sorted(cited) == [1, 2, 3, 4, 5], sorted(cited)
    assert "Image 6" not in prompt
    assert "Image 7" not in prompt


def test_each_cast_member_keeps_every_garment_from_a_shared_lock() -> None:
    cfg = {
        "pipeline": "clip",
        "on_screen": ["char_1", "char_2"],
        "costume_lock": (
            "Grandmother: top=cream blouse; bottom=dark skirt; outerwear=plum cardigan; "
            "Grandfather: top=white shirt; outerwear=brown waistcoat"
        ),
    }

    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        _costume_blocks,
    )

    blocks = dict(_costume_blocks(cfg["costume_lock"]))
    assert "plum cardigan" in blocks["Grandmother"]
    assert "dark skirt" in blocks["Grandmother"]
    assert "brown waistcoat" in blocks["Grandfather"]
    assert "blouse" not in blocks["Grandfather"]


def test_director_wardrobe_field_keeps_one_look_per_shot() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.wardrobe_plan import (
        wardrobe_by_character,
    )

    shot = {
        "wardrobe_by_character": {
            "char_1": "dark green gown with gold embroidery",
            "char_2": "   ",
            "char_3": "charcoal wool coat over cream shirt",
        }
    }
    assert wardrobe_by_character(shot) == {
        "char_1": "dark green gown with gold embroidery",
        "char_3": "charcoal wool coat over cream shirt",
    }
    # No field means the identity card is authoritative for this shot.
    assert wardrobe_by_character({"clip_index": 1}) == {}
    assert wardrobe_by_character(None) == {}
    assert wardrobe_by_character({"wardrobe_by_character": ["nope"]}) == {}


def test_a_shot_wardrobe_override_reaches_the_prompt() -> None:
    cfg = {
        "pipeline": "clip",
        "on_screen": ["char_1"],
        "scene_node_id": "n_scene_2",
        "costume_lock": "Anna: brown linen dress",
        "costume_by_id": {"char_1": "dark green gown with gold embroidery"},
    }

    prompt = compose_practice_prompt(cfg=cfg, graph=None, action="She waits by the door.")

    assert "dark green gown with gold embroidery" in prompt
    assert "brown linen dress" not in prompt


def test_the_graph_carries_the_directors_shot_wardrobe(monkeypatch) -> None:
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
    analysis = {
        "source": "llm",
        "characters": [
            {"id": "char_1", "name": "Anna", "description": "Anna, brown linen dress"},
        ],
        "scenes": [
            {"id": "set_1", "name": "Market", "description": "morning market"},
            {"id": "set_2", "name": "Inn Room", "description": "night inn room"},
        ],
        "clips": [
            {
                "clip_index": 1,
                "action": "Anna buys bread.",
                "camera": "medium",
                "on_screen": ["char_1"],
                "setting_id": "set_1",
                "timeline": "0-5s",
            },
            {
                "clip_index": 2,
                "action": "Anna waits by the door in her gown.",
                "camera": "medium",
                "on_screen": ["char_1"],
                "setting_id": "set_2",
                "timeline": "5-10s",
                "wardrobe_by_character": {"char_1": "dark green gown with gold embroidery"},
            },
        ],
        "audio": {"policy": "silent", "include_speech": False, "include_music": False},
    }

    graph = build_smart_video_graph(
        project_id="wardrobe-plan",
        prompt="A two-scene film where Anna changes for the evening.",
        analysis=analysis,
    )

    clips = [n for n in graph["nodes"] if node_pipeline(n) == "clip"]
    by_index = {int((n["config"] or {}).get("clip_index") or 0): n for n in clips}
    assert by_index[1]["config"].get("costume_by_id") in (None, {})
    assert by_index[2]["config"]["costume_by_id"] == {
        "char_1": "dark green gown with gold embroidery"
    }




def test_a_garment_clause_is_condensed_and_stable() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
        condense_garment,
    )

    zh = "\u7070\u8272\u9488\u7ec7\u5957\u5934\u886b"  # grey knit jumper
    nested = (
        "plain mid-grey knit crew-neck jumper (plain mid-grey knit crew-neck "
        f"jumper ({zh}), fine wool ribbed knit, crew neckline, straight hem), "
        "fine wool ribbed knit, crew neckline, straight hem"
    )
    once = condense_garment(nested)
    assert once == f"plain mid-grey knit crew-neck jumper ({zh})"
    # Reading the same lock again changes nothing.
    assert condense_garment(once) == once
    # A clean clause passes through untouched.
    clean = "\u6885\u7d2b\u8272\u9488\u7ec7\u5f00\u886b"
    assert condense_garment(clean) == clean


def test_a_nameless_garment_block_belongs_to_nobody() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        _costume_blocks,
        _wardrobe_for,
    )

    mother = "\u5988\u5988"
    son = "\u5c11\u5e74\u513f\u5b50"
    stray = "plain mid-grey knit crew-neck jumper"
    lock = f"{mother}: top=green blouse; {stray}; {son}: top=blue hoodie"

    blocks = _costume_blocks(lock)
    assert [who for who, _ in blocks] == [mother, "", son]
    assert "jumper" not in dict(blocks)[mother]

    cfg = {
        "pipeline": "clip",
        "cast_names": [mother],
        "on_screen": ["char_3"],
        "costume_lock": lock,
    }
    graph = {"metadata": {"script_analysis": {"characters": [{"id": "char_3", "name": mother}]}}}
    assert "green blouse" in _wardrobe_for("char_3", cfg, graph)
    assert "jumper" not in _wardrobe_for("char_3", cfg, graph)


def test_a_seated_setting_repeats_its_layout_in_every_shot() -> None:
    layout = "Long table down the centre; grandmother and grandfather at the left end."
    cfg = {
        "pipeline": "clip",
        "on_screen": ["char_1", "char_2"],
        "spatial_lock": {"layout": layout},
        "clip_action": "They sit down.",
    }

    prompt = compose_practice_prompt(cfg=cfg, graph=None, action="They sit down.")

    assert layout in prompt
    assert "people stay in frame together: 1, 2." in prompt
    assert "Positions held for this setting" in prompt


def test_structured_seats_do_not_repeat_the_layout_essay() -> None:
    cfg = {
        "pipeline": "clip",
        "on_screen": ["char_1"],
        "spatial_lock": {"layout": "Long table down the centre."},
        "seat_anchors": {"char_1": {"zone": "left"}},
        "clip_action": "Ruth waits.",
    }

    prompt = compose_practice_prompt(cfg=cfg, graph=None, action="Ruth waits.")

    assert "Long table down the centre" not in prompt


def test_a_stray_slot_in_a_shared_lock_never_reaches_another_character() -> None:
    graph = {
        "metadata": {
            "script_analysis": {
                "characters": [
                    {"id": "char_1", "name": "Father", "clothing_parts": {"top": "grey jumper", "bottom": "navy trousers"}},
                    {"id": "char_2", "name": "Girl", "clothing_parts": {"outfit": "yellow dress"}},
                ]
            }
        }
    }
    cfg = {
        "pipeline": "clip",
        "on_screen": ["char_1", "char_2"],
        # The girl's slots lost their name when the lock was written.
        "costume_lock": "Father: top=grey jumper; bottom=navy trousers; outfit = yellow dress",
        "clip_action": "They sit down.",
    }

    prompt = compose_practice_prompt(cfg=cfg, graph=graph, action="They sit down.")

    sentences = [s.strip() for s in prompt.replace("\u3002", ".").split(".") if s.strip()]
    father = next(s for s in sentences if s.startswith("Father"))
    assert "yellow dress" not in father
    assert "grey jumper" in father


def test_a_shot_wardrobe_keyed_by_id_reaches_a_name_only_composer() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        _wardrobe_for,
    )

    anna = "\u5b89\u5a1c"
    cfg = {
        "pipeline": "clip",
        "cast_names": [anna],
        "on_screen": ["char_1"],
        "costume_by_id": {"char_1": "night gown"},
        "costume_lock": f"{anna}: outfit=day dress",
    }
    graph = {"metadata": {"script_analysis": {"characters": [{"id": "char_1", "name": anna}]}}}
    assert "night gown" in _wardrobe_for(anna, cfg, graph)
    assert "day dress" not in _wardrobe_for(anna, cfg, graph)


def test_an_offscreen_character_never_wears_the_on_screen_look() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        _wardrobe_for,
    )

    anna = "\u5b89\u5a1c"
    baker = "\u9762\u5305\u5e08"
    cfg = {
        "pipeline": "clip",
        "cast_names": [anna],
        "on_screen": ["char_1"],
        "costume_lock": f"{anna}: outfit=day dress",
    }
    graph = {
        "metadata": {
            "script_analysis": {
                "characters": [
                    {"id": "char_1", "name": anna},
                    {"id": "char_2", "name": baker, "costume_lock": f"{baker}: top=white apron"},
                ]
            }
        }
    }
    assert "day dress" not in _wardrobe_for(baker, cfg, graph)


def test_wardrobe_phrase_drops_the_slot_label_and_never_cuts_mid_word() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import wearable_phrase

    assert wearable_phrase("Outer wear: olive raincoat") == "olive raincoat"
    assert wearable_phrase("foot wear=black boots") == "black boots"
    assert wearable_phrase("Outerwear: plum cardigan") == "plum cardigan"

    cfg = {
        "pipeline": "clip",
        "on_screen": ["char_1"],
        "costume_lock": (
            "Outerwear: olive duffel coat, knee-length, heavy wool, natural wood toggles "
            "with rope loops, roomy shoulders, large patch flap pockets, hood folded back, "
            "storm cuffs, corduroy collar"
        ),
    }

    prompt = compose_practice_prompt(cfg=cfg, graph=None, action="Apprentice climbs.")

    assert "Outerwear:" not in prompt
    assert "olive duffel coat" in prompt
    assert "corduroy" not in prompt.split("wearing")[1].split(",")[0]
    assert ", ol" not in prompt  # no mid-word cut such as "ol"
