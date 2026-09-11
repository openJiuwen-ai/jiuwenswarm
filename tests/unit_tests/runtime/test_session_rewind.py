# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Behavior contracts for the transport-neutral Runtime rewind API."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.agents.harness.common import session_ops_service
from jiuwenswarm.runtime.service import AgentRuntime, RuntimeStateError
from jiuwenswarm.runtime.session_mutation import session_mutation_lock
from jiuwenswarm.runtime.session_provisioner import SessionDescriptor
from jiuwenswarm.runtime.session_rewind import (
    SessionRewindAction,
    SessionRewindContextPolicy,
    SessionRewindError,
    SessionRewindInput,
    SessionRewindListInput,
)


class _AgentManager:
    def __init__(self) -> None:
        self.session_agent: Any = None
        self.channel_agent: Any = None
        self.created_agent: Any = None
        self.get_agent_calls: list[dict[str, Any]] = []

    def get_agent_for_session_nowait(self, **_kwargs: Any) -> Any:
        return self.session_agent

    def get_agent_nowait(self, **_kwargs: Any) -> Any:
        return self.channel_agent

    async def get_agent(self, **kwargs: Any) -> Any:
        self.get_agent_calls.append(kwargs)
        return self.created_agent


async def _new_runtime(manager: _AgentManager | None = None) -> AgentRuntime:
    async def _initialize() -> None:
        return None

    runtime = AgentRuntime(
        agent_manager=cast(Any, manager or _AgentManager()),
        initializer=_initialize,
    )
    await runtime.start()
    return runtime


def _descriptor(
    *,
    channel_id: str = "process_cli",
    mode: str = "code.normal",
    work_mode: str = "code",
    project_dir: str = "D:/project",
) -> SessionDescriptor:
    return SessionDescriptor(
        session_id="session-1",
        channel_id=channel_id,
        mode=mode,
        work_mode=work_mode,
        project_dir=project_dir,
    )


def _input(
    action: SessionRewindAction = SessionRewindAction.CONVERSATION,
    *,
    context_policy: SessionRewindContextPolicy = (SessionRewindContextPolicy.LIVE_ONLY),
    require_context: bool = False,
    turn_index: Any = 2,
) -> SessionRewindInput:
    return SessionRewindInput(
        operation_id="rewind-1",
        channel_id="process_cli",
        session_id="session-1",
        turn_index=turn_index,
        action=action,
        context_policy=context_policy,
        require_context=require_context,
        compact_summary="  compact summary  ",
        summarized_count=4,
    )


def _install_scope(
    monkeypatch: pytest.MonkeyPatch,
    runtime: AgentRuntime,
    session_dir: Path,
    *,
    descriptor: SessionDescriptor | None = None,
) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        runtime,
        "_rewind_session_scope",
        AsyncMock(
            return_value=(
                "session-1",
                descriptor if descriptor is not None else _descriptor(),
                session_dir,
            )
        ),
    )


@pytest.mark.asyncio
async def test_list_rewind_turns_returns_typed_transport_neutral_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = await _new_runtime()
    _install_scope(monkeypatch, runtime, tmp_path, descriptor=_descriptor())
    calls: list[dict[str, Any]] = []

    def _list(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "turns": [
                {
                    "turn_index": "2",
                    "content_preview": "second prompt",
                    "timestamp": 12.5,
                    "id": "message-2",
                    "request_id": "request-2",
                    "stats": {
                        "filesChanged": "1",
                        "linesAdded": 3,
                        "linesRemoved": False,
                    },
                    "files": [
                        {
                            "path": "src/a.py",
                            "linesAdded": "3",
                            "linesRemoved": None,
                            "isNewFile": True,
                        },
                        "ignore malformed entry",
                    ],
                },
                "ignore malformed turn",
            ],
            "total": "3",
        }

    monkeypatch.setattr(session_ops_service, "list_session_turns", _list)

    result = await runtime.list_rewind_turns(
        SessionRewindListInput(
            channel_id="process_cli",
            session_id="session-1",
        )
    )

    assert calls == [{"session_id": "session-1", "project_dir": "D:/project"}]
    assert result.to_dict() == {
        "turns": [
            {
                "turn_index": 2,
                "content_preview": "second prompt",
                "timestamp": 12.5,
                "id": "message-2",
                "request_id": "request-2",
                "stats": {
                    "filesChanged": 1,
                    "linesAdded": 3,
                    "linesRemoved": 0,
                },
                "files": [
                    {
                        "path": "src/a.py",
                        "linesAdded": 3,
                        "linesRemoved": 0,
                        "isNewFile": True,
                    }
                ],
            }
        ],
        "total": 3,
    }


