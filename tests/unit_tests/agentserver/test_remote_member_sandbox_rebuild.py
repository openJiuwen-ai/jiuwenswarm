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
