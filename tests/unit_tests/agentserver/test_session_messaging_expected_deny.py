# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JW-69: cron 执行会话不再每次模型调用吃 "Session was not found" 告警。

跨会话消息 rail（SessionMessagingRouteRail.before_model_call）把服务端设计内
拒绝（cron 执行会话被 SessionMessageService._supported 拒绝）当错误处理：每次
模型调用 1 条 WARNING + 完整 traceback，并把 ``state_unavailable`` 假快照注入
系统提示。修复分两层：

1. adapter 层（``_ensure_session_messaging_tools_registered``）按 cron 会话
   判定直接摘除 rail 的 service——cron 执行会话根本不挂跨会话消息工具；
2. rail 层把 ``SessionMessagingError``（服务端拒绝语义）降为 debug 且跳过
   session_tools 段；OSError/sqlite3.Error 等真实异常保留 WARNING。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.agents.harness.common.tools.session_messaging_toolkit import (
    SessionMessagingRouteRail,
    SessionMessagingRoute,
    SessionMessagingToolkit,
    with_session_messaging_route,
)
from jiuwenswarm.server.runtime.session.session_message_service import (
    SessionMessagingError,
)


def _rail_with_builder(service) -> tuple[SessionMessagingRouteRail, Mock]:
    rail = SessionMessagingRouteRail()
    builder = Mock()
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    rail.set_service(service)
    return rail, builder


def _route_ctx(route: SessionMessagingRoute) -> SimpleNamespace:
    return SimpleNamespace(
        extra={
            "run_context": with_session_messaging_route({}, route)["run"]["context"]
        }
    )


@pytest.mark.asyncio
async def test_server_rejection_skips_prompt_without_warning(caplog):
    """A server-side deny is expected behavior: debug only, no snapshot injected."""
    service = Mock()
    service.pending_context = AsyncMock(
        side_effect=SessionMessagingError("NOT_FOUND_OR_FORBIDDEN", "Session was not found")
    )
    rail, builder = _rail_with_builder(service)
    route = SessionMessagingRoute(session_id="source-1", request_id="query")
    try:
        with caplog.at_level(logging.DEBUG, logger="jiuwenswarm.agents.harness.common.tools.session_messaging_toolkit"):
            await rail.before_model_call(_route_ctx(route))
        # No session_tools section is injected at all — no state_unavailable snapshot.
        builder.add_section.assert_not_called()
        builder.remove_section.assert_called_once_with("session_tools")
        # The expected deny is logged at debug level without a traceback.
        denials = [r for r in caplog.records if "Skipping cross-Session queue context" in r.message]
        assert len(denials) == 1
        assert denials[0].levelno == logging.DEBUG
        assert denials[0].exc_info is None
    finally:
        rail.uninit(None)


@pytest.mark.asyncio
async def test_unexpected_messaging_error_keeps_warning_with_snapshot(caplog):
    """Only design-level denials go silent; unexpected codes stay observable."""
    service = Mock()
    service.pending_context = AsyncMock(
        side_effect=SessionMessagingError(
            "STORE_CORRUPT", "mailbox store returned an invalid record"
        )
    )
    rail, builder = _rail_with_builder(service)
    route = SessionMessagingRoute(session_id="source-1", request_id="query")
    try:
        with caplog.at_level(logging.WARNING, logger="jiuwenswarm.agents.harness.common.tools.session_messaging_toolkit"):
            await rail.before_model_call(_route_ctx(route))
        assert builder.add_section.call_count == 1
        section = builder.add_section.call_args.args[0]
        state = json.loads(section.content["en"].split("\n", 1)[1])
        assert state == {"state_unavailable": True}
        warnings = [r for r in caplog.records if "Could not refresh cross-Session queue context" in r.message]
        assert len(warnings) == 1
        assert warnings[0].levelno == logging.WARNING
        assert any(
            "SessionMessagingError" in line for line in caplog.text.splitlines()
        )
    finally:
        rail.uninit(None)


@pytest.mark.asyncio
async def test_real_io_failure_keeps_warning_with_snapshot(caplog):
    """OSError/sqlite3.Error remain WARNING with traceback and the fail-safe snapshot."""
    service = Mock()
    service.pending_context = AsyncMock(side_effect=OSError("mailbox store unreadable"))
    rail, builder = _rail_with_builder(service)
    route = SessionMessagingRoute(session_id="source-1", request_id="query")
    try:
        with caplog.at_level(logging.WARNING, logger="jiuwenswarm.agents.harness.common.tools.session_messaging_toolkit"):
            await rail.before_model_call(_route_ctx(route))
        assert builder.add_section.call_count == 1
        section = builder.add_section.call_args.args[0]
        state = json.loads(section.content["en"].split("\n", 1)[1])
        assert state == {"state_unavailable": True}
        warnings = [r for r in caplog.records if "Could not refresh cross-Session queue context" in r.message]
        assert len(warnings) == 1
        assert warnings[0].levelno == logging.WARNING
        # caplog's handler record keeps the rendered traceback text; assert via it.
        assert any("OSError" in line for line in caplog.text.splitlines())
    finally:
        rail.uninit(None)


