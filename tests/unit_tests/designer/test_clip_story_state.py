# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.designer.pipeline.cast_prop_locks import (
    occupancy_clause_for_clip,
)
from jiuwenswarm.server.runtime.designer.pipeline.clip_prompt_handoff import (
    handoff_clause_for_prompt,
    stamp_wan_prompt_handoff,
)
from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
    apply_story_state_to_next_cfg,
    compact_wan_story_clause,
    extract_finished_events,
    narrative_from_wan_prompt,
)
from jiuwenswarm.server.runtime.designer.pipeline.wan_call_locks import apply_wan_call_locks
from jiuwenswarm.server.runtime.designer.pipeline.wan_reference_binding import (
    build_wan_reference_binding,
)


def test_child_walk_away_is_finished_event_not_replayed() -> None:
    chars = [{"id": "char_child", "name": "Child"}, {"id": "char_dad", "name": "Dad"}]
    events = extract_finished_events(
        "Dad sits by the window. Child walked away from the table.",
        characters=chars,
        on_screen=["char_child", "char_dad"],
        shot_index=1,
    )
    kinds = {e["type"] for e in events}
    assert "exit" in kinds
    child_exit = next(e for e in events if e.get("character_id") == "char_child")
    assert "walk" in child_exit["already_done"].lower() or "left" in child_exit["already_done"].lower()
    assert not any(e.get("character_id") == "char_dad" and e["type"] == "exit" for e in events)


def test_seat_hold_and_exit_stamp_onto_next_clip() -> None:
    graph = {"nodes": [], "metadata": {}}
    src = {
        "setting_id": "set_1",
        "on_screen": ["char_dad", "char_child"],
        "blocking": {
            "landmark": "window",
            "positions": [
                {"character_id": "char_dad", "zone": "head_of_table", "near": "window"},
            ],
        },
        "screen_axis": {"char_dad": "screen_center"},
    }
    nxt = {
        "setting_id": "set_1",
        "on_screen": ["char_dad", "char_mum"],
        "offscreen": [],
        "blocking": {
            "landmark": "window",
            "positions": [
                {"character_id": "char_mum", "zone": "left_chair"},
            ],
        },
    }
    chars = [
        {"id": "char_dad", "name": "Dad"},
        {"id": "char_child", "name": "Child"},
        {"id": "char_mum", "name": "Mum"},
    ]
    out = apply_story_state_to_next_cfg(
        nxt,
        from_cfg=src,
        from_prompt="character1 Dad sits at the window. Child walked away from the table.",
        from_action="Dad reads; child walks away",
        from_shot_index=1,
        graph=graph,
        characters=chars,
    )
    done = " ".join(out.get("already_done") or []).lower()
    assert "child" in done
    assert "left" in done or "walk" in done or "exit" in done
    assert "char_child" in (out.get("offscreen") or [])
    assert "char_child" not in (out.get("on_screen") or ["char_dad", "char_mum"])
    seats = out.get("seat_anchors") or {}
    assert "char_dad" in seats
    assert "window" in str(seats["char_dad"]).lower()
    clause = compact_wan_story_clause(out, characters=chars)
    assert "SEAT HOLDS" in clause
    assert "Dad" in clause
    assert "window" in clause.lower()
    assert "already_done" not in clause.lower()
    assert "do not" not in clause.lower()
    assert "Child" not in clause


