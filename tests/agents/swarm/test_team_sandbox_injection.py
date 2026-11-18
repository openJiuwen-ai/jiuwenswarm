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


def test_injected_specs_survive_serialization_roundtrip(_sandbox_enabled) -> None:
    spec = _make_team_spec()
    spec.agents["browser-usd-sgd"] = DeepAgentSpec()
    spec.reviewer_agent = DeepAgentSpec()
    _build_member_sandbox_specs({"react": {"evolution": {}}}, spec, session_id="sess-1")
    original_ids = {n: s.sys_operation.id for n, s in spec.agents.items()}

    # dict round-trip (from_spawn_payload path: payload.py model_dump -> team_agent.py model_validate)
    restored = TeamAgentSpec.model_validate(spec.model_dump(mode="json", exclude={"build_context"}))
    for name, oid in original_ids.items():
        assert restored.agents[name].sys_operation is not None
        assert restored.agents[name].sys_operation.id == oid
        assert restored.agents[name].sys_operation.mode == OperationMode.SANDBOX

    # json round-trip (the entry !9041's monkey patch missed)
    restored2 = TeamAgentSpec.model_validate_json(spec.model_dump_json(exclude={"build_context"}))
    assert restored2.agents["leader"].sys_operation.id == original_ids["leader"]

    # reviewer keeps the shared teammate id across the boundary too
    assert restored.reviewer_agent.sys_operation.id == original_ids["teammate"]


def test_code_and_browser_subagents_inherit_parent_sysop(_sandbox_enabled) -> None:
    from openjiuwen.harness.schema.build_context import PARENT_SYS_OPERATION_EXTRAS_KEY

    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from jiuwenswarm.agents.swarm.providers import code_subagents as cs

    spec = _make_team_spec()
    _build_member_sandbox_specs({"react": {"evolution": {}}}, spec, session_id="sess-1")
    sysop_id = spec.agents["teammate"].sys_operation.id

    ctx = SwarmBuildContext(session_id="sess-1", mode="team", channel="web")
    # resolve() stand-in: the parent sysop published on the build context is the
    # live SysOperation object; the id is the stable observable on its config.
    ctx.extras[PARENT_SYS_OPERATION_EXTRAS_KEY] = SimpleNamespace(
        id=sysop_id,
        mode=OperationMode.SANDBOX,
        isolation_key_template=None,
    )
    from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig

    ctx.extras[cs._PARENT_MODEL_EXTRAS_KEY] = SimpleNamespace(
        model_client_config=ModelClientConfig(
        model_name="m",
        client_provider="openai",
        api_key="test-key",
        api_base="http://localhost:8000",
    ),
        model_config=ModelRequestConfig(model_name="m"),
    )

    built_code = cs.build_code_agent({}, ctx)
    assert built_code is not None
    # SubAgentConfig carries sys_operation as a field (create_subagent consumes it)
    assert built_code.sys_operation is ctx.extras[PARENT_SYS_OPERATION_EXTRAS_KEY]

    built_browser = cs.build_swarm_browser_agent({}, ctx)
    assert built_browser is not None
    assert built_browser.sys_operation is ctx.extras[PARENT_SYS_OPERATION_EXTRAS_KEY]


def test_entry_slug_enforces_lowercase_charset_and_collision_resistance() -> None:
    # jiuwenbox sandbox_id charset is ^[0-9a-z_-]+$ — uppercase must not survive
    slug = _entry_sandbox_slug("MyTeam_ABC", "Expert")
    assert re.fullmatch(r"[0-9a-z_-]+", slug)
    assert "myteam_abc" in slug

    # two distinct oversized keys must not collapse onto the same 80-char slug
    slug_a = _entry_sandbox_slug("x" * 200 + "AAA", "m")
    slug_b = _entry_sandbox_slug("x" * 200 + "BBB", "m")
    assert slug_a != slug_b
    # each fragment stays within the 80-char cap (67 body + 1 dash + 12 digest)
    assert len(slug_a) <= len("entry_") + 80 + 1 + len("_m")


