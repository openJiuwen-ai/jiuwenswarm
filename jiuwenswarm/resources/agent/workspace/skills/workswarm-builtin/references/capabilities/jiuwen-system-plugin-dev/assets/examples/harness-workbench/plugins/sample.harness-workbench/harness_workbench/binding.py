# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Resolve the Harness actually stored on a session."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

logger = logging.getLogger(__name__)

_PAIRS = {
    ("harness-claude-expert", "claudecode"): "claude",
    ("harness-codex-expert", "codex"): "codex",
}


def bound_mode(metadata: Mapping[str, object]) -> str | None:
    """Return claude or codex only when template and provider match a fixed pair.

    A missing binding, a template without its provider, or a swapped pair
    returns None. The workbench then leaves Harness chrome off.
    """
    binding = metadata.get("session_runtime_binding")
    if not isinstance(binding, Mapping):
        return None
    pair = (binding.get("agent_template_name"), binding.get("provider_name"))
    return _PAIRS.get(pair)


def session_mode(session_id: str, reader: Callable[[str], object] | None = None) -> str:
    """Return the bound mode, or normal when the binding is absent or unreadable."""
    metadata = load_metadata(session_id, reader)
    if metadata is None:
        return "normal"
    return bound_mode(metadata) or "normal"


def load_metadata(session_id: str, reader: Callable[[str], object] | None = None) -> Mapping[str, object] | None:
    """Return session metadata, an empty mapping when none is stored, or None if unreadable."""
    load = _read_metadata if reader is None else reader
    try:
        metadata = load(session_id)
    except Exception:
        logger.warning("[harness-workbench] session binding unreadable: %s", session_id, exc_info=True)
        return None
    if not isinstance(metadata, Mapping):
        return {}
    return metadata


def _read_metadata(session_id: str) -> object:
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    return get_session_metadata(session_id, cache_bust=True, enable_writeback=False)
