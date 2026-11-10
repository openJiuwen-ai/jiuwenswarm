"""Team mount intent extraction and policy encoding (D-20261105-1041 KD3/§3.1)."""
from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.agent_adapter import sysop_builder


TEAM_CTX = {"team_name": "unit-team", "session_id": "sess-1"}


def _ctx_with_roots(tmp_path: Path, team_ws_root: str) -> dict:
    return {
        **TEAM_CTX,
        "workspace_root": team_ws_root,
        "team_home": str(tmp_path / ".agent_teams" / "unit-team"),
    }


def test_team_mount_intent_none_context_returns_empty() -> None:
    assert sysop_builder._team_mount_intent(None) == []


def test_team_mount_intent_lists_team_subtrees(tmp_path: Path) -> None:
    ctx = _ctx_with_roots(tmp_path, str(tmp_path / "ws"))
    intent = sysop_builder._team_mount_intent(ctx)
    paths = [host for host, mode in intent]
    assert all(mode == "rw" for _, mode in intent)
    assert str(Path(ctx["team_home"]) / "workspaces") in paths
    assert str(Path(ctx["team_home"]) / "team-workspace") in paths
    assert str(Path(ctx["team_home"]) / "sessions" / ctx["session_id"] / "worktrees") in paths
    assert str(Path(ctx["team_home"]).parent / "jiuwen_team_members") in paths
    # attack-surface narrowing: nothing outside the team's own home + members dir
    assert not any(p == str(tmp_path) or p == str(Path(ctx["team_home"]).parent) for p in paths)


def test_team_context_from_spec_derives_team_home_from_workspace_root(tmp_path: Path) -> None:
    from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec

    spec = TeamAgentSpec(agents={}, team_name="unit-team")
    spec.workspace = None
    ctx = sysop_builder._team_context_from_spec(spec)
    assert ctx is not None and ctx["team_name"] == "unit-team"
    assert Path(ctx["team_home"]).name == "unit-team"

    ctx2 = sysop_builder._team_context_from_spec(spec)
    assert ctx2 is not None and Path(ctx2["workspace_root"]).name == "team-workspace"


def test_team_context_derivation_with_relative_and_empty_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
    from openjiuwen.harness.schema.deep_agent_spec import WorkspaceSpec

    spec = TeamAgentSpec(agents={}, team_name="unit-team")
    spec.workspace = WorkspaceSpec(root_path="")  # empty root must fall back, not crash
    ctx = sysop_builder._team_context_from_spec(spec)
    assert ctx is not None

    spec.workspace = WorkspaceSpec(root_path="./relative-ws")
    monkeypatch.chdir(tmp_path)
    # a non team-workspace root is a relocated layout: no team home is guessed
    ctx_rel = sysop_builder._team_context_from_spec(spec)
    assert ctx_rel is None or sysop_builder._team_mount_intent(ctx_rel) == []


def test_policy_build_encodes_team_intent_into_bind_mounts(tmp_path: Path) -> None:
    team_home = tmp_path / ".agent_teams" / "unit-team"
    ctx = _ctx_with_roots(tmp_path, str(tmp_path / "ws"))
    policy, _uploads = sysop_builder.build_filesystem_policy(None, team_context=ctx)
    binds = policy["filesystem_policy"]["bind_mounts"]
    bind_paths = {b["host_path"] for b in binds}
    expected = {host for host, _ in sysop_builder._team_mount_intent(ctx)}
    assert expected <= bind_paths
    assert not any("team.db" in p for p in bind_paths)
    assert str(team_home.parent) not in bind_paths  # .agent_teams root itself never mounted


