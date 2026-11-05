# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The history.get stream must still emit its done frame when an appended snapshot fails.

Regression context: inside an AgentOS sandbox CWD=/ with a read-only root,
openjiuwen's default relative log path cannot be created; the page_idx=1 todo
snapshot lazily imports openjiuwen and raises. If the exception escaped
``_dispatch_history_stream``, neither ``todo.updated`` nor ``status: done``
would be sent, leaving the frontend stuck on "loading history..." with retries.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.front import router as router_module
from jiuwenswarm.server.front.router import MethodRouter
from jiuwenswarm.server.lifecycle import Readiness


class _FakeAdmission:
    async def dispatch(self, ws, request, send_lock) -> None:
        raise AssertionError("history.get is a control method; admission must not run")


def _records() -> list[dict[str, Any]]:
    return [
        {"id": "m1", "role": "user", "content": "hi"},
        {"id": "m2", "role": "assistant", "event_type": "chat.final", "content": "hello"},
    ]


def _install(monkeypatch: pytest.MonkeyPatch, *, todo, usage) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []

    async def _send_wire_payload(ws, payload) -> bool:
        _ = ws
        frames.append(payload)
        return True

    def _encode_chunk(chunk, *, response_id: str, sequence: int) -> dict[str, Any]:
        _ = response_id
        return {
            "sequence": sequence,
            "payload": chunk.payload,
            "is_complete": chunk.is_complete,
        }

    monkeypatch.setattr(router_module, "send_wire_payload", _send_wire_payload)
    monkeypatch.setattr(router_module, "encode_chunk", _encode_chunk)
    # A non-None value makes _load_history_service() return early, skipping the real service load.
    monkeypatch.setattr(router_module, "_HANDLE_HISTORY", lambda request: None)
    monkeypatch.setattr(
        router_module,
        "_LOAD_HISTORY_QUERY",
        lambda params: {
            "messages": _records(),
            "total_pages": 1,
            "page_idx": 1,
        },
    )
    monkeypatch.setattr(
        router_module,
        "_STREAM_HISTORY_RECORDS",
        lambda messages, *, use_split: list(messages),
    )
    monkeypatch.setattr(router_module, "_LOAD_HISTORY_TODO_SNAPSHOT", todo)
    monkeypatch.setattr(router_module, "_LOAD_HISTORY_SESSION_USAGE", usage)
    return frames


def _request() -> AgentRequest:
    return AgentRequest(
        request_id="req-history",
        channel_id="web",
        session_id="sess-1",
        req_method=ReqMethod.HISTORY_GET,
        params={"session_id": "sess-1", "page_idx": 1},
        is_stream=True,
    )


async def _run_stream(monkeypatch: pytest.MonkeyPatch, *, todo, usage) -> list[dict[str, Any]]:
    frames = _install(monkeypatch, todo=todo, usage=usage)
    router = MethodRouter(Readiness(), _FakeAdmission())  # type: ignore[arg-type]
    await router._dispatch_history_stream(object(), _request(), asyncio.Lock())
    return frames


def _payloads(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [frame["payload"] for frame in frames]


@pytest.mark.asyncio
async def test_todo_snapshot_failure_still_sends_done(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(session_id: str):
        _ = session_id
        raise RuntimeError("openjiuwen log path init failed")

    frames = await _run_stream(monkeypatch, todo=_boom, usage=lambda _sid: None)
    payloads = _payloads(frames)

    assert [p["message"]["id"] for p in payloads if p.get("message")] == ["m1", "m2"]
    assert not any(p.get("event_type") == "todo.updated" for p in payloads)
    assert payloads[-1]["status"] == "done"
    assert payloads[-1]["session_id"] == "sess-1"
    assert frames[-1]["is_complete"] is True


@pytest.mark.asyncio
async def test_session_usage_failure_still_sends_done(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(session_id: str):
        _ = session_id
        raise RuntimeError("usage store unavailable")

    frames = await _run_stream(monkeypatch, todo=lambda _sid: [], usage=_boom)
    payloads = _payloads(frames)

    assert payloads[-1]["status"] == "done"
    assert "session_usage" not in payloads[-1]
    assert frames[-1]["is_complete"] is True


@pytest.mark.asyncio
async def test_initial_batch_keeps_todo_snapshot_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    frames = await _run_stream(
        monkeypatch,
        todo=lambda _sid: [{"id": "t1"}],
        usage=lambda _sid: {"revision": 1},
    )
    payloads = _payloads(frames)

    todo_frames = [p for p in payloads if p.get("event_type") == "todo.updated"]
    assert len(todo_frames) == 1
    assert todo_frames[0]["todos"] == [{"id": "t1"}]
    assert payloads[-1]["status"] == "done"
    assert payloads[-1]["session_usage"] == {"revision": 1}
    assert frames[-1]["is_complete"] is True
