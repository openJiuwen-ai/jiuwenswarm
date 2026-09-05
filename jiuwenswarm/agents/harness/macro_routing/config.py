# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Load MACRO lightweight scheduler settings."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def load_macro_routing_config(config_base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve modes.macro_routing with safe defaults."""
    from jiuwenswarm.common.config import get_config

    base = config_base if isinstance(config_base, dict) else get_config()
    modes = base.get("modes") if isinstance(base.get("modes"), dict) else {}
    raw = modes.get("macro_routing") if isinstance(modes, dict) else {}
    if not isinstance(raw, dict):
        raw = {}

    strategy = str(raw.get("strategy", "hybrid")).strip().lower() or "hybrid"
    if strategy not in {"rules", "llm", "hybrid"}:
        strategy = "hybrid"

    try:
        confidence_threshold = float(raw.get("confidence_threshold", 0.72))
    except (TypeError, ValueError):
        confidence_threshold = 0.72
    confidence_threshold = max(0.0, min(1.0, confidence_threshold))

    def _str_list(key: str) -> list[str]:
        value = raw.get(key)
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item or "").strip()]

    return {
        "enabled": bool(raw.get("enabled", True)),
        "strategy": strategy,
        "model_name": str(raw.get("model_name", "")).strip(),
        "confidence_threshold": confidence_threshold,
        "team_markers": _str_list("team_markers"),
        "fast_markers": _str_list("fast_markers"),
        "raw": deepcopy(raw),
    }


__all__ = ["load_macro_routing_config"]
