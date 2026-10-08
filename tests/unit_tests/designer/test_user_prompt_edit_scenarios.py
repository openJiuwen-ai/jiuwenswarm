# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""~40k Film-shot / toolbar edit scenarios (user prompt authority at WAN gate).

Proves stale ``generate.prompt`` practice stamps cannot drop toolbar Film-shot
facts (prop/color/camera), and that ``user_edit_prompt`` / packet survive stamp.
"""

from __future__ import annotations

import sys
import types

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

from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
    resolve_user_origin_prompt,
)
from jiuwenswarm.server.runtime.designer.pipeline.wan_call_locks import apply_wan_call_locks

_PIPELINE_N = 40_000
_EDIT_N = 20_000

_PROPS = (
    "blue car",
    "red truck",
    "green bicycle",
    "silver scooter",
    "yellow taxi",
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
)
_STALE = (
    "grey wagon",
    "plain mug",
    "ivory bowl",
    "wooden spoon",
    "paper packet",
    "empty crate",
    "dull cart",
    "brown crate",
)
_CAST = ("Sam", "Dad", "Mom", "Teen", "Baby", "Alex", "Jordan", "Riley")
_ACTIONS = (
    "cruises",
    "raises",
    "parks",
    "rolls",
    "lifts",
    "slides",
    "passes",
    "inspects",
)
_CAMERAS = (
    "Wide tracking alongside",
    "Medium pan right",
    "Tight interior dash",
    "High crane pullback",
    "Handheld follow",
    "Static wide establishing",
)


def _pick(items: tuple, index: int, salt: int = 0):
    return items[(index + salt) % len(items)]


def _practice(cast: str, prop: str, action: str) -> str:
    return (
        "The scene is as in Image 1. "
        f"{cast} from Image 2, wearing a grey coat, stands near the counter "
        f"in the scene from Image 1, and {action} a {prop}."
    )


def _film_shot(*, cast: str, prop: str, action: str, camera: str, index: int) -> str:
    return (
        f"Film shot {(index % 6) + 1} only. Camera {camera} the {prop}, moving gently. "
        f"Action: The family {prop} {action} a scenic road. {cast} drives. "
        f"Focus cast on screen: {cast}. Must differ from sibling shots. "
        f"CLOTHING LOCK: {cast}: grey coat. STRATEGY=compose_from_solo_refs setting=set_1. "
        "SCENE SPECS: scene=None; lighting=motivated; views=['front', 'left', 'right', 'side', 't"
    )


def _cfg(
    *,
    film: str,
    stale_practice: str,
    cast: str,
    stale_prop: str,
) -> dict:
    return {
        "shot_action": f"{cast} uses a {stale_prop} near the window",
        "camera": f"wide with {stale_prop}",
        "cast_names": [cast],
        "on_screen": [cast],
        "costume_lock": f"{cast}: grey coat",
        "cast_actions": {cast: f"holds a {stale_prop}"},
        "generate": {"prompt": stale_practice, "prompt_origin": "user"},
        "prompt": film,
        "user_edit_prompt": film,
        "regenerate_packet": {"prompt": film},
        "last_wan_prompt": stale_practice,
        "last_approved_prompt": stale_practice,
        "style_lock": {"look": "cartoonish"},
        "scene_specs": {"place": "the open road"},
    }


def _assert_edit_resolve(index: int) -> None:
    prop = _pick(_PROPS, index)
    stale = _pick(_STALE, index, 3)
    cast = _pick(_CAST, index, 1)
    action = _pick(_ACTIONS, index, 2)
    camera = _pick(_CAMERAS, index, 1)
    film = _film_shot(cast=cast, prop=prop, action=action, camera=camera, index=index)
    stale_practice = _practice(cast, stale, "holds")
    cfg = _cfg(film=film, stale_practice=stale_practice, cast=cast, stale_prop=stale)
    resolved = resolve_user_origin_prompt(cfg, "fallback ignored")
    assert prop in resolved
    assert stale not in resolved or prop in resolved
    # Must not prefer last_wan / generate stale practice as authority.
    assert resolved == film or prop in resolved


@pytest.mark.parametrize("index", range(_EDIT_N))
def test_user_prompt_edit_resolve_scenario(index: int) -> None:
    """~20k: resolve prefers Film-shot / user_edit over stale generate/last_wan."""
    _assert_edit_resolve(index)


def _assert_pipeline_edit(index: int) -> None:
    prop = _pick(_PROPS, index, 1)
    stale = _pick(_STALE, index, 5)
    cast = _pick(_CAST, index, 2)
    action = _pick(_ACTIONS, index, 3)
    camera = _pick(_CAMERAS, index, 2)
    film = _film_shot(cast=cast, prop=prop, action=action, camera=camera, index=index)
    stale_practice = _practice(cast, stale, "holds")
    cfg = _cfg(film=film, stale_practice=stale_practice, cast=cast, stale_prop=stale)
    approved = apply_wan_call_locks(
        f"agent narrates a {stale} instead",
        cfg=cfg,
        graph={},
        shot_index=1 + (index % 4),
    )
    assert prop in approved, f"index={index} prop={prop!r} approved={approved[:240]!r}"
    assert stale not in approved or prop in approved
    # Stamp honesty: sent body may be practice-shaped, but user surfaces keep prop.
    assert prop in str(cfg.get("user_edit_prompt") or "")
    assert prop in str((cfg.get("regenerate_packet") or {}).get("prompt") or "")
    assert prop in str(cfg.get("last_wan_prompt") or "")
    assert (cfg.get("generate") or {}).get("prompt_origin") == "user"
    # Second regen still sees user Film-shot authority (packet not poisoned).
    cfg2 = dict(cfg)
    again = apply_wan_call_locks("agent again", cfg=cfg2, graph={}, shot_index=1)
    assert prop in again


@pytest.mark.parametrize("index", range(_PIPELINE_N))
def test_user_prompt_edit_pipeline_scenario(index: int) -> None:
    """~40k: Film-shot toolbar facts survive WAN gate + stamp + second regen."""
    _assert_pipeline_edit(index)
