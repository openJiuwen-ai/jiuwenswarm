# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the session_metadata WS remote provider (the only gateway form)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from gateway.routing import session_metadata_remote
from gateway.routing.session_metadata_remote import (
    SessionMetadataRemoteProvider,
    SessionMetadataRemoteUnavailable,
    bind_agent_client,
    install,
)
from jiuwenswarm.common import external_providers
from jiuwenswarm.common.external_providers import provider


class _FakeUnary:
    """Captures fetch_agent_unary kwargs and replays a canned (ok, payload)."""

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}
        self.result: tuple[bool, dict[str, Any]] = (True, {"team_name": "remote-team"})

    async def __call__(self, **kwargs: Any) -> tuple[bool, dict[str, Any]]:
        self.captured.update(kwargs)
        return self.result


@pytest.fixture(autouse=True)
def _reset_agent_client(monkeypatch: pytest.MonkeyPatch):
    """bind_agent_client 写的是模块级全局，跨用例必须复位防状态泄漏。"""
    monkeypatch.setattr(session_metadata_remote, "_agent_client", None)


@pytest.fixture(name="fake_unary")
def _fake_unary(monkeypatch: pytest.MonkeyPatch) -> _FakeUnary:
    fake = _FakeUnary()
    monkeypatch.setattr(session_metadata_remote, "fetch_agent_unary", fake)
    return fake


@pytest.mark.asyncio
async def test_remote_returns_payload_and_shapes_request(fake_unary) -> None:
    bind_agent_client(SimpleNamespace(name="agent-client"))

    result = await SessionMetadataRemoteProvider().get_session_metadata_async(
        "sess-1", cache_bust=True
    )

    assert result == {"team_name": "remote-team"}
    assert fake_unary.captured["agent_client"] is not None
    assert fake_unary.captured["req_method"].value == "session.get_metadata"
    assert fake_unary.captured["params"] == {
        "session_id": "sess-1",
        "enable_writeback": True,
    }
    assert fake_unary.captured["session_id"] == "sess-1"
    # 内部通道标识避开 "web"，不触发 server 侧排队会话消息附加逻辑
    assert fake_unary.captured["channel_id"] == "gateway"
    # 控制路径（CHAT_CANCEL 等）复用本读取，超时上限必须保持短等待
    assert (
        fake_unary.captured["timeout_seconds"]
        == session_metadata_remote.SESSION_METADATA_QUERY_TIMEOUT_SECONDS
    )


@pytest.mark.asyncio
async def test_remote_forwards_user_id_for_routed_deployments(fake_unary) -> None:
    """AgentOS/ADX 多用户路由必需 user_id——路由层对空身份直接拒绝。"""
    bind_agent_client(SimpleNamespace(name="agent-client"))

    await SessionMetadataRemoteProvider().get_session_metadata_async(
        "sess-1", user_id="owner-1"
    )

    assert fake_unary.captured["user_id"] == "owner-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        (False, {"error": "session not found", "code": "NOT_FOUND"}),
    ],
)
async def test_remote_returns_empty_dict_when_session_missing(fake_unary, result) -> None:
    """仅"会话不存在"返回空 dict——与本地实现缺失会话语义对齐。"""
    fake_unary.result = result

    assert await SessionMetadataRemoteProvider().get_session_metadata_async("sess") == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        (False, {"error": "AgentServer is unavailable", "code": "SERVICE_UNAVAILABLE"}),
        (False, {"error": "AgentServer request timed out", "code": "AGENT_SERVER_TIMEOUT"}),
        (False, {"error": "routing rejected", "code": "PERMISSION"}),
    ],
)
async def test_remote_raises_on_transport_failure(fake_unary, result) -> None:
    """传输失败抛 SessionMetadataRemoteUnavailable——控制路径据此区分"读失败"与"读为空"。"""
    fake_unary.result = result

    with pytest.raises(SessionMetadataRemoteUnavailable):
        await SessionMetadataRemoteProvider().get_session_metadata_async("sess")


def test_remote_unknown_method_raises_domain_aware_error() -> None:
    """域内写方法等未知符号给出带域信息的显式错误，而非裸 AttributeError。

    写方法（update_session_model_selection 等）在 AgentServer 进程执行，
    gateway 远端模式不承载——哑弹必须变成可读的指引。
    """
    with pytest.raises(RuntimeError, match="AgentServer process"):
        SessionMetadataRemoteProvider().update_session_model_selection("sess", {})


def test_remote_sync_entry_degrades_to_empty_dict() -> None:
    """同步入口降级返回空 dict 并告警，未迁移消费方优雅降级而非 500。"""
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    # 直接挂在模块 logger 上断言：conftest 的 setup_logger 会把 gateway logger
    # 家族重挂到 jiuwenswarm root 下，caplog（真 root）不保证能收到记录。
    module_logger = logging.getLogger("gateway.routing.session_metadata_remote")
    handler = _Capture()
    module_logger.addHandler(handler)
    try:
        result = SessionMetadataRemoteProvider().get_session_metadata("sess")
    finally:
        module_logger.removeHandler(handler)

    assert result == {}
    assert any("sync entry degraded" in message for message in records)


@pytest.fixture(name="reset_registry")
def _reset_registry(monkeypatch: pytest.MonkeyPatch):
    """Isolate the provider registry around install()."""
    saved = dict(external_providers._providers)
    yield
    external_providers._providers.clear()
    external_providers._providers.update(saved)


def test_install_registers_the_remote_provider(reset_registry) -> None:
    """bridge 退役后 install 是 gateway 进程的唯一注册路径（无条件）。"""
    install()

    assert isinstance(provider("session_metadata"), SessionMetadataRemoteProvider)
