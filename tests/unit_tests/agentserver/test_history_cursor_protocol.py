# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import json

import pytest

from jiuwenswarm.common.e2a.wire_codec import (
    parse_agent_server_wire_chunk,
    parse_agent_server_wire_unary,
)
from jiuwenswarm.common.schema.agent import AgentRequest, ReqMethod
from jiuwenswarm.server import agent_ws_server as module
from jiuwenswarm.server.runtime.session import session_history


class RecordingTransport:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(module, "load_todo_snapshot_for_frontend", lambda sid: [])
    return module.AgentWebSocketServer.__new__(module.AgentWebSocketServer)


def history_request(*, stream: bool, cursor=None, limit=50):
    params = {"session_id": "cursor-session", "cursor": cursor}
    if limit is not None:
        params["limit"] = limit
    return AgentRequest(
        request_id="cursor-request",
        channel_id="desktop",
        req_method=ReqMethod.HISTORY_GET,
        params=params,
        is_stream=stream,
    )


def parse_payload(frame, stream):
    parse = parse_agent_server_wire_chunk if stream else parse_agent_server_wire_unary
    return parse(frame).payload


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_cursor_history_returns_batch_metadata(server, monkeypatch, stream):
    batch = {
        "messages": [
            {
                "id": "answer",
                "role": "assistant",
                "event_type": "chat.final",
                "content": "answer",
            }
        ],
        "next_cursor": "next-token",
        "has_more": True,
        "snapshot_id": "snapshot",
        "snapshot_end": 1024,
        "scanned_bytes": 128,
    }

    def read_page(session_id, *, cursor, limit, is_restorable):
        assert session_id == "cursor-session"
        assert cursor is None
        assert limit == 50
        assert is_restorable(batch["messages"][0]) is True
        return batch

    monkeypatch.setattr(module, "read_history_cursor_page", read_page)
    ws = RecordingTransport()
    handler = (
        server._handle_history_get_stream if stream else server._handle_history_get
    )
    await handler(ws, history_request(stream=stream), asyncio.Lock())
    payloads = [parse_payload(frame, stream) for frame in ws.sent]

    if stream:
        assert payloads[0]["event_type"] == "history.message"
        assert payloads[0]["message"]["content"] == "answer"
        assert payloads[0]["next_cursor"] == "next-token"
        assert payloads[0]["snapshot_end"] == 1024
        assert payloads[-1]["status"] == "done"
        assert payloads[-1]["has_more"] is True
    else:
        result = payloads[0]
        assert result["messages"][0]["content"] == "answer"
        assert result["next_cursor"] == "next-token"
        assert result["has_more"] is True
        assert result["snapshot_id"] == "snapshot"
        assert result["snapshot_end"] == 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_cursor_history_rejects_invalid_limit(server, stream):
    ws = RecordingTransport()
    handler = (
        server._handle_history_get_stream if stream else server._handle_history_get
    )
    await handler(ws, history_request(stream=stream, limit=0), asyncio.Lock())
    payload = parse_payload(ws.sent[0], stream)
    assert payload["code"] == "INVALID_HISTORY_CURSOR"
    assert "between 1 and 50" in payload["error"]
    if stream:
        assert payload["event_type"] == "chat.error"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_cursor_history_reports_snapshot_change(server, monkeypatch, stream):
    def fail_snapshot(*args, **kwargs):
        raise module.HistorySnapshotChanged("history snapshot changed during restore")

    monkeypatch.setattr(module, "read_history_cursor_page", fail_snapshot)
    ws = RecordingTransport()
    handler = (
        server._handle_history_get_stream if stream else server._handle_history_get
    )
    await handler(ws, history_request(stream=stream), asyncio.Lock())
    payload = parse_payload(ws.sent[0], stream)
    assert payload["code"] == "HISTORY_SNAPSHOT_CHANGED"
    assert "snapshot changed" in payload["error"]
    if stream:
        assert payload["event_type"] == "chat.error"


def test_cursor_history_empty_session_returns_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    result = module.AgentWebSocketServer.get_conversation_history_cursor(
        "missing_session",
        None,
        limit=50,
    )
    assert result == {
        "messages": [],
        "next_cursor": None,
        "has_more": False,
        "snapshot_id": None,
        "snapshot_end": 0,
        "scanned_bytes": 0,
    }
