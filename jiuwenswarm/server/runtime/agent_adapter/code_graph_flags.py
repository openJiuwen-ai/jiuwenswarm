# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Code Graph configuration for the Code adapter.

``code_graph.profile`` is the only switch that matters: ``off`` is the original
agent; ``graph`` hangs find_* retrieval tools on the Code Agent. Root, Plan,
and Explore never get graph tools. Unknown spellings are ``off``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PROFILE_OFF = "off"
PROFILE_GRAPH = "graph"
VALID_PROFILES = (PROFILE_OFF, PROFILE_GRAPH)


@dataclass(frozen=True)
class CodeGraphFlags:
    """Resolved Code Graph settings for one run."""

    profile: str = PROFILE_OFF

    @property
    def enabled(self) -> bool:
        return self.profile != PROFILE_OFF


def resolve_profile(value: Any, *, default: str = PROFILE_OFF) -> str:
    """Accept ``off`` / ``graph`` only. Anything else falls back to ``default``."""
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    text = str(value).strip().lower()
    if text in VALID_PROFILES:
        return text
    return default


def resolve_code_graph_flags(config_base: dict[str, Any] | None) -> CodeGraphFlags:
    raw = (config_base or {}).get("code_graph") if isinstance(config_base, dict) else None
    if not isinstance(raw, dict):
        return CodeGraphFlags()
    return CodeGraphFlags(profile=resolve_profile(raw.get("profile")))


def apply_code_graph_profile(
    config_base: dict[str, Any],
    profile: str,
) -> dict[str, Any]:
    """Eval overlay: set ``code_graph.profile`` and keep ``code_agent`` on.

    Product yaml is already ``profile`` plus index limits. Extra keys in a live
    config are ignored by ``resolve_code_graph_flags``.
    """
    from copy import deepcopy

    cfg = deepcopy(config_base)
    graph = dict(cfg.get("code_graph") or {})
    graph["profile"] = resolve_profile(profile)
    cfg["code_graph"] = graph
    react = dict(cfg.get("react") or {})
    subagents = dict(react.get("subagents") or {})
    code_agent_cfg = dict(subagents.get("code_agent") or {})
    code_agent_cfg["enabled"] = True
    subagents["code_agent"] = code_agent_cfg
    react["subagents"] = subagents
    cfg["react"] = react
    return cfg
