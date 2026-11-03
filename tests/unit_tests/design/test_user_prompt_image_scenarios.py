# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Still/image user-prompt authority (no hard-coded ensure_still rewrite).

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

from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
    resolve_user_origin_prompt,
)
from jiuwenswarm.server.runtime.designer.pipeline.wan_call_locks import (
    apply_image_call_locks,
)

_STALE = (
    "One empty setting. The setting is empty. SPATIAL LOCK: setting=set_1; "
    "architecture=keep one coherent place; static_rule=STATIC OBJECTS LOCKED "
    "Later same-setting clips reuse"
)


def _user_plate(detail: str) -> str:
    return (
        f"Cinematic empty environment plate: {detail}, golden-hour light from "
        "camera-left, completely empty of people, 16:9, one clear image."
    )


def test_image_llm_prompt_passes_without_rewrite() -> None:
    """No user_edit → leaf LLM text is sent as-is (no ensure_still compose)."""
    llm = _user_plate("red wagon dashboard with blue air-freshener")
    cfg = {
        "role": "scene",
        "generate": {"prompt": _STALE, "prompt_origin": "storyboard"},
        "last_approved_prompt": _STALE,
        "style_lock": {"look": "cartoonish"},
        "scene_specs": {"setting_id": "set_1"},
    }
    approved = apply_image_call_locks(llm, cfg=cfg, graph={})
    assert approved == llm
    assert "red wagon" in approved
    assert "image_llm_authority" in (cfg.get("director_image_prompt_notes") or [])


def test_image_user_edit_wins_without_origin_flag() -> None:
    user = _user_plate("cream-and-teal canopy")
    cfg = {
        "role": "scene",
        "user_edit_prompt": user,
        "generate": {"prompt": _STALE},  # origin omitted / lagging
        "last_approved_prompt": _STALE,
    }
    assert resolve_user_origin_prompt(cfg, "") == user
    assert "cream-and-teal" in apply_image_call_locks(_STALE, cfg=cfg, graph={})