@pytest.mark.asyncio
async def test_list_rewind_turns_explicit_project_dir_overrides_session_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = await _new_runtime()
    _install_scope(monkeypatch, runtime, tmp_path, descriptor=_descriptor())
    list_turns = MagicMock(return_value={"turns": [], "total": 0})
    monkeypatch.setattr(session_ops_service, "list_session_turns", list_turns)

    await runtime.list_rewind_turns(
        SessionRewindListInput(
            channel_id="process_cli",
            session_id="session-1",
            project_dir="D:/override",
        )
    )

    list_turns.assert_called_once_with(
        session_id="session-1",
        project_dir="D:/override",
    )


@pytest.mark.asyncio
async def test_conversation_rewind_preserves_legacy_mutation_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = await _new_runtime()
    _install_scope(monkeypatch, runtime, tmp_path)
    trace: list[tuple[Any, ...]] = []
    deep_agent = object()

    def _rewind(**kwargs: Any) -> dict[str, Any]:
        trace.append(("rewind", kwargs))
        return {
            **kwargs,
            "content": "second prompt",
            "content_preview": "second prompt",
            "remaining_records": 2,
            "removed_records": 3,
        }

    async def _resolve(**kwargs: Any) -> tuple[Any, Any]:
        trace.append(("resolve", kwargs))
        return deep_agent, object()

    async def _context(**kwargs: Any) -> bool:
        trace.append(("context", kwargs))
        return True

    monkeypatch.setattr(session_ops_service, "rewind_session", _rewind)
    monkeypatch.setattr(session_ops_service, "rewind_session_context", _context)
    monkeypatch.setattr(runtime, "_resolve_rewind_context_agent", _resolve)

    result = await runtime.rewind_session(_input())

    assert [entry[0] for entry in trace] == ["rewind", "resolve", "context"]
    assert trace[0][1] == {"session_id": "session-1", "turn_index": 2}
    assert trace[1][1]["ensure"] is False
    assert trace[2][1] == {
        "deep_agent": deep_agent,
        "session_id": "session-1",
        "turn_index": 2,
    }
    assert result.to_dict() == {
        "session_id": "session-1",
        "turn_index": 2,
        "content": "second prompt",
        "content_preview": "second prompt",
        "remaining_records": 2,
        "removed_records": 3,
        "rewind_context": True,
    }


@pytest.mark.asyncio
async def test_conversation_and_files_restores_before_history_and_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = await _new_runtime()
    _install_scope(monkeypatch, runtime, tmp_path)
    trace: list[str] = []

    def _restore(**kwargs: Any) -> dict[str, Any]:
        trace.append("restore")
        assert kwargs == {
            "session_id": "session-1",
            "turn_index": 2,
            "project_dir": "D:/project",
        }
        return {
            "restored_files": ["restored.txt"],
            "deleted_files": ["created.txt"],
            "errors": [{"file": "locked.txt", "error": "locked"}],
        }

    def _rewind(**_kwargs: Any) -> dict[str, Any]:
        trace.append("rewind")
        return {"session_id": "session-1", "turn_index": 2}

    async def _resolve(**_kwargs: Any) -> tuple[Any, Any]:
        trace.append("resolve")
        return object(), object()

    async def _context(**_kwargs: Any) -> bool:
        trace.append("context")
        return True

    monkeypatch.setattr(session_ops_service, "restore_session_files", _restore)
    monkeypatch.setattr(session_ops_service, "rewind_session", _rewind)
    monkeypatch.setattr(session_ops_service, "rewind_session_context", _context)
    monkeypatch.setattr(runtime, "_resolve_rewind_context_agent", _resolve)

    result = await runtime.rewind_session(
        _input(SessionRewindAction.CONVERSATION_AND_FILES)
    )

    assert trace == ["restore", "rewind", "resolve", "context"]
    assert result.to_dict() == {
        "session_id": "session-1",
        "turn_index": 2,
        "rewind_context": True,
        "restored_files": ["restored.txt"],
        "deleted_files": ["created.txt"],
        "restore_errors": [{"file": "locked.txt", "error": "locked"}],
    }