def test_wan_api_clause_does_not_paste_prior_prompt_marker() -> None:
    marker = "SHOT1_ONLY_IDENTITY_MARKER_XYZ"
    graph = {
        "nodes": [
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {
                    "role": "clip",
                    "shot_index": 1,
                    "shot_action": "child walked away",
                    "on_screen": ["char_child"],
                },
            },
            {
                "id": "n_clip_2",
                "type": "video",
                "config": {
                    "role": "clip",
                    "shot_index": 2,
                    "scene_node_id": "n_scene_1",
                    "on_screen": ["char_dad"],
                    "setting_id": "set_1",
                },
            },
        ],
        "metadata": {},
    }
    stamp_wan_prompt_handoff(
        graph,
        shot_index=1,
        prompt=f"{marker} STYLE LOCK dump " + ("lock " * 20) + " Child walked away from the table.",
        node_id="n_clip_1",
        shot_action="child walked away",
    )
    c1 = graph["nodes"][0]["config"]
    c2 = graph["nodes"][1]["config"]
    # Handoff keeps the Wan prompt on the completed clip only (no next-clip paste).
    assert marker in str(c1.get("last_wan_prompt") or "")
    assert marker not in str(c2.get("previous_clip_wan_prompt") or "")
    wan = apply_wan_call_locks(
        "character1 sits with the family at the table in the last reference.",
        cfg=c2,
        graph=graph,
        shot_index=2,
    )
    assert marker not in wan
    assert "from Image" in wan or "from image" in wan.lower() or "Image 1 is" in wan
    assert "do not" not in wan.lower()
    assert "R2V CAST LOCK" not in wan
    assert "animate ONLY people already in Image 1" not in wan
    bind = build_wan_reference_binding(cfg=c2, graph=graph, prior_last_frame=True, prior_last_frame_count=1)
    assert "NOT attached" in bind or "not attached" in bind.lower()
    clause = handoff_clause_for_prompt(
        [{"shot_index": 1, "shot_action": "child walked away", "wan_prompt": marker * 8}],
        this_shot_index=2,
        this_action="family sits together",
    )
    assert marker not in clause
    assert "NEW" in clause or "do not" in clause.lower()


def test_r2v_occupancy_does_not_use_image1_cast_lock() -> None:
    text = occupancy_clause_for_clip(
        {"occupancy": {"must_appear": ["char_1"], "must_not_appear": ["char_2"]}},
        [{"id": "char_1", "name": "Dad"}, {"id": "char_2", "name": "Child"}],
    )
    assert "R2V CAST LOCK" in text
    assert "Image 1" not in text
    assert "Child" in text
    assert "peopled" in text.lower() or "last environment" in text.lower()


def test_narrative_strips_lock_banners() -> None:
    raw = (
        "STYLE LOCK (film-wide): photoreal\n\n"
        "character1 sits by the window and reads.\n"
        "CLOTHING LOCK: blue shirt\n"
    )
    out = narrative_from_wan_prompt(raw)
    assert "sits by the window" in out
    assert "STYLE LOCK" not in out
    assert "CLOTHING LOCK" not in out


