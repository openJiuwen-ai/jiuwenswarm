from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import (
    AgentManager,
    AgentRuntime,
)
from jiuwenswarm.extensions.agentos.agentos_router.models import AgentInfo, AgentStatus
from jiuwenswarm.extensions.agentos.agentos_router.router_client import AgentOSRouterClient
from jiuwenswarm.extensions.agentos.auth.credential_authenticator import AuthContext, AuthResult
from jiuwenswarm.extensions.yuanrong_frontend_client import AgentFileDownloadChunk, SandboxInfo
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_channel_app import build_web_channel_app
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig


class _FakeRegistry:
    async def close(self) -> None:
        return None

    async def register_agent(self, agent_info: AgentInfo) -> None:
        del agent_info

    async def update_instance(self, service_id: str, **kwargs: Any) -> None:
        del service_id, kwargs


class _FakeYuanrong:
    server_ready = True
    frontend_endpoint = "http://yuanrong.test:8888"
    agent_namespace = "default"

    async def connect(self, uri: str) -> None:
        del uri

    async def disconnect(self) -> None:
        return None

    async def create_sandbox(self, **kwargs: Any) -> SandboxInfo:
        del kwargs
        return SandboxInfo(sandbox_id="sbx-1", metadata={})

    async def get_agent_info(self, instance_id: str) -> dict[str, Any]:
        return {"instance_id": instance_id}


class _FakeAuthClient:
    def __init__(self, result: AuthResult) -> None:
        self.result = result
        self.contexts: list[AuthContext] = []

    async def authenticate(self, context: AuthContext) -> AuthResult:
        self.contexts.append(context)
        return self.result


def _make_router() -> AgentOSRouterClient:
    manager = AgentManager(
        creating_timeout_seconds=1.0,
        key_fields=("user_id", "agent_type", "session_id"),
    )
    router = AgentOSRouterClient(
        _FakeYuanrong(),  # type: ignore[arg-type]
        _FakeRegistry(),  # type: ignore[arg-type]
        manager,
    )
    key = AgentRuntime.build_key(
        manager.key_fields,
        user_id="user-1",
        agent_type="claude",
        key_values={"session_id": "sess-1"},
    )
    info = AgentInfo(
        agent_id="agent-1",
        user_id="user-1",
        agent_type="claude",
        status=AgentStatus.READY,
        sandbox_id="inst-1",
        metadata={"session_id": "sess-1"},
    )
    manager._runtimes[key] = AgentRuntime(info=info, key=key)  # noqa: SLF001
    router._current_agent_types["user-1"] = "claude"  # noqa: SLF001
    return router


def _enable_auth(router: AgentOSRouterClient, result: AuthResult) -> _FakeAuthClient:
    auth = _FakeAuthClient(result)
    router._auth_client = auth  # noqa: SLF001
    return auth


def _file_api_app(router: AgentOSRouterClient):
    channel = WebChannel(WebChannelConfig(enabled=True, dual_protocol=True), RobotMessageRouter())
    channel.container_file_client = router
    return build_web_channel_app(channel)


@pytest.mark.asyncio
async def test_authenticate_http_skips_when_auth_disabled() -> None:
    router = _make_router()
    result = await router.authenticate_http(
        path="/file-api/list-files?user_id=user-1",
        headers={"X-User-Id": "user-1"},
        channel="file-api",
    )
    assert result.success is True
    assert result.user_id == "user-1"


@pytest.mark.asyncio
async def test_authenticate_http_download_ignores_query_token() -> None:
    router = _make_router()
    auth = _enable_auth(router, AuthResult(success=True, user_id="iam-user"))
    result = await router.authenticate_http(
        path="/file-api/download?token=file-hmac.signature",
        headers={"Authorization": "Bearer iam-tok"},
        allow_query_token=False,
    )
    assert result.success is True
    assert auth.contexts[0].credentials.get("token") == "iam-tok"


@pytest.mark.asyncio
async def test_file_api_iam_deny_does_not_call_router() -> None:
    router = _make_router()
    router.list_container_files = AsyncMock()  # type: ignore[method-assign]
    _enable_auth(
        router,
        AuthResult(
            success=False,
            error="缺少 token",
            extensions={"error_code": "MISSING_TOKEN"},
        ),
    )
    app = _file_api_app(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/file-api/list-files",
            params={"dir": "/home/agentos", "user_id": "user-1"},
        )
    assert resp.status_code == 401
    assert resp.json()["code"] == "MISSING_TOKEN"
    router.list_container_files.assert_not_called()