@pytest.mark.asyncio
async def test_files_only_does_not_touch_history_or_agent_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = await _new_runtime()
    _install_scope(monkeypatch, runtime, tmp_path)
    rewind = MagicMock()
    resolve = AsyncMock()

    def _restore(**_kwargs: Any) -> dict[str, Any]:
        return {
            "session_id": "session-1",
            "turn_index": 2,
            "restored_files": ["a.py"],
            "deleted_files": [],
            "errors": None,
        }

    monkeypatch.setattr(session_ops_service, "restore_session_files", _restore)
    monkeypatch.setattr(session_ops_service, "rewind_session", rewind)
    monkeypatch.setattr(runtime, "_resolve_rewind_context_agent", resolve)

    result = await runtime.rewind_session(_input(SessionRewindAction.FILES_ONLY))

    assert result.to_dict() == {
        "session_id": "session-1",
        "turn_index": 2,
        "restored_files": ["a.py"],
        "deleted_files": [],
        "errors": [],
    }
    rewind.assert_not_called()
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_only_missing_agent_preserves_partial_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = await _new_runtime()
    _install_scope(monkeypatch, runtime, tmp_path)
    monkeypatch.setattr(
        session_ops_service,
        "rewind_session",
        lambda **kwargs: dict(kwargs),
    )
    monkeypatch.setattr(
        runtime,
        "_resolve_rewind_context_agent",
        AsyncMock(return_value=None),
    )

    result = await runtime.rewind_session(_input())

    assert result.to_dict() == {
        "session_id": "session-1",
        "turn_index": 2,
        "rewind_context": False,
    }


@pytest.mark.asyncio
async def test_live_only_context_failure_is_nonfatal_after_history_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = await _new_runtime()
    _install_scope(monkeypatch, runtime, tmp_path)
    monkeypatch.setattr(
        session_ops_service,
        "rewind_session",
        lambda **kwargs: dict(kwargs),
    )
    monkeypatch.setattr(
        runtime,
        "_resolve_rewind_context_agent",
        AsyncMock(return_value=(object(), object())),
    )

    async def _fail_context(**_kwargs: Any) -> bool:
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(
        session_ops_service,
        "rewind_session_context",
        _fail_context,
    )

    result = await runtime.rewind_session(_input())

    assert result.context_rebuilt is False


@pytest.mark.asyncio
async def test_compact_from_rewinds_then_rebuilds_then_appends_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = await _new_runtime()
    _install_scope(monkeypatch, runtime, tmp_path)
    trace: list[tuple[str, Any]] = []

    def _rewind(**kwargs: Any) -> dict[str, Any]:
        trace.append(("rewind", kwargs))
        return {**kwargs, "removed_records": 4}

    async def _context(**kwargs: Any) -> bool:
        trace.append(("context", kwargs))
        return True

    def _append(**kwargs: Any) -> None:
        trace.append(("append", kwargs))

    monkeypatch.setattr(session_ops_service, "rewind_session", _rewind)
    monkeypatch.setattr(session_ops_service, "rewind_session_context", _context)
    monkeypatch.setattr(
        runtime,
        "_resolve_rewind_context_agent",
        AsyncMock(return_value=(object(), object())),
    )
    monkeypatch.setattr(runtime, "_append_rewind_compact_records", _append)

    result = await runtime.rewind_session(_input(SessionRewindAction.COMPACT_FROM))

    assert [entry[0] for entry in trace] == ["rewind", "context", "append"]
    assert trace[2][1] == {
        "session_id": "session-1",
        "channel_id": "process_cli",
        "turn_index": 2,
        "summarized_count": 4,
        "compact_summary": "  compact summary  ",
    }
    assert result.summarized_messages == 4


@pytest.mark.asyncio
async def test_compact_up_to_uses_domain_compaction_before_context_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = await _new_runtime()
    _install_scope(monkeypatch, runtime, tmp_path)
    trace: list[tuple[str, Any]] = []

    def _compact(**kwargs: Any) -> dict[str, Any]:
        trace.append(("compact", kwargs))
        return {
            "session_id": "session-1",
            "turn_index": 2,
            "summarized_messages": 3,
            "direction": "up_to",
        }

    async def _context(**kwargs: Any) -> bool:
        trace.append(("context", kwargs))
        return True

    monkeypatch.setattr(session_ops_service, "compact_partial_session", _compact)
    monkeypatch.setattr(
        session_ops_service,
        "rewind_session",
        lambda **_kwargs: pytest.fail("plain rewind must not run for compact_up_to"),
    )
    monkeypatch.setattr(session_ops_service, "rewind_session_context", _context)
    monkeypatch.setattr(
        runtime,
        "_resolve_rewind_context_agent",
        AsyncMock(return_value=(object(), object())),
    )

    result = await runtime.rewind_session(_input(SessionRewindAction.COMPACT_UP_TO))

    assert [entry[0] for entry in trace] == ["compact", "context"]
    assert trace[0][1] == {
        "session_id": "session-1",
        "turn_index": 2,
        "direction": "up_to",
        "llm_summary": "  compact summary  ",
    }
    assert result.summarized_messages == 3
    assert result.direction == "up_to"