@pytest.mark.asyncio
async def test_clip_execute_does_not_attach_prior_last_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_ROLE_CHARACTER_DESIGN,
        NODE_ROLE_SCENE,
        NODE_TYPE_IMAGE,
        NODE_TYPE_VIDEO,
        SCHEMA_VERSION,
        normalize_execution_graph,
    )
    from jiuwenswarm.server.runtime.designer.handlers.clip import ClipNodeHandler
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    solo = tmp_path / "dad.png"
    scene = tmp_path / "room.png"
    last = tmp_path / "shot1_lastframe.jpg"
    video = tmp_path / "out.mp4"
    solo.write_bytes(b"png-dad")
    scene.write_bytes(b"png-room")
    last.write_bytes(b"jpg-last")
    video.write_bytes(b"fake-mp4")
    graph = normalize_execution_graph(
        {
            "schema_version": SCHEMA_VERSION,
            "graph_id": "g_state",
            "project_id": "p_state",
            "title": "state",
            "nodes": [
                {
                    "id": "n_character",
                    "type": NODE_TYPE_IMAGE,
                    "config": {
                        "role": NODE_ROLE_CHARACTER_DESIGN,
                        "character_ids": ["char_dad"],
                    },
                },
                {
                    "id": "n_scene_1",
                    "type": NODE_TYPE_IMAGE,
                    "config": {"role": NODE_ROLE_SCENE, "setting_id": "set_1"},
                },
                {
                    "id": "n_clip_2",
                    "type": NODE_TYPE_VIDEO,
                    "config": {
                        "role": "clip",
                        "shot_index": 2,
                        "first_of_setting": False,
                        "scene_node_id": "n_scene_1",
                        "character_node_ids": ["n_character"],
                        "on_screen": ["char_dad"],
                        "setting_id": "set_1",
                        "shot_action": "Dad stays seated by the window",
                        "use_prior_last_frame": True,
                        "previous_clip_last_frame": str(last),
                        "previous_clip_action": "Child walks away from the table",
                        "previous_clip_wan_prompt": (
                            "The child walks away from the table toward the door."
                        ),
                        "already_done": [
                            "shot 1: Child already left — do not show walking away"
                        ],
                        "scene_last_frame_chain": [
                            {"path": str(last), "shot_index": 1, "action": "dad close-up"}
                        ],
                    },
                },
            ],
            "edges": [],
        }
    )
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_state",
        node_id="n_clip_2",
        run={
            "node_states": {
                "n_character": {
                    "status": "completed",
                    "output_ref": file_output_ref(solo, kind=NODE_TYPE_IMAGE, mime_type="image/png"),
                },
                "n_scene_1": {
                    "status": "completed",
                    "output_ref": file_output_ref(scene, kind=NODE_TYPE_IMAGE, mime_type="image/png"),
                },
            }
        },
    )
    seen: dict[str, object] = {}

    async def fake_generate(
        prompt: str,
        save_dir: str | None = None,
        first_frame: str | None = None,
        reference_images: list[str] | None = None,
        **kwargs,
    ) -> dict[str, str]:
        seen["first_frame"] = first_frame
        seen["reference_images"] = [str(x) for x in (reference_images or [])]
        seen["prompt"] = prompt
        return {"video_path": str(video), "revised_prompt": prompt}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_generate,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.pipeline.clip_last_frame_handoff.extract_last_frame",
        lambda *a, **k: None,
    )
    clip = next(n for n in graph["nodes"] if n["id"] == "n_clip_2")
    await ClipNodeHandler().execute(clip, ctx)
    refs = [Path(x).resolve() for x in (seen.get("reference_images") or [])]
    assert last.resolve() not in refs
    assert solo.resolve() in refs
    assert scene.resolve() in refs
    assert seen.get("first_frame") in (None, "")
    prompt = str(seen.get("prompt") or "")
    assert str(last.resolve()).lower() not in prompt.lower()
    assert "from Image" in prompt or "from image" in prompt.lower() or "image 1 is" in prompt.lower()
    assert "ALREADY_DONE" not in prompt
    assert "do not" not in prompt.lower()
    assert "FORBID" not in prompt.upper()


