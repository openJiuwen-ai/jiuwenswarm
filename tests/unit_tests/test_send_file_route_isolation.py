# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SendFileToolkit 路由隔离测试。

回归场景：send_file_to_user 工具按全局名注册成单例时，并发请求会互相覆盖
实例字段。改造后优先从请求级 ContextVar 解析路由，并在 ContextVar 默认未绑定时
回退实例字段（不会误用 _CRON_TOOL_CHANNEL_ID 默认的 web）。
"""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
    SendFileToolkit,
    get_send_file_request_context,
    reset_send_file_request_context,
    set_send_file_request_context,
)


def test_resolve_route_falls_back_to_instance_fields_when_no_context():
    toolkit = SendFileToolkit(
        request_id="req-1",
        session_id="sess-1",
        channel_id="officeclaw",
        metadata={"k": "v"},
    )
    route = toolkit._resolve_route()
    assert route.request_id == "req-1"
    assert route.session_id == "sess-1"
    assert route.channel_id == "officeclaw"
    assert route.metadata == {"k": "v"}


def test_resolve_route_prefers_context_over_instance_fields():
    toolkit = SendFileToolkit(
        request_id="stale-req",
        session_id="stale-sess",
        channel_id="officeclaw",
        metadata={"stale": True},
    )
    token = set_send_file_request_context(
        request_id="real-req",
        session_id="real-sess",
        channel_id="officeclaw",
        metadata={"real": True},
    )
    try:
        route = toolkit._resolve_route()
        assert route.request_id == "real-req"
        assert route.session_id == "real-sess"
        assert route.metadata == {"real": True}
    finally:
        reset_send_file_request_context(token)


def test_resolve_route_ignores_unbound_cron_channel_default_web():
    """Unbound cron ContextVar defaults to web; must not override officeclaw instance."""
    toolkit = SendFileToolkit(
        request_id="req-office",
        session_id="sess-office",
        channel_id="officeclaw",
    )
    route = toolkit._resolve_route()
    assert route.channel_id == "officeclaw"
    assert route.request_id == "req-office"


@pytest.mark.asyncio
async def test_concurrent_contexts_are_isolated():
    shared_toolkit = SendFileToolkit(
        request_id="last-registered",
        session_id="last-registered",
        channel_id="officeclaw",
        metadata=None,
    )

    async def run_request(session_id: str, request_id: str) -> tuple[str, str]:
        token = set_send_file_request_context(
            request_id=request_id,
            session_id=session_id,
            channel_id="officeclaw",
        )
        try:
            await asyncio.sleep(0)
            route = shared_toolkit._resolve_route()
            await asyncio.sleep(0)
            return route.session_id, route.request_id
        finally:
            reset_send_file_request_context(token)

    results = await asyncio.gather(
        run_request("sess-A", "req-A"),
        run_request("sess-B", "req-B"),
    )
    assert ("sess-A", "req-A") in results
    assert ("sess-B", "req-B") in results


def test_context_reset_restores_previous_value():
    assert get_send_file_request_context() is None
    token = set_send_file_request_context(session_id="sess-x")
    try:
        assert get_send_file_request_context() == {"session_id": "sess-x"}
    finally:
        reset_send_file_request_context(token)
    assert get_send_file_request_context() is None
