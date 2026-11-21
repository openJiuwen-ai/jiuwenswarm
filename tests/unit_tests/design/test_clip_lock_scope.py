"""Locks stay where their authority is, and a clip prompt fits the model's budget.

The character sheet shows face and wardrobe from several angles and the scene sheet
shows the room, so a clip binds them by slot instead of restating them in words. What
a clip must still carry is the shot, the seats, the lines and any change — and it must
fit the same character budget the gate checks, cut at sentence boundaries.
"""

from __future__ import annotations

import json

from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
    compose_practice_prompt,
    director_approve_video_prompt,
    fit_prompt_to_budget,
    prompt_respects_practice,
    resolve_clip_prompt_budget,
)

NAMES = {"char_1": "Grandma", "char_2": "Maya"}


def _graph() -> dict:
    return {
        "nodes": [],
        "metadata": {
            "script_analysis": {
                "characters": [
                    {"id": "char_1", "name": "Grandma"},
                    {"id": "char_2", "name": "Maya"},
                ]
            }
        },
    }


def _cfg(**overrides) -> dict:
    cfg = {
        "pipeline": "clip",
        "setting_id": "set_1",
        "on_screen": ["char_1", "char_2"],
        "character_node_ids": ["n_character_1", "n_character_2"],
        "character_ids": ["char_1", "char_2"],
        "cast_names": ["Grandma", "Maya"],
        "scene_node_id": "n_scene_1",
        "costume_lock": "Grandma: burgundy turtleneck; Maya: goose-yellow knit dress",
        "scene_specs": {
            "scene_name": "the small living room",
            "lighting": "warm lamp key light from the ceiling",
            "objects": ["long table", "sideboard"],
        },
        "style_lock": {"look": "photoreal cinematic"},
        "seat_anchors": {
            "char_1": {"pose": "seated", "landmark": "table", "facing": "the window"},
            "char_2": {"pose": "seated", "landmark": "table", "facing": "the door"},
        },
        "clip_action": "Grandma lifts her glass; Maya watches her.",
        "camera": "fixed medium shot from the doorway",
        "language_lock": "en",
    }
    cfg.update(overrides)
    return cfg


def test_a_clip_binds_the_sheets_instead_of_restating_wardrobe() -> None:
    prompt = compose_practice_prompt(cfg=_cfg(), graph=_graph(), action="Grandma lifts her glass.")

    assert "Grandma" in prompt
    assert "burgundy turtleneck" not in prompt
    assert "goose-yellow knit dress" not in prompt


def test_a_clip_without_a_sheet_still_states_the_wardrobe() -> None:
    cfg = _cfg(character_node_ids=[], character_ids=["char_1", "char_2"], on_screen=["char_1"])

    prompt = compose_practice_prompt(cfg=cfg, graph=_graph(), action="Grandma lifts her glass.")

    assert "wearing" in prompt.lower()


def test_a_clip_with_a_scene_sheet_does_not_repeat_the_room_description() -> None:
    prompt = compose_practice_prompt(cfg=_cfg(), graph=_graph(), action="Grandma lifts her glass.")

    assert "warm lamp key light" not in prompt
    assert "long table" not in prompt
    assert "small living room" in prompt


def test_a_scene_without_a_plate_keeps_its_description() -> None:
    cfg = _cfg(scene_node_id="")

    prompt = compose_practice_prompt(cfg=cfg, graph=_graph(), action="Grandma lifts her glass.")

    assert "warm lamp key light" in prompt


def test_a_wardrobe_delta_still_reaches_the_clip() -> None:
    cfg = _cfg(wardrobe_delta={"char_2": "now wearing Grandma's shawl over her dress"})

    prompt = compose_practice_prompt(cfg=cfg, graph=_graph(), action="Maya wraps the shawl.")

    assert "now wearing Grandma's shawl" in prompt


