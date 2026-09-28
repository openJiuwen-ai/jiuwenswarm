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