@pytest.mark.asyncio
async def test_first_clip_uses_solos_and_empty_scene_not_first_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_ROLE_CHARACTER_DESIGN,
        NODE_ROLE_SCENE,
        NODE_TYPE_IMAGE,
        NODE_TYPE_VIDEO,
        SCHEMA_VERSION,
        normalize_execution_graph,
    )
    from jiuwenswarm.server.runtime.designer.handlers.clip import ClipNodeHandler
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    solo = tmp_path / "dad.png"
    scene = tmp_path / "composed.png"
    video = tmp_path / "out.mp4"
    solo.write_bytes(b"png-dad")
    scene.write_bytes(b"png-scene")
    video.write_bytes(b"fake-mp4")
    graph = normalize_execution_graph(
        {
            "schema_version": SCHEMA_VERSION,
            "graph_id": "g_first",
            "project_id": "p_first",
            "title": "first",
            "nodes": [
                {
                    "id": "n_character",
                    "type": NODE_TYPE_IMAGE,
                    "config": {
                        "role": NODE_ROLE_CHARACTER_DESIGN,
                        "character_ids": ["char_dad"],
                    },
                },
                {
                    "id": "n_scene_1",
                    "type": NODE_TYPE_IMAGE,
                    "config": {"role": NODE_ROLE_SCENE, "setting_id": "set_1", "composed_scene": True},
                },
                {
                    "id": "n_clip_1",
                    "type": NODE_TYPE_VIDEO,
                    "config": {
                        "role": "clip",
                        "shot_index": 1,
                        "first_of_setting": True,
                        "scene_node_id": "n_scene_1",
                        "character_node_ids": ["n_character"],
                        "on_screen": ["char_dad"],
                        "setting_id": "set_1",
                    },
                },
            ],
            "edges": [],
        }
    )
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_first",
        node_id="n_clip_1",
        run={
            "node_states": {
                "n_character": {
                    "status": "completed",
                    "output_ref": file_output_ref(solo, kind=NODE_TYPE_IMAGE, mime_type="image/png"),
                },
                "n_scene_1": {
                    "status": "completed",
                    "output_ref": file_output_ref(scene, kind=NODE_TYPE_IMAGE, mime_type="image/png"),
                },
            }
        },
    )
    seen: dict[str, object] = {}

    async def fake_generate(
        prompt: str,
        save_dir: str | None = None,
        first_frame: str | None = None,
        reference_images: list[str] | None = None,
        **kwargs,
    ) -> dict[str, str]:
        seen["first_frame"] = first_frame
        seen["reference_images"] = [str(x) for x in (reference_images or [])]
        seen["force_reference_mode"] = kwargs.get("force_reference_mode")
        seen["prompt"] = prompt
        return {"video_path": str(video), "revised_prompt": prompt}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_generate,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.pipeline.clip_last_frame_handoff.extract_last_frame",
        lambda *a, **k: None,
    )
    clip = next(n for n in graph["nodes"] if n["id"] == "n_clip_1")
    await ClipNodeHandler().execute(clip, ctx)
    refs = [Path(x).resolve() for x in (seen.get("reference_images") or [])]
    assert seen.get("first_frame") in (None, "")
    assert solo.resolve() in refs
    assert scene.resolve() in refs
    assert seen.get("force_reference_mode") is True
    prompt = str(seen.get("prompt") or "")
    assert "from Image" in prompt or "from image" in prompt.lower() or "image 1 is" in prompt.lower()
    assert "ALREADY_DONE" not in prompt
    assert "do not" not in prompt.lower()
    assert "empty" in prompt.lower() or "scene" in prompt.lower() or "room" in prompt.lower()


