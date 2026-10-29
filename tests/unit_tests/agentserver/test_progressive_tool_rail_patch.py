# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for the ProgressiveToolRail fingerprint-key escape patch.

Root cause: session state expands "." in dict keys as nested paths
(``openjiuwen/core/session/utils.py`` ``expand_nested_structure``), so
ProgressiveToolRail's per-session fingerprint map — keyed by the raw tool name —
loses every dotted MCP tool name (tencent-docs ``manage.*`` / ``doc.*`` /
``sheet.*`` / ...). The authorization lookup then always misses and the call is
rejected with "must be searched again before it can be called".
"""

from __future__ import annotations

from openjiuwen.core.session.state.agent_state import StateCollection
from openjiuwen.harness.rails import progressive_tool_rail as rail_mod
from openjiuwen.harness.rails.progressive_tool_rail import ProgressiveToolRail

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail_patch import (
    apply_progressive_tool_fingerprint_patch,
)

DOTTED = "mcp_tencent-docs_manage.folder_list"
PLAIN = "mcp_tencent-docs_query_space_node"


class _FakeSession:
    """Rail-facing session surface over the production StateCollection."""

    def __init__(self) -> None:
        self._state = StateCollection()

    def update_state(self, data: dict) -> None:
        self._state.update_global(data)

    def get_state(self, key):
        return self._state.get_global(key)


def _bare_rail() -> ProgressiveToolRail:
    return ProgressiveToolRail.__new__(ProgressiveToolRail)


def test_unpatched_state_storage_mangles_dotted_keys() -> None:
    """Pin the root cause: raw session storage nests dotted dict keys."""
    state = StateCollection()
    state.update_global({"fps": {DOTTED: "fp1", PLAIN: "fp2"}})

    stored = state.get_global("fps")

    assert stored[PLAIN] == "fp2"
    assert DOTTED not in stored
    assert stored["mcp_tencent-docs_manage"] == {"folder_list": "fp1"}


def test_patched_fingerprints_roundtrip_dotted_names() -> None:
    apply_progressive_tool_fingerprint_patch()
    rail = _bare_rail()
    session = _FakeSession()

    rail._set_discovered_tools(
        session, [DOTTED, PLAIN], fingerprints={DOTTED: "fp1", PLAIN: "fp2"}
    )

    fingerprints = rail._get_discovered_tool_fingerprints(session)
    assert fingerprints is not None
    assert fingerprints[DOTTED] == "fp1"
    assert fingerprints[PLAIN] == "fp2"
    assert rail._get_discovered_tools(session) == [DOTTED, PLAIN]


def test_patched_storage_keys_carry_no_ascii_dot() -> None:
    apply_progressive_tool_fingerprint_patch()
    rail = _bare_rail()
    session = _FakeSession()

    rail._set_discovered_tools(session, [DOTTED], fingerprints={DOTTED: "fp1"})

    raw = session.get_state(rail_mod._DISCOVERED_TOOL_FINGERPRINTS_KEY)
    assert isinstance(raw, dict) and raw
    assert all("." not in key for key in raw)
    assert list(raw.values()) == ["fp1"]


def test_patched_read_skips_legacy_nested_debris() -> None:
    apply_progressive_tool_fingerprint_patch()
    rail = _bare_rail()
    session = _FakeSession()
    # Pre-patch state shape: the dotted key was expanded into nested dicts.
    session.update_state({rail_mod._DISCOVERED_TOOL_FINGERPRINTS_KEY: {DOTTED: "fp1"}})

    fingerprints = rail._get_discovered_tool_fingerprints(session)

    assert fingerprints is not None
    assert DOTTED not in fingerprints


def test_apply_is_idempotent() -> None:
    apply_progressive_tool_fingerprint_patch()

    assert apply_progressive_tool_fingerprint_patch() is False
