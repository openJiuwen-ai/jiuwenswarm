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

import re
from dataclasses import dataclass
from pathlib import Path
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


_MIB = 1024 * 1024
_GIB = 1024 ** 3
DEFAULT_MAX_SOURCE_BYTES = 40 * _MIB
_SOURCE_VOLUME_UNITS = {
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": _MIB,
    "mb": _MIB,
    "mib": _MIB,
    "g": _GIB,
    "gb": _GIB,
    "gib": _GIB,
}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_source_volume_to_bytes(value: Any, default: int = DEFAULT_MAX_SOURCE_BYTES) -> int:
    """Panel ``40`` / yaml ``40MB`` / legacy ``41943040`` → engine bytes.

    Bare integers below 1 MiB are megabytes (panel unit). Integers at or
    above 1 MiB stay bytes so old yaml still works.
    """
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        if value >= _MIB:
            return value
        return max(1, value * _MIB)
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            return default
        if value >= _MIB:
            return max(1, int(value))
        return max(1, int(value * _MIB))
    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text:
        return default
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([a-zA-Z]+)?", text)
    if not match:
        return default
    amount = float(match.group(1))
    if amount != amount or amount in {float("inf"), float("-inf")}:
        return default
    unit = (match.group(2) or "").lower()
    if unit:
        factor = _SOURCE_VOLUME_UNITS.get(unit)
        if factor is None:
            return default
        return max(1, int(amount * factor))
    if "." not in match.group(1) and amount >= _MIB:
        return max(1, int(amount))
    return max(1, int(amount * _MIB))


def format_source_volume_for_panel(n_bytes: int) -> str:
    """Show yaml bytes as MB. ``41943040`` / ``40MB`` → ``40``."""
    if n_bytes <= 0:
        return "0"
    mb = n_bytes / _MIB
    rounded = round(mb)
    if abs(mb - rounded) < 1e-9:
        return str(int(rounded))
    text = f"{mb:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def format_source_volume_for_yaml(n_bytes: int) -> str:
    """Write the same unit the panel uses. ``41943040`` → ``40MB``."""
    if n_bytes <= 0:
        return "0MB"
    if n_bytes % _GIB == 0:
        return f"{n_bytes // _GIB}GB"
    mb = n_bytes / _MIB
    if abs(mb - round(mb)) < 1e-9:
        return f"{int(round(mb))}MB"
    text = f"{mb:.4f}".rstrip("0").rstrip(".")
    return f"{text}MB"


def product_code_graph_config(config_base: dict[str, Any] | None) -> Any:
    """Live yaml caps for ``/status`` and manager lookups. Cache path is not needed."""
    from openjiuwen.core.retrieval.code_graph.models import CodeGraphConfig

    raw = (config_base or {}).get("code_graph") if isinstance(config_base, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    defaults = CodeGraphConfig()
    return CodeGraphConfig(
        cache_dir=None,
        max_files=_as_int(raw.get("max_files"), defaults.max_files),
        max_source_bytes=parse_source_volume_to_bytes(
            raw.get("max_source_bytes"), defaults.max_source_bytes
        ),
        max_build_rss_mb=_as_int(raw.get("max_build_rss_mb"), defaults.max_build_rss_mb),
        max_cache_size_mb=_as_int(raw.get("max_cache_size_mb"), defaults.max_cache_size_mb or 2048),
    )


def resolve_code_graph_flags(config_base: dict[str, Any] | None) -> CodeGraphFlags:
    raw = (config_base or {}).get("code_graph") if isinstance(config_base, dict) else None
    if not isinstance(raw, dict):
        return CodeGraphFlags()
    return CodeGraphFlags(
        profile=resolve_profile(raw.get("profile")),
        agent=resolve_agent(raw.get("agent")),
    )


_MB_LIMIT_MESSAGE = re.compile(
    r"(max_(?:build_rss|cache_size)_mb) is (\d+), cap is (\d+)",
    re.IGNORECASE,
)


def _bytes_as_mb_label(value: int) -> str:
    megabytes = value / _MIB
    if abs(megabytes - round(megabytes)) < 1e-6:
        return str(int(round(megabytes)))
    return f"{megabytes:.1f}"


def rewrite_code_graph_limit_message(message: object) -> str:
    """Show RSS/disk caps in MB. The engine fills ``*_mb`` fields with bytes."""
    text = str(message or "")

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        observed = int(match.group(2))
        cap = int(match.group(3))
        if observed < _MIB and cap < _MIB:
            return match.group(0)
        return f"{name} is {_bytes_as_mb_label(observed)}, cap is {_bytes_as_mb_label(cap)}"

    return _MB_LIMIT_MESSAGE.sub(_replace, text)


def admit_code_graph_workspace(workspace: str, config: Any) -> Any | None:
    """Return the engine limit error if the live tree is already over cap.

    Used by ``/status`` so adding files past ``max_files`` / ``max_source_bytes``
    reports ``unavailable`` without waiting for a ``find_*`` call. ``None``
    means under cap or the walk failed; callers keep the manager's state.
    """
    root = str(workspace or "").strip()
    if not root or config is None:
        return None
    try:
        from openjiuwen.core.retrieval.code_graph.errors import CodeGraphLimitExceeded
        from openjiuwen.core.retrieval.code_graph.indexing.builder import (
            _admit_source_files,
            _iter_source_files,
        )
    except Exception:  # noqa: BLE001 — status / session create must still return
        return None
    try:
        _admit_source_files(list(_iter_source_files(Path(root), config)), config)
    except CodeGraphLimitExceeded as exc:
        return exc
    except Exception:  # noqa: BLE001
        return None
    return None


def enable_code_agent_subagent(config: dict[str, Any]) -> None:
    """Turn on ``react.subagents.code_agent`` in place.

    Graph hang on ``code_agent`` needs that sub-agent present. Product yaml
    leaves it ``enabled: false`` so Root hang does not also open it.
    """
    react = config.get("react")
    if not isinstance(react, dict):
        react = {}
        config["react"] = react
    subagents = react.get("subagents")
    if not isinstance(subagents, dict):
        subagents = {}
        react["subagents"] = subagents
    code_agent_cfg = subagents.get("code_agent")
    if not isinstance(code_agent_cfg, dict):
        code_agent_cfg = {}
        subagents["code_agent"] = code_agent_cfg
    code_agent_cfg["enabled"] = True


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