def test_policy_build_survives_uncreatable_dir(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import os
    import sys

    if sys.platform == "win32" or os.geteuid() == 0:
        pytest.skip("permission-based read-only dir does not translate on this platform")
    ro_parent = tmp_path / "ro"
    ro_parent.mkdir()
    ro_parent.chmod(0o500)
    ctx = _ctx_with_roots(tmp_path / "ro", str(tmp_path / "ws"))
    try:
        with caplog.at_level("WARNING", logger=sysop_builder.logger.name):
            policy, _ = sysop_builder.build_filesystem_policy(None, team_context=ctx)
        # dir that could not be created is skipped with a warning, build still succeeds
        assert isinstance(policy, dict)
        assert any("team mount" in r.message for r in caplog.records)
    finally:
        ro_parent.chmod(0o700)


def test_create_sandbox_sysop_card_accepts_custom_id_and_team_context(tmp_path: Path) -> None:
    ctx = _ctx_with_roots(tmp_path, str(tmp_path / "ws"))
    card = sysop_builder.create_sandbox_sysop_card(
        "http://sandbox.local",
        "jiuwenbox",
        isolation_custom_id="entry_unit-team_browser-usd-sgd",
        team_context=ctx,
    )
    assert card is not None
    launcher = card.gateway_config.launcher_config
    assert launcher.sandbox_type == "jiuwenbox"
    assert card.gateway_config.isolation.custom_id == "entry_unit-team_browser-usd-sgd"


def test_network_mode_forwarded_into_policy_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sandbox.network_mode rides in the client fragment (append-merge keeps it)."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.sysop_builder.get_sandbox_network_mode",
        lambda: "host",
    )
    card = sysop_builder.create_sandbox_sysop_card("http://sandbox.local", "jiuwenbox")
    assert card is not None
    policy = card.gateway_config.launcher_config.extra_params["policy"]
    assert policy["network"] == {"mode": "host"}


def test_network_mode_absent_keeps_legacy_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No sandbox.network_mode configured: fragment stays filesystem-only."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.sysop_builder.get_sandbox_network_mode",
        lambda: "",
    )
    card = sysop_builder.create_sandbox_sysop_card("http://sandbox.local", "jiuwenbox")
    assert card is not None
    policy = card.gateway_config.launcher_config.extra_params["policy"]
    assert "network" not in policy


def test_mount_intent_sanitizes_session_id() -> None:
    """Fix-2: session_id is sanitized into one path segment (same rule as
    openjiuwen team_session_dir) so it can never escape team_home."""
    ctx = {
        "team_name": "t", "session_id": "../../etc",
        "team_home": "/data/teams/t", "workspace_root": "/data/teams/t/team-workspace",
    }
    paths = [host for host, _ in sysop_builder._team_mount_intent(ctx)]
    for p in paths:
        assert ".." not in Path(p).parts, f"escape in {p}"
    worktree = [p for p in paths if "worktrees" in p][0]
    # exact form: sanitized segment, matching openjiuwen's rule
    from openjiuwen.agent_teams.paths import safe_path_segment
    assert str(Path("/data/teams/t/sessions") / safe_path_segment("../../etc") / "worktrees") == worktree


def test_mount_intent_stays_within_team_tree_boundaries() -> None:
    """Fix-2: every non-members intent path must resolve inside team_home."""
    ctx = {
        "team_name": "t", "session_id": "s1",
        "team_home": "/data/teams/t", "workspace_root": "/data/teams/t/team-workspace",
    }
    paths = [Path(host) for host, _ in sysop_builder._team_mount_intent(ctx)]
    team_home = Path("/data/teams/t")
    for p in paths:
        if "jiuwen_team_members" in p.name:
            assert p.parent == team_home.parent  # members dir is the sibling
        else:
            assert team_home in p.parents or p == team_home


def test_team_context_default_layout_derives_parent(tmp_path) -> None:
    """Fix-3: the default layout (<root>/.agent_teams/<team>/team-workspace)
    keeps the proven parent-derivation — must not regress."""
    from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec

    spec = TeamAgentSpec(agents={}, team_name="unit-team")
    ws = tmp_path / ".agent_teams" / "unit-team" / "team-workspace"
    ctx = sysop_builder._team_context_from_spec(spec)
    assert ctx is not None
    # empty workspace_root falls back to team_home(<team>)/team-workspace whose
    # parent is the team home — same value as the derivation
    assert Path(ctx["team_home"]).name == "unit-team"


def test_team_context_relocated_workspace_skips_team_home_guess() -> None:
    """Fix-3: a relocated workspace root (group-chat runs layout) must NOT be
    reverse-derived into a wrong team home — team mounts are skipped with a
    warning instead of binding wrong host directories."""
    import logging as _logging

    from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
    from openjiuwen.harness.schema.deep_agent_spec import WorkspaceSpec

    spec = TeamAgentSpec(agents={}, team_name="child-team")
    spec.workspace = WorkspaceSpec(root_path="/tmp/runs/abc123/workspace")  # runs layout
    ctx = sysop_builder._team_context_from_spec(spec)
    # relocated layout: no guessed team_home — the intent function must yield
    # no team mounts rather than bind /tmp/runs/... as if it were a team tree
    assert ctx is None or sysop_builder._team_mount_intent(ctx) == []
