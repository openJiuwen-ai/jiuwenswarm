# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""M08: prompt_origin=user is authoritative at the WAN / director gate."""

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
    director_approve_video_prompt,
    narrative_seed_from_user_prompt,
)
from jiuwenswarm.server.runtime.designer.pipeline.wan_call_locks import apply_wan_call_locks

# Generic prop-swap language (not white/blue cup pipeline rules).
_USER_PROP = "cobalt flask"
_STALE_PROP = "ceramic bowl"
_STALE_ACTION = "Sam pours cold water into a ceramic bowl near the window"
_USER_ACTION = "Sam raises a cobalt flask toward the lamp"


def _practice_prompt(prop_phrase: str) -> str:
    return (
        "The scene is as in Image 1. "
        "Sam from Image 2, wearing a grey coat, stands near the counter screen-left "
        f"in the scene from Image 1, and {prop_phrase}."
    )


def _cfg(*, origin: str, user_prompt: str, stale_action: str = _STALE_ACTION) -> dict:
    return {
        "clip_action": stale_action,
        "camera": "wide establishing with ceramic bowl",
        "cast_names": ["Sam"],
        "on_screen": ["Sam"],
        "costume_lock": "Sam: grey coat",
        "cast_actions": {"Sam": "pours cold water into a ceramic bowl"},
        "generate": {"prompt": user_prompt, "prompt_origin": origin},
        "prompt": user_prompt,
        "regenerate_packet": {"prompt": user_prompt},
        "style_lock": {"look": "photoreal"},
        "scene_specs": {"place": "the quiet workshop"},
    }


def test_user_origin_keeps_toolbar_prop_over_stale_beat() -> None:
    user_prompt = _practice_prompt(f"raises a {_USER_PROP}")
    cfg = _cfg(origin="user", user_prompt=user_prompt)
    approved, notes = director_approve_video_prompt("agent ignored", cfg=cfg, graph={})
    assert _USER_PROP in approved
    assert _STALE_PROP not in approved
    assert "kept_user_prompt" in notes or "director_approved_user" in notes


def test_storyboard_origin_rewrites_toward_stale_beat() -> None:
    user_prompt = _practice_prompt(f"raises a {_USER_PROP}")
    cfg = _cfg(origin="storyboard", user_prompt=user_prompt)
    approved, notes = director_approve_video_prompt(user_prompt, cfg=cfg, graph={})
    assert _STALE_PROP in approved or "pours" in approved.lower() or "cold water" in approved.lower()
    assert _USER_PROP not in approved
    assert "kept_user_prompt" not in notes


def test_apply_wan_call_locks_stamps_sent_prompt_on_caller_cfg() -> None:
    user_prompt = _practice_prompt(f"raises a {_USER_PROP}")
    cfg = _cfg(origin="user", user_prompt=user_prompt)
    approved = apply_wan_call_locks("agent ignored", cfg=cfg, graph={}, clip_index=2)
    assert _USER_PROP in approved
    assert cfg.get("last_wan_prompt")
    assert _USER_PROP in str(cfg.get("last_wan_prompt") or "")
    assert _USER_PROP in str((cfg.get("generate") or {}).get("prompt") or "")
    assert (cfg.get("generate") or {}).get("prompt_origin") == "user"
    assert cfg.get("director_video_prompt_approved") is True
    assert isinstance(cfg.get("director_video_prompt_notes"), list)


def test_user_origin_lock_essay_seeds_narrative_not_stale_beat() -> None:
    essay = (
        "STYLE LOCK: photoreal. FORBID: morphing. "
        f"{_USER_ACTION}."
    )
    assert _USER_PROP in narrative_seed_from_user_prompt(essay)
    cfg = _cfg(origin="user", user_prompt=essay)
    approved = apply_wan_call_locks("ignored", cfg=cfg, graph={}, clip_index=1)
    assert _USER_PROP in approved
    assert _STALE_PROP not in approved
    assert "FORBID" not in approved
    assert "STYLE LOCK" not in approved


def test_user_origin_empty_falls_back_to_beat_path() -> None:
    cfg = _cfg(origin="user", user_prompt="")
    cfg["generate"] = {"prompt": "", "prompt_origin": "user"}
    cfg["prompt"] = ""
    cfg.pop("regenerate_packet", None)
    approved, notes = director_approve_video_prompt(
        "agent text without binding",
        cfg=cfg,
        graph={},
        action=_STALE_ACTION,
        camera="wide",
    )
    # No user surface → storyboard path may rewrite toward beat.
    assert "kept_user_prompt" not in notes
    assert approved


def test_packet_film_shot_beats_stale_generate_practice() -> None:
    """Stale generate.prompt (prior Image-N) must not override Film-shot packet edit."""
    stale_practice = _practice_prompt(f"holds a {_STALE_PROP}")
    film_shot = (
        f"Film shot 1 only. Camera Wide tracking from outside the {_USER_PROP}. "
        f"Action: {_USER_ACTION}. "
        "CLOTHING LOCK: Sam: grey coat. STRATEGY=compose_from_character_specs. "
        "SCENE SPECS: scene=workshop; views=['front', 'left'"
    )
    cfg = _cfg(origin="user", user_prompt=stale_practice)
    cfg["generate"] = {"prompt": stale_practice, "prompt_origin": "user"}
    cfg["prompt"] = film_shot
    cfg["regenerate_packet"] = {"prompt": film_shot}
    cfg["user_edit_prompt"] = film_shot
    approved = apply_wan_call_locks("agent white narration", cfg=cfg, graph={}, clip_index=1)
    assert _USER_PROP in approved
    assert _STALE_PROP not in approved
    # Durable user intent survives the stamp; last_wan is the sent body.
    assert _USER_PROP in str(cfg.get("user_edit_prompt") or "")
    assert _USER_PROP in str((cfg.get("regenerate_packet") or {}).get("prompt") or "")
    assert _USER_PROP in str(cfg.get("last_wan_prompt") or "")


def test_stamp_does_not_poison_packet_with_api_body() -> None:
    user_prompt = _practice_prompt(f"raises a {_USER_PROP}")
    cfg = _cfg(origin="user", user_prompt=user_prompt)
    cfg["user_edit_prompt"] = user_prompt
    apply_wan_call_locks("ignored", cfg=cfg, graph={}, clip_index=1)
    assert (cfg.get("generate") or {}).get("prompt_origin") == "user"
    assert str((cfg.get("regenerate_packet") or {}).get("prompt") or "") == user_prompt
    assert str(cfg.get("user_edit_prompt") or "") == user_prompt
