# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Code Graph configuration for the Code adapter.

``code_graph.profile`` turns graph tools on: ``off`` is the original agent;
``graph`` hangs find_* retrieval tools. ``code_graph.agent`` selects who owns
them: ``root`` or ``code_agent``. Plan and Explore never get graph tools.

Product yaml writes ``agent: root``. An omitted or unknown ``agent`` key still
resolves to ``code_agent`` so previous ContextBench runs stay comparable.
Unknown ``profile`` spellings are ``off``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PROFILE_OFF = "off"
PROFILE_GRAPH = "graph"
VALID_PROFILES = (PROFILE_OFF, PROFILE_GRAPH)

AGENT_ROOT = "root"
AGENT_CODE = "code_agent"
VALID_AGENTS = (AGENT_ROOT, AGENT_CODE)


@dataclass(frozen=True)
class CodeGraphFlags:
    """Resolved Code Graph settings for one run."""

    profile: str = PROFILE_OFF
    agent: str = AGENT_CODE

    @property
    def enabled(self) -> bool:
        return self.profile != PROFILE_OFF

    @property
    def on_root(self) -> bool:
        return self.enabled and self.agent == AGENT_ROOT

    @property
    def on_code_agent(self) -> bool:
        return self.enabled and self.agent == AGENT_CODE


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


def resolve_agent(value: Any, *, default: str = AGENT_CODE) -> str:
    """Accept ``root`` / ``code_agent``. Missing or unknown values use ``default``."""
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    text = str(value).strip().lower()
    if text in VALID_AGENTS:
        return text
    return default


def resolve_code_graph_flags(config_base: dict[str, Any] | None) -> CodeGraphFlags:
    raw = (config_base or {}).get("code_graph") if isinstance(config_base, dict) else None
    if not isinstance(raw, dict):
        return CodeGraphFlags()
    return CodeGraphFlags(
        profile=resolve_profile(raw.get("profile")),
        agent=resolve_agent(raw.get("agent")),
    )


def apply_code_graph_profile(
    config_base: dict[str, Any],
    profile: str,
) -> dict[str, Any]:
    """Eval overlay: set ``code_graph.profile`` and keep ``code_agent`` on.

    Does not rewrite ``code_graph.agent``. Eval hang is ``--graph-agent``.
    Extra keys in a live config stay in yaml; flags only read ``profile`` and
    ``agent``.
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
