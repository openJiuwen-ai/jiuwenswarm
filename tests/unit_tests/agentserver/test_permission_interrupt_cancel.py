# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cancelled permission HITL must become a legal tool_result, not a leftover interrupt."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage, UserMessage
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.interrupt.state import (
    INTERRUPTION_KEY,
    ToolInterruptEntry,
    ToolInterruptionState,
)
from openjiuwen.harness.rails.interrupt.ask_user_rail import AskUserPayload
from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmPayload

from jiuwenswarm.server.runtime.session.permission_interrupt_cancel import (
    collect_cancelled_confirm_tool_calls,
    insert_cancelled_tool_messages,
    is_pure_confirm_payload_interrupt,
    settle_cancelled_confirm_interrupt,
    settle_live_cancelled_confirm_interrupt,
)


class _MemSession:
    def __init__(self, session_id: str = "sess-1") -> None:
        self._session_id = session_id
        self._state: dict[str, Any] = {}
        self.commit = AsyncMock()

    def get_session_id(self) -> str:
        return self._session_id

    def get_state(self, key: str) -> Any:
        return self._state.get(key)

    def update_state(self, values: dict[str, Any]) -> None:
        self._state.update(values)


class _MemContext:
    def __init__(self, messages: list[Any] | None = None) -> None:
        self.messages = list(messages or [])

    def get_messages(self, *args: Any, **kwargs: Any) -> list[Any]:
        return list(self.messages)

    def set_messages(self, messages: list[Any], with_history: bool = True) -> None:
        self.messages = list(messages)

    async def add_messages(self, messages: Any, **kwargs: Any) -> list[Any]:
        to_add = messages if isinstance(messages, list) else [messages]
        self.messages.extend(to_add)
        return list(to_add)


def _confirm_state(*, tool_name: str = "cron_list_jobs", tool_call_id: str = "call-1") -> ToolInterruptionState:
    return ToolInterruptionState(
        ai_message=AssistantMessage(
            content="",
            tool_calls=[
                ToolCall(
                    id=tool_call_id,
                    type="function",
                    name=tool_name,
                    arguments="{}",
                )
            ],
        ),
        iteration=1,
        interrupted_tools={
            tool_call_id: ToolInterruptEntry(
                tool_call=ToolCall(
                    id=tool_call_id,
                    type="function",
                    name=tool_name,
                    arguments="{}",
                ),
                interrupt_requests={
                    tool_call_id: InterruptRequest(
                        message="approve?",
                        payload_schema=ConfirmPayload.to_schema(),
                    )
                },
            )
        },
    )


def _ask_user_state() -> ToolInterruptionState:
    return ToolInterruptionState(
        ai_message=AssistantMessage(content="question"),
        iteration=1,
        interrupted_tools={
            "call-ask": ToolInterruptEntry(
                tool_call=ToolCall(
                    id="call-ask",
                    type="function",
                    name="ask_user",
                    arguments="{}",
                ),
                interrupt_requests={
                    "call-ask": InterruptRequest(
                        message="name?",
                        payload_schema=AskUserPayload.to_schema(),
                    )
                },
            )
        },
    )


def _skill_turbo_state() -> ToolInterruptionState:
    return ToolInterruptionState(
        ai_message=AssistantMessage(content=""),
        iteration=1,
        interrupted_tools={
            "call-st": ToolInterruptEntry(
                tool_call=ToolCall(
                    id="call-st",
                    type="function",
                    name="skill_acceleration_exec",
                    arguments="{}",
                ),
                interrupt_requests={
                    "call-st": InterruptRequest(
                        message="continue?",
                        payload_schema=ConfirmPayload.to_schema(),
                    )
                },
            )
        },
    )


def test_pure_confirm_interrupt_is_detected() -> None:
    assert is_pure_confirm_payload_interrupt(_confirm_state()) is True
    assert is_pure_confirm_payload_interrupt(_ask_user_state()) is False
    assert is_pure_confirm_payload_interrupt(_skill_turbo_state()) is False
    assert is_pure_confirm_payload_interrupt(None) is False


def test_mixed_confirm_and_ask_user_is_skipped() -> None:
    mixed = _confirm_state()
    mixed.interrupted_tools["call-ask"] = _ask_user_state().interrupted_tools["call-ask"]
    assert is_pure_confirm_payload_interrupt(mixed) is False


