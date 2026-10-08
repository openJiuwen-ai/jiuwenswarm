# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Still/image user-prompt authority (no hard-coded ensure_still rewrite).

Mirrors the video toolbar authority fix for scene/character/keyframe calls:
``user_edit_prompt`` / packet win; LLM prompt passes through; never rewrite
into a practice empty-plate when the user or leaf already authored text.
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
    resolve_still_call_prompt,
    resolve_user_origin_prompt,
)
from jiuwenswarm.server.runtime.designer.pipeline.wan_call_locks import (
    apply_keyframe_call_locks,
)

_PIPELINE_N = 40_000
_EDIT_N = 20_000

_DETAILS = (
    "red wagon dashboard",
    "blue air-freshener",
    "cream-and-teal canopy",
    "cracked nozzle boot",
    "mile marker 42",
    "joshua trees right",
    "amber interior fill",
    "roof-rack through rear window",
    "hand-painted GAS sign",
    "gravel pull-out",
    "purple mesas on horizon",
    "warm candlelight booth",
    "slate counter left",
    "brass lamp shade",
    "oak window mullion",
    "cobalt flask on sill",
)
_STALE = (
    "One empty setting. The setting is empty. SPATIAL LOCK: setting=set_1; "
    "architecture=keep one coherent place; static_rule=STATIC OBJECTS LOCKED "
    "Later same-setting keyframes reuse"
)
_ROLES = ("scene", "character", "character_design", "frame", "keyframe")


def _pick(items: tuple, index: int, salt: int = 0):
    return items[(index + salt) % len(items)]


def _user_plate(detail: str) -> str:
    return (
        f"Cinematic empty environment plate: {detail}, golden-hour light from "
        "camera-left, completely empty of people, 16:9, one clear image."
    )


def _cfg(*, role: str, user_prompt: str, stale: str = _STALE) -> dict:
    return {
        "role": role,
        "user_edit_prompt": user_prompt,
        "prompt": user_prompt,
        "regenerate_packet": {"prompt": user_prompt},
        "generate": {"prompt": stale, "prompt_origin": "user"},
        "last_approved_prompt": stale,
        "last_wan_prompt": stale,
        "style_lock": {"look": "cartoonish animated feature look"},
        "scene_specs": {"setting_id": "set_1", "architecture": ""},
    }


@pytest.mark.parametrize("index", range(_EDIT_N))
def test_still_user_prompt_resolve_scenario(index: int) -> None:
    detail = _pick(_DETAILS, index)
    role = _pick(_ROLES, index, 3)
    user = _user_plate(detail)
    cfg = _cfg(role=role, user_prompt=user)
    assert resolve_user_origin_prompt(cfg, "leaf ignored") == user
    assert resolve_still_call_prompt(cfg, "leaf ignored") == user
    assert detail in resolve_still_call_prompt(cfg, "")
    assert "SPATIAL LOCK" not in resolve_still_call_prompt(cfg, "")


@pytest.mark.parametrize("index", range(_PIPELINE_N))
def test_still_user_prompt_pipeline_scenario(index: int) -> None:
    detail = _pick(_DETAILS, index, 1)
    role = _pick(_ROLES, index, 5)
    user = _user_plate(detail)
    cfg = _cfg(role=role, user_prompt=user)
    # Leaf narration / stale practice must not win.
    approved = apply_keyframe_call_locks(
        "One empty setting. The setting is empty. One clear image.",
        cfg=cfg,
        graph={},
    )
    assert detail in approved
    assert "SPATIAL LOCK" not in approved
    assert "One empty setting" not in approved
    # Durable user surfaces survive stamp; last_* records what was sent.
    assert cfg.get("user_edit_prompt") == user
    assert (cfg.get("regenerate_packet") or {}).get("prompt") == user
    assert detail in str(cfg.get("last_wan_prompt") or "")
    assert (cfg.get("generate") or {}).get("prompt_origin") == "user"
    assert "still_user_authority" in (cfg.get("director_still_prompt_notes") or [])

    # Second regen: stale generate cannot poison authority.
    cfg["generate"] = {"prompt": _STALE, "prompt_origin": "user"}
    again = apply_keyframe_call_locks(_STALE, cfg=cfg, graph={})
    assert detail in again
    assert cfg.get("user_edit_prompt") == user


def test_still_llm_prompt_passes_without_rewrite() -> None:
    """No user_edit → leaf LLM text is sent as-is (no ensure_still compose)."""
    llm = _user_plate("red wagon dashboard with blue air-freshener")
    cfg = {
        "role": "scene",
        "generate": {"prompt": _STALE, "prompt_origin": "storyboard"},
        "last_approved_prompt": _STALE,
        "style_lock": {"look": "cartoonish"},
        "scene_specs": {"setting_id": "set_1"},
    }
    approved = apply_keyframe_call_locks(llm, cfg=cfg, graph={})
    assert approved == llm
    assert "red wagon" in approved
    assert "still_llm_authority" in (cfg.get("director_still_prompt_notes") or [])


def test_still_user_edit_wins_without_origin_flag() -> None:
    user = _user_plate("cream-and-teal canopy")
    cfg = {
        "role": "scene",
        "user_edit_prompt": user,
        "generate": {"prompt": _STALE},  # origin omitted / lagging
        "last_approved_prompt": _STALE,
    }
    assert resolve_user_origin_prompt(cfg, "") == user
    assert "cream-and-teal" in apply_keyframe_call_locks(_STALE, cfg=cfg, graph={})