@pytest.mark.asyncio
async def test_sqlite_failure_keeps_warning_and_successful_refresh_keeps_snapshot():
    """sqlite3.Error stays on the WARNING path; a healthy refresh injects queues."""
    service = Mock()
    rail, builder = _rail_with_builder(service)
    route = SessionMessagingRoute(session_id="source-1", request_id="query")
    try:
        service.pending_context = AsyncMock(side_effect=sqlite3.Error("locked"))
        await rail.before_model_call(_route_ctx(route))
        assert builder.add_section.call_count == 1
        section = builder.add_section.call_args.args[0]
        assert json.loads(section.content["en"].split("\n", 1)[1]) == {"state_unavailable": True}

        builder.reset_mock()
        service.pending_context = AsyncMock(return_value={"queues": [], "has_more": False})
        await rail.before_model_call(_route_ctx(route))
        assert builder.add_section.call_count == 1
        section = builder.add_section.call_args.args[0]
        assert json.loads(section.content["en"].split("\n", 1)[1]) == {"queues": [], "has_more": False}
    finally:
        rail.uninit(None)


class _AbilityManager:
    def __init__(self, names: set[str] | None = None) -> None:
        self.cards = {name: SimpleNamespace(name=name) for name in (names or set())}

    def list(self):
        return list(self.cards.values())

    def add(self, card):
        self.cards[card.name] = card

    def remove(self, name):
        self.cards.pop(name, None)


def _adapter_with_messaging(service) -> Any:
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(ability_manager=_AbilityManager())
    adapter._last_mode = "agent.code.normal"
    adapter._session_messaging_toolkit = None
    adapter._session_messaging_route_rail = SessionMessagingRouteRail()
    adapter._register_agent_owned_tool = lambda tool, owner_id: None
    adapter._tool_owner_id = lambda: "test-owner"
    return adapter


def _messaging_runtime(service) -> SimpleNamespace:
    return SimpleNamespace(session_message_service=service)


_MESSAGING_TOOL_NAMES = {
    "session_list",
    "session_send_message",
    "session_message_list",
    "session_message_resolve",
    "session_continue_queued",
    "session_read",
}


@pytest.mark.parametrize(
    "session_id",
    ["__cron___20261123_abc123", "cron_881503d0"],
    ids=["legacy_cron_channel", "cron_prefix"],
)
def test_adapter_unbinds_messaging_for_cron_sessions(session_id, monkeypatch):
    """cron 执行会话不挂跨会话消息工具，rail service 一并摘除。"""
    from jiuwenswarm.runtime.context import reset_runtime_context, set_runtime_context

    service = object()
    adapter = _adapter_with_messaging(service)
    adapter._session_messaging_toolkit = SessionMessagingToolkit(service)
    adapter._session_messaging_route_rail.set_service(service)
    runtime_token = set_runtime_context(_messaging_runtime(service), SimpleNamespace())
    try:
        adapter._ensure_session_messaging_tools_registered(session_id, "web")
    finally:
        reset_runtime_context(runtime_token)

    assert set(adapter._instance.ability_manager.cards) == set()
    assert adapter._session_messaging_route_rail._service is None


def test_adapter_unbinds_messaging_for_persisted_cron_id_session(monkeypatch):
    """持久化 cron_id 的 web 会话同样识别为 cron 执行会话（新链路形态）。"""
    from jiuwenswarm.runtime.context import reset_runtime_context, set_runtime_context

    service = object()
    adapter = _adapter_with_messaging(service)
    adapter._session_messaging_toolkit = SessionMessagingToolkit(service)
    adapter._session_messaging_route_rail.set_service(service)
    runtime = _messaging_runtime(service)
    runtime_token = set_runtime_context(runtime, SimpleNamespace())
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda sid, **kwargs: {
            "session_id": sid, "channel_id": "web", "mode": "agent.work.normal",
            "cron_id": "881503d0ad8641a88452ca80246acf4c",
        },
    )
    try:
        adapter._ensure_session_messaging_tools_registered(
            "web_1a206464c75_87d8e7f01e95", "web"
        )
    finally:
        reset_runtime_context(runtime_token)

    assert set(adapter._instance.ability_manager.cards) == set()
    assert adapter._session_messaging_route_rail._service is None


def test_adapter_keeps_messaging_for_normal_web_session(monkeypatch):
    """正常 web 会话不受影响：工具注册齐全，rail service 保持绑定。"""
    from jiuwenswarm.runtime.context import reset_runtime_context, set_runtime_context

    service = object()
    adapter = _adapter_with_messaging(service)
    runtime_token = set_runtime_context(_messaging_runtime(service), SimpleNamespace())
    try:
        adapter._ensure_session_messaging_tools_registered("web_normal_1", "web")
    finally:
        reset_runtime_context(runtime_token)

    assert _MESSAGING_TOOL_NAMES <= set(adapter._instance.ability_manager.cards)
    assert adapter._session_messaging_route_rail._service is service


def test_adapter_cron_check_failure_does_not_break_normal_sessions(monkeypatch):
    """cron 判定异常时按非 cron 处理，正常会话不受影响。"""
    from jiuwenswarm.runtime.context import reset_runtime_context, set_runtime_context

    def broken_metadata(session_id, **kwargs):
        raise RuntimeError("metadata store unavailable")

    service = object()
    adapter = _adapter_with_messaging(service)
    runtime_token = set_runtime_context(_messaging_runtime(service), SimpleNamespace())
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        broken_metadata,
    )
    try:
        adapter._ensure_session_messaging_tools_registered("web_normal_2", "web")
    finally:
        reset_runtime_context(runtime_token)

    assert _MESSAGING_TOOL_NAMES <= set(adapter._instance.ability_manager.cards)
    assert adapter._session_messaging_route_rail._service is service
