# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for interrupt handling semantics in interface facade."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod


class _InterruptHarness(JiuWenSwarm):
    @property
    def session_manager_for_test(self):
        return getattr(self, "_session_manager")

    async def process_interrupt_for_test(self, request: AgentRequest):
        return await getattr(self, "_process_interrupt")(request)


class _FakeTeamManager:
    def __init__(self, pause_result: bool = True, cancel_result: bool = True) -> None:
        self.pause_result = pause_result
        self.cancel_result = cancel_result
        self.pause_calls: list[tuple[str, str]] = []
        self.cancel_calls: list[tuple[str, str]] = []

    async def pause_session_runtime(self, session_id: str, reason: str = "") -> bool:
        self.pause_calls.append((session_id, reason))
        return self.pause_result

    async def cancel_session_runtime(self, session_id: str, reason: str = "") -> bool:
        self.cancel_calls.append((session_id, reason))
        return self.cancel_result


def _build_team_interrupt_request(
    intent: str,
    *,
    mode: str = "team",
    team: bool = True,
) -> AgentRequest:
    params = {
        "intent": intent,
        "mode": mode,
    }
    if team:
        params["team"] = True
    return AgentRequest(
        request_id=f"req-{intent}",
        channel_id="web",
        session_id="team-session-1",
        req_method=ReqMethod.CHAT_CANCEL,
        params=params,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent", "expected_message"),
    [
        ("pause", "团队已暂停"),
        ("cancel", "团队当前执行已结束"),
    ],
)
async def test_team_interrupt_pause_like_intents_use_team_manager(
    monkeypatch: pytest.MonkeyPatch,
    intent: str,
    expected_message: str,
) -> None:
    claw = _InterruptHarness()
    fake_manager = _FakeTeamManager(pause_result=True)
    cancelled: list[tuple[str, str, float | None]] = []

    def _unexpected_adapter():
        raise AssertionError("team interrupt should not use deep adapter interrupt path")

    async def _fake_cancel_session_task(
        session_id: str,
        reason: str = "",
        wait_timeout: float | None = None,
    ) -> None:
        cancelled.append((session_id, reason, wait_timeout))

    monkeypatch.setattr(claw, "_ensure_adapter", _unexpected_adapter)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda channel_id=None: fake_manager,
    )
    monkeypatch.setattr(claw.session_manager_for_test, "cancel_session_task", _fake_cancel_session_task)

    response = await claw.process_interrupt_for_test(_build_team_interrupt_request(intent))

    assert response.payload == {
        "event_type": "chat.interrupt_result",
        "intent": intent,
        "success": True,
        "message": expected_message,
    }
    assert cancelled == [("team-session-1", f"interrupt(intent={intent}): ", 5.0)]
    if intent == "pause":
        assert fake_manager.pause_calls == [("team-session-1", f"interrupt(intent={intent}): ")]
        assert fake_manager.cancel_calls == []
    else:
        assert fake_manager.cancel_calls == [("team-session-1", f"interrupt(intent={intent}): ")]
        assert fake_manager.pause_calls == []


@pytest.mark.asyncio
async def test_team_interrupt_resume_is_ack_only(monkeypatch: pytest.MonkeyPatch) -> None:
    claw = _InterruptHarness()
    fake_manager = _FakeTeamManager(pause_result=True)
    cancelled: list[tuple[str, str, float | None]] = []

    def _unexpected_adapter():
        raise AssertionError("team resume should not use deep adapter interrupt path")

    async def _fake_cancel_session_task(
        session_id: str,
        reason: str = "",
        wait_timeout: float | None = None,
    ) -> None:
        cancelled.append((session_id, reason, wait_timeout))

    monkeypatch.setattr(claw, "_ensure_adapter", _unexpected_adapter)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda channel_id=None: fake_manager,
    )
    monkeypatch.setattr(claw.session_manager_for_test, "cancel_session_task", _fake_cancel_session_task)

    response = await claw.process_interrupt_for_test(_build_team_interrupt_request("resume"))

    assert response.payload == {
        "event_type": "chat.interrupt_result",
        "intent": "resume",
        "success": True,
        "message": "团队暂停后，直接发送下一条消息即可继续。",
    }
    assert cancelled == []
    assert fake_manager.pause_calls == []
    assert fake_manager.cancel_calls == []


@pytest.mark.asyncio
async def test_code_team_interrupt_uses_team_manager_without_team_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claw = _InterruptHarness()
    fake_manager = _FakeTeamManager(pause_result=True)
    cancelled: list[tuple[str, str, float | None]] = []

    def _unexpected_adapter():
        raise AssertionError("code.team interrupt should not use deep adapter interrupt path")

    async def _fake_cancel_session_task(
        session_id: str,
        reason: str = "",
        wait_timeout: float | None = None,
    ) -> None:
        cancelled.append((session_id, reason, wait_timeout))

    monkeypatch.setattr(claw, "_ensure_adapter", _unexpected_adapter)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda channel_id=None: fake_manager,
    )
    monkeypatch.setattr(claw.session_manager_for_test, "cancel_session_task", _fake_cancel_session_task)

    response = await claw.process_interrupt_for_test(
        _build_team_interrupt_request("pause", mode="code.team", team=False)
    )

    assert response.payload == {
        "event_type": "chat.interrupt_result",
        "intent": "pause",
        "success": True,
        "message": "团队已暂停",
    }
    assert cancelled == [("team-session-1", "interrupt(intent=pause): ", 5.0)]
    assert fake_manager.pause_calls == [("team-session-1", "interrupt(intent=pause): ")]
    assert fake_manager.cancel_calls == []
