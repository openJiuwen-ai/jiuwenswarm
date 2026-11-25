"""Distributed teammate local sandbox rebuild gate (D-20261105-1041 KD6/KD7)."""
from __future__ import annotations

import logging

import pytest

from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec

from jiuwenswarm.agents.harness.team import remote_member_bootstrap as rmb


def _spec() -> TeamAgentSpec:
    return TeamAgentSpec(
        agents={"leader": DeepAgentSpec(), "teammate": DeepAgentSpec()},
        team_name="dist-team",
    )


def test_gate_rebuilds_with_local_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.agents.swarm.assembly import _entry_sandbox_slug

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_runtime",
        lambda: {"enabled": True, "files": {"allow": [], "deny": []}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_endpoint",
        lambda: {"url": "http://teammate-local:8080", "type": "jiuwenbox"},
    )
    monkeypatch.setattr("jiuwenswarm.enterprise.edition.is_enterprise", lambda: False)

    spec = _spec()
    rmb._rebuild_member_sandbox_gate(spec, session_id="sess-9")

    teammate_slug = _entry_sandbox_slug("dist-team", "teammate")
    sysop = spec.agents["teammate"].sys_operation
    assert sysop is not None
    launcher = sysop.gateway_config.launcher_config
    assert str(launcher.base_url) == "http://teammate-local:8080"  # local url, not the leader's
    assert sysop.id == f"sandbox.{teammate_slug}"  # same derivation rule as the leader side


def test_gate_local_unavailable_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_runtime",
        lambda: {"enabled": True, "files": {"allow": [], "deny": []}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_endpoint", lambda: {"url": "", "type": ""}
    )
    monkeypatch.setattr("jiuwenswarm.enterprise.edition.is_enterprise", lambda: False)
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_team_fail_closed", lambda: False
    )
    spec = _spec()
    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.agents.harness.team.remote_member_bootstrap"):
        rmb._rebuild_member_sandbox_gate(spec, session_id="sess-9")
    assert any("stay LOCAL" in r.getMessage() for r in caplog.records)


def test_gate_fail_closed_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_runtime",
        lambda: {"enabled": True, "files": {"allow": [], "deny": []}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_endpoint", lambda: {"url": "", "type": ""}
    )
    monkeypatch.setattr("jiuwenswarm.enterprise.edition.is_enterprise", lambda: False)
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_team_fail_closed", lambda: True
    )
    with pytest.raises(RuntimeError, match="sandbox"):
        rmb._rebuild_member_sandbox_gate(_spec(), session_id="sess-9")


def test_gate_replaces_leader_injected_gateway_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """KD6 core: an entry that arrives with leader-side gateway_config is rebuilt to local."""
    from openjiuwen.core.sys_operation import OperationMode
    from openjiuwen.core.sys_operation.config import (
        PreDeployLauncherConfig,
        SandboxGatewayConfig,
    )
    from openjiuwen.harness.schema.deep_agent_spec import SysOperationSpec
    from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_runtime",
        lambda: {"enabled": True, "files": {"allow": [], "deny": []}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_endpoint",
        lambda: {"url": "http://teammate-local:8080", "type": "jiuwenbox"},
    )
    monkeypatch.setattr("jiuwenswarm.enterprise.edition.is_enterprise", lambda: False)

    spec = _spec()
    spec.agents["teammate"] = DeepAgentSpec(
        sys_operation=SysOperationSpec(
            id="sandbox.entry_dist-team_teammate",
            mode=OperationMode.SANDBOX,
            gateway_config=SandboxGatewayConfig(
                launcher_config=PreDeployLauncherConfig(
                    base_url="http://leader-remote:9999",  # leader's url — must not survive
                    sandbox_type="jiuwenbox",
                ),
            ),
        )
    )
    spec.reviewer_agent = DeepAgentSpec()

    rmb._rebuild_member_sandbox_gate(spec, session_id="sess-9")

    launcher = spec.agents["teammate"].sys_operation.gateway_config.launcher_config
    assert str(launcher.base_url) == "http://teammate-local:8080"
    # reviewer re-shares the freshly rebuilt teammate sysop (shared-id invariant)
    assert spec.reviewer_agent.sys_operation is spec.agents["teammate"].sys_operation


