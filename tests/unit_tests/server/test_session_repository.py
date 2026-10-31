# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.control.repositories.session_repository import (
    handle_session_request,
)


def _request(
    method: ReqMethod,
    params: dict | None = None,
    *,
    channel_id: str = "web",
    user_id: str = "",
    session_id: str = "",
) -> AgentRequest:
    return AgentRequest(
        request_id="req-1",
        channel_id=channel_id,
        req_method=method,
        params=params or {},
        user_id=user_id,
        session_id=session_id,
    )


@pytest.mark.asyncio
async def test_session_list_projects_web_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_all_sessions_metadata",
        lambda limit=20, offset=0, user_id="": (
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
async def test_session_list_scopes_by_caller_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """session.list must only return the caller's own user_id when identified."""
    seen_user_id = {}

    def fake_get_all_sessions_metadata(limit=20, offset=0, user_id=""):
        seen_user_id["value"] = user_id
        sessions = [
            {"session_id": "sess-alice", "mode": "agent", "title": "alice's", "user_id": "alice"},
        ]
        return sessions, len(sessions)

    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_all_sessions_metadata",
        fake_get_all_sessions_metadata,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.to_session_info",
        lambda session: {"session_id": session["session_id"], "mode": session["mode"]},
    )
    response = await handle_session_request(
        _request(ReqMethod.SESSION_LIST, {"limit": 5}, user_id="alice")
    )
    assert response.ok is True
    assert seen_user_id["value"] == "alice"


@pytest.mark.asyncio
async def test_session_get_metadata_denies_other_users_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller identified as one user must not read another user's session by ID."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.lifecycle.guard", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_session_metadata",
        lambda session_id, cache_bust=True: {
            "session_id": "sess-bob",
            "user_id": "bob",
            "title": "bob's private session",
        },
    )
    response = await handle_session_request(
        _request(
            ReqMethod.SESSION_GET_METADATA,
            {"session_id": "sess-bob"},
            user_id="alice",
        )
    )
    assert response.ok is False
    assert response.payload["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_session_get_metadata_allows_owning_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owning user must still be able to read their own session metadata."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.lifecycle.guard", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_session_metadata",
        lambda session_id, cache_bust=True: {
            "session_id": "sess-alice",
            "user_id": "alice",
            "title": "alice's own session",
        },
    )
    response = await handle_session_request(
        _request(
            ReqMethod.SESSION_GET_METADATA,
            {"session_id": "sess-alice"},
            user_id="alice",
        )
    )
    assert response.ok is True
    assert response.payload["session_id"] == "sess-alice"


@pytest.mark.asyncio
async def test_session_get_metadata_unidentified_caller_keeps_legacy_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller with no user_id (desktop/TUI) is unaffected by the new ownership check."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.lifecycle.guard", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.session_repository.get_session_metadata",
        lambda session_id, cache_bust=True: {
            "session_id": "sess-bob",
            "user_id": "bob",
            "title": "bob's private session",
        },
    )
    response = await handle_session_request(
        _request(
            ReqMethod.SESSION_GET_METADATA,
            {"session_id": "sess-bob"},
            user_id="",
        )
    )
    assert response.ok is True
    assert response.payload["session_id"] == "sess-bob"
