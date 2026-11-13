# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests: _skill_scan_dirs filters MCP skill dirs by adapter scope.

A session-scoped child scans only its ``_session_selected_mcp`` MCP skill dirs
(per-session cli/skill enable via chat.send's ``mcp`` field). The root adapter
(NON session-scoped — TUI/cron/admin/rewind runs) scans NO MCP skill dirs at
all: cli/skill MCPs are session-level only and the TUI channel does not
support them, so the root must not leak connected cli/skill skills into root
runs. (Previously the root scanned ALL connected MCP dirs.)
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_adapter(*, session_scoped: bool, selected=None):
    """Bare adapter with just the attrs _skill_scan_dirs reads."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )
    a = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    a._is_session_scoped_adapter = session_scoped
    a._session_selected_mcp = selected if selected is not None else set()
    return a


def _skill_mgr_mock(mcp_dirs):
    """SkillManager stub returning a fixed MCP skill-dir list."""
    m = MagicMock()
    m._mcp_skills_dirs.return_value = list(mcp_dirs)
    return m


def test_root_adapter_scans_no_mcp_skill_dirs() -> None:
    """Root (non session-scoped) must not scan ANY MCP skill dir — cli/skill
    MCPs are session-level only; leaking them into root runs (rewind/cron/
    admin) breaks the session-level default-False contract."""
    adapter = _make_adapter(session_scoped=False)
    adapter._skill_manager = _skill_mgr_mock([
        {"name": "feishu", "dir": "/ws/mcp/skills/feishu"},
        {"name": "ctrip", "dir": "/ws/mcp/skills/ctrip"},
    ])
    roots = adapter._skill_scan_dirs()
    # Only the base agent skills dir; no MCP skill dirs.
    assert len(roots) == 1


def test_session_child_scans_only_selected_mcp_dirs() -> None:
    """Session child scans only the MCPs in its _session_selected_mcp set."""
    adapter = _make_adapter(
        session_scoped=True, selected={"feishu"})
    adapter._skill_manager = _skill_mgr_mock([
        {"name": "feishu", "dir": "/ws/mcp/skills/feishu"},
        {"name": "ctrip", "dir": "/ws/mcp/skills/ctrip"},
    ])
    roots = adapter._skill_scan_dirs()
    assert len(roots) == 2  # agent skills dir + feishu
    assert "/ws/mcp/skills/feishu" in roots
    assert "/ws/mcp/skills/ctrip" not in roots


def test_session_child_empty_selection_scans_no_mcp_dirs() -> None:
    """Session child with empty selection (default False) sees no cli/skill
    MCP skills — matches the session-level default-False contract."""
    adapter = _make_adapter(session_scoped=True, selected=set())
    adapter._skill_manager = _skill_mgr_mock([
        {"name": "feishu", "dir": "/ws/mcp/skills/feishu"},
    ])
    roots = adapter._skill_scan_dirs()
    assert len(roots) == 1  # agent skills dir only


def test_session_child_multiple_selections() -> None:
    """Multiple selected MCPs all surface."""
    adapter = _make_adapter(
        session_scoped=True, selected={"feishu", "ctrip"})
    adapter._skill_manager = _skill_mgr_mock([
        {"name": "feishu", "dir": "/ws/mcp/skills/feishu"},
        {"name": "ctrip", "dir": "/ws/mcp/skills/ctrip"},
        {"name": "dingtalk", "dir": "/ws/mcp/skills/dingtalk"},
    ])
    roots = adapter._skill_scan_dirs()
    assert len(roots) == 3  # agent skills dir + feishu + ctrip
    assert "/ws/mcp/skills/dingtalk" not in roots


def test_project_dir_adds_project_skill_roots_after_agent_skills_dir(tmp_path) -> None:
    """A project set from the frontend adds its .agents/.claude skill roots.

    They follow the agent skills dir (an installed Skill keeps precedence over a
    same-named project Skill) and are listed even before they exist, so a
    directory created mid-session is found by the next refresh.
    """
    from jiuwenswarm.common.utils import get_agent_skills_dir

    for session_scoped in (False, True):
        adapter = _make_adapter(session_scoped=session_scoped)
        adapter._skill_manager = _skill_mgr_mock([])
        adapter._project_dir = str(tmp_path)
        roots = adapter._skill_scan_dirs()
        assert roots == [
            str(get_agent_skills_dir()),
            str(tmp_path.resolve() / ".agents" / "skills"),
            str(tmp_path.resolve() / ".claude" / "skills"),
        ]


def test_no_project_dir_adds_no_project_skill_roots() -> None:
    """Without a project only the agent skills dir is scanned."""
    adapter = _make_adapter(session_scoped=False)
    adapter._skill_manager = _skill_mgr_mock([])
    adapter._project_dir = None
    assert len(adapter._skill_scan_dirs()) == 1


def test_project_skill_roots_precede_selected_mcp_dirs(tmp_path) -> None:
    """Project roots sit between the agent skills dir and session MCP dirs."""
    adapter = _make_adapter(session_scoped=True, selected={"feishu"})
    adapter._skill_manager = _skill_mgr_mock([
        {"name": "feishu", "dir": "/ws/mcp/skills/feishu"},
    ])
    adapter._project_dir = str(tmp_path)
    roots = adapter._skill_scan_dirs()
    assert roots[1:] == [
        str(tmp_path.resolve() / ".agents" / "skills"),
        str(tmp_path.resolve() / ".claude" / "skills"),
        "/ws/mcp/skills/feishu",
    ]