def test_gate_fail_closed_covers_card_build_failure(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """fail_closed rejects members on card-build failure too, not just endpoint issues."""
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_runtime",
        lambda: {"enabled": True, "files": {"allow": [], "deny": []}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_endpoint",
        lambda: {"url": "http://teammate-local:8080", "type": "jiuwenbox"},
    )
    monkeypatch.setattr("jiuwenswarm.enterprise.edition.is_enterprise", lambda: False)
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_team_fail_closed", lambda: True
    )

    import jiuwenswarm.server.runtime.agent_adapter.sysop_builder as builder_mod

    monkeypatch.setattr(builder_mod, "create_sandbox_sysop_card", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="fail_closed|unsandboxed"):
        rmb._rebuild_member_sandbox_gate(_spec(), session_id="sess-9")


def test_gate_threads_project_dir_into_local_rebuild(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Fix-1: the gate must forward the request's project_dir so the local
    rebuild mounts the project instead of falling back to the agent-server cwd."""
    project = tmp_path / "remote-proj"
    project.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_runtime",
        lambda: {"enabled": True, "files": {"allow": [], "deny": []}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_endpoint",
        lambda: {"url": "http://teammate-local:8080", "type": "jiuwenbox"},
    )
    monkeypatch.setattr("jiuwenswarm.enterprise.edition.is_enterprise", lambda: False)

    spec = _spec()
    rmb._rebuild_member_sandbox_gate(spec, session_id="sess-9", project_dir=str(project))
    launcher = spec.agents["teammate"].sys_operation.gateway_config.launcher_config
    binds = launcher.extra_params["policy"]["filesystem_policy"]["bind_mounts"]
    assert str(project) in {b["host_path"] for b in binds}


def test_gate_project_dir_missing_on_remote_node_skips_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A project_dir that does not exist on the remote node is skipped (the
    builder falls back to cwd only when project_dir is None — a nonexistent
    explicit dir yields no project bind, not a wrong one)."""
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_runtime",
        lambda: {"enabled": True, "files": {"allow": [], "deny": []}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_endpoint",
        lambda: {"url": "http://teammate-local:8080", "type": "jiuwenbox"},
    )
    monkeypatch.setattr("jiuwenswarm.enterprise.edition.is_enterprise", lambda: False)

    spec = _spec()
    absent = str(tmp_path / "not-on-this-node")
    rmb._rebuild_member_sandbox_gate(spec, session_id="sess-9", project_dir=absent)
    launcher = spec.agents["teammate"].sys_operation.gateway_config.launcher_config
    binds = {b["host_path"] for b in launcher.extra_params["policy"]["filesystem_policy"]["bind_mounts"]}
    assert absent not in binds



@pytest.mark.asyncio
async def test_fail_closed_rejection_runs_cleanup_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix-4: a team_fail_closed rejection must run the same cleanup as the
    normal path and report an explicit 'sandbox rejected' warning — never be
    swallowed by the loop's tail broad except."""
    cleaned = {"discard": 0, "stop": 0, "workspace": 0}

    async def fake_discard(tm, sid, agent):
        cleaned["discard"] += 1

    def fake_workspace(team, leader):
        cleaned["workspace"] += 1

    async def fake_stop(sid, member):
        cleaned["stop"] += 1

    monkeypatch.setattr(rmb, "_discard_auxiliary_team_agent", fake_discard)
    monkeypatch.setattr(rmb, "_cleanup_auxiliary_leader_workspace", fake_workspace)
    monkeypatch.setattr(rmb, "_stop_dynamic_member_agent", fake_stop)
    monkeypatch.setattr(
        rmb, "_rebuild_member_sandbox_gate",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(
            "teammate refuses unsandboxed members (sandbox.team_fail_closed=true)"
        )),
    )

    import logging

    logs: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record):
            logs.append(record.getMessage())

    rmb.logger.addHandler(_Cap())
    try:
        rejected = await rmb._apply_member_sandbox_gate_with_cleanup(
            spec_obj=object(), team_manager=None, sid="sess-x",
            leader_team_agent=None, leader_member_name="l", member="m-x",
            assembly_project_dir="",
        )
    finally:
        rmb.logger.removeHandler(_Cap())

    assert rejected is False
    assert cleaned["discard"] >= 1, "auxiliary team agent left in cache"
    assert cleaned["stop"] >= 1, "stale member agent not stopped"
    assert cleaned["workspace"] >= 1, "auxiliary leader workspace not cleaned"
    assert any("sandbox rejected" in m for m in logs), logs[:3]