def test_compact_from_records_preserve_established_history_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.session import session_history

    records: list[dict[str, Any]] = []
    monkeypatch.setattr(
        session_history,
        "append_history_record",
        lambda **kwargs: records.append(kwargs),
    )

    AgentRuntime._append_rewind_compact_records(
        session_id="session-1",
        channel_id="process_cli",
        turn_index=2,
        summarized_count=4,
        compact_summary="  compact summary  ",
    )

    assert [record["event_type"] for record in records] == [
        "context.compact_boundary",
        "context.rewind_summary",
        "context.compact_summary",
    ]
    assert {record["request_id"] for record in records} == {records[0]["request_id"]}
    assert [record["channel_id"] for record in records] == [
        "process_cli",
        "process_cli",
        "process_cli",
    ]
    assert records[2]["content"] == "compact summary"
    assert records[2]["extra"]["transcript_only"] is True
    assert all(
        record["extra"]["compact_metadata"]
        == {
            "trigger": "manual_rewind",
            "direction": "from",
            "turn_index": 2,
            "summarized_messages": 4,
        }
        for record in records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn_index", "message"),
    [
        (True, "turn_index must be integer"),
        ("two", "turn_index must be integer"),
        (0, "turn_index must be >= 1"),
    ],
)
async def test_rewind_rejects_invalid_turn_before_session_lookup(
    monkeypatch: pytest.MonkeyPatch,
    turn_index: Any,
    message: str,
) -> None:
    runtime = await _new_runtime()
    scope = AsyncMock()
    monkeypatch.setattr(runtime, "_rewind_session_scope", scope)

    with pytest.raises(SessionRewindError, match=message) as caught:
        await runtime.rewind_session(_input(turn_index=turn_index))

    assert caught.value.code == "BAD_REQUEST"
    scope.assert_not_awaited()


@pytest.mark.asyncio
async def test_rewind_rejects_unknown_action_before_session_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _new_runtime()
    scope = AsyncMock()
    monkeypatch.setattr(runtime, "_rewind_session_scope", scope)
    rewind_input = _input(cast(SessionRewindAction, "unknown"))

    with pytest.raises(SessionRewindError, match="unknown rewind action") as caught:
        await runtime.rewind_session(rewind_input)

    assert caught.value.code == "BAD_REQUEST"
    scope.assert_not_awaited()


@pytest.mark.asyncio
async def test_rewind_requires_started_runtime() -> None:
    runtime = AgentRuntime(
        agent_manager=cast(Any, _AgentManager()),
        initializer=AsyncMock(),
    )

    with pytest.raises(RuntimeStateError, match="runtime is not started"):
        await runtime.rewind_session(_input())


@pytest.mark.asyncio
async def test_rewind_scope_hides_foreign_channel_as_not_found(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from jiuwenswarm.common import utils

    session_dir = tmp_path / "session-1"
    session_dir.mkdir()
    runtime = await _new_runtime()
    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(
        runtime,
        "describe_session",
        AsyncMock(return_value=_descriptor(channel_id="web")),
    )

    with pytest.raises(SessionRewindError, match="session not found") as caught:
        await runtime._rewind_session_scope(
            channel_id="process_cli",
            session_id="session-1",
            require_existing=True,
        )

    assert caught.value.code == "NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel_id", "session_id", "expected"),
    [
        (" ", "session-1", "channel_id is required"),
        ("process_cli", " ", "session_id is required"),
        ("process_cli", "../outside", "invalid"),
    ],
)
async def test_rewind_scope_rejects_missing_or_unsafe_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    channel_id: str,
    session_id: str,
    expected: str,
) -> None:
    from jiuwenswarm.common import utils

    runtime = await _new_runtime()
    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(runtime, "describe_session", AsyncMock(return_value=None))

    with pytest.raises(SessionRewindError, match=expected):
        await runtime._rewind_session_scope(
            channel_id=channel_id,
            session_id=session_id,
            require_existing=True,
        )


