# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.control.repositories.session_repository import (
    handle_session_request,
)


def _request(method: ReqMethod, params: dict | None = None, *, channel_id: str = "web") -> AgentRequest:
    return AgentRequest(
        request_id="req-1",
        channel_id=channel_id,
        req_method=method,
        params=params or {},
    )


@pytest.fixture(autouse=True)
def isolated_lifecycle(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.session import lifecycle

    sessions = tmp_path / "agent" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: sessions)
    assert lifecycle.get_agent_root_dir() == sessions.parent
    assert lifecycle.resource_path("session", "sess-1").is_relative_to(tmp_path)


@pytest.mark.asyncio
async def test_session_list_projects_web_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_all_sessions_metadata",
        lambda limit=20, offset=0: (
            [{"session_id": "sess-1", "mode": "agent", "title": "one"}],
            1,
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.to_session_info",
        lambda session: {"session_id": session["session_id"], "mode": session["mode"]},
    )
    response = await handle_session_request(_request(ReqMethod.SESSION_LIST, {"limit": 5}))
    assert response.ok is True
    assert response.payload["total"] == 1
    assert response.payload["sessions"][0]["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_session_create_is_rejected_by_repository() -> None:
    response = await handle_session_request(_request(ReqMethod.SESSION_CREATE))
    assert response.ok is False
    assert response.payload["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
@pytest.mark.parametrize("params, writeback", [
    ({"session_id": "sess-1"}, True),
    ({"session_id": "sess-1", "enable_writeback": False}, False),
    ({"session_id": "sess-1", "enable_writeback": True}, True),
])
async def test_get_metadata_preserves_read_options_and_payload(monkeypatch, params, writeback):
    calls = []
    payload = {"session_id": "sess-1", "status": "processing", "message_count": 3}

    def read(session_id, *, cache_bust, enable_writeback):
        calls.append((session_id, cache_bust, enable_writeback))
        return payload

    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_session_metadata", read,
    )
    response = await handle_session_request(_request(ReqMethod.SESSION_GET_METADATA, params))
    assert response.ok and response.payload == payload
    assert calls == [("sess-1", True, writeback)]
    assert response.request_id == "req-1" and response.channel_id == "web"


@pytest.mark.asyncio
@pytest.mark.parametrize("result, code, message", [
    ({}, "NOT_FOUND", "session not found"),
    (OSError("disk unavailable"), "INTERNAL_ERROR", "disk unavailable"),
])
async def test_get_metadata_preserves_read_errors(monkeypatch, result, code, message):
    def read(*_args, **_kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_session_metadata", read,
    )
    response = await handle_session_request(
        _request(ReqMethod.SESSION_GET_METADATA, {"session_id": "sess-1"}),
    )
    assert not response.ok
    assert response.payload["code"] == code and response.payload["error"] == message


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, " ", 1])
async def test_get_metadata_rejects_invalid_session_without_reading(monkeypatch, session_id):
    def unexpected_read(*_args, **_kwargs):
        pytest.fail("Invalid session metadata query must not access the store")

    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_session_metadata", unexpected_read,
    )
    response = await handle_session_request(
        _request(ReqMethod.SESSION_GET_METADATA, {"session_id": session_id}),
    )
    assert not response.ok and response.payload["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_get_metadata_reports_non_boolean_writeback(monkeypatch, caplog):
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_session_metadata",
        lambda *_args, **_kwargs: {"session_id": "sess-1"},
    )
    await handle_session_request(_request(
        ReqMethod.SESSION_GET_METADATA, {"session_id": "sess-1", "enable_writeback": "invalid"},
    ))
    assert "enable_writeback must be boolean" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata, expected", [(None, "default"), ({}, "default"), ({"accent_color": "blue"}, "blue")])
async def test_color_query_preserves_defaults_and_payload(monkeypatch, metadata, expected):
    calls = []

    def read(session_id, *, cache_bust):
        calls.append((session_id, cache_bust))
        return metadata

    monkeypatch.setattr("jiuwenswarm.server.control.repositories.session_repository.get_session_metadata", read)
    response = await handle_session_request(_request(ReqMethod.SESSION_COLOR_SET, {"session_id": "sess-1"}))
    assert response.ok and response.payload == {"session_id": "sess-1", "accent_color": expected}
    assert calls == [("sess-1", True)]


@pytest.mark.asyncio
async def test_color_query_preserves_storage_errors(monkeypatch):
    def read(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr("jiuwenswarm.server.control.repositories.session_repository.get_session_metadata", read)
    response = await handle_session_request(_request(ReqMethod.SESSION_COLOR_SET, {"session_id": "sess-1"}))
    assert not response.ok
    assert response.payload == {"code": "INTERNAL_ERROR", "error": "disk unavailable"}


@pytest.mark.asyncio
async def test_preview_preserves_filtering_and_requested_count(monkeypatch):
    raw = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "partial", "event_type": "chat.delta"},
        {"role": "assistant", "content": "second", "event_type": "chat.final"},
        {"role": "user", "content": "third"},
    ]
    monkeypatch.setattr("jiuwenswarm.server.control.repositories.session_repository.load_history_records", lambda sid: raw)
    response = await handle_session_request(_request(ReqMethod.SESSION_PREVIEW, {"session_id": "sess-1", "count": 2}))
    assert response.ok and response.payload == {"session_id": "sess-1", "preview_messages": [
        {"role": "assistant", "content": "second", "event_type": "chat.final"},
        {"role": "user", "content": "third", "event_type": ""},
    ]}


@pytest.mark.asyncio
async def test_preview_storage_failure_preserves_empty_success(monkeypatch):
    def read(_sid):
        raise OSError("history unavailable")

    monkeypatch.setattr("jiuwenswarm.server.control.repositories.session_repository.load_history_records", read)
    response = await handle_session_request(_request(ReqMethod.SESSION_PREVIEW, {"session_id": "sess-1"}))
    assert response.ok and response.payload == {"session_id": "sess-1", "preview_messages": []}
