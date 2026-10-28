# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session-level team dispatch / bootstrap modes: resolution, persistence and spec wiring."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.agents.harness.team.team_manager import TeamManager
from jiuwenswarm.server.runtime.agent_adapter import team_helpers
from jiuwenswarm.server.runtime.session import session_metadata


def _patch_metadata(monkeypatch: pytest.MonkeyPatch, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(session_metadata, "_read_metadata", lambda _sid, cache_bust=True: dict(metadata))
    monkeypatch.setattr(session_metadata, "_enqueue_write", lambda _sid, data: written.append(dict(data)))
    return written


def test_resolve_dispatch_mode_prefers_request_params(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_metadata(monkeypatch, {"session_id": "s1", "session_team_dispatch_mode": "autonomous"})

    resolved = team_helpers._resolve_session_team_dispatch_mode(
        {"team_dispatch_mode": "scheduled"},
        session_id="s1",
    )

    assert resolved == "scheduled"


def test_resolve_dispatch_mode_falls_back_to_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_metadata(monkeypatch, {"session_id": "s1", "session_team_dispatch_mode": "scheduled"})

    assert team_helpers._resolve_session_team_dispatch_mode({}, session_id="s1") == "scheduled"


def test_resolve_dispatch_mode_ignores_unknown_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_metadata(monkeypatch, {"session_id": "s1"})

    resolved = team_helpers._resolve_session_team_dispatch_mode(
        {"team_dispatch_mode": "bogus"},
        session_id="s1",
    )

    assert resolved is None


def test_persist_dispatch_mode_writes_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    written = _patch_metadata(monkeypatch, {"session_id": "s1", "title": "t"})

    team_helpers.persist_session_team_dispatch_mode("s1", "scheduled")

    assert written == [{"session_id": "s1", "title": "t", "session_team_dispatch_mode": "scheduled"}]


async def _build_spec(
    monkeypatch: pytest.MonkeyPatch,
    dispatch_mode: str | None,
    bootstrap_mode: str | None = None,
    *,
    config_bootstrap_mode: str = "leader",
) -> SimpleNamespace:
    spec = SimpleNamespace(
        team_name="team",
        dispatch_mode="scheduled",
        bootstrap_mode=config_bootstrap_mode,
        enable_task_verification=True,
        enable_member_messaging=True,
    )

    async def fake_ensure_postgresql(self, config_base: dict) -> None:
        _ = self, config_base

    monkeypatch.setattr("jiuwenswarm.agents.harness.team.team_manager.get_config", lambda: {})
    monkeypatch.setattr(TeamManager, "_ensure_postgresql_for_leader", fake_ensure_postgresql)
    monkeypatch.setattr(TeamManager, "_load_session_team_spec", lambda self, _sid, **_kw: (spec, True))
    monkeypatch.setattr("jiuwenswarm.agents.swarm.enrich_team_spec_for_swarm", lambda _spec, **_kw: None)

    return await TeamManager().get_swarm_enriched_team_spec(
        "s1",
        mode="team",
        request_metadata={"mode": "team"},
        dispatch_mode=dispatch_mode,
        bootstrap_mode=bootstrap_mode,
    )


@pytest.mark.asyncio
async def test_autonomous_session_overrides_spec_and_drops_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = await _build_spec(monkeypatch, "autonomous")

    assert spec.dispatch_mode == "autonomous"
    assert spec.enable_task_verification is False
    assert spec.enable_member_messaging is False


@pytest.mark.asyncio
async def test_unspecified_dispatch_mode_keeps_team_config(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = await _build_spec(monkeypatch, None)

    assert spec.dispatch_mode == "scheduled"
    assert spec.enable_task_verification is True
    assert spec.enable_member_messaging is True


# ---------------------------------------------------------------------------
# Bootstrap mode (F_122): the scheduled-dispatch sub-switch
# ---------------------------------------------------------------------------


def test_resolve_bootstrap_mode_prefers_request_params(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_metadata(monkeypatch, {"session_id": "s1", "session_team_bootstrap_mode": "leader"})

    resolved = team_helpers._resolve_session_team_bootstrap_mode(
        {"team_bootstrap_mode": "framework"},
        session_id="s1",
    )

    assert resolved == "framework"


def test_resolve_bootstrap_mode_falls_back_to_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_metadata(monkeypatch, {"session_id": "s1", "session_team_bootstrap_mode": "framework"})

    assert team_helpers._resolve_session_team_bootstrap_mode({}, session_id="s1") == "framework"


def test_resolve_bootstrap_mode_ignores_unknown_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_metadata(monkeypatch, {"session_id": "s1"})

    resolved = team_helpers._resolve_session_team_bootstrap_mode(
        {"team_bootstrap_mode": "bogus"},
        session_id="s1",
    )

    assert resolved is None


def test_persist_bootstrap_mode_writes_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    written = _patch_metadata(monkeypatch, {"session_id": "s1", "title": "t"})

    team_helpers.persist_session_team_bootstrap_mode("s1", "framework")

    assert written == [{"session_id": "s1", "title": "t", "session_team_bootstrap_mode": "framework"}]


@pytest.mark.asyncio
async def test_framework_bootstrap_lands_on_a_scheduled_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = await _build_spec(monkeypatch, "scheduled", "framework")

    assert spec.dispatch_mode == "scheduled"
    assert spec.bootstrap_mode == "framework"


@pytest.mark.asyncio
async def test_unspecified_bootstrap_mode_keeps_team_config(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = await _build_spec(monkeypatch, None, None, config_bootstrap_mode="framework")

    assert spec.bootstrap_mode == "framework"


@pytest.mark.asyncio
async def test_framework_bootstrap_rejects_autonomous_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The combination is rejected at the request boundary, not at spec build."""
    with pytest.raises(ValueError, match="requires scheduled dispatch"):
        await _build_spec(monkeypatch, "autonomous", "framework")


@pytest.mark.asyncio
async def test_framework_team_config_rejects_autonomous_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A team configured for framework bootstrap cannot be run as an autonomous session."""
    with pytest.raises(ValueError, match="requires scheduled dispatch"):
        await _build_spec(monkeypatch, "autonomous", None, config_bootstrap_mode="framework")