def test_smart_graph_scene_card_is_empty_plate() -> None:
    from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph
    from jiuwenswarm.server.runtime.designer.pipeline.production_bible import build_production_bible

    graph = build_smart_video_graph(
        project_id="p_first",
        prompt="Dad sits by the window. Mum pours tea. Child walks to the door.",
        analysis={
            "characters": [
                {"id": "char_dad", "name": "Dad", "description": "navy sweater"},
                {"id": "char_mum", "name": "Mum", "description": "red dress"},
                {"id": "char_child", "name": "Child", "description": "blue shirt"},
            ],
            "scenes": [
                {
                    "id": "set_1",
                    "name": "Family room",
                    "description": "window, tea table, and door",
                }
            ],
            "shots": [
                {
                    "shot_index": 1,
                    "setting_id": "set_1",
                    "action": "Dad sits by the window, Mum pours tea",
                    "on_screen": ["char_dad", "char_mum", "char_child"],
                    "camera": "wide",
                    "timeline": "0-5s",
                },
                {
                    "shot_index": 2,
                    "setting_id": "set_1",
                    "action": "Child walks to the door",
                    "on_screen": ["char_dad", "char_mum"],
                    "camera": "medium",
                    "timeline": "5-10s",
                },
            ],
        },
    )
    scene = next(n for n in graph["nodes"] if str(n.get("id") or "").startswith("n_scene"))
    scfg = scene.get("config") or {}
    prompt = str(scfg.get("prompt") or "")
    low = prompt.lower()
    assert "empty" in low or "furniture" in low or "environment plate" in low
    assert "STYLE LOCK" not in prompt
    assert "no people" not in low
    assert "do not" not in low
    assert scfg.get("composed_scene") is False
    assert not scfg.get("character_node_ids")
    assert scfg.get("style_lock")
    clip1 = next(n for n in graph["nodes"] if n.get("id") == "n_clip_1")
    clip2 = next(n for n in graph["nodes"] if n.get("id") == "n_clip_2")
    assert (clip1.get("config") or {}).get("first_of_setting") is True
    assert (clip2.get("config") or {}).get("first_of_setting") is False
    assert (clip1.get("config") or {}).get("style_lock")
    clip1_prompt = str(((clip1.get("config") or {}).get("generate") or {}).get("prompt") or "")
    clip2_prompt = str(((clip2.get("config") or {}).get("generate") or {}).get("prompt") or "")
    assert "from Image" in clip1_prompt or "from image" in clip1_prompt.lower() or "image 1 is" in clip1_prompt.lower()
    assert "from Image" in clip2_prompt or "from image" in clip2_prompt.lower() or "image 1 is" in clip2_prompt.lower()
    assert "in the scene from" in clip2_prompt.lower() or "are in" in clip2_prompt.lower() or "scene is as in" in clip2_prompt.lower()
    assert "ALREADY_DONE" not in clip2_prompt
    # Language lock may say "Do not switch languages" — that is fine; ban
    # finished-event forbid banners only.
    assert "do not show" not in clip2_prompt.lower()
    assert "do not redo" not in clip2_prompt.lower()
    assert "do not restart" not in clip2_prompt.lower()
    assert graph["metadata"].get("freeze_shot_topology") is True
    brief = next(n for n in graph["nodes"] if n.get("id") == "n_brief")
    assert "prewritten" not in (brief.get("config") or {})
    assert "draft_prewritten" not in (brief.get("config") or {})
    bible = build_production_bible(
        graph.get("metadata", {}).get("script_analysis") or {},
        user_prompt=str(graph.get("description") or graph.get("prompt") or ""),
    )
    assert "USER INTENT" in bible
    assert "PRODUCTION LOCK BIBLE" in bible


def test_wan_prompt_drops_already_done_and_keeps_style() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.wan_prompt_hygiene import (
        scrub_negative_wan_prompt,
    )

    raw = (
        "STYLE LOCK (film-wide): watercolor storybook.\n"
        "Dad stays at the table.\n"
        'ALREADY_DONE (do not restage): the child says "I am leaving".\n'
        "This window: Mum pours tea."
    )
    cleaned = scrub_negative_wan_prompt(raw)
    assert "STYLE LOCK" in cleaned
    assert "Dad stays" in cleaned
    assert "Mum pours tea" in cleaned
    assert "ALREADY_DONE" not in cleaned
    assert "I am leaving" not in cleaned


