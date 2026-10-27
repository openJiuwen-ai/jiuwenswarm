# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Skill plaza RPCs must not be blocked by a deleted chat session (#5886)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


@pytest.mark.parametrize(
    "method_value",
    [
        "skills.swarmskillshub.recommend",
        "skills.swarmskillshub.detail",
        "skills.online_search.search",
        "skills.list",
        "plugins.list",
    ],
)
def test_skill_plaza_methods_are_stateless(method_value: str) -> None:
    """Plaza / catalog reads are prefix-matched as stateless and skip lifecycle.guard."""
    request = AgentRequest(
        request_id="r1",
        channel_id="web",
        req_method=SimpleNamespace(value=method_value),  # type: ignore[arg-type]
        session_id="web_deleted_session",
        params={"session_id": "web_deleted_session"},
    )
    assert AgentWebSocketServer._is_stateless_method_request(request) is True


def test_chat_send_still_requires_lifecycle_guard() -> None:
    request = AgentRequest(
        request_id="r2",
        channel_id="web",
        req_method=ReqMethod.CHAT_SEND,
        session_id="web_deleted_session",
    )
    assert AgentWebSocketServer._is_stateless_method_request(request) is False
