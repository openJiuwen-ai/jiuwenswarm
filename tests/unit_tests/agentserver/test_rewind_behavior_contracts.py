"""Characterize rewind domain behavior and the legacy Server wire contract.

These tests intentionally describe the current single-Agent behavior.  They do
not define new summary or multi-mode semantics.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common import session_ops_service
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import (
    SessionRewindAction,
    SessionRewindContextPolicy,
    SessionRewindFileError,
    SessionRewindResult,
)
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.runtime.gateway_adapter import SessionAdapter


class _FakeWebSocket:
    pass


class _RuntimeStub:
    def __init__(self, handler) -> None:
        self._handler = handler

    async def rewind_session(self, rewind_input):
        return await self._handler(rewind_input)


def _install_runtime(monkeypatch, server, handler) -> None:
    runtime = _RuntimeStub(handler)
    monkeypatch.setattr(server, "_execution_runtime", lambda: runtime)


def _request(
    method: ReqMethod,
    params: dict[str, Any] | None = None,
    *,
    session_id: str | None = "envelope-session",
) -> AgentRequest:
    return AgentRequest(
        request_id="request-1",
        channel_id="tui",
        session_id=session_id,
        req_method=method,
        params=params or {},
        metadata={"view_id": "view-1"},
    )


def _install_wire_capture(
    monkeypatch: pytest.MonkeyPatch,
    trace: list[tuple[Any, ...]],
) -> None:
    def _encode(response: AgentResponse, *, response_id: str) -> AgentResponse:
        trace.append(("encode", response_id, response))
        return response

    async def _send(_ws: Any, wire: AgentResponse) -> None:
        trace.append(("send", wire))

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        _encode,
    )
    monkeypatch.setattr(agent_ws_server_module, "send_wire_payload", _send)


def _sent_response(trace: list[tuple[Any, ...]]) -> AgentResponse:
    sends = [entry[1] for entry in trace if entry[0] == "send"]
    assert len(sends) == 1
    response = sends[0]
    assert isinstance(response, AgentResponse)
    return response


@pytest.mark.asyncio
async def test_history_list_turns_prefers_param_session_and_preserves_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    expected = {
        "turns": [{"turn_index": 1, "content_preview": "hello"}],
        "total": 1,
    }

    def _list(*, session_id: str) -> dict[str, Any]:
        calls.append({"session_id": session_id})
        return expected

    monkeypatch.setattr(session_ops_service, "list_session_turns", _list)

    response = await SessionAdapter().handle(
        _request(
            ReqMethod.HISTORY_LIST_TURNS,
            {"session_id": "param-session", "project_dir": "ignored"},
        )
    )

    assert calls == [{"session_id": "param-session"}]
    assert response.ok is True
    assert response.payload is expected
    assert response.metadata == {"view_id": "view-1"}


@pytest.mark.asyncio
async def test_history_list_turns_uses_envelope_session_and_maps_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _list(*, session_id: str) -> dict[str, Any]:
        calls.append(session_id)
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(session_ops_service, "list_session_turns", _list)
    response = await SessionAdapter().handle(
        _request(ReqMethod.HISTORY_LIST_TURNS)
    )

    assert calls == ["envelope-session"]
    assert response.ok is False
    assert response.payload == {
        "error": "history unavailable",
        "code": "INTERNAL_ERROR",
    }

    missing = await SessionAdapter().handle(
        _request(ReqMethod.HISTORY_LIST_TURNS, session_id=None)
    )
    assert missing.ok is False
    assert missing.payload == {
        "error": "session_id is required",
        "code": "BAD_REQUEST",
    }


@pytest.mark.asyncio
async def test_files_only_restore_converts_turn_and_preserves_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    expected = {
        "session_id": "param-session",
        "turn_index": 2,
        "restored_files": ["restored.txt"],
        "deleted_files": ["created.txt"],
        "errors": [],
    }

    def _restore(*, session_id: str, turn_index: int) -> dict[str, Any]:
        calls.append({"session_id": session_id, "turn_index": turn_index})
        return expected

    monkeypatch.setattr(session_ops_service, "restore_session_files", _restore)
    response = await SessionAdapter().handle(
        _request(
            ReqMethod.SESSION_RESTORE_FILES,
            {"session_id": "param-session", "turn_index": "2"},
        )
    )

    assert calls == [{"session_id": "param-session", "turn_index": 2}]
    assert response.ok is True
    assert response.payload is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "session_id", "expected_error"),
    [
        ({"turn_index": 1}, None, "session_id is required"),
        ({}, "envelope-session", "turn_index is required"),
        ({"turn_index": "two"}, "envelope-session", "turn_index must be an integer"),
    ],
)
async def test_files_only_restore_rejects_invalid_parameters(
    params: dict[str, Any],
    session_id: str | None,
    expected_error: str,
) -> None:
    response = await SessionAdapter().handle(
        _request(ReqMethod.SESSION_RESTORE_FILES, params, session_id=session_id)
    )

    assert response.ok is False
    assert response.payload == {
        "error": expected_error,
        "code": "BAD_REQUEST",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (ValueError("turn outside history"), "BAD_REQUEST"),
        (RuntimeError("restore failed"), "INTERNAL_ERROR"),
    ],
)
async def test_files_only_restore_maps_service_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_code: str,
) -> None:
    def _restore(**_kwargs: Any) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(session_ops_service, "restore_session_files", _restore)
    response = await SessionAdapter().handle(
        _request(ReqMethod.SESSION_RESTORE_FILES, {"turn_index": 1})
    )

    assert response.ok is False
    assert response.payload == {
        "error": str(error),
        "code": expected_code,
    }


@pytest.mark.asyncio
async def test_conversation_rewind_maps_wire_request_to_runtime_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[tuple[Any, ...]] = []
    _install_wire_capture(monkeypatch, trace)

    async def _rewind(rewind_input):
        trace.append(("runtime", rewind_input))
        return SessionRewindResult(
            action=rewind_input.action,
            session_id=rewind_input.session_id,
            turn_index=rewind_input.turn_index,
            content="second prompt",
            content_preview="second prompt",
            remaining_records=2,
            removed_records=3,
            context_rebuilt=True,
        )

    server = agent_ws_server_module.AgentWebSocketServer.__new__(
        agent_ws_server_module.AgentWebSocketServer
    )
    _install_runtime(monkeypatch, server, _rewind)

    await server._handle_session_rewind_full(
        _FakeWebSocket(),
        _request(
            ReqMethod.SESSION_REWIND,
            {"session_id": "param-session", "turn_index": "2"},
        ),
        asyncio.Lock(),
    )

    assert [entry[0] for entry in trace] == ["runtime", "encode", "send"]
    runtime_input = trace[0][1]
    assert runtime_input.operation_id == "request-1"
    assert runtime_input.channel_id == "tui"
    assert runtime_input.session_id == "param-session"
    assert runtime_input.turn_index == 2
    assert runtime_input.action is SessionRewindAction.CONVERSATION
    assert (
        runtime_input.context_policy is SessionRewindContextPolicy.LIVE_ONLY
    )
    response = _sent_response(trace)
    assert response.ok is True
    assert response.payload == {
        "session_id": "param-session",
        "turn_index": 2,
        "content": "second prompt",
        "content_preview": "second prompt",
        "remaining_records": 2,
        "removed_records": 3,
        "rewind_context": True,
    }
    assert response.metadata == {"view_id": "view-1"}


@pytest.mark.asyncio
async def test_conversation_and_files_maps_to_one_runtime_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[tuple[Any, ...]] = []
    _install_wire_capture(monkeypatch, trace)

    async def _rewind(rewind_input):
        trace.append(("runtime", rewind_input))
        return SessionRewindResult(
            action=rewind_input.action,
            session_id=rewind_input.session_id,
            turn_index=rewind_input.turn_index,
            context_rebuilt=True,
            restored_files=("restored.txt",),
            deleted_files=("created.txt",),
            restore_errors=(
                SessionRewindFileError(file="locked.txt", error="locked"),
            ),
        )

    server = agent_ws_server_module.AgentWebSocketServer.__new__(
        agent_ws_server_module.AgentWebSocketServer
    )
    _install_runtime(monkeypatch, server, _rewind)

    await server._handle_session_rewind_full(
        _FakeWebSocket(),
        _request(
            ReqMethod.SESSION_REWIND_AND_RESTORE,
            {"session_id": "target", "turn_index": 3},
        ),
        asyncio.Lock(),
        restore_files=True,
    )

    assert [entry[0] for entry in trace] == ["runtime", "encode", "send"]
    runtime_input = trace[0][1]
    assert runtime_input.action is SessionRewindAction.CONVERSATION_AND_FILES
    response = _sent_response(trace)
    assert response.ok is True
    assert response.payload == {
        "session_id": "target",
        "turn_index": 3,
        "rewind_context": True,
        "restored_files": ["restored.txt"],
        "deleted_files": ["created.txt"],
        "restore_errors": [{"file": "locked.txt", "error": "locked"}],
    }


@pytest.mark.asyncio
async def test_server_preserves_runtime_partial_rewind_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[tuple[Any, ...]] = []
    _install_wire_capture(monkeypatch, trace)

    async def _rewind(rewind_input):
        trace.append(("runtime", rewind_input))
        return SessionRewindResult(
            action=rewind_input.action,
            session_id=rewind_input.session_id,
            turn_index=rewind_input.turn_index,
            context_rebuilt=False,
        )

    server = agent_ws_server_module.AgentWebSocketServer.__new__(
        agent_ws_server_module.AgentWebSocketServer
    )
    _install_runtime(monkeypatch, server, _rewind)

    await server._handle_session_rewind_full(
        _FakeWebSocket(),
        _request(ReqMethod.SESSION_REWIND, {"turn_index": 1}),
        asyncio.Lock(),
    )

    assert [entry[0] for entry in trace] == ["runtime", "encode", "send"]
    response = _sent_response(trace)
    assert response.ok is True
    assert response.payload == {
        "session_id": "envelope-session",
        "turn_index": 1,
        "rewind_context": False,
    }


@pytest.mark.asyncio
async def test_context_required_handler_sets_runtime_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[tuple[Any, ...]] = []
    _install_wire_capture(monkeypatch, trace)
    async def _rewind(rewind_input):
        trace.append(("runtime", rewind_input))
        return SessionRewindResult(
            action=rewind_input.action,
            session_id=rewind_input.session_id,
            turn_index=rewind_input.turn_index,
            context_rebuilt=True,
        )

    server = agent_ws_server_module.AgentWebSocketServer.__new__(
        agent_ws_server_module.AgentWebSocketServer
    )
    _install_runtime(monkeypatch, server, _rewind)

    await server._handle_session_rewind_context(
        _FakeWebSocket(),
        _request(ReqMethod.SESSION_REWIND, {"turn_index": 1}),
        asyncio.Lock(),
    )

    runtime_input = trace[0][1]
    assert runtime_input.require_context is True
    assert runtime_input.context_policy is SessionRewindContextPolicy.LIVE_ONLY
    response = _sent_response(trace)
    assert response.ok is True
    assert response.payload == {
        "session_id": "envelope-session",
        "turn_index": 1,
        "rewind_context": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "session_id", "expected_error"),
    [
        ({"turn_index": 1}, None, "session_id and turn_index required"),
        ({}, "envelope-session", "session_id and turn_index required"),
        ({"turn_index": "one"}, "envelope-session", "turn_index must be integer"),
    ],
)
async def test_conversation_rewind_rejects_invalid_parameters(
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
    session_id: str | None,
    expected_error: str,
) -> None:
    trace: list[tuple[Any, ...]] = []
    _install_wire_capture(monkeypatch, trace)
    server = agent_ws_server_module.AgentWebSocketServer.__new__(
        agent_ws_server_module.AgentWebSocketServer
    )

    await server._handle_session_rewind_full(
        _FakeWebSocket(),
        _request(ReqMethod.SESSION_REWIND, params, session_id=session_id),
        asyncio.Lock(),
    )

    response = _sent_response(trace)
    assert response.ok is False
    assert response.payload == {
        "error": expected_error,
        "code": "BAD_REQUEST",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_payload"),
    [
        (
            ValueError("turn_index 4 exceeds total turns (3)"),
            {
                "error": "turn_index 4 exceeds total turns (3)",
                "code": "BAD_REQUEST",
            },
        ),
        (RuntimeError("disk failed"), {"error": "disk failed"}),
    ],
)
async def test_conversation_rewind_maps_service_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_payload: dict[str, Any],
) -> None:
    trace: list[tuple[Any, ...]] = []
    _install_wire_capture(monkeypatch, trace)

    async def _fail(_rewind_input):
        raise error

    server = agent_ws_server_module.AgentWebSocketServer.__new__(
        agent_ws_server_module.AgentWebSocketServer
    )
    _install_runtime(monkeypatch, server, _fail)

    await server._handle_session_rewind_full(
        _FakeWebSocket(),
        _request(ReqMethod.SESSION_REWIND, {"turn_index": 1}),
        asyncio.Lock(),
    )

    response = _sent_response(trace)
    assert response.ok is False
    assert response.payload == expected_payload


def test_session_ops_list_turns_preserves_physical_user_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.utils import diff_service as diff_service_module

    history = [
        {
            "id": "user-1",
            "request_id": "request-1",
            "role": "user",
            "timestamp": 10,
            "content": (
                '<file-content path="hidden.txt">secret</file-content> first prompt'
            ),
        },
        {"role": "assistant", "content": "answer"},
        {
            "id": "injected-2",
            "request_id": "request-2",
            "role": "user",
            "timestamp": 20,
            "content": "<local-command-stdout>generated output",
        },
        {
            "id": "user-3",
            "request_id": "request-3",
            "role": "user",
            "timestamp": 30,
            "content": "third prompt",
        },
    ]

    class _DiffService:
        @staticmethod
        def get_turn_diffs(session_id: str, project_dir: str | None) -> list[dict[str, Any]]:
            assert (session_id, project_dir) == ("session-1", "D:/project")
            return [
                {
                    "turnIndex": 1,
                    "stats": {
                        "filesChanged": 1,
                        "linesAdded": 2,
                        "linesRemoved": 0,
                    },
                    "files": {
                        "a.py": {
                            "linesAdded": 2,
                            "linesRemoved": 0,
                            "isNewFile": True,
                        }
                    },
                },
                {
                    "turnIndex": 3,
                    "stats": {
                        "filesChanged": 1,
                        "linesAdded": 0,
                        "linesRemoved": 1,
                    },
                    "files": {},
                },
            ]

    monkeypatch.setattr(session_ops_service, "history_exists", lambda _sid: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _sid: history,
    )
    monkeypatch.setattr(
        diff_service_module,
        "get_diff_service",
        lambda: _DiffService(),
    )

    result = session_ops_service.list_session_turns(
        session_id="session-1",
        project_dir="D:/project",
    )

    assert result == {
        "turns": [
            {
                "turn_index": 1,
                "content_preview": "first prompt",
                "timestamp": 10,
                "id": "user-1",
                "request_id": "request-1",
                "stats": {
                    "filesChanged": 1,
                    "linesAdded": 2,
                    "linesRemoved": 0,
                },
                "files": [
                    {
                        "path": "a.py",
                        "linesAdded": 2,
                        "linesRemoved": 0,
                        "isNewFile": True,
                    }
                ],
            },
            {
                "turn_index": 3,
                "content_preview": "third prompt",
                "timestamp": 30,
                "id": "user-3",
                "request_id": "request-3",
                "stats": {
                    "filesChanged": 1,
                    "linesAdded": 0,
                    "linesRemoved": 1,
                },
                "files": [],
            },
        ],
        "total": 3,
    }


def test_session_ops_rewind_orders_persistence_and_soft_file_log_truncation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from jiuwenswarm.server.runtime.session import session_history, session_metadata
    from jiuwenswarm.server.utils import diff_service as diff_service_module

    trace: list[tuple[Any, ...]] = []
    history_path = tmp_path / "history.jsonl"
    history_path.touch()
    history = [
        {"role": "user", "content": "first", "timestamp": 10},
        {"role": "assistant", "content": "answer", "timestamp": 11},
        {
            "role": "user",
            "content": '<file-content path="a.py">hidden</file-content> second prompt',
            "timestamp": 20,
        },
    ]

    class _DiffService:
        @staticmethod
        def resolve_project_dir(session_id: str) -> str:
            trace.append(("resolve_project", session_id))
            return "D:/project"

        @staticmethod
        def truncate_file_ops_by_timestamp(
            session_id: str,
            timestamp: int,
            *,
            project_dir: str,
            soft: bool,
        ) -> None:
            trace.append(
                ("truncate_files", session_id, timestamp, project_dir, soft)
            )

    def _load(session_id: str) -> list[dict[str, Any]]:
        trace.append(("load", session_id))
        return history

    def _truncate(*, session_id: str, cut_index: int) -> dict[str, int]:
        trace.append(("truncate_history", session_id, cut_index))
        return {"remaining_records": 2, "removed_records": 1}

    def _update(**kwargs: Any) -> None:
        trace.append(("update_metadata", kwargs))

    monkeypatch.setattr(
        session_ops_service,
        "get_read_history_path",
        lambda _sid: history_path,
    )
    monkeypatch.setattr(session_ops_service, "load_history_records", _load)
    monkeypatch.setattr(session_history, "truncate_history_records", _truncate)
    monkeypatch.setattr(session_metadata, "update_session_metadata", _update)
    monkeypatch.setattr(
        diff_service_module,
        "get_diff_service",
        lambda: _DiffService(),
    )

    result = session_ops_service.rewind_session(
        session_id="session-1",
        turn_index=2,
    )

    assert [entry[0] for entry in trace] == [
        "load",
        "resolve_project",
        "truncate_history",
        "update_metadata",
        "truncate_files",
    ]
    assert trace[2] == ("truncate_history", "session-1", 2)
    assert trace[3] == (
        "update_metadata",
        {"session_id": "session-1", "set_message_count": 2},
    )
    assert trace[4] == (
        "truncate_files",
        "session-1",
        20,
        "D:/project",
        True,
    )
    assert result == {
        "session_id": "session-1",
        "turn_index": 2,
        "content": "second prompt",
        "content_preview": "second prompt",
        "remaining_records": 2,
        "removed_records": 1,
    }


def test_session_ops_restore_files_applies_write_delete_and_reports_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from jiuwenswarm.server.utils import diff_service as diff_service_module

    restored_path = tmp_path / "restored.txt"
    restored_path.write_text("new", encoding="utf-8")
    deleted_path = tmp_path / "created.txt"
    deleted_path.write_text("created", encoding="utf-8")
    failing_path = tmp_path / "directory"
    failing_path.mkdir()

    class _DiffService:
        @staticmethod
        def get_files_to_restore(
            session_id: str,
            turn_index: int,
            *,
            project_dir: str | None,
            extra_history_roots: list[str] | None,
        ) -> dict[str, dict[str, str]]:
            assert (session_id, turn_index) == ("session-1", 2)
            assert project_dir == "D:/project"
            assert extra_history_roots == ["D:/history"]
            return {
                str(restored_path): {
                    "action": "write",
                    "restore_content": "old",
                },
                str(deleted_path): {
                    "action": "delete",
                    "restore_content": "",
                },
                str(failing_path): {
                    "action": "write",
                    "restore_content": "cannot write a directory",
                },
            }

    monkeypatch.setattr(
        diff_service_module,
        "get_diff_service",
        lambda: _DiffService(),
    )

    result = session_ops_service.restore_session_files(
        session_id="session-1",
        turn_index=2,
        project_dir="D:/project",
        extra_history_roots=["D:/history"],
    )

    assert restored_path.read_text(encoding="utf-8") == "old"
    assert not deleted_path.exists()
    assert result["session_id"] == "session-1"
    assert result["turn_index"] == 2
    assert result["restored_files"] == [str(restored_path)]
    assert result["deleted_files"] == [str(deleted_path)]
    assert len(result["errors"]) == 1
    assert result["errors"][0]["file"] == str(failing_path)
    assert result["errors"][0]["error"]
