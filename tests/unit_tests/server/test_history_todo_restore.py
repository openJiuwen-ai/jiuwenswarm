# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.context import build_ws_request_context
from jiuwenswarm.server.handlers.session import handle_history_get_stream


class _FakeWs:
    pass


@pytest.mark.asyncio
async def test_history_get_stream_emits_todo_updated_on_page_one(tmp_path, monkeypatch):
    session_id = "web_hist_todo_1"
    todo_dir = tmp_path / "todo" / session_id
    todo_dir.mkdir(parents=True)
    (todo_dir / "todo.json").write_text(
        json.dumps(
            [
                {
                    "id": "t1",
                    "content": "restore me",
                    "activeForm": "restoring",
                    "status": "in_progress",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.todo_snapshot.get_deepagent_todo_dir",
        lambda: tmp_path / "todo",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.handlers.session._todo_snapshot_session_fields",
        lambda _session_id: {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.handlers.session.get_conversation_history",
            lambda session_id, page_idx, **_kwargs: {
                "messages": [{"role": "user", "content": "hi"}],
                "total_pages": 1,
                "page_idx": page_idx,
            },
    )

    sent_wires: list[dict] = []

    async def _capture_send(_ws, wire):
        sent_wires.append(wire)
        return True

    monkeypatch.setattr(
        "jiuwenswarm.server.transports.ws_sink.send_wire_payload",
        _capture_send,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.transports.ws_sink.encode_agent_chunk_for_wire",
        lambda chunk, response_id, sequence: {
            "response_id": response_id,
            "sequence": sequence,
            "payload": chunk.payload,
            "is_complete": chunk.is_complete,
        },
    )

    request = AgentRequest(
        request_id="req-hist-1",
        channel_id="web",
        req_method="history.get",
        params={"session_id": session_id, "page_idx": 1},
        is_stream=True,
    )
    await handle_history_get_stream(
        build_ws_request_context(SimpleNamespace(), _FakeWs(), request, asyncio.Lock())
    )

    event_types = [
        w["payload"].get("event_type")
        for w in sent_wires
        if isinstance(w.get("payload"), dict)
    ]
    assert "todo.updated" in event_types
    assert event_types[-1] == "history.message"
    todo_wire = next(w for w in sent_wires if w["payload"].get("event_type") == "todo.updated")
    assert todo_wire["is_complete"] is False
    assert todo_wire["payload"]["session_id"] == session_id
    assert todo_wire["payload"]["todos"] == [
        {
            "id": "t1",
            "content": "restore me",
            "activeForm": "restoring",
            "status": "in_progress",
        }
    ]
    # done frame must come after todo snapshot
    todo_idx = event_types.index("todo.updated")
    done_idx = next(
        i
        for i, w in enumerate(sent_wires)
        if w["payload"].get("event_type") == "history.message"
        and w["payload"].get("status") == "done"
    )
    assert todo_idx < done_idx


@pytest.mark.asyncio
async def test_history_get_stream_skips_todo_on_later_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.server.handlers.session.get_conversation_history",
            lambda session_id, page_idx, **_kwargs: {
                "messages": [],
                "total_pages": 2,
                "page_idx": page_idx,
            },
    )
    sent_wires: list[dict] = []

    async def _capture_send(_ws, wire):
        sent_wires.append(wire)
        return True

    monkeypatch.setattr(
        "jiuwenswarm.server.transports.ws_sink.send_wire_payload",
        _capture_send,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.transports.ws_sink.encode_agent_chunk_for_wire",
        lambda chunk, response_id, sequence: {
            "response_id": response_id,
            "sequence": sequence,
            "payload": chunk.payload,
            "is_complete": chunk.is_complete,
        },
    )
    load_mock = SimpleNamespace(called=False)

    def _should_not_load(_session_id: str, **_kwargs):
        load_mock.called = True
        return [{"id": "x"}]

    monkeypatch.setattr(
        "jiuwenswarm.server.handlers.session.load_todo_snapshot_for_frontend",
        _should_not_load,
    )

    request = AgentRequest(
        request_id="req-hist-2",
        channel_id="web",
        req_method="history.get",
        params={"session_id": "sess-page-2", "page_idx": 2},
        is_stream=True,
    )
    await handle_history_get_stream(
        build_ws_request_context(SimpleNamespace(), _FakeWs(), request, asyncio.Lock())
    )

    assert load_mock.called is False
    assert all(
        w["payload"].get("event_type") != "todo.updated"
        for w in sent_wires
        if isinstance(w.get("payload"), dict)
    )


@pytest.mark.asyncio
async def test_history_get_stream_reads_code_project_todo(tmp_path, monkeypatch):
    session_id = "web_code_hist_todo"
    project = tmp_path / "agent-core"
    todo_dir = project / "todo" / session_id
    todo_dir.mkdir(parents=True)
    (todo_dir / "todo.json").write_text(
        json.dumps(
            [
                {
                    "id": "t1",
                    "content": "from project",
                    "activeForm": "restoring",
                    "status": "in_progress",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.todo_snapshot.get_deepagent_todo_dir",
        lambda: tmp_path / "unused-default-todo",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.handlers.session._todo_snapshot_session_fields",
        lambda _session_id: {
            "project_dir": str(project),
            "work_mode": "code",
            "mode": "agent.code.normal",
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.handlers.session.get_conversation_history",
            lambda session_id, page_idx, **_kwargs: {
                "messages": [],
                "total_pages": 1,
                "page_idx": page_idx,
            },
    )

    sent_wires: list[dict] = []

    async def _capture_send(_ws, wire):
        sent_wires.append(wire)
        return True

    monkeypatch.setattr(
        "jiuwenswarm.server.transports.ws_sink.send_wire_payload",
        _capture_send,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.transports.ws_sink.encode_agent_chunk_for_wire",
        lambda chunk, response_id, sequence: {
            "response_id": response_id,
            "sequence": sequence,
            "payload": chunk.payload,
            "is_complete": chunk.is_complete,
        },
    )

    request = AgentRequest(
        request_id="req-hist-code-proj",
        channel_id="web",
        req_method="history.get",
        params={"session_id": session_id, "page_idx": 1},
        is_stream=True,
    )
    await handle_history_get_stream(
        build_ws_request_context(SimpleNamespace(), _FakeWs(), request, asyncio.Lock())
    )

    todo_wire = next(w for w in sent_wires if w["payload"].get("event_type") == "todo.updated")
    assert todo_wire["payload"]["todos"] == [
        {
            "id": "t1",
            "content": "from project",
            "activeForm": "restoring",
            "status": "in_progress",
        }
    ]