def test_regenerate_packet_keeps_prompt_and_upstream_image(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.wan_prompt_hygiene import (
        capture_regenerate_packet,
    )

    image = tmp_path / "dad.png"
    image.write_bytes(b"png")
    uri = image.resolve().as_uri()
    node = {
        "id": "n_clip_1",
        "config": {
            "prompt": "Dad reads at the table.",
            "shot_action": "Dad reads",
            "style_lock": {"look": "watercolor", "medium": "stylized_animation"},
            "speech_line": "Good evening",
            "character_node_ids": ["n_character"],
            "scene_node_id": "n_scene_1",
        },
    }
    graph = {
        "nodes": [
            node,
            {
                "id": "n_character",
                "config": {"prompt": "ONE person only: Dad. STYLE LOCK watercolor."},
            },
        ]
    }
    states = {
        "n_character": {
            "status": "completed",
            "output_ref": {"uri": uri, "mime_type": "image/png"},
        }
    }
    packet = capture_regenerate_packet(node, graph, states)
    assert "Dad reads" in packet["prompt"]
    assert packet["style_lock"]["look"] == "watercolor"
    assert packet["speech_line"] == "Good evening"
    assert any(str(image.resolve()) == str(Path(p).resolve()) for p in packet["reference_images"])
    assert packet["upstream"][0]["prompt"].startswith("ONE person")


def test_director_rewrites_lock_essay_into_image_binding() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        director_approve_video_prompt,
    )

    essay = (
        "STYLE LOCK (film-wide, non-negotiable): photoreal cinematic — never cartoon restyle.\n"
        "FORBID: no morphing, no extras. The Teenage Son does not appear.\n"
        "The Mother nods once while the Young Child leans forward.\n"
        'The Father says: "You shall write them on the doorposts of your house and on your gates."'
    )
    cfg = {
        "cast_names": ["Mother", "Young Child"],
        "on_screen": ["Mother", "Young Child"],
        "offscreen": ["Father", "Teenage Son"],
        "shot_action": "The Mother nods once while the Young Child leans forward.",
        "camera": "slow pan right",
        "costume_lock": "Mother: dusty rose blouse; Young Child: pale yellow sweater",
        "speech_by_character": {
            "Father": "You shall write them on the doorposts of your house and on your gates.",
        },
        "previous_clip_wan_prompt": (
            "The scene is as in Image 3: the warm dining room. "
            "Mother from Image 1, wearing dusty rose, seated near the table screen-left "
            "in the scene from Image 3, is listening. "
            "Young Child from Image 2, wearing pale yellow, seated near the table screen-right "
            "in the scene from Image 3, is leaning forward."
        ),
        "previous_clip_action": "The Mother listens while the Young Child leans forward.",
        "seat_anchors": {
            "Mother": {"pose": "seated", "landmark": "table", "screen": "screen-left"},
            "Young Child": {"pose": "seated", "landmark": "table", "screen": "screen-right"},
        },
        "scene_specs": {
            "place": "the warm dining room",
            "lighting": "warm golden-hour from the window",
            "objects": ["wooden table", "Bible"],
        },
        "style_lock": {"look": "photoreal cinematic"},
    }
    approved, notes = director_approve_video_prompt(essay, cfg=cfg, graph={})
    assert "director_rewrote" in notes
    low = approved.lower()
    assert "mother from image 1" in low or "from image 1" in low
    assert "young child from image 2" in low or "from image 2" in low
    assert "scene is as in image 3" in low or "in the scene from image 3" in low
    assert "wearing" in low
    # Silent offscreen omitted; speaking offscreen may be heard — exited never named.
    assert "teenage son" not in low
    assert (
        "continue after" in low
        or "same setting" in low
        or "same placement" in low
        or "continues" in low
        or "scene is as in image" in low
        or "in the scene from image" in low
    )
    assert "seated" in low or "near the table" in low or "screen-left" in low
    assert "scene description" in low or "golden-hour" in low or "warm" in low
    assert "nods once" in approved or "leans forward" in approved.lower()
    assert "doorposts" in approved
    assert "FORBID" not in approved
    assert "do not" not in approved.lower()
    assert "STYLE LOCK" not in approved
    assert "On screen:" not in approved
    assert "for example" not in low


def test_compose_weaves_language_and_time_of_day() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        compose_practice_prompt,
        ensure_story_lock_coverage,
    )

    cfg = {
        "cast_names": ["Alex"],
        "on_screen": ["Alex"],
        "shot_action": "Alex speaks to the camera.",
        "speech_line": "We keep going.",
        "language_lock": "en",
        "costume_lock": "Alex: grey coat",
        "time_of_day_lock": {
            "time_of_day": "night",
            "lighting": "cool night practicals with stable key direction",
        },
        "scene_specs": {"scene_name": "empty room", "objects": ["lamp"]},
        "style_lock": {"look": "photoreal cinematic"},
    }
    text = compose_practice_prompt(cfg=cfg, graph={}, action=cfg["shot_action"])
    low = text.lower()
    assert "spoken dialogue is in english" in low
    assert "night" in low
    assert "wearing" in low
    assert "style lock" not in low
    assert "time of day lock" not in low
    assert "do not" not in low

    # Coverage helper fills gaps without LOCK banners.
    sparse = "The scene is as in Image 2: empty room. Alex from Image 1 is speaking."
    filled, notes = ensure_story_lock_coverage(sparse, cfg)
    assert notes
    assert "spoken dialogue is in english" in filled.lower()
    assert "night" in filled.lower()
    assert "LANGUAGE LOCK" not in filled
    assert "TIME OF DAY LOCK" not in filled


