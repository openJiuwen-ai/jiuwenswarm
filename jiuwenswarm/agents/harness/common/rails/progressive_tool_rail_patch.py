# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Escape dotted MCP tool names in ProgressiveToolRail's fingerprint map.

openjiuwen's bundled ProgressiveToolRail authorizes deferred tool calls via a
per-session fingerprint dict keyed by the raw tool name
(``__progressive_discovered_tool_fingerprints__``). Session state treats "." in
dict keys as a nested-path separator (openjiuwen ``core/session/utils.py``
``expand_nested_structure``), so a name like ``mcp_tencent-docs_manage.folder_list``
is stored as nested dicts and the lookup by the full name always misses — the
call is rejected with "must be searched again before it can be called" and never
reaches the MCP client. Tencent Docs' official MCP exposes ~60% of its tools
under dotted namespaces (``manage.*``, ``doc.*``, ``sheet.*``, ...).

The patch escapes "." in fingerprint-map keys on write and unescapes on read.
The discovered-names list is unaffected: list items are values, not dict keys.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from openjiuwen.harness.rails import progressive_tool_rail as _rail_mod
from openjiuwen.harness.rails.progressive_tool_rail import ProgressiveToolRail

_DOT = "."
# ONE DOT LEADER: renders like a dot but is not ASCII ".", so session state
# never treats it as a nested-path separator. Server tool names use ASCII dots.
_DOT_ESCAPE = "․"

_PATCHED_ATTR = "_jiuwenswarm_fingerprint_key_escape_applied"


def _escape_key(name: str) -> str:
    return name.replace(_DOT, _DOT_ESCAPE)


def _unescape_key(name: str) -> str:
    return name.replace(_DOT_ESCAPE, _DOT)


def _set_discovered_tools(
    self,
    session: Any,
    names: List[str],
    *,
    fingerprints: Optional[Dict[str, str]] = None,
) -> None:
    """Patched ProgressiveToolRail._set_discovered_tools with escaped keys."""
    if session is None:
        return
    normalized_names = list(
        dict.fromkeys(str(name).strip() for name in names if str(name).strip())
    )
    discovered_fingerprints = self._get_discovered_tool_fingerprints(session) or {}
    if fingerprints is not None:
        discovered_fingerprints.update(
            {
                str(name).strip(): str(fingerprint).strip()
                for name, fingerprint in fingerprints.items()
                if str(name).strip() and str(fingerprint).strip()
            }
        )
    session.update_state(
        {
            _rail_mod._DISCOVERED_TOOLS_KEY: normalized_names,
            _rail_mod._DISCOVERED_TOOL_FINGERPRINTS_KEY: {
                _escape_key(name): discovered_fingerprints[name]
                for name in normalized_names
                if name in discovered_fingerprints
            },
        }
    )


def _get_discovered_tool_fingerprints(
    self, session: Any
) -> Optional[Dict[str, str]]:
    """Patched ProgressiveToolRail._get_discovered_tool_fingerprints."""
    if session is None:
        return None
    state = session.get_state(_rail_mod._DISCOVERED_TOOL_FINGERPRINTS_KEY)
    if not isinstance(state, dict):
        return None
    result: Dict[str, str] = {}
    for name, fingerprint in state.items():
        # Skip legacy nested debris left by the unpatched dot-expansion and any
        # non-scalar value; only flat "escaped name -> fingerprint" entries are
        # valid authorizations.
        if not isinstance(fingerprint, str):
            continue
        key = _unescape_key(str(name).strip())
        value = fingerprint.strip()
        if key and value:
            result[key] = value
    return result


def apply_progressive_tool_fingerprint_patch() -> bool:
    """Idempotently swap ProgressiveToolRail's fingerprint-map accessors.

    Returns True when the patch was applied by this call, False if already on.
    """
    if getattr(ProgressiveToolRail, _PATCHED_ATTR, False):
        return False
    ProgressiveToolRail._set_discovered_tools = _set_discovered_tools
    ProgressiveToolRail._get_discovered_tool_fingerprints = (
        _get_discovered_tool_fingerprints
    )
    setattr(ProgressiveToolRail, _PATCHED_ATTR, True)
    return True
