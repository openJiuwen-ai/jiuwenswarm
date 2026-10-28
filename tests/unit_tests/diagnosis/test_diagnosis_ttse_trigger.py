# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for triggering the resident TTSE pipeline from diagnosis signals."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.server.diagnosis import ttse_trigger
from jiuwenswarm.server.diagnosis.ttse_trigger import explicit_score, trigger_ttse_induction

_MESSAGES = [{"role": "user", "content": "fix the build"}, {"role": "assistant", "content": "done"}]


class _FakeRail:
    """Records the induction calls a real ``TTSERail`` would receive."""

    def __init__(self, *, evolve_enabled: bool = True, fail_on: str | None = None) -> None:
        self._ttse_config = SimpleNamespace(evolve_enabled=evolve_enabled, success_threshold=0.7)
        self._last_capabilities = "tools: read, write"
        self.snapshots: list[dict[str, Any]] = []
        self._fail_on = fail_on

    async def _run_ttse_induction(self, trajectory: Any, ctx: Any, snapshot: dict[str, Any]) -> None:
        if self._fail_on is not None and snapshot["ttse_task_query"] == self._fail_on:
            raise RuntimeError("induction failed")
        self.snapshots.append(snapshot)


def _agent_manager(rail: Any) -> Any:
    agent = SimpleNamespace(_adapter=SimpleNamespace(_ttse_rail=rail)) if rail is not None else None
    return SimpleNamespace(get_agent_for_session_nowait=lambda **_kwargs: agent)


def _signal(signal_id: str = "tt-1", *, outcome: str = "fail", score: float = 0.0, query: str = "q") -> dict[str, Any]:
    return {"id": signal_id, "outcome": outcome, "score": score, "task_query": query, "messages": _MESSAGES}


async def _drain() -> None:
    while ttse_trigger._background_tasks:
        await asyncio.gather(*list(ttse_trigger._background_tasks))


def test_explicit_score_keeps_the_outcome_authoritative() -> None:
    assert explicit_score("fail", 0.6, 0.7) == 0.0
    assert explicit_score("partial", 0.5, 0.7) == 0.5
    # Out-of-band scores are pulled into the partial band, never into success or fail.
    assert explicit_score("partial", 0.0, 0.7) == pytest.approx(0.35)
    assert explicit_score("partial", 0.9, 0.7) == pytest.approx(0.35)
    # A success always lands at or above the threshold, whatever score came with it.
    assert explicit_score("success", 1.0, 0.999) == 1.0
    assert explicit_score("success", 0.5, 0.7) == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_trigger_feeds_the_rail_induction_with_the_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ttse_trigger, "_is_agent_work_session", lambda _sid: True)
    rail = _FakeRail()

    response = await trigger_ttse_induction(
        agent_manager=_agent_manager(rail),
        channel_id="web",
        session_id="session-1",
        signals=[_signal("tt-1", query="fix the build"), _signal("tt-2", outcome="partial", score=0.4)],
    )
    await _drain()

    assert response == {
        "ok": True,
        "payload": {"results": {"tt-1": {"result": "triggered"}, "tt-2": {"result": "triggered"}}},
    }
    assert [item["ttse_score"] for item in rail.snapshots] == [0.0, 0.4]
    first = rail.snapshots[0]
    assert first["messages"] == _MESSAGES
    assert first["ttse_task_query"] == "fix the build"
    assert first["ttse_capabilities"] == "tools: read, write"


@pytest.mark.asyncio
async def test_trigger_survives_a_failing_induction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ttse_trigger, "_is_agent_work_session", lambda _sid: True)
    rail = _FakeRail(fail_on="boom")

    response = await trigger_ttse_induction(
        agent_manager=_agent_manager(rail),
        channel_id="web",
        session_id="session-1",
        signals=[_signal("tt-1", query="boom"), _signal("tt-2", query="fine")],
    )
    await _drain()

    assert response["ok"] is True
    assert [item["ttse_task_query"] for item in rail.snapshots] == ["fine"]


