"""Tests for cross-platform system-operation policy construction."""

from __future__ import annotations

from jiuwenswarm.server.runtime.agent_adapter import sysop_builder


def test_build_process_policy_skips_non_posix_platform(
    monkeypatch,
) -> None:
    monkeypatch.delattr(sysop_builder.os, "geteuid", raising=False)

    assert sysop_builder.build_process_policy() == {}


def test_session_files_readonly_even_without_tenant_context(tmp_path, monkeypatch):
    from jiuwenswarm.common import utils

    tenant = tmp_path / "tenant"
    tenant.mkdir()
    monkeypatch.setattr(
        utils, "get_agent_sessions_dir", lambda: tmp_path / "unbound" / "sessions"
    )
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)
    policy, _ = sysop_builder.build_filesystem_policy({}, shared_dir=tenant)
    fs = policy["filesystem_policy"]
    path = str(tenant / "agent" / "sessions")
    assert path in fs["read_only"]
    assert {"host_path": path, "sandbox_path": path, "mode": "ro"} in fs["bind_mounts"]


def test_session_state_cannot_be_reopened_by_explicit_allow_or_alias(
    tmp_path, monkeypatch
):
    import pytest

    from jiuwenswarm.common import utils

    tenant = tmp_path / "tenant"
    sessions = tenant / "agent" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)
    with pytest.raises(ValueError, match="override"):
        sysop_builder.build_filesystem_policy(
            {"allow": [str(sessions)]}, shared_dir=tenant
        )
    alias = tmp_path / "alias"
    alias.symlink_to(tenant, target_is_directory=True)
    with pytest.raises(ValueError, match="alias"):
        sysop_builder.build_filesystem_policy(
            {"allow": [str(alias)]}, shared_dir=tenant
        )
    with pytest.raises(ValueError, match="project_dir"):
        sysop_builder.build_filesystem_policy(
            {}, shared_dir=tenant, project_dir=sessions
        )


def test_yuanrong_protects_skill_files_from_workspace_mounts(tmp_path, monkeypatch):
    import pytest

    from jiuwenswarm.common import utils

    root = tmp_path / "tenant" / "agent"
    sessions = root / "sessions"
    root.mkdir(parents=True)
    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)
    mounts = [{"source": str(root), "target": str(root), "readonly": False}]
    result = sysop_builder._protect_yuanrong_skill_state(mounts, root)
    assert {
        "source": str(sessions),
        "target": str(sessions),
        "readonly": True,
    } in result
    with pytest.raises(ValueError, match="alias"):
        sysop_builder._protect_yuanrong_skill_state(
            [{"source": str(root), "target": "/writable-alias", "readonly": False}],
            root,
        )
    with pytest.raises(ValueError, match="override"):
        sysop_builder._protect_yuanrong_skill_state(
            [{"source": str(sessions), "target": str(sessions), "readonly": False}],
            root,
        )