def test_insert_cancelled_tool_messages_keeps_assistant_and_fills_gap() -> None:
    messages = [
        UserMessage(content="list cron jobs"),
        AssistantMessage(
            content="",
            tool_calls=[
                ToolCall(
                    id="call-1",
                    type="function",
                    name="cron_list_jobs",
                    arguments="{}",
                )
            ],
        ),
        UserMessage(content="继续"),
    ]
    rebuilt, inserted = insert_cancelled_tool_messages(
        messages,
        [("call-1", "cron_list_jobs")],
        language="cn",
    )
    assert inserted == 1
    assert isinstance(rebuilt[1], AssistantMessage)
    assert isinstance(rebuilt[2], ToolMessage)
    assert rebuilt[2].tool_call_id == "call-1"
    assert "cron_list_jobs" in rebuilt[2].content
    assert isinstance(rebuilt[3], UserMessage)


def test_insert_cancelled_tool_messages_skips_transcript_without_assistant() -> None:
    messages = [UserMessage(content="unrelated member context")]
    rebuilt, inserted = insert_cancelled_tool_messages(
        messages,
        [("call-1", "cron_list_jobs")],
    )
    assert inserted == 0
    assert rebuilt == messages
    assert all(not isinstance(item, ToolMessage) for item in rebuilt)


def test_insert_cancelled_tool_messages_is_idempotent() -> None:
    messages = [
        AssistantMessage(
            content="",
            tool_calls=[
                ToolCall(
                    id="call-1",
                    type="function",
                    name="bash",
                    arguments="{}",
                )
            ],
        ),
        ToolMessage(content="already cancelled", tool_call_id="call-1"),
    ]
    rebuilt, inserted = insert_cancelled_tool_messages(
        messages,
        [("call-1", "bash")],
    )
    assert inserted == 0
    assert rebuilt == messages


@pytest.mark.asyncio
async def test_settle_confirm_interrupt_writes_tool_message_and_clears_key() -> None:
    session = _MemSession()
    state = _confirm_state()
    session.update_state({INTERRUPTION_KEY: state})
    context = _MemContext(
        [
            UserMessage(content="list jobs"),
            state.ai_message,
        ]
    )
    hitl = SimpleNamespace(clear=lambda sess: sess.update_state({"hitl_cleared": True}))

    settled = await settle_cancelled_confirm_interrupt(
        session,
        context=context,
        hitl_handler=hitl,
        language="cn",
    )

    assert settled is True
    assert session.get_state(INTERRUPTION_KEY) is None
    assert session._state.get("hitl_cleared") is True
    session.commit.assert_awaited()
    assert isinstance(context.messages[1], AssistantMessage)
    assert isinstance(context.messages[2], ToolMessage)
    assert context.messages[2].tool_call_id == "call-1"
    assert collect_cancelled_confirm_tool_calls(state) == [("call-1", "cron_list_jobs")]


@pytest.mark.asyncio
async def test_settle_confirm_interrupt_patches_serialized_context_without_engine() -> None:
    session = _MemSession()
    state = _confirm_state()
    session.update_state({INTERRUPTION_KEY: state})
    session.update_state(
        {
            "context": {
                "default_context_id": {
                    "messages": [
                        UserMessage(content="list jobs"),
                        state.ai_message,
                    ],
                    "offload_messages": {},
                }
            }
        }
    )

    settled = await settle_cancelled_confirm_interrupt(session, language="en")

    assert settled is True
    assert session.get_state(INTERRUPTION_KEY) is None
    messages = session.get_state("context")["default_context_id"]["messages"]
    assert isinstance(messages[2], ToolMessage)
    assert messages[2].tool_call_id == "call-1"
    assert "interrupted by the user" in messages[2].content


@pytest.mark.asyncio
async def test_settle_falls_back_to_session_state_when_live_context_has_no_match() -> None:
    session = _MemSession()
    state = _confirm_state()
    session.update_state({INTERRUPTION_KEY: state})
    session.update_state(
        {
            "context": {
                "default_context_id": {
                    "messages": [
                        UserMessage(content="list jobs"),
                        state.ai_message,
                    ],
                    "offload_messages": {},
                }
            }
        }
    )
    context = _MemContext([UserMessage(content="unrelated")])
    engine = SimpleNamespace(save_contexts=AsyncMock())

    settled = await settle_cancelled_confirm_interrupt(
        session,
        context=context,
        context_engine=engine,
        language="cn",
    )

    assert settled is True
    assert session.get_state(INTERRUPTION_KEY) is None
    messages = session.get_state("context")["default_context_id"]["messages"]
    assert isinstance(messages[2], ToolMessage)
    assert messages[2].tool_call_id == "call-1"
    engine.save_contexts.assert_awaited()


