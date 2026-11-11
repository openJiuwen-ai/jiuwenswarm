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
    # team home resolves authoritatively regardless of the root's shape
    ctx_rel = sysop_builder._team_context_from_spec(spec)
    assert ctx_rel is not None
    from openjiuwen.agent_teams.paths import team_home as _oj2
    assert ctx_rel["team_home"] == str(_oj2("unit-team"))


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
    ctx = sysop_builder._team_context_from_spec(spec)
    assert ctx is not None
    # empty workspace_root falls back to team_home(<team>)/team-workspace whose
    # parent is the team home — same value as the derivation
    assert Path(ctx["team_home"]).name == "unit-team"


def test_team_context_relocated_workspace_resolves_authoritative_team_home() -> None:
    """Widened mounts: team_home always resolves via team_home(team_name)
    (openjiuwen's authoritative layout, aligned to the jiuwenswarm data root
    by configure_openjiuwen_home at startup). A group-chat runs workspace
    root no longer disables team context — members keep their real team
    mounts (their workspaces live under team_home(team_name), verified
    against prepare_member_workspace, NOT under the runs dir)."""
    from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
    from openjiuwen.harness.schema.deep_agent_spec import WorkspaceSpec

    spec = TeamAgentSpec(agents={}, team_name="child-team")
    spec.workspace = WorkspaceSpec(root_path="/tmp/runs/abc123/workspace")  # runs layout
    ctx = sysop_builder._team_context_from_spec(spec)
    assert ctx is not None, "relocated layout must not disable team context"
    from openjiuwen.agent_teams.paths import team_home as _oj_team_home
    assert ctx["team_home"] == str(_oj_team_home("child-team"))
    # mounts derive from the authoritative team home, never from the runs dir
    paths = [h for h, _ in sysop_builder._team_mount_intent(ctx)]
    assert all("/tmp/runs" not in h for h in paths), paths


def test_mount_intent_covers_group_chat_runs_workspace() -> None:
    """B': a group-chat runs workspace root must be mounted so projectless
    members' cwd (workspace_root/artifacts/.../work/<member>) resolves in
    the sandbox — the review finding."""
    ctx = {
        "team_name": "group-expert-abc",
        "session_id": "s1",
        "team_home": "/data/.agent_teams/group-expert-abc",
        "workspace_root": "/data/.agent_teams/outer-team/runs/abc123def456",
    }
    from pathlib import Path as _P
    paths = [h for h, _ in sysop_builder._team_mount_intent(ctx)]
    ws = str(_P("/data/.agent_teams/outer-team/runs/abc123def456"))
    assert ws in paths, paths


def test_mount_intent_rejects_teams_root_itself_and_direct_child() -> None:
    """B' hardening: depth >= 2 only — the teams root itself (all teams,
    team.db) and a direct child (another team's home) must NOT be mounted."""
    # ws == the .agent_teams root
    ctx = {
        "team_name": "t", "session_id": "s",
        "team_home": "/data/.agent_teams/t",
        "workspace_root": "/data/.agent_teams",
    }
    from pathlib import Path as _P
    paths = [h for h, _ in sysop_builder._team_mount_intent(ctx)]
    assert str(_P("/data/.agent_teams")) not in paths
    # ws == a sibling team's home (direct child of the teams root)
    ctx2 = {
        "team_name": "t", "session_id": "s",
        "team_home": "/data/.agent_teams/t",
        "workspace_root": "/data/.agent_teams/other-team",
    }
    from pathlib import Path as _P2
    paths2 = [h for h, _ in sysop_builder._team_mount_intent(ctx2)]
    assert str(_P2("/data/.agent_teams/other-team")) not in paths2


def test_mount_intent_rejects_workspace_root_outside_teams_tree(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """B': a workspace root pointing outside the .agent_teams tree (e.g. a
    tampered spec) is not mounted and warns."""
    import logging

    ctx = {
        "team_name": "t", "session_id": "s",
        "team_home": "/data/.agent_teams/t",
        "workspace_root": "/home/ling/.ssh/x",
    }
    with caplog.at_level(logging.WARNING, logger=sysop_builder.logger.name):
        from pathlib import Path as _P
    paths = [h for h, _ in sysop_builder._team_mount_intent(ctx)]
    assert str(_P("/home/ling/.ssh/x")) not in paths
    assert any("workspace root" in r.message for r in caplog.records)


def test_mount_intent_default_layout_no_duplicate_workspace_mount() -> None:
    """B': the default layout (workspace_root == team_home/team-workspace)
    adds no duplicate bind — the existing team-workspace entry covers it."""
    ctx = {
        "team_name": "t", "session_id": "s",
        "team_home": "/data/.agent_teams/t",
        "workspace_root": "/data/.agent_teams/t/team-workspace",
    }
    from pathlib import Path as _P
    paths = [h for h, _ in sysop_builder._team_mount_intent(ctx)]
    assert paths.count(str(_P("/data/.agent_teams/t/team-workspace"))) == 1
