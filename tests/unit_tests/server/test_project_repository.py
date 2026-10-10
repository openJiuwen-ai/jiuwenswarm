# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.control.repositories.project_repository import (
    handle_project_request,
)


def _request(method: ReqMethod, params: dict | None = None) -> AgentRequest:
    return AgentRequest(
        request_id="req-1",
        channel_id="web",
        req_method=method,
        params=params or {},
    )


@pytest.mark.asyncio
async def test_project_list_uses_disk_queries_not_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.control.repositories.project_repository.load_project_list",
        lambda params: ({"projects": [{"project_id": "p1"}]}, None, None),
    )
    response = await handle_project_request(_request(ReqMethod.PROJECT_LIST))
    assert response.ok is True
    assert response.payload["projects"][0]["project_id"] == "p1"


@pytest.mark.asyncio
async def test_project_git_status_is_rejected_by_repository() -> None:
    response = await handle_project_request(_request(ReqMethod.PROJECT_GIT_STATUS))
    assert response.ok is False
    assert response.payload["code"] == "BAD_REQUEST"