@pytest.mark.asyncio
async def test_settle_does_not_append_orphan_to_unrelated_context_blob() -> None:
    session = _MemSession()
    state = _confirm_state()
    unrelated = [UserMessage(content="member notes")]
    session.update_state({INTERRUPTION_KEY: state})
    session.update_state(
        {
            "context": {
                "other_context_id": {
                    "messages": list(unrelated),
                    "offload_messages": {},
                },
                "default_context_id": {
                    "messages": [
                        UserMessage(content="list jobs"),
                        state.ai_message,
                    ],
                    "offload_messages": {},
                },
            }
        }
    )

    settled = await settle_cancelled_confirm_interrupt(session)

    assert settled is True
    assert session.get_state(INTERRUPTION_KEY) is None
    blobs = session.get_state("context")
    assert blobs["other_context_id"]["messages"] == unrelated
    matched = blobs["default_context_id"]["messages"]
    assert isinstance(matched[-1], ToolMessage)
    assert matched[-1].tool_call_id == "call-1"


@pytest.mark.asyncio
async def test_settle_skips_ask_user_and_skill_turbo() -> None:
    session = _MemSession()
    session.update_state({INTERRUPTION_KEY: _ask_user_state()})
    assert await settle_cancelled_confirm_interrupt(session) is False
    assert isinstance(session.get_state(INTERRUPTION_KEY), ToolInterruptionState)

    session.update_state({INTERRUPTION_KEY: _skill_turbo_state()})
    assert await settle_cancelled_confirm_interrupt(session) is False
    assert session.get_state(INTERRUPTION_KEY).interrupted_tools["call-st"]


@pytest.mark.asyncio
async def test_settle_live_uses_team_agent_harness_session() -> None:
    session = _MemSession()
    state = _confirm_state()
    session.update_state({INTERRUPTION_KEY: state})
    context = _MemContext([state.ai_message])
    context_engine = SimpleNamespace(
        get_context=lambda session_id: context,
        save_contexts=AsyncMock(),
    )
    native = SimpleNamespace(
        _session=session,
        react_agent=SimpleNamespace(
            context_engine=context_engine,
            _hitl_handler=SimpleNamespace(clear=lambda sess: None),
        ),
        system_prompt_builder=SimpleNamespace(language="cn"),
        loop_session=session,
    )
    team_agent = SimpleNamespace(
        harness=SimpleNamespace(
            _native=native,
            _interrupt_session=lambda: session,
        )
    )

    settled = await settle_live_cancelled_confirm_interrupt(team_agent)

    assert settled is True
    assert session.get_state(INTERRUPTION_KEY) is None
    context_engine.save_contexts.assert_awaited()
    assert isinstance(context.messages[-1], ToolMessage)


@pytest.mark.asyncio
async def test_settle_live_prefers_session_that_holds_interrupt() -> None:
    empty_session = _MemSession("empty")
    session = _MemSession()
    session.update_state({INTERRUPTION_KEY: _confirm_state()})
    native = SimpleNamespace(
        _session=session,
        loop_session=empty_session,
        react_agent=SimpleNamespace(
            context_engine=None,
            _hitl_handler=SimpleNamespace(clear=lambda sess: None),
        ),
        system_prompt_builder=SimpleNamespace(language="cn"),
    )
    team_agent = SimpleNamespace(
        harness=SimpleNamespace(
            _native=native,
            _interrupt_session=lambda: empty_session,
        )
    )

    settled = await settle_live_cancelled_confirm_interrupt(team_agent)

    assert settled is True
    assert session.get_state(INTERRUPTION_KEY) is None
    assert empty_session.get_state(INTERRUPTION_KEY) is None