def test_compose_keeps_prior_prompt_continuity() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        compose_practice_prompt,
    )

    cfg = {
        "cast_names": ["Dad", "Mum"],
        "offscreen": ["Child"],
        "shot_action": "Dad and Mum sit quietly.",
        "camera": "slow push in",
        "costume_lock": "Dad: navy sweater; Mum: red dress",
        "previous_clip_wan_prompt": (
            "The scene is as in Image 2: kitchen. "
            "Dad from Image 1, wearing navy sweater, is walking to the door."
        ),
        "previous_clip_action": "Dad walks to the door",
        "already_done": ["Dad walked to the door"],
        "seat_anchors": {"Dad": {"pose": "seated", "screen": "screen-left"}},
        "scene_specs": {
            "place": "kitchen",
            "lighting": "soft morning light",
            "objects": ["table"],
        },
        "style_lock": {"look": "photoreal cinematic"},
    }
    text = compose_practice_prompt(cfg=cfg, graph={}, action=cfg["shot_action"], camera="slow push in")
    low = text.lower()
    assert "dad from image 1" in low or "from image 1" in low
    assert "scene is as in" in low or "in the scene from" in low
    assert "wearing" in low
    # Silent offscreen Child is omitted from the video call (lock stays on node).
    assert "child" not in low or "child" in (cfg.get("cast_names") or [])
    assert "continue after" in low or "same setting" in low or "same placement" in low
    assert "seated" in low or "screen-left" in low
    assert "scene description" in low or "morning light" in low
    assert "sit quietly" in low or "dad and mum" in low
    assert "On screen:" not in text
    assert "do not" not in low


def test_exited_cast_omitted_until_returned() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
        compose_practice_prompt,
    )

    cfg = {
        "cast_names": ["Mum"],
        "on_screen": ["Mum"],
        "exited_ids": ["char_child"],
        "shot_action": "Mum pours tea.",
        "costume_lock": "Mum: red dress",
        "scene_specs": {"scene_name": "kitchen", "lighting": "soft light"},
        "style_lock": {"look": "photoreal cinematic"},
    }
    graph = {
        "metadata": {
            "script_analysis": {
                "characters": [
                    {"id": "char_mum", "name": "Mum"},
                    {"id": "char_child", "name": "Child"},
                ]
            }
        }
    }
    text = compose_practice_prompt(cfg=cfg, graph=graph, action=cfg["shot_action"])
    assert "Mum" in text
    assert "Child" not in text
    assert "do not" not in text.lower()

def test_prior_clip_story_pull_does_not_copy_prior_wan_prompt() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.clip_story_state import (
        ensure_prior_clip_story_on_cfg,
    )

    graph = {
        "nodes": [
            {
                "id": "n_clip_1",
                "config": {
                    "role": "clip",
                    "shot_index": 1,
                    "setting_id": "set_dining",
                    "last_wan_prompt": "Image 1 is Father.\nFather sits at the table.",
                    "shot_action": "Father sits at the table",
                },
            },
            {
                "id": "n_clip_2",
                "config": {
                    "role": "clip",
                    "shot_index": 2,
                    "setting_id": "set_dining",
                    "continuity_clip_node_id": "n_clip_1",
                },
            },
        ]
    }
    out = ensure_prior_clip_story_on_cfg(dict(graph["nodes"][1]["config"]), graph)
    assert "continuity_clip_node_id" not in out
    assert not out.get("previous_clip_wan_prompt")
    assert out["setting_id"] == "set_dining"
