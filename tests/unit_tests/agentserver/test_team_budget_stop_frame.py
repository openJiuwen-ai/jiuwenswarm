# coding: utf-8
"""迭代预算耗尽帧的分流与成员兜底定时器生命周期。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter import team_helpers
from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
    _cancel_member_exhaustion_fallbacks,
    _handle_budget_stop_frame,
)


class _Arbiter:
    def __init__(self) -> None:
        self.proposals: list[dict] = []

    async def propose(self, proposal: dict) -> str:
        self.proposals.append(proposal)
        return "emitted_clean"


@pytest.mark.asyncio
async def test_leader_max_rounds_proposes_iteration_exhausted() -> None:
    arb = _Arbiter()
    finish = await _handle_budget_stop_frame(
        parsed={"stop_reason": "max_rounds"},
        is_leader=True,
        chunk=SimpleNamespace(source_member=None),
        channel_id="ch",
        session_id="s1",
        arbiter=arb,
    )
    assert finish is True
    assert arb.proposals == [{
        "source": "iteration_exhausted",
        "scope": "leader",
    }]


@pytest.mark.asyncio
async def test_leader_total_scope() -> None:
    arb = _Arbiter()
    finish = await _handle_budget_stop_frame(
        parsed={"stop_reason": "budget_total"},
        is_leader=True,
        chunk=SimpleNamespace(source_member=None),
        channel_id="ch",
        session_id="s1",
        arbiter=arb,
    )
    assert finish is True
    assert arb.proposals[0]["scope"] == "total"


@pytest.mark.asyncio
async def test_member_frame_arms_fallback_not_finish(monkeypatch) -> None:
    armed: list[tuple] = []

    def _fake_arm(
            channel_id, session_id, member_name, *, propose, round_id=None,
    ):
        armed.append((channel_id, session_id, member_name, round_id))

    monkeypatch.setattr(
        team_helpers, "_arm_member_exhaustion_fallback", _fake_arm,
    )
    arb = _Arbiter()
    finish = await _handle_budget_stop_frame(
        parsed={"stop_reason": "max_rounds"},
        is_leader=False,
        chunk=SimpleNamespace(source_member="m1"),
        channel_id="ch",
        session_id="s1",
        arbiter=arb,
        round_id=7,
    )
    assert finish is False
    assert armed == [("ch", "s1", "m1", 7)]
    assert arb.proposals == []


@pytest.mark.asyncio
async def test_non_budget_frame_is_ignored() -> None:
    arb = _Arbiter()
    finish = await _handle_budget_stop_frame(
        parsed={"stop_reason": None},
        is_leader=True,
        chunk=SimpleNamespace(source_member=None),
        channel_id="ch",
        session_id="s1",
        arbiter=arb,
    )
    assert finish is False
    assert arb.proposals == []


@pytest.mark.asyncio
async def test_leader_frame_cancels_member_fallbacks(monkeypatch) -> None:
    """leader 触顶帧 = 收尾边界，成员判死兜底立即撤表。

    收尾轮会把在途任务回池，600s 兜底会把“收尾自己造成的无主 PENDING”
    读成无人接棒；leader 帧到达即撤销，避免九连误提案。
    """
    cancelled: list[tuple] = []
    monkeypatch.setattr(
        team_helpers,
        "_cancel_member_exhaustion_fallbacks",
        lambda session_id, round_id=None: cancelled.append(
            (session_id, round_id)
        ),
    )
    arb = _Arbiter()
    finish = await _handle_budget_stop_frame(
        parsed={"stop_reason": "budget_total"},
        is_leader=True,
        chunk=SimpleNamespace(source_member=None),
        channel_id="ch",
        session_id="s1",
        arbiter=arb,
        round_id=7,
    )
    assert finish is True
    assert cancelled == [("s1", 7)]
    assert arb.proposals[0]["source"] == "iteration_exhausted"


@pytest.mark.asyncio
async def test_member_frame_does_not_cancel_fallbacks(monkeypatch) -> None:
    cancelled: list[str] = []
    monkeypatch.setattr(
        team_helpers,
        "_cancel_member_exhaustion_fallbacks",
        lambda session_id, round_id=None: cancelled.append(session_id),
    )
    monkeypatch.setattr(
        team_helpers,
        "_arm_member_exhaustion_fallback",
        lambda *_args, **_kwargs: None,
    )
    arb = _Arbiter()
    finish = await _handle_budget_stop_frame(
        parsed={"stop_reason": "max_rounds"},
        is_leader=False,
        chunk=SimpleNamespace(source_member="m1"),
        channel_id="ch",
        session_id="s1",
        arbiter=arb,
    )
    assert finish is False
    assert cancelled == []


@pytest.mark.asyncio
async def test_cancel_member_exhaustion_fallbacks() -> None:
    async def _sleep_forever() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(_sleep_forever())
    other = asyncio.create_task(_sleep_forever())
    team_helpers._member_exhaustion_timers["s1:m1"] = task
    team_helpers._member_exhaustion_timers["s2:m2"] = other
    try:
        _cancel_member_exhaustion_fallbacks("s1")
        await asyncio.sleep(0)
        assert task.cancelled()
        assert not other.cancelled()
        assert "s1:m1" not in team_helpers._member_exhaustion_timers
        assert "s2:m2" in team_helpers._member_exhaustion_timers
    finally:
        team_helpers._member_exhaustion_timers.pop("s2:m2", None)
        other.cancel()


@pytest.mark.asyncio
async def test_cancel_member_exhaustion_fallbacks_is_round_scoped() -> None:
    """跨回合复用流时，旧回合的收尾不得撤掉新回合刚武装的兜底。"""

    async def _sleep_forever() -> None:
        await asyncio.sleep(3600)

    old_round = asyncio.create_task(_sleep_forever())
    new_round = asyncio.create_task(_sleep_forever())
    other_session = asyncio.create_task(_sleep_forever())
    team_helpers._member_exhaustion_timers["s1:1:m1"] = old_round
    team_helpers._member_exhaustion_timers["s1:2:m1"] = new_round
    team_helpers._member_exhaustion_timers["s2:1:m1"] = other_session
    try:
        _cancel_member_exhaustion_fallbacks("s1", round_id=1)
        await asyncio.sleep(0)
        assert old_round.cancelled()
        assert not new_round.cancelled()
        assert not other_session.cancelled()
        assert "s1:1:m1" not in team_helpers._member_exhaustion_timers
        assert "s1:2:m1" in team_helpers._member_exhaustion_timers
        assert "s2:1:m1" in team_helpers._member_exhaustion_timers
    finally:
        team_helpers._member_exhaustion_timers.pop("s1:2:m1", None)
        team_helpers._member_exhaustion_timers.pop("s2:1:m1", None)
        new_round.cancel()
        other_session.cancel()


@pytest.mark.asyncio
async def test_member_fallback_fires_and_proposes(monkeypatch) -> None:
    """走真实定时器：超时后向裁决器提案，并从注册表自清（不重复提案）。"""
    import jiuwenswarm.server.runtime.agent_adapter.team_stall_watchdog as watchdog

    monkeypatch.setattr(
        team_helpers, "_MEMBER_EXHAUSTED_KILL_FALLBACK_SEC", 0.01,
    )

    async def _count(_channel_id, _session_id):
        return 2

    monkeypatch.setattr(
        watchdog, "team_unassigned_pending_count_db", _count,
    )
    arb = _Arbiter()
    team_helpers._arm_member_exhaustion_fallback(
        "ch", "s1", "m1", propose=arb.propose, round_id=7,
    )
    key = "s1:7:m1"
    assert key in team_helpers._member_exhaustion_timers
    for _ in range(200):
        if arb.proposals:
            break
        await asyncio.sleep(0.01)
    assert arb.proposals == [{
        "source": "member_exhausted",
        "error": "成员 m1 耗尽后任务无人接棒",
    }]
    await asyncio.sleep(0.05)
    assert key not in team_helpers._member_exhaustion_timers
    assert len(arb.proposals) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("unassigned", [0, None])
async def test_member_fallback_silent_without_unassigned(
        monkeypatch, unassigned,
) -> None:
    """读数 0 / 不可得：不判死，定时器照常自清。"""
    import jiuwenswarm.server.runtime.agent_adapter.team_stall_watchdog as watchdog

    monkeypatch.setattr(
        team_helpers, "_MEMBER_EXHAUSTED_KILL_FALLBACK_SEC", 0.01,
    )

    async def _count(_channel_id, _session_id):
        return unassigned

    monkeypatch.setattr(
        watchdog, "team_unassigned_pending_count_db", _count,
    )
    arb = _Arbiter()
    team_helpers._arm_member_exhaustion_fallback(
        "ch", "s1", "m1", propose=arb.propose, round_id=7,
    )
    await asyncio.sleep(0.1)
    assert arb.proposals == []
    assert "s1:7:m1" not in team_helpers._member_exhaustion_timers


def test_cancel_fallbacks_only_on_terminal_emitted(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        team_helpers,
        "_cancel_member_exhaustion_fallbacks",
        lambda session_id, round_id=None: calls.append(
            (session_id, round_id)
        ),
    )
    for outcome in ("deferred", "suppressed", None):
        team_helpers._cancel_member_exhaustion_fallbacks_if_terminal(
            outcome, "s1", 7,
        )
    assert calls == []
    team_helpers._cancel_member_exhaustion_fallbacks_if_terminal(
        "emitted_clean", "s1", 7,
    )
    team_helpers._cancel_member_exhaustion_fallbacks_if_terminal(
        "emitted_error", "s1", 7,
    )
    assert calls == [("s1", 7), ("s1", 7)]
