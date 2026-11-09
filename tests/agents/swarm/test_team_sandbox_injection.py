"""P-B entry-level sandbox spec injection (D-20261105-1041 KD1/KD2, §4.0)."""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace

import pytest

from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec
from openjiuwen.core.sys_operation import OperationMode

from jiuwenswarm.agents.swarm.assembly import (
    _build_member_sandbox_specs,
    _entry_sandbox_slug,
)


def _make_team_spec() -> TeamAgentSpec:
    return TeamAgentSpec(
        agents={"leader": DeepAgentSpec(), "teammate": DeepAgentSpec()},
        team_name="unit_team",
    )


def _sandbox_config(*, enabled=True, sandbox_type="jiuwenbox"):
    return {
        "react": {"evolution": {"skill_evolution": True}},
        "sandbox": {
            "enabled": enabled,
            "type": sandbox_type,
            "url": "http://sandbox.local" if sandbox_type else "",
        },
    }


def test_entry_sandbox_slug_is_deterministic_and_safe() -> None:
    assert _entry_sandbox_slug("unit-team", "browser-usd-sgd") == "entry_unit-team_browser-usd-sgd"
    # defensive: '#' and non-ascii are squashed, empty result degrades
    assert "#" not in _entry_sandbox_slug("t#1", "ex*pert")
    assert _entry_sandbox_slug("", "") != ""


def test_entry_sandbox_slug_handles_adversarial_names() -> None:
    slug = _entry_sandbox_slug("x" * 200, "y" * 200)
    # each fragment is capped at 80 chars, so the whole slug is bounded
    assert len(slug) <= len("entry_") + 80 + 1 + 80
    assert re.fullmatch(r"[0-9a-zA-Z_-]+", slug)


@pytest.fixture
def _sandbox_enabled(monkeypatch: pytest.MonkeyPatch):
    cfg = _sandbox_config()
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.assembly.get_config", lambda: cfg
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_runtime",
        lambda: {"enabled": True, "files": {"allow": [], "deny": []}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_endpoint",
        lambda: {"url": "http://sandbox.local", "type": "jiuwenbox"},
    )
    monkeypatch.setattr("jiuwenswarm.enterprise.edition.is_enterprise", lambda: False)


def test_injection_gives_every_entry_its_own_sandbox_spec(_sandbox_enabled) -> None:
    spec = _make_team_spec()
    spec.agents["browser-usd-sgd"] = DeepAgentSpec()  # a named entry, as _apply_agent_group would add
    from openjiuwen.agent_teams.schema.team import TeamMemberSpec, TeamRole

    spec.predefined_members = [
        TeamMemberSpec(member_name="browser-usd-sgd", display_name="B", role_type=TeamRole.TEAMMATE)
    ]

    _build_member_sandbox_specs({"react": {"evolution": {}}}, spec, session_id="sess-1")

    ids = {}
    for name, agent_spec in spec.agents.items():
        sysop = agent_spec.sys_operation
        assert sysop is not None, f"entry {name} not injected"
        assert sysop.mode == OperationMode.SANDBOX
        ids[name] = sysop.id
    assert len(set(ids.values())) == len(ids), "each entry must own a distinct id"
    assert ids["leader"] == f"sandbox.{_entry_sandbox_slug('unit_team', 'leader')}"
    assert ids["browser-usd-sgd"] == f"sandbox.{_entry_sandbox_slug('unit_team', 'browser-usd-sgd')}"


def test_reviewer_shares_teammate_spec_object(_sandbox_enabled) -> None:
    spec = _make_team_spec()
    # enrich builds the reviewer from the teammate before injecting; a direct
    # unit call stands in for that ordering with a bare reviewer entry.
    spec.reviewer_agent = DeepAgentSpec()
    _build_member_sandbox_specs({"react": {"evolution": {}}}, spec, session_id="sess-1")
    assert spec.reviewer_agent is not None
    assert spec.reviewer_agent.sys_operation is spec.agents["teammate"].sys_operation


def test_disabled_sandbox_stays_silent_local(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.assembly.get_config", lambda: _sandbox_config(enabled=False)
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_runtime",
        lambda: {"enabled": False, "files": {"allow": [], "deny": []}},
    )
    spec = _make_team_spec()
    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.agents.swarm.assembly"):
        _build_member_sandbox_specs({"react": {"evolution": {}}}, spec, session_id="sess-1")
    assert spec.agents["leader"].sys_operation is None
    assert not [r for r in caplog.records if "stay LOCAL" in r.getMessage()]


def test_endpoint_missing_warns_loudly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.assembly.get_config", lambda: _sandbox_config(enabled=True)
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_runtime",
        lambda: {"enabled": True, "files": {"allow": [], "deny": []}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_endpoint", lambda: {"url": "", "type": ""}
    )
    monkeypatch.setattr("jiuwenswarm.enterprise.edition.is_enterprise", lambda: False)
    spec = _make_team_spec()
    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.agents.swarm.assembly"):
        _build_member_sandbox_specs({"react": {"evolution": {}}}, spec, session_id="sess-1")
    assert spec.agents["leader"].sys_operation is None
    assert any("stay LOCAL" in r.getMessage() for r in caplog.records)


def test_unsupported_type_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.assembly.get_config",
        lambda: _sandbox_config(sandbox_type="yuanrong"),
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_runtime",
        lambda: {"enabled": True, "files": {"allow": [], "deny": []}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_sandbox_endpoint",
        lambda: {"url": "http://y", "type": "yuanrong"},
    )
    monkeypatch.setattr("jiuwenswarm.enterprise.edition.is_enterprise", lambda: False)
    spec = _make_team_spec()
    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.agents.swarm.assembly"):
        _build_member_sandbox_specs({"react": {"evolution": {}}}, spec, session_id="sess-1")
    assert spec.agents["leader"].sys_operation is None
    assert any(
        "yuanrong" in r.getMessage() and "stay LOCAL" in r.getMessage() for r in caplog.records
    )


def test_scheduled_mode_injects_roles_only(_sandbox_enabled) -> None:
    # scheduled dispatch produces no named entries (assembly.py early return);
    # enrich-level injection still covers the two role entries — behavior equals P-A here.
    spec = _make_team_spec()
    spec.dispatch_mode = "scheduled"
    _build_member_sandbox_specs({"react": {"evolution": {}}}, spec, session_id="sess-1")
    assert set(spec.agents) == {"leader", "teammate"}
    assert all(s.sys_operation is not None for s in spec.agents.values())


def test_double_enrich_keeps_ids_stable(_sandbox_enabled) -> None:
    spec = _make_team_spec()
    _build_member_sandbox_specs({"react": {"evolution": {}}}, spec, session_id="sess-1")
    first = {n: s.sys_operation.id for n, s in spec.agents.items()}
    _build_member_sandbox_specs({"react": {"evolution": {}}}, spec, session_id="sess-1")
    second = {n: s.sys_operation.id for n, s in spec.agents.items()}
    assert first == second  # deterministic derivation: concurrent/rerun enrich is id-stable