def test_card_failure_for_one_entry_warns_and_names_the_entry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.assembly.get_config", lambda: _sandbox_config()
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

    import jiuwenswarm.server.runtime.agent_adapter.sysop_builder as builder_mod

    real_builder = builder_mod.create_sandbox_sysop_card

    def flaky_builder(*args, isolation_custom_id=None, **kwargs):
        if isolation_custom_id and isolation_custom_id.endswith("_teammate"):
            return None  # simulate a card-build failure for one entry only
        return real_builder(*args, isolation_custom_id=isolation_custom_id, **kwargs)

    monkeypatch.setattr(builder_mod, "create_sandbox_sysop_card", flaky_builder)

    spec = _make_team_spec()
    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.agents.swarm.assembly"):
        _build_member_sandbox_specs({"react": {"evolution": {}}}, spec, session_id="sess-1")
    assert spec.agents["leader"].sys_operation is not None
    assert spec.agents["teammate"].sys_operation is None  # failed entry stays LOCAL
    assert any(
        "stay LOCAL" in r.getMessage() and "teammate" in r.getMessage()
        for r in caplog.records
    )


def test_injection_threads_project_dir_into_policy(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    project = tmp_path / "my-project"
    project.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.assembly.get_config", lambda: _sandbox_config()
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

    spec = _make_team_spec()
    _build_member_sandbox_specs(
        {"react": {"evolution": {}}}, spec, session_id="sess-1", project_dir=str(project)
    )
    launcher = spec.agents["leader"].sys_operation.gateway_config.launcher_config
    binds = launcher.extra_params["policy"]["filesystem_policy"]["bind_mounts"]
    assert str(project) in {b["host_path"] for b in binds}


def test_entry_slug_cjk_names_do_not_collapse() -> None:
    """SF-1: pure-CJK names must not all fold to the same "entry" fallback.

    Chinese member/team names are common in this repo's user base; if every
    纯中文 name collapsed to "entry", multiple members in one team would map
    to the same isolation key and share one container — KD2 bijection would
    fail systemically. Distinct raw names must yield distinct slugs (digest
    suffix on fold, mirroring the oversized-name protection).
    """
    from jiuwenswarm.agents.swarm.assembly import _entry_slug

    a = _entry_slug("张三")
    b = _entry_slug("李四")
    assert a != b, f"CJK names collapsed to the same slug: {a!r}"
    # folded names keep a stable, jiuwenbox-safe charset
    assert re.fullmatch(r"[0-9a-z_-]+", a)
    # stability: same name maps to the same slug across enrich runs
    assert _entry_slug("张三") == a


def test_entry_sandbox_slug_cjk_team_and_member() -> None:
    """Full id composition stays distinct for CJK teams/members too."""
    id_a = _entry_sandbox_slug("我的团队", "王五")
    id_b = _entry_sandbox_slug("我的团队", "赵六")
    assert id_a != id_b
    assert re.fullmatch(r"[0-9a-z_-]+", id_a)


def test_injection_survives_unresolvable_team_context(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Widened mounts: an unresolvable team context must NOT cancel sandbox
    injection — members still get sandboxed with the non-team mounts
    (project_dir, agent workspace, files.*) and only the 4 team mounts are
    skipped. Cancelling injection would drop members back to the host."""
    import logging

    import jiuwenswarm.server.runtime.agent_adapter.sysop_builder as sb_mod

    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.assembly.get_config", lambda: _sandbox_config()
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
    # force the team context to be unresolvable (e.g. exotic layout edge)
    monkeypatch.setattr(sb_mod, "_team_context_from_spec", lambda spec: None)

    spec = _make_team_spec()
    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.agents.swarm.assembly"):
        _build_member_sandbox_specs(
            {"react": {"evolution": {}}}, spec, session_id="sess-1"
        )

    # members ARE sandboxed (not LOCAL) despite the missing team context
    assert spec.agents["leader"].sys_operation is not None
    assert spec.agents["leader"].sys_operation.mode == OperationMode.SANDBOX
    # the team-mount absence is loud
    assert any("team mounts" in r.getMessage() for r in caplog.records)
    # and the built policy carries NO team mounts (intent empty), but the
    # card exists — sandboxing itself never regressed to LOCAL
    pol = spec.agents["leader"].sys_operation.gateway_config.launcher_config.extra_params["policy"]
    binds = pol["filesystem_policy"]["bind_mounts"]
    assert not any("agent_teams" in b["host_path"] for b in binds)