@pytest.mark.asyncio
async def test_rewind_scope_reports_missing_history_without_enumerating_channel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from jiuwenswarm.common import utils

    runtime = await _new_runtime()
    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(runtime, "describe_session", AsyncMock(return_value=None))

    with pytest.raises(SessionRewindError, match="session history not found") as caught:
        await runtime._rewind_session_scope(
            channel_id="process_cli",
            session_id="missing",
            require_existing=True,
        )

    assert caught.value.code == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_ensure_persisted_rejects_non_single_agent_mode_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = await _new_runtime()
    _install_scope(
        monkeypatch,
        runtime,
        tmp_path,
        descriptor=_descriptor(mode="team.code.normal"),
    )
    mutation = AsyncMock()
    monkeypatch.setattr(runtime, "_apply_session_rewind", mutation)

    with pytest.raises(
        SessionRewindError,
        match="session rewind is not supported for this mode",
    ) as caught:
        await runtime.rewind_session(
            _input(
                context_policy=SessionRewindContextPolicy.ENSURE_PERSISTED,
                require_context=True,
            )
        )

    assert caught.value.code == "UNSUPPORTED_MODE"
    mutation.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_persisted_prepares_context_before_durable_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = await _new_runtime()
    _install_scope(monkeypatch, runtime, tmp_path)
    trace: list[tuple[str, Any]] = []

    async def _resolve(**kwargs: Any) -> tuple[Any, Any]:
        trace.append(("resolve", kwargs))
        return object(), object()

    async def _mutate(*_args: Any, **kwargs: Any) -> Any:
        trace.append(("mutate", kwargs))
        return AgentRuntime._build_rewind_result(
            SessionRewindAction.CONVERSATION,
            {},
            session_id="session-1",
            turn_index=2,
            context_rebuilt=True,
        )

    monkeypatch.setattr(runtime, "_resolve_rewind_context_agent", _resolve)
    monkeypatch.setattr(runtime, "_apply_session_rewind", _mutate)

    await runtime.rewind_session(
        _input(
            context_policy=SessionRewindContextPolicy.ENSURE_PERSISTED,
            require_context=True,
        )
    )

    assert [entry[0] for entry in trace] == ["resolve", "mutate"]
    assert trace[0][1]["ensure"] is True
    assert trace[1][1]["resolved_pair"] is not None


@pytest.mark.asyncio
async def test_require_context_fails_before_durable_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = await _new_runtime()
    _install_scope(monkeypatch, runtime, tmp_path)
    monkeypatch.setattr(
        runtime,
        "_resolve_rewind_context_agent",
        AsyncMock(return_value=None),
    )
    mutation = AsyncMock()
    monkeypatch.setattr(runtime, "_apply_session_rewind", mutation)

    with pytest.raises(RuntimeError, match="no agent instance available"):
        await runtime.rewind_session(_input(require_context=True))

    mutation.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_session_mutation_lock_serializes_callers(tmp_path: Path) -> None:
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def _first() -> None:
        async with session_mutation_lock(tmp_path):
            first_entered.set()
            await release_first.wait()

    async def _second() -> None:
        async with session_mutation_lock(tmp_path):
            second_entered.set()

    first = asyncio.create_task(_first())
    await asyncio.wait_for(first_entered.wait(), timeout=1)
    second = asyncio.create_task(_second())
    await asyncio.sleep(0.12)
    assert not second_entered.is_set()

    release_first.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    assert second_entered.is_set()


@pytest.mark.asyncio
async def test_cancellation_while_waiting_for_lock_has_no_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = await _new_runtime()
    _install_scope(monkeypatch, runtime, tmp_path)
    mutation = AsyncMock()
    monkeypatch.setattr(runtime, "_apply_session_rewind", mutation)

    async with session_mutation_lock(tmp_path):
        waiting = asyncio.create_task(runtime.rewind_session(_input()))
        await asyncio.sleep(0.12)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

    mutation.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_after_mutation_starts_waits_for_consistent_finish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = await _new_runtime()
    _install_scope(monkeypatch, runtime, tmp_path)
    started = asyncio.Event()
    allow_finish = asyncio.Event()
    finished = asyncio.Event()

    async def _mutate(*_args: Any, **_kwargs: Any) -> Any:
        started.set()
        await allow_finish.wait()
        finished.set()
        return AgentRuntime._build_rewind_result(
            SessionRewindAction.CONVERSATION,
            {},
            session_id="session-1",
            turn_index=2,
            context_rebuilt=True,
        )

    monkeypatch.setattr(runtime, "_apply_session_rewind", _mutate)
    operation = asyncio.create_task(runtime.rewind_session(_input()))
    await asyncio.wait_for(started.wait(), timeout=1)

    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done()
    assert not finished.is_set()

    allow_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, timeout=1)
    assert finished.is_set()
