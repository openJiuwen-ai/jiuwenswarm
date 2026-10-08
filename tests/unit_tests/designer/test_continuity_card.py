# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.server.runtime.designer.pipeline.clip_prompt_handoff import (
    handoff_clause_for_prompt,
    stamp_wan_prompt_handoff,
)
from jiuwenswarm.server.runtime.designer.pipeline.continuity_card import (
    continuity_card_clause,
    continuity_card_from_prior,
    extract_already_done_beats,
)
from jiuwenswarm.server.runtime.designer.audio_locks import (
    resolve_video_audio_request,
    video_model_supports_native_audio,
)


def test_extract_already_done_forbids_restarting_run():
    beats = extract_already_done_beats("the man starts running toward the door")
    joined = " ".join(beats).lower()
    assert beats
    assert "onset" in joined or "do not" in joined
    assert "run" in joined


def test_continuity_card_clause_optional_helper_still_works():
    card = continuity_card_from_prior(
        prior_action="woman begins walking away from the altar",
        prior_prompt="HUGE FULL WAN PROMPT " * 80,
        shot_index=1,
        node_id="n_clip_1",
    )
    clause = continuity_card_clause(card)
    assert "CHARACTER CONSISTENCY" in clause
    assert "ALREADY_DONE" in clause


def test_stamp_handoff_saves_own_prompt_and_does_not_stamp_next_clip():
    graph = {
        "nodes": [
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {"role": "clip", "shot_index": 1, "shot_action": "man starts running"},
            },
            {
                "id": "n_clip_2",
                "type": "video",
                "config": {"role": "clip", "shot_index": 2, "continuity_clip_node_id": "n_clip_1"},
            },
        ],
        "metadata": {},
    }
    big = "SHOT1_ONLY_IDENTITY_MARKER " + ("style lock dump " * 40)
    notes = stamp_wan_prompt_handoff(
        graph,
        shot_index=1,
        prompt=big,
        node_id="n_clip_1",
        shot_action="man starts running",
    )
    assert notes
    c1 = graph["nodes"][0]["config"]
    c2 = graph["nodes"][1]["config"]
    assert c1["last_wan_prompt"].startswith("SHOT1_ONLY_IDENTITY_MARKER")
    assert c1["handoff_artifact_ready"] is True
    assert "previous_clip_action" not in c2
    assert "previous_clip_wan_prompt" not in c2
    # Soft-dep may keep a short readiness marker, but clause must not paste the marker.
    clause = handoff_clause_for_prompt(
        [
            {
                "shot_index": 1,
                "node_id": "n_clip_1",
                "shot_action": "man starts running",
                "wan_prompt": big,
            }
        ],
        this_shot_index=2,
        this_action="man enters the hallway",
    )
    assert "PREVIOUS CLIP HAD" in clause
    assert "YOUR ASSIGNMENT" in clause
    assert "SHOT1_ONLY_IDENTITY_MARKER" not in clause
    assert "man starts running" in clause
    assert "man enters the hallway" in clause
    assert "do not" in clause.lower() or "do NOT" in clause


def test_audio_request_never_overrides_model(monkeypatch):
    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "custom-silent-video")
    monkeypatch.delenv("VIDEO_GEN_NATIVE_AUDIO", raising=False)
    assert not video_model_supports_native_audio("custom-silent-video")
    want, override = resolve_video_audio_request(
        {"clip_embedded_audio": True, "include_speech": True, "speech_line": "hello"},
        {"prefer_clip_native_audio": True},
        current_model="custom-silent-video",
    )
    assert want is False
    assert override is None

    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "wan3.0-video")
    want2, override2 = resolve_video_audio_request(
        {"clip_embedded_audio": True, "include_speech": True, "speech_line": "hello"},
        {"prefer_clip_native_audio": True},
        current_model="wan3.0-video",
    )
    assert want2 is True
    assert override2 is None
