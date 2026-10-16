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


def _request_with_user(
    method: ReqMethod,
    params: dict | None = None,
    *,
    user_id: str = "",
    channel_id: str = "web",
) -> AgentRequest:
    return AgentRequest(
        request_id="req-1",
        channel_id=channel_id,
        req_method=method,
        params=params or {},
        user_id=user_id,
    )


@pytest.mark.asyncio
async def test_session_list_projects_web_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_all_sessions_metadata",
        lambda limit=20, offset=0, user_id=None: (
            [{"session_id": "sess-1", "mode": "agent", "title": "one", "user_id": ""}],
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
async def test_session_list_passes_user_id_for_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def _fake_get_all(limit=20, offset=0, user_id=None):
        captured["user_id"] = user_id
        return ([], 0)

    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_all_sessions_metadata",
        _fake_get_all,
    )
    request = AgentRequest(
        request_id="req-1",
        channel_id="web",
        req_method=ReqMethod.SESSION_LIST,
        params={},
        user_id="alice",
    )
    await handle_session_request(request)
    assert captured["user_id"] == "alice"


@pytest.mark.asyncio
async def test_session_create_is_rejected_by_repository() -> None:
    response = await handle_session_request(_request(ReqMethod.SESSION_CREATE))
    assert response.ok is False
    assert response.payload["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_get_metadata_rejects_other_users_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_session_metadata",
        lambda sid, cache_bust=True: {"session_id": sid, "user_id": "alice"},
    )
    request = _request_with_user(
        ReqMethod.SESSION_GET_METADATA, {"session_id": "sess-1"}, user_id="bob"
    )
    response = await handle_session_request(request)
    assert response.ok is False
    assert response.payload["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_get_metadata_allows_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_session_metadata",
        lambda sid, cache_bust=True: {"session_id": sid, "user_id": "alice"},
    )
    request = _request_with_user(
        ReqMethod.SESSION_GET_METADATA, {"session_id": "sess-1"}, user_id="alice"
    )
    response = await handle_session_request(request)
    assert response.ok is True
    assert response.payload["user_id"] == "alice"


@pytest.mark.asyncio
async def test_pin_rejects_other_users_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_session_metadata",
        lambda sid, cache_bust=True: {"session_id": sid, "user_id": "alice"},
    )
    request = _request_with_user(
        ReqMethod.SESSION_PIN, {"session_id": "sess-1", "pinned": True}, user_id="bob"
    )
    response = await handle_session_request(request)
    assert response.ok is False
    assert response.payload["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_color_set_rejects_other_users_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository._read_metadata",
        lambda sid: {"session_id": sid, "user_id": "alice"},
    )
    request = _request_with_user(
        ReqMethod.SESSION_COLOR_SET, {"session_id": "sess-1", "color": "blue"}, user_id="bob"
    )
    response = await handle_session_request(request)
    assert response.ok is False
    assert response.payload["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_color_set_query_other_users_session_hides_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_session_metadata",
        lambda sid, cache_bust=True: {
            "session_id": sid, "user_id": "alice", "accent_color": "pink",
        },
    )
    request = _request_with_user(
        ReqMethod.SESSION_COLOR_SET, {"session_id": "sess-1"}, user_id="bob"
    )
    response = await handle_session_request(request)
    assert response.ok is True
    assert response.payload["accent_color"] == "default"


@pytest.mark.asyncio
async def test_preview_rejects_other_users_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_session_metadata",
        lambda sid, cache_bust=True: {"session_id": sid, "user_id": "alice"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.load_history_records",
        lambda sid: [{"role": "user", "content": "secret"}],
    )
    request = _request_with_user(
        ReqMethod.SESSION_PREVIEW, {"session_id": "sess-1"}, user_id="bob"
    )
    response = await handle_session_request(request)
    assert response.ok is True
    assert response.payload["preview_messages"] == []


@pytest.mark.asyncio
async def test_preview_allows_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_session_metadata",
        lambda sid, cache_bust=True: {"session_id": sid, "user_id": "alice"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.load_history_records",
        lambda sid: [{"role": "user", "content": "hello"}],
    )
    request = _request_with_user(
        ReqMethod.SESSION_PREVIEW, {"session_id": "sess-1"}, user_id="alice"
    )
    response = await handle_session_request(request)
    assert response.ok is True
    assert response.payload["preview_messages"][0]["content"] == "hello"
