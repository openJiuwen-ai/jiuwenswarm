# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Large parameterized edit-prompt-authority + shot-reference-scrub scenarios.

Exercises real pipeline functions with synthetic generic props/cameras/shot
indices — not product-specific café / cup prompts as the only cases.
"""

from __future__ import annotations

import sys
import types
from copy import deepcopy

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

from jiuwenswarm.server.runtime.designer.chat_document_sync import (
    ChatDocument,
    prepare_document_update,
)
from jiuwenswarm.server.runtime.designer.chat_shot_references import (
    referenced_shot_indices,
    scrub_missing_shot_references,
)
from jiuwenswarm.server.runtime.designer.pipeline.wan_call_locks import apply_wan_call_locks

_EDIT_N = 20_000
_PIPELINE_N = 40_000

_ACTIONS = (
    "lifts",
    "raises",
    "rotates",
    "slides",
    "places",
    "inspects",
    "passes",
    "sets down",
)
_PROPS = (
    "cobalt flask",
    "amber vial",
    "teak frame",
    "brass compass",
    "linen pouch",
    "slate tablet",
    "copper kettle",
    "oak token",
    "glass prism",
    "iron latch",
    "ceramic dish",
    "silk ribbon",
    "pine wedge",
    "steel stylus",
    "clay jar",
    "silver ring",
    "wool scarf",
)
_STALE_PROPS = (
    "ivory bowl",
    "plain mug",
    "wooden spoon",
    "paper packet",
    "stone cup",
    "tin plate",
    "cotton cloth",
    "plastic tray",
)
_CAMERAS = (
    "medium shot",
    "close-up",
    "wide establishing",
    "slow push in",
    "orbit right",
    "low angle hold",
)
_CAST = ("Sam", "Alex", "Riley", "Jordan", "Casey", "Morgan")
_DURATION_UNITS = (
    ("seconds", "each shot {n} seconds"),
    ("sec", "every shot {n} sec"),
    ("s", "per shot {n} s"),
    ("分钟", "每个镜头 {n} 分钟"),
    ("秒", "每个镜头 {n} 秒"),
    ("seconds_cn", "每一镜头 {n} 秒"),
)
_ORIGINS = ("user", "storyboard")
_GRAMMARS = ("duration_fp", "heading", "chain", "mixed", "none")


def _pick(items: tuple[str, ...], index: int, salt: int = 0) -> str:
    return items[(index + salt) % len(items)]


def _practice_prompt(*, cast: str, prop: str, action: str, camera: str) -> str:
    return (
        "The scene is as in Image 1. "
        f"{cast} from Image 2, wearing a grey coat, stands near the counter screen-left "
        f"in the scene from Image 1, and {action} a {prop}. "
        f"Camera: {camera}."
    )


def _stale_action(*, cast: str, stale_prop: str) -> str:
    return f"{cast} pours cold water into a {stale_prop} near the window"


def _duration_line(index: int) -> tuple[int, str]:
    unit_key, template = _DURATION_UNITS[index % len(_DURATION_UNITS)]
    del unit_key
    n = 3 + (index % 10)
    if n == 0:
        n = 3
    return n, template.format(n=n)


def _brief_corpus(
    *,
    live: set[int],
    missing: set[int],
    duration_line: str,
    edit_shot: int,
    edit_phrase: str,
) -> str:
    parts: list[str] = []
    for shot in sorted(live | missing):
        body = edit_phrase if shot == edit_shot else f"action-{shot}"
        parts.append(f"### Shot {shot} | beat-{shot}\n{body}")
    parts.append(duration_line)
    chain = " -> ".join(f"shot {i}" for i in sorted(live | missing))
    if chain:
        parts.append(f"BGM: {chain}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# ~20k edit scenarios: toolbar user-origin, canvas-delete scrub, duration idioms
# ---------------------------------------------------------------------------


def _assert_edit_scenario(index: int) -> None:
    kind = index % 3
    cast = _pick(_CAST, index)
    prop = _pick(_PROPS, index)
    stale_prop = _pick(_STALE_PROPS, index, 3)
    action = _pick(_ACTIONS, index)
    camera = _pick(_CAMERAS, index, 1)
    duration_n, duration_line = _duration_line(index)

    if kind == 0:
        # Toolbar user-origin prompt edit vs stale beats.
        user_prompt = _practice_prompt(
            cast=cast, prop=prop, action=action, camera=camera,
        )
        cfg = {
            "shot_action": _stale_action(cast=cast, stale_prop=stale_prop),
            "camera": f"wide with {stale_prop}",
            "cast_names": [cast],
            "on_screen": [cast],
            "costume_lock": f"{cast}: grey coat",
            "cast_actions": {cast: f"pours cold water into a {stale_prop}"},
            "generate": {"prompt": user_prompt, "prompt_origin": "user"},
            "prompt": user_prompt,
            "style_lock": {"look": "photoreal"},
            "scene_specs": {"place": f"workshop-{index % 17}"},
        }
        approved = apply_wan_call_locks(
            "agent rewrite ignored", cfg=cfg, graph={}, shot_index=1 + (index % 4),
        )
        assert prop in approved
        assert stale_prop not in approved
        assert prop in str(cfg.get("last_wan_prompt") or "")
        assert (cfg.get("generate") or {}).get("prompt_origin") == "user"
        return

    if kind == 1:
        # Canvas-delete stale docs: live {1,2}, missing 3 (+ optional higher).
        live = {1, 2}
        missing = {3}
        if index % 5 == 0:
            missing.add(4 + (index % 3))
        edit_phrase = f"{camera} on {prop}"
        brief = _brief_corpus(
            live=live,
            missing=missing,
            duration_line=duration_line,
            edit_shot=2,
            edit_phrase="medium framing",
        )
        before = {
            "nodes": [
                {"id": "n_brief", "type": "text", "config": {"role": "brief", "prompt": brief}},
                {"id": "n_clip_1", "type": "video", "config": {"role": "clip", "shot_index": 1}},
                {"id": "n_clip_2", "type": "video", "config": {"role": "clip", "shot_index": 2}},
            ],
            "edges": [],
            "metadata": {},
        }
        candidate = deepcopy(before)
        candidate["nodes"][2]["config"]["camera"] = camera
        docs = {"n_brief": ChatDocument("n_brief", "brief", brief, None)}
        edits = [
            {
                "node_id": "n_brief",
                "replacements": [{"old": "medium framing", "new": edit_phrase}],
            }
        ]
        _graph, texts, changed = prepare_document_update(before, candidate, docs, edits)
        assert changed
        assert edit_phrase in texts["n_brief"]
        refs = referenced_shot_indices(texts["n_brief"])
        assert refs <= live
        assert missing.isdisjoint(refs)
        assert duration_n not in refs or duration_n in live
        return

    # Duration-idiom only: scrub / regex must not invent the duration number as a shot.
    text = (
        f"### Shot 1 | open\n{action}\n"
        f"### Shot 2 | mid\n{camera}\n"
        f"{duration_line}\n"
    )
    if index % 2:
        text = scrub_missing_shot_references(text, {1, 2})
    refs = referenced_shot_indices(text)
    assert refs <= {1, 2}
    assert duration_n not in refs or duration_n in {1, 2}


@pytest.mark.parametrize("index", range(_EDIT_N))
def test_user_edit_prompt_authority(index: int) -> None:
    _assert_edit_scenario(index)


# ---------------------------------------------------------------------------
# ~40k whole-pipeline: authority × shot-ref grammar × scrub outcomes
# ---------------------------------------------------------------------------


def _assert_pipeline_scenario(index: int) -> None:
    origin = _ORIGINS[index % len(_ORIGINS)]
    grammar = _GRAMMARS[(index // 2) % len(_GRAMMARS)]
    cast = _pick(_CAST, index, 2)
    prop = _pick(_PROPS, index, 1)
    stale_prop = _pick(_STALE_PROPS, index, 4)
    action = _pick(_ACTIONS, index, 1)
    camera = _pick(_CAMERAS, index, 2)
    duration_n, duration_line = _duration_line(index + 7)
    live = {1, 2}
    missing_extra = 3 + (index % 4)
    if missing_extra in live:
        missing_extra = 3

    # --- Authority matrix (WAN / director) ---
    user_prompt = _practice_prompt(cast=cast, prop=prop, action=action, camera=camera)
    cfg = {
        "shot_action": _stale_action(cast=cast, stale_prop=stale_prop),
        "camera": f"wide with {stale_prop}",
        "cast_names": [cast],
        "on_screen": [cast],
        "costume_lock": f"{cast}: grey coat",
        "cast_actions": {cast: f"pours cold water into a {stale_prop}"},
        "generate": {"prompt": user_prompt, "prompt_origin": origin},
        "prompt": user_prompt,
        "style_lock": {"look": f"look-{index % 13}"},
        "scene_specs": {"place": f"set-{index % 19}"},
    }
    # apply_wan_call_locks is the real choke point (calls director_approve + stamps).
    wan_cfg = dict(cfg)
    wan = apply_wan_call_locks(
        "agent ignored",
        cfg=wan_cfg,
        graph={},
        shot_index=1 + (index % 3),
    )
    notes = list(wan_cfg.get("director_video_prompt_notes") or [])
    if origin == "user":
        assert prop in wan
        assert stale_prop not in wan
        assert prop in str(wan_cfg.get("last_wan_prompt") or "")
        assert (
            "kept_user_prompt" in notes
            or "director_approved_user" in notes
            or "director_rewrote" in notes
        )
    else:
        # Storyboard authority may rewrite toward the stale beat when coverage fails.
        assert wan
        assert "kept_user_prompt" not in notes
        assert "director_approved_user" not in notes

    # --- Shot-ref grammar + scrub outcomes ---
    if grammar == "duration_fp":
        text = duration_line
        assert referenced_shot_indices(text) == set()
        scrubbed = scrub_missing_shot_references(text, live)
        assert referenced_shot_indices(scrubbed) == set()
        return

    if grammar == "heading":
        text = f"### Shot {missing_extra} | stale\nbody\n### Shot 2 | live\nok\n"
        assert missing_extra in referenced_shot_indices(text)
        scrubbed = scrub_missing_shot_references(text, live)
        refs = referenced_shot_indices(scrubbed)
        assert missing_extra not in refs
        assert refs <= live
        return

    if grammar == "chain":
        text = "BGM: " + " -> ".join(
            f"shot {i}" for i in sorted(live | {missing_extra})
        )
        scrubbed = scrub_missing_shot_references(text, live)
        refs = referenced_shot_indices(scrubbed)
        assert missing_extra not in refs
        assert refs <= live
        return

    if grammar == "mixed":
        text = (
            f"### Shot 1 | a\nx\n### Shot 2 | b\ny\n"
            f"### Shot {missing_extra} | gone\nz\n"
            f"{duration_line}\n"
            f"BGM: shot 1 -> shot 2 -> shot {missing_extra}\n"
        )
        scrubbed = scrub_missing_shot_references(text, live)
        refs = referenced_shot_indices(scrubbed)
        assert refs <= live
        assert missing_extra not in refs
        assert duration_n not in refs or duration_n in live
        return

    # grammar == "none": live-only prose; scrub is a no-op identity for refs.
    text = "### Shot 1 | a\n### Shot 2 | b\n"
    scrubbed = scrub_missing_shot_references(text, live)
    assert referenced_shot_indices(scrubbed) == {1, 2}


@pytest.mark.parametrize("index", range(_PIPELINE_N))
def test_edit_prompt_authority_and_shot_scrub_pipeline(index: int) -> None:
    _assert_pipeline_scenario(index)
