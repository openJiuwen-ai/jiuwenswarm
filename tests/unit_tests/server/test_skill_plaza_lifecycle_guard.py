# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Skill plaza RPCs must not be blocked by a deleted chat session (#5886)."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


@pytest.mark.parametrize(
    "method",
    [
        ReqMethod.SKILLS_SWARMSKILLS_HUB_RECOMMEND,
        ReqMethod.SKILLS_SWARMSKILLS_HUB_DETAIL,
        ReqMethod.SKILLS_ONLINE_SEARCH,
        ReqMethod.SKILLS_LIST,
        ReqMethod.PLUGINS_LIST,
    ],
)
def test_skill_plaza_reads_skip_session_lifecycle_guard(method: ReqMethod) -> None:
    """Plaza / catalog reads bypass lifecycle.guard when the chat session is gone."""
    request = AgentRequest(
        request_id="r1",
        channel_id="web",
        req_method=method,
        session_id="web_deleted_session",
        params={"session_id": "web_deleted_session"},
    )
    assert AgentWebSocketServer._skips_session_lifecycle_guard(request) is True


@pytest.mark.parametrize(
    "method",
    [
        ReqMethod.SKILLS_TOGGLE,
        ReqMethod.SKILLS_REBUILD,
        ReqMethod.CHAT_SEND,
    ],
)
def test_skill_writes_and_chat_still_require_lifecycle_guard(method: ReqMethod) -> None:
    """Write / chat RPCs must not inherit the plaza read bypass."""
    request = AgentRequest(
        request_id="r2",
        channel_id="web",
        req_method=method,
        session_id="web_deleted_session",
        params={"session_id": "web_deleted_session"},
    )
    assert AgentWebSocketServer._skips_session_lifecycle_guard(request) is False
    # Broader "stateless" (skip adapter) still covers skills.* writes; that is
    # intentional and distinct from the session-lifecycle bypass.
    if method.value.startswith("skills."):
        assert AgentWebSocketServer._is_stateless_method_request(request) is True