@pytest.mark.asyncio
async def test_team_cancel_settles_live_then_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    manager = TeamManager()
    manager.commit_runtime_ready("sess-1", "demo-team")
    team_agent = SimpleNamespace(card=SimpleNamespace(id="team-card", name="team"))
    manager._runner_team_agents["sess-1"] = team_agent  # type: ignore[index]
    live_calls: list[Any] = []
    persist_calls: list[tuple[str, Any]] = []

    async def fake_live(agent: Any, *, language: str = "cn") -> bool:
        live_calls.append((agent, language))
        return True

    async def fake_persist(session_id: str, *, card: Any = None, language: str = "cn") -> bool:
        persist_calls.append((session_id, card))
        return False

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.permission_interrupt_cancel."
        "settle_live_cancelled_confirm_interrupt",
        fake_live,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.permission_interrupt_cancel."
        "prompt_language_from_team_agent",
        lambda agent: "cn",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.permission_interrupt_cancel."
        "settle_persisted_cancelled_confirm_interrupt",
        fake_persist,
    )

    async def fake_stop_agent_team(*, team_name: str, session_id: str) -> bool:
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.stop_agent_team",
        fake_stop_agent_team,
    )

    cancelled = await manager.cancel_session_runtime("sess-1", reason="interrupt(intent=cancel): ")

    assert cancelled is True
    assert live_calls[0][0] is team_agent
    assert persist_calls == [("sess-1", team_agent.card)]
    assert manager.is_runtime_active("sess-1") is False


@pytest.mark.asyncio
async def test_team_cancel_settles_live_from_runner_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    manager = TeamManager()
    manager.commit_runtime_ready("sess-1", "demo-team")
    team_agent = SimpleNamespace(card=SimpleNamespace(id="team-card", name="team"))
    live_calls: list[Any] = []
    persist_calls: list[tuple[str, Any]] = []

    async def fake_live(agent: Any, *, language: str = "cn") -> bool:
        live_calls.append(agent)
        return True

    async def fake_persist(session_id: str, *, card: Any = None, language: str = "cn") -> bool:
        persist_calls.append((session_id, card))
        return False

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.permission_interrupt_cancel."
        "settle_live_cancelled_confirm_interrupt",
        fake_live,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.permission_interrupt_cancel."
        "prompt_language_from_team_agent",
        lambda agent: "cn",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.permission_interrupt_cancel."
        "settle_persisted_cancelled_confirm_interrupt",
        fake_persist,
    )

    class _FakePool:
        @staticmethod
        async def teams_for_session(session_id: str):
            assert session_id == "sess-1"
            return [SimpleNamespace(agent=team_agent, current_session_id=session_id)]

        @staticmethod
        async def get(team_name: str):
            raise AssertionError("teams_for_session should already resolve the agent")

    fake_runner = SimpleNamespace(_team_runtime_manager=SimpleNamespace(pool=_FakePool()))
    monkeypatch.setattr("openjiuwen.core.runner.runner.GLOBAL_RUNNER", fake_runner)

    async def fake_stop_agent_team(*, team_name: str, session_id: str) -> bool:
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.stop_agent_team",
        fake_stop_agent_team,
    )

    cancelled = await manager.cancel_session_runtime("sess-1", reason="interrupt(intent=cancel): ")

    assert cancelled is True
    assert live_calls == [team_agent]
    assert persist_calls == [("sess-1", team_agent.card)]
    assert manager.is_runtime_active("sess-1") is False


@pytest.mark.asyncio
async def test_team_pause_does_not_settle_confirm_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    manager = TeamManager()
    manager.commit_runtime_ready("sess-1", "demo-team")

    async def boom(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("pause must not settle cancelled confirm interrupts")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.permission_interrupt_cancel."
        "settle_live_cancelled_confirm_interrupt",
        boom,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.permission_interrupt_cancel."
        "settle_persisted_cancelled_confirm_interrupt",
        boom,
    )

    async def fake_pause_agent_team(*, team_name: str, session_id: str) -> bool:
        return True

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.Runner.pause_agent_team",
        fake_pause_agent_team,
    )

    paused = await manager.pause_session_runtime("sess-1", reason="interrupt(intent=pause): ")
    assert paused is True


@pytest.mark.asyncio
async def test_team_cancel_without_runtime_still_persists_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    manager = TeamManager()
    persist_calls: list[str] = []

    async def fake_persist(session_id: str, *, card: Any = None, language: str = "cn") -> bool:
        persist_calls.append(session_id)
        return True

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.permission_interrupt_cancel."
        "settle_persisted_cancelled_confirm_interrupt",
        fake_persist,
    )

    cancelled = await manager.cancel_session_runtime("sess-idle", reason="interrupt(intent=cancel): ")

    assert cancelled is False
    assert persist_calls == ["sess-idle"]
