# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""app_agentserver HTTP 启动策略：企业版/enforce 下起不来必须致命退出。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server import http_startup_policy as policy


@pytest.mark.asyncio
async def test_abort_enterprise_when_http_start_returns_none(monkeypatch):
    server = MagicMock()
    server.stop = AsyncMock()
    monkeypatch.setattr(policy, "is_enterprise", lambda: True)

    with pytest.raises(RuntimeError, match="enterprise requires HTTP"):
        await policy.abort_if_http_required_missing(
            server,
            http_server=None,
            http_enabled=True,
            http_host="127.0.0.1",
            http_port=8766,
            link_mtls_enforced=False,
        )

    server.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_abort_enforce_when_http_missing(monkeypatch):
    server = MagicMock()
    server.stop = AsyncMock()
    monkeypatch.setattr(policy, "is_enterprise", lambda: False)

    with pytest.raises(RuntimeError, match="enforce requires a working HTTPS"):
        await policy.abort_if_http_required_missing(
            server,
            http_server=None,
            http_enabled=False,
            http_host="127.0.0.1",
            http_port=8766,
            link_mtls_enforced=True,
        )

    server.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_personal_allows_ws_only_when_http_missing(monkeypatch):
    server = MagicMock()
    server.stop = AsyncMock()
    monkeypatch.setattr(policy, "is_enterprise", lambda: False)

    await policy.abort_if_http_required_missing(
        server,
        http_server=None,
        http_enabled=True,
        http_host="127.0.0.1",
        http_port=8766,
        link_mtls_enforced=False,
    )

    server.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_exception_enterprise_raises(monkeypatch):
    server = MagicMock()
    server.stop = AsyncMock()
    monkeypatch.setattr(policy, "is_enterprise", lambda: True)

    with pytest.raises(RuntimeError, match="enterprise HTTP startup failed"):
        await policy.handle_http_startup_exception(
            server,
            RuntimeError("boom"),
            http_enabled=True,
            link_mtls_enforced=False,
        )

    server.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_exception_personal_logs_only(monkeypatch):
    server = MagicMock()
    server.stop = AsyncMock()
    monkeypatch.setattr(policy, "is_enterprise", lambda: False)

    await policy.handle_http_startup_exception(
        server,
        RuntimeError("boom"),
        http_enabled=True,
        link_mtls_enforced=False,
    )

    server.stop.assert_not_awaited()
