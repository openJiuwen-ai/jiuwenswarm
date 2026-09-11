# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.runtime import AgentRuntime, ContextCompactInput


class _CompactAgent:
    def __init__(self, result: dict[str, object], order: list[str]) -> None:
        self._result = result
        self._order = order
        self.ensure_calls = 0
        self.compress_calls: list[tuple[str, bool]] = []

    async def ensure_instance(self) -> None:
        self.ensure_calls += 1
        self._order.append("ensure")

    async def compress_context(
        self,
        session_id: str,
        *,
        return_state: bool = False,
    ) -> dict[str, object]:
        self.compress_calls.append((session_id, return_state))
        self._order.append("compress")
        return self._result


class _CancellingCompactAgent(_CompactAgent):
    async def compress_context(
        self,
        session_id: str,
        *,
        return_state: bool = False,
    ) -> dict[str, object]:
        self.compress_calls.append((session_id, return_state))
        self._order.append("compress")
        raise asyncio.CancelledError


class _CompactAgentManager:
    def __init__(
        self,
        *,
        session_agent: _CompactAgent | None,
        fallback_agent: _CompactAgent | None = None,
    ) -> None:
        self.session_agent = session_agent
        self.fallback_agent = fallback_agent
        self.session_lookups: list[tuple[str, str]] = []
        self.agent_lookups: list[dict[str, object]] = []

    def get_agent_for_session_nowait(
        self,
        *,
        channel_id: str,
        session_id: str,
    ) -> _CompactAgent | None:
        self.session_lookups.append((channel_id, session_id))
        return self.session_agent

    async def get_agent(self, **kwargs: object) -> _CompactAgent | None:
        self.agent_lookups.append(kwargs)
        return self.fallback_agent

    async def cancel_all_inflight_work(self, *args: object, **kwargs: object) -> None:
        return None

    async def cleanup(self) -> None:
        return None


def _patch_compact_observability(
    monkeypatch: pytest.MonkeyPatch,
    order: list[str],
    *,
    opened: dict[str, object] | None = None,
) -> None:
    from openjiuwen.harness import observability as harness_observability

    from jiuwenswarm.agents.harness import agent_observability

    def open_span(**kwargs: object) -> object:
        order.append("span.open")
        if opened is not None:
            opened.update(kwargs)
        return SimpleNamespace(name="compact-span")

    def close_span(*args: object, **kwargs: object) -> None:
        order.append("span.close")

    monkeypatch.setattr(
        agent_observability,
        "sync_agent_observability",
        lambda: order.append("observability.sync"),
    )
    monkeypatch.setattr(harness_observability, "open_agent_run_span", open_span)
    monkeypatch.setattr(harness_observability, "close_agent_run_span", close_span)


