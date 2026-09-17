"""Goal 用户历史推迟落盘：忙碌时不立刻 append，flush 后时间 ≥ 上一轮 final。"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _make_adapter() -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._active_session_ids = __import__("collections").Counter()
    adapter._session_agent_tasks = {}
    adapter._stream_content_run_kind = None
    adapter._stream_round_kind_latch = None
    adapter._resolve_interrupt_session_id = lambda sid: sid or "default"
    adapter._current_interaction_run_kind = lambda: None
    return adapter


class _FakeRunningTask:
    def done(self) -> bool:
        return False


def _running_task() -> _FakeRunningTask:
    return _FakeRunningTask()


@pytest.fixture(autouse=True)
def _clear_pending_goal_history() -> Any:
    interface_deep._pending_goal_objective_history.clear()
    yield
    interface_deep._pending_goal_objective_history.clear()


def _goal_set_request(session_id: str = "sess-defer") -> AgentRequest:
    return AgentRequest(
        request_id="req-goal-set",
        channel_id="web",
        session_id=session_id,
        params={"mode": "agent", "objective": "查杭州天气"},
    )


def test_idle_goal_set_appends_user_history_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _make_adapter()
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        interface_deep, "append_history_record", lambda **kwargs: recorded.append(kwargs)
    )

    adapter._record_goal_set_history_if_needed(
        _goal_set_request(),
        action="set",
        result_type="goal_stream",
        goal_payload={"goal_id": "g1", "objective": "查杭州天气"},
        defer=False,
    )

    assert len(recorded) == 1
    assert recorded[0]["role"] == "user"
    assert recorded[0]["content"] == "查杭州天气"
    assert recorded[0]["extra"]["is_goal_objective_message"] is True
    assert not interface_deep._pending_goal_objective_history


def test_busy_goal_set_defers_until_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _make_adapter()
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        interface_deep, "append_history_record", lambda **kwargs: recorded.append(kwargs)
    )

    adapter._record_goal_set_history_if_needed(
        _goal_set_request(),
        action="set",
        result_type="goal_stream",
        goal_payload={"goal_id": "g1", "objective": "查杭州天气"},
        defer=True,
    )
    assert recorded == []
    assert "sess-defer" in interface_deep._pending_goal_objective_history

    prev_final_ts = 2000.0
    adapter._flush_pending_goal_objective_history("sess-defer", timestamp=prev_final_ts + 0.5)

    assert len(recorded) == 1
    assert recorded[0]["role"] == "user"
    assert recorded[0]["content"] == "查杭州天气"
    assert float(recorded[0]["timestamp"]) >= prev_final_ts
    assert not interface_deep._pending_goal_objective_history


def test_should_defer_when_user_round_active() -> None:
    adapter = _make_adapter()
    adapter._current_interaction_run_kind = lambda: "user"
    assert adapter._should_defer_goal_objective_history("sess-defer") is True


def test_should_defer_when_another_request_active() -> None:
    adapter = _make_adapter()
    adapter._active_session_ids["sess-defer"] = 2
    assert adapter._should_defer_goal_objective_history("sess-defer") is True


def test_should_not_defer_when_idle() -> None:
    adapter = _make_adapter()
    assert adapter._should_defer_goal_objective_history("sess-defer") is False


def test_should_defer_when_other_agent_task_running() -> None:
    adapter = _make_adapter()
    adapter._session_agent_tasks = {"sess-defer": {_running_task()}}
    assert adapter._session_has_other_running_agent_tasks("sess-defer") is True
    assert adapter._should_defer_goal_objective_history("sess-defer") is True


def test_lease_held_early_return_does_not_flush_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat 持有 lease 时 attach 为 None：忙碌推迟绝不能立刻 flush。"""
    adapter = _make_adapter()
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        interface_deep, "append_history_record", lambda **kwargs: recorded.append(kwargs)
    )

    adapter._record_goal_set_history_if_needed(
        _goal_set_request(),
        action="set",
        result_type="goal_stream",
        goal_payload={"goal_id": "g1", "objective": "查杭州天气"},
        defer=True,
    )
    # 复刻 interaction_stream is None + defer 早退：不 flush
    defer_goal_history = True
    interaction_stream = None
    if interaction_stream is None and not defer_goal_history:
        adapter._flush_pending_goal_objective_history("sess-defer")

    assert recorded == []
    assert "sess-defer" in interface_deep._pending_goal_objective_history

    adapter._flush_pending_goal_objective_history("sess-defer", timestamp=3000.5)
    assert len(recorded) == 1
    assert float(recorded[0]["timestamp"]) == 3000.5


def test_finally_skips_flush_when_other_stream_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter()
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        interface_deep, "append_history_record", lambda **kwargs: recorded.append(kwargs)
    )
    adapter._record_goal_set_history_if_needed(
        _goal_set_request(),
        action="set",
        result_type="goal_stream",
        goal_payload={"goal_id": "g1", "objective": "查杭州天气"},
        defer=True,
    )
    adapter._session_agent_tasks = {"sess-defer": {_running_task()}}

    if not adapter._session_has_other_running_agent_tasks("sess-defer"):
        adapter._flush_pending_goal_objective_history("sess-defer")

    assert recorded == []
    assert "sess-defer" in interface_deep._pending_goal_objective_history