def test_the_gate_and_the_composer_use_one_budget() -> None:
    budget = resolve_clip_prompt_budget()

    assert budget >= 2200
    long_prompt = "A shot sentence. " * (budget // 8)
    assert "too_long" not in prompt_respects_practice(
        "Image 1 is Grandma. The scene is as in Image 3.", cfg=_cfg(), graph=_graph()
    )
    assert "too_long" in prompt_respects_practice(long_prompt, cfg=_cfg(), graph=_graph())


def test_trimming_cuts_whole_sentences_and_reports_them() -> None:
    text = "First sentence stays. Second sentence stays. Third sentence goes."

    fitted, dropped = fit_prompt_to_budget(text, len("First sentence stays. Second sentence stays.") + 1)

    assert fitted == "First sentence stays. Second sentence stays."
    assert dropped == ["Third sentence goes."]


def test_a_composed_prompt_never_exceeds_the_budget() -> None:
    verbose = " ".join(f"Sentence {n} about the dinner." for n in range(400))
    prompt = compose_practice_prompt(
        cfg=_cfg(clip_action=verbose), graph=_graph(), action=verbose
    )

    assert len(prompt) <= resolve_clip_prompt_budget()


def test_a_dropped_speech_line_is_reported() -> None:
    cfg = _cfg(speech_line="char_1: Whoever welcomes one such child welcomes me.")

    reasons = prompt_respects_practice(
        "Image 1 is Grandma. The scene is as in Image 3.", cfg=cfg, graph=_graph()
    )

    assert "missing_speech_line" in reasons


def test_the_verbatim_speech_line_passes_the_gate() -> None:
    cfg = _cfg(speech_line="char_1: Whoever welcomes one such child welcomes me.")

    reasons = prompt_respects_practice(
        "Image 1 is Grandma. The scene is as in Image 3. Grandma, wearing a cardigan, is "
        "seated near the table. She says: Whoever welcomes one such child welcomes me.",
        cfg=cfg,
        graph=_graph(),
    )

    assert "missing_speech_line" not in reasons


def test_a_clip_keeps_the_seat_map_and_the_shot() -> None:
    prompt = compose_practice_prompt(cfg=_cfg(), graph=_graph(), action="Grandma lifts her glass.")

    assert "Grandma lifts her glass" in prompt
    assert "facing the window" in prompt or "seat" in prompt.lower()


def test_a_clip_prompt_still_survives_the_director_gate() -> None:
    approved, notes = director_approve_video_prompt(
        "Grandma lifts her glass.", cfg=_cfg(), graph=_graph(), clip_index=1
    )

    assert approved
    assert json.dumps(notes)  # notes are serialisable for the canvas


def test_a_rewritten_wardrobe_is_rejected_when_a_sheet_is_attached() -> None:
    cfg = _cfg()

    reasons = prompt_respects_practice(
        "Image 1 is Grandma, Image 2 is Maya. The scene is as in Image 3. "
        "Grandma from Image 1, wearing a burgundy turtleneck and a long grey skirt, "
        "seated near the table facing the window.",
        cfg=cfg,
        graph=_graph(),
    )

    assert "wardrobe_restated" in reasons


def test_a_stated_wardrobe_delta_is_allowed() -> None:
    cfg = _cfg()

    reasons = prompt_respects_practice(
        "Image 1 is Grandma, Image 2 is Maya. The scene is as in Image 3. "
        "Maya from Image 2, now wearing Grandma's shawl over her dress, sits near the table.",
        cfg=cfg,
        graph=_graph(),
    )

    assert "wardrobe_restated" not in reasons


def test_a_compliant_agent_prompt_is_kept_instead_of_recomposed() -> None:
    cfg = _cfg()
    agent_text = (
        "Image 1 is Grandma, Image 2 is Maya. The scene is as in Image 3. "
        "Grandma from Image 1, seated near the table facing the window, lifts her glass. "
        "Maya from Image 2, seated near the table facing the door, watches her."
    )

    reasons = prompt_respects_practice(agent_text, cfg=cfg, graph=_graph())

    assert reasons == []


def test_an_agent_prompt_with_a_lock_essay_is_recomposed() -> None:
    from jiuwenswarm.server.runtime.designer.handlers.clip import build_clip_prompt

    cfg = _cfg(
        prompt=(
            "COSTUME LOCK: Grandma must wear the burgundy turtleneck. "
            "Do not change her clothing."
        )
    )
    node = {"id": "n_clip_1", "type": "video", "config": cfg}
    graph = {
        "nodes": [node],
        "edges": [],
        "metadata": {"script_analysis": {"clips": [{"clip_index": 1, "action": "Grandma lifts her glass."}]}},
    }

    prompt = build_clip_prompt(graph, node, None)

    assert "COSTUME LOCK" not in prompt