@pytest.mark.asyncio
async def test_file_api_iam_ok_uses_iam_user_id_without_yuanrong_passthrough() -> None:
    router = _make_router()
    router.list_container_files = AsyncMock(return_value=[])  # type: ignore[method-assign]
    auth = _enable_auth(router, AuthResult(success=True, user_id="iam-user"))
    app = _file_api_app(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/file-api/list-files",
            params={"dir": "/home/agentos", "user_id": "iam-user"},
            headers={"Authorization": "Bearer iam-tok"},
        )
    assert resp.status_code == 200
    assert auth.contexts[0].credentials.get("token") == "iam-tok"
    kwargs = router.list_container_files.await_args.kwargs
    assert kwargs["user_id"] == "iam-user"
    assert "auth_headers" not in kwargs


@pytest.mark.asyncio
async def test_file_api_iam_rejects_user_id_mismatch() -> None:
    router = _make_router()
    router.list_container_files = AsyncMock()  # type: ignore[method-assign]
    _enable_auth(
        router,
        AuthResult(success=True, user_id="iam-user", extensions={"username": "admin"}),
    )
    app = _file_api_app(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/file-api/list-files",
            params={"dir": "/home/agentos", "user_id": "spoofed"},
            headers={"Authorization": "Bearer iam-tok"},
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "USER_MISMATCH"
    router.list_container_files.assert_not_called()


@pytest.mark.asyncio
async def test_file_api_iam_accepts_username_as_user_id() -> None:
    router = _make_router()
    router.list_container_files = AsyncMock(return_value=[])  # type: ignore[method-assign]
    _enable_auth(
        router,
        AuthResult(success=True, user_id="iam-user", extensions={"username": "admin"}),
    )
    app = _file_api_app(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/file-api/list-files",
            params={"dir": "/home/agentos", "user_id": "admin"},
            headers={"Authorization": "Bearer iam-tok"},
        )
    assert resp.status_code == 200
    assert router.list_container_files.await_args.kwargs["user_id"] == "iam-user"


@pytest.mark.asyncio
async def test_file_api_iam_rejects_header_user_id_mismatch() -> None:
    router = _make_router()
    router.list_container_files = AsyncMock()  # type: ignore[method-assign]
    _enable_auth(router, AuthResult(success=True, user_id="iam-user"))
    app = _file_api_app(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/file-api/list-files",
            params={"dir": "/home/agentos"},
            headers={"Authorization": "Bearer iam-tok", "X-User-Id": "spoofed"},
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "USER_MISMATCH"
    router.list_container_files.assert_not_called()


@pytest.mark.asyncio
async def test_file_api_iam_rejects_body_user_id_mismatch() -> None:
    router = _make_router()
    router.upload_container_file = AsyncMock()  # type: ignore[method-assign]
    _enable_auth(router, AuthResult(success=True, user_id="iam-user"))
    app = _file_api_app(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/file-api/file-content",
            headers={"Authorization": "Bearer iam-tok"},
            json={"path": "notes.md", "content": "hi", "user_id": "spoofed"},
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "USER_MISMATCH"
    router.upload_container_file.assert_not_called()


@pytest.mark.asyncio
async def test_file_api_iam_ok_without_request_user_id() -> None:
    router = _make_router()
    router.list_container_files = AsyncMock(return_value=[])  # type: ignore[method-assign]
    _enable_auth(router, AuthResult(success=True, user_id="iam-user"))
    app = _file_api_app(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/file-api/list-files",
            params={"dir": "/home/agentos"},
            headers={"Authorization": "Bearer iam-tok"},
        )
    assert resp.status_code == 200
    assert router.list_container_files.await_args.kwargs["user_id"] == "iam-user"


@pytest.mark.asyncio
async def test_file_api_download_does_not_send_file_token_to_iam() -> None:
    router = _make_router()
    router.download_container_file = AsyncMock(  # type: ignore[method-assign]
        return_value=AgentFileDownloadChunk(
            data=b"report",
            path="/home/agentos/reports/result.txt",
            offset=0,
            chunk_size=6,
            size=6,
            content_type="text/plain",
            eof=True,
        )
    )
    auth = _enable_auth(router, AuthResult(success=True, user_id="user-1"))
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"path": "/home/agentos/reports/result.txt", "sid": "session-1"}).encode()
        )
        .decode()
        .rstrip("=")
    )
    app = _file_api_app(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/file-api/download?token={payload}.signature&user_id=user-1",
            headers={"Authorization": "Bearer iam-tok"},
        )
    assert resp.status_code == 200
    assert auth.contexts[0].credentials.get("token") == "iam-tok"
    assert "auth_headers" not in router.download_container_file.await_args.kwargs