@pytest.mark.asyncio
async def test_compact_context_preserves_history_event_span_order_and_team_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness import team as team_package
    from jiuwenswarm.server.runtime.session import session_history

    order: list[str] = []
    opened: dict[str, object] = {}
    stats = {"raw_total_tokens": 1000, "total_tokens": 300}
    agent = _CompactAgent(
        {
            "result": "compressed",
            "stats": stats,
            "state": {"source": "runtime"},
            "compact_summary": "Processor: RoundLevelCompressor\nkept facts",
        },
        order,
    )
    manager = _CompactAgentManager(session_agent=agent)
    subject = SimpleNamespace(subject_id="team-leader")
    leader = SimpleNamespace(observability_execution_subject=lambda session_id: subject)
    team_manager = SimpleNamespace(get_team_agent=lambda session_id: leader)
    history_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        team_package,
        "get_team_manager",
        lambda channel_id: team_manager,
    )

    def append_history(**kwargs: object) -> None:
        order.append("history")
        history_calls.append(kwargs)

    monkeypatch.setattr(
        session_history,
        "append_compact_history_records",
        append_history,
    )
    _patch_compact_observability(monkeypatch, order, opened=opened)

    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock())
    await runtime.start()
    delivered_events = []

    async def deliver_event(event) -> None:
        order.append("push")
        delivered_events.append(event)

    result = await runtime.compact_context(
        ContextCompactInput(
            request_id="compact-request",
            channel_id="web",
            session_id="team-session",
            mode="team.work.normal",
            project_dir="D:/project",
        ),
        on_event=deliver_event,
    )

    assert result.result == "compressed"
    assert result.stats is stats
    assert result.summary == "Processor: RoundLevelCompressor\nkept facts"
    assert result.events == tuple(delivered_events)
    assert order == [
        "ensure",
        "observability.sync",
        "span.open",
        "compress",
        "history",
        "push",
        "span.close",
    ]
    assert manager.session_lookups == [("web", "team-session")]
    assert manager.agent_lookups == []
    assert agent.compress_calls == [("team-session", True)]
    assert opened["execution_subject"] is subject
    assert opened["mode"] == "team.work.normal"
    assert history_calls == [
        {
            "session_id": "team-session",
            "request_id": "compact-request",
            "channel_id": "web",
            "summary": "Processor: RoundLevelCompressor\nkept facts",
            "timestamp": history_calls[0]["timestamp"],
            "trigger": "manual",
            "stats": stats,
            "mode": "team.work.normal",
        }
    ]
    assert len(delivered_events) == 1
    assert delivered_events[0].request_id == "compact-request"
    assert delivered_events[0].payload == {
        "source": "runtime",
        "event_type": "context.compression_state",
        "status": "completed",
        "phase": "active_compress",
        "processor": "RoundLevelCompressor",
        "before": {"tokens": 1000},
        "after": {"tokens": 300},
        "saved": {"tokens": 700, "percent": 70.0},
        "summary": "\u2713 Context compacted: 0.3K/1.0K tokens (70.0% saved)",
        "compact_summary": "Processor: RoundLevelCompressor\nkept facts",
    }


@pytest.mark.asyncio
async def test_compact_context_returns_event_without_delivery_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.session import session_history

    order: list[str] = []
    agent = _CompactAgent(
        {
            "result": "compressed",
            "stats": {"raw_total_tokens": 20, "total_tokens": 10},
            "summary": "compact summary",
        },
        order,
    )
    manager = _CompactAgentManager(session_agent=None, fallback_agent=agent)
    monkeypatch.setattr(
        session_history,
        "append_compact_history_records",
        lambda **kwargs: order.append("history"),
    )
    _patch_compact_observability(monkeypatch, order)

    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock())
    await runtime.start()
    result = await runtime.compact_context(
        ContextCompactInput(
            request_id="compact-fallback",
            channel_id="process_cli",
            session_id="local-session",
            mode="code.normal",
            project_dir="D:/workspace",
        )
    )

    assert len(result.events) == 1
    assert result.events[0].event_type == "context.compression_state"
    assert manager.agent_lookups == [
        {
            "channel_id": "process_cli",
            "mode": "code",
            "project_dir": "D:/workspace",
            "sub_mode": "normal",
        }
    ]
    assert order[-2:] == ["history", "span.close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("compact_result", ["busy", "noop"])
async def test_compact_context_preserves_non_compressed_success_results(
    monkeypatch: pytest.MonkeyPatch,
    compact_result: str,
) -> None:
    order: list[str] = []
    agent = _CompactAgent(
        {"result": compact_result, "stats": None},
        order,
    )
    manager = _CompactAgentManager(session_agent=agent)
    _patch_compact_observability(monkeypatch, order)

    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock())
    await runtime.start()
    result = await runtime.compact_context(
        ContextCompactInput(
            request_id="compact-noop",
            channel_id="tui",
            session_id="session-noop",
        )
    )

    assert result.result == compact_result
    assert result.stats is None
    assert result.summary == ""
    assert result.events == ()
    assert order[-1] == "span.close"


@pytest.mark.asyncio
async def test_compact_context_propagates_cancellation_after_span_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    agent = _CancellingCompactAgent({}, order)
    manager = _CompactAgentManager(session_agent=agent)
    _patch_compact_observability(monkeypatch, order)

    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock())
    await runtime.start()

    with pytest.raises(asyncio.CancelledError):
        await runtime.compact_context(
            ContextCompactInput(
                request_id="compact-cancelled",
                channel_id="tui",
                session_id="session-cancelled",
            )
        )

    assert order[-2:] == ["compress", "span.close"]