@pytest.mark.asyncio
async def test_trigger_reports_signals_without_a_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ttse_trigger, "_is_agent_work_session", lambda _sid: True)
    rail = _FakeRail()
    empty = {"id": "tt-1", "outcome": "fail", "score": 0.0, "task_query": "q", "messages": []}

    response = await trigger_ttse_induction(
        agent_manager=_agent_manager(rail),
        channel_id="web",
        session_id="session-1",
        signals=[empty],
    )
    await _drain()

    assert response["payload"]["results"]["tt-1"]["result"] == "failed"
    assert rail.snapshots == []


def _channel_agent(child: Any) -> Any:
    """A channel agent whose TTSE rail lives on the per-session child."""
    parent = SimpleNamespace(
        _ttse_rail=None,
        _get_cached_session_adapter=lambda _sid: child,
    )
    return SimpleNamespace(_adapter=parent)


@pytest.mark.asyncio
async def test_trigger_uses_the_session_adapter_rail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ttse_trigger, "_is_agent_work_session", lambda _sid: True)
    rail = _FakeRail()
    child = SimpleNamespace(_ttse_rail=rail, _instance=object())

    response = await trigger_ttse_induction(
        agent_manager=SimpleNamespace(get_agent_for_session_nowait=lambda **_kwargs: _channel_agent(child)),
        channel_id="web",
        session_id="session-1",
        signals=[_signal()],
    )
    await _drain()

    assert response["ok"] is True
    assert rail.snapshots[0]["ttse_task_query"] == "q"


@pytest.mark.asyncio
async def test_trigger_mounts_a_missing_rail_when_config_enables_ttse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ttse_trigger, "_is_agent_work_session", lambda _sid: True)
    monkeypatch.setattr(ttse_trigger, "_ttse_enabled_in_config", lambda: True)
    rail = _FakeRail()

    class _Child:
        def __init__(self) -> None:
            self._instance = object()
            self._ttse_rail: Any = None

        async def _ensure_ttse_rail_registered(self) -> None:
            self._ttse_rail = rail

    response = await trigger_ttse_induction(
        agent_manager=SimpleNamespace(get_agent_for_session_nowait=lambda **_kwargs: _channel_agent(_Child())),
        channel_id="web",
        session_id="session-1",
        signals=[_signal()],
    )
    await _drain()

    assert response["ok"] is True
    assert rail.snapshots


@pytest.mark.asyncio
async def test_trigger_rejects_sessions_that_are_not_agent_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ttse_trigger, "_is_agent_work_session", lambda _sid: False)
    rail = _FakeRail()

    response = await trigger_ttse_induction(
        agent_manager=_agent_manager(rail),
        channel_id="web",
        session_id="session-1",
        signals=[_signal()],
    )

    assert response["ok"] is False
    assert response["code"] == "TTSE_NOT_SUPPORTED"
    assert rail.snapshots == []


@pytest.mark.asyncio
@pytest.mark.parametrize("signals", [None, [], "text", {"id": "tt-1"}])
async def test_trigger_validates_the_signal_payload(signals: Any) -> None:
    response = await trigger_ttse_induction(
        agent_manager=None,
        channel_id="web",
        session_id="session-1",
        signals=signals,
    )

    assert response["ok"] is False
    assert response["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
@pytest.mark.parametrize("rail", [None, _FakeRail(evolve_enabled=False)])
async def test_trigger_requires_a_resident_enabled_rail(
    monkeypatch: pytest.MonkeyPatch,
    rail: Any,
) -> None:
    monkeypatch.setattr(ttse_trigger, "_is_agent_work_session", lambda _sid: True)

    response = await trigger_ttse_induction(
        agent_manager=_agent_manager(rail),
        channel_id="web",
        session_id="session-1",
        signals=[_signal()],
    )

    assert response["ok"] is False
    assert response["code"] == "TTSE_UNAVAILABLE"
