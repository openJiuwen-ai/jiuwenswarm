from __future__ import annotations

# TEST ONLY: URL literals use RFC-reserved domains or ASGI's synthetic
# ``http://test`` base; loopback listeners bind ephemeral local test servers.
import base64
import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_gateway.channel_manager.web import app_web_handlers
from agent_gateway.channel_manager.web.web_connect import WebChannel
from agent_gateway.extensions.agentos.agentos_router.agent_manager import (
    BUILTIN_AGENT_TYPE,
    AgentManager,
    AgentRuntime,
)
from agent_gateway.extensions.agentos.agentos_router.models import AgentInfo, AgentStatus
from agent_gateway.extensions.agentos.agentos_router.router_client import (
    AgentOSFileTransferError,
    AgentOSRouterClient,
    build_auth_headers_from_token,
    normalize_agent_file_download_path,
    normalize_agent_file_upload_path,
)
from agent_gateway.extensions.adx.config import AdxRouterConfig
from agent_gateway.extensions.adx.control_client import AdxControlClient
from agent_gateway.extensions.adx.router_client import AdxRouterClient
from agent_gateway.extensions.agentos.agentos_router.config import RegistryConfig, RouterConfig
from agent_gateway.extensions.agentos.agentos_router.backend import AgentFileError
from agent_gateway.routing.container_files import AgentFileDownloadChunk
from agent_gateway.southbound.control.managed import SandboxInfo
from agent_gateway.routing.agent_client import WebSocketAgentServerClient


class _FakeRegistry:
    async def close(self) -> None:
        return None

    async def register_agent(self, agent_info: AgentInfo) -> None:
        del agent_info

    async def update_instance(self, service_id: str, **kwargs: Any) -> None:
        del service_id, kwargs


class _FileBackend:
    server_ready = True
    frontend_endpoint = "https://backend.test"
    agent_namespace = "default"

    def __init__(self) -> None:
        self.upload_calls: list[dict[str, Any]] = []
        self.download_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.mkdir_calls: list[dict[str, Any]] = []
        self.create_calls = 0
        self.mkdir_result: dict[str, Any] = {
            "success": True,
            "path": "/home/agentos/sub",
            "created": True,
        }
        self.list_result: list[dict[str, Any]] = [
            {
                "name": "README.md",
                "path": "/home/agentos/README.md",
                "size": 256,
                "is_directory": False,
                "modified_time": "2026-08-08T10:31:00.000000",
                "type": ".md",
            },
            {
                "name": "logs",
                "path": "/home/agentos/logs",
                "size": 0,
                "is_directory": True,
                "modified_time": "2026-08-08T10:29:00.000000",
                "type": None,
            },
            {
                "name": "app.py",
                "path": "/home/agentos/app.py",
                "size": 1024,
                "is_directory": False,
                "modified_time": "2026-08-08T10:30:00.000000",
                "type": ".py",
            },
        ]

    async def connect(self, uri: str) -> None:
        del uri

    async def disconnect(self) -> None:
        return None

    async def create_sandbox(
        self,
        *,
        namespace: str,
        name: str,
        workspace: str,
        runtime_spec: dict[str, Any],
        env_vars: dict[str, str] | None = None,
        builtin: bool = False,
        web_port: int | None = None,
        trace_id: str = "",
    ) -> SandboxInfo:
        del namespace, name, workspace, runtime_spec, env_vars, builtin, web_port, trace_id
        self.create_calls += 1
        return SandboxInfo(sandbox_id=f"sbx-{self.create_calls}", metadata={})

    async def get_agent_info(self, instance_id: str) -> dict[str, Any]:
        return {"instance_id": instance_id}

    async def upload_agent_file(
        self,
        instance_id: str,
        path: str,
        data: bytes,
        *,
        auth_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.upload_calls.append(
            {
                "instance_id": instance_id,
                "path": path,
                "data": data,
                "auth_headers": dict(auth_headers or {}),
            }
        )
        return {"success": True, "path": path, "size": len(data)}

    async def download_agent_file(
        self,
        instance_id: str,
        path: str,
        *,
        offset: int = 0,
        limit: int = 65536,
        auth_headers: dict[str, str] | None = None,
    ) -> AgentFileDownloadChunk:
        self.download_calls.append(
            {
                "instance_id": instance_id,
                "path": path,
                "offset": offset,
                "limit": limit,
                "auth_headers": dict(auth_headers or {}),
            }
        )
        payload = b"hello-file"
        return AgentFileDownloadChunk(
            data=payload,
            path=path,
            offset=offset,
            chunk_size=len(payload),
            size=len(payload),
            content_type="application/octet-stream",
            eof=True,
        )

    async def list_agent_files(
        self,
        instance_id: str,
        path: str,
        *,
        recursive: bool = False,
        max_depth: int = 0,
        auth_headers: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        self.list_calls.append(
            {
                "instance_id": instance_id,
                "path": path,
                "recursive": recursive,
                "max_depth": max_depth,
                "auth_headers": dict(auth_headers or {}),
            }
        )
        return list(self.list_result)

    async def mkdir_agent_dir(
        self,
        instance_id: str,
        path: str,
        *,
        mode: str | None = None,
        recursive: bool = False,
        auth_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.mkdir_calls.append(
            {
                "instance_id": instance_id,
                "path": path,
                "mode": mode,
                "recursive": recursive,
                "auth_headers": dict(auth_headers or {}),
            }
        )
        result = dict(self.mkdir_result)
        result["path"] = path
        return result


def _make_router_with_runtime(*, sandbox_id: str = "inst-1") -> AgentOSRouterClient:
    backend = _FileBackend()
    manager = AgentManager(creating_timeout_seconds=1.0, key_fields=("user_id", "agent_type", "session_id"))
    router = AgentOSRouterClient(
        backend,  # type: ignore[arg-type]
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
        sandbox_id=sandbox_id,
        metadata={"session_id": "sess-1"},
    )
    runtime = AgentRuntime(info=info, key=key)
    manager._runtimes[key] = runtime  # noqa: SLF001 - test helper
    router._current_agent_types["user-1"] = "claude"  # noqa: SLF001 - test helper
    return router


def test_build_auth_headers_from_token_adds_bearer_prefix() -> None:
    assert build_auth_headers_from_token("abc") == {"Authorization": "Bearer abc"}
    assert build_auth_headers_from_token("Bearer xyz") == {"Authorization": "Bearer xyz"}


def test_normalize_agent_file_upload_path_prefixes_home_agentos() -> None:
    assert (
        normalize_agent_file_upload_path("report.md", user_id="alice")
        == "/home/agentos/report.md"
    )
    assert (
        normalize_agent_file_upload_path("docs/a.md", user_id="alice")
        == "/home/agentos/docs/a.md"
    )
    assert (
        normalize_agent_file_upload_path("a.md", dir_prefix="docs", user_id="alice")
        == "/home/agentos/docs/a.md"
    )
    assert (
        normalize_agent_file_upload_path("a.md", dir_prefix="uploads/inbox", user_id="alice")
        == "/home/agentos/uploads/inbox/a.md"
    )
    assert (
        normalize_agent_file_upload_path("uploads/docs/a.md", user_id="alice")
        == "/home/agentos/uploads/docs/a.md"
    )
    with pytest.raises(AgentOSFileTransferError) as absolute:
        normalize_agent_file_upload_path("/home/agentos/a.md", user_id="alice")
    assert absolute.value.code == "BAD_REQUEST"
    assert "relative" in str(absolute.value)
    with pytest.raises(AgentOSFileTransferError) as abs_dir:
        normalize_agent_file_upload_path("a.md", dir_prefix="//home/agentos/docs")
    assert abs_dir.value.code == "BAD_REQUEST"
    with pytest.raises(AgentOSFileTransferError) as exc:
        normalize_agent_file_upload_path("../etc/passwd", user_id="alice")
    assert exc.value.code == "BAD_REQUEST"
    with pytest.raises(AgentOSFileTransferError) as forbidden:
        normalize_agent_file_upload_path("malware.exe", user_id="alice")
    assert forbidden.value.code == "BAD_REQUEST"
    assert "forbidden" in str(forbidden.value)


def test_normalize_agent_file_download_path_requires_absolute_under_home_agentos() -> None:
    assert (
        normalize_agent_file_download_path("/home/agentos/uploads/a.pdf")
        == "/home/agentos/uploads/a.pdf"
    )
    with pytest.raises(AgentOSFileTransferError) as relative:
        normalize_agent_file_download_path("outputs/a.pdf")
    assert relative.value.code == "BAD_REQUEST"
    assert "absolute" in str(relative.value)
    with pytest.raises(AgentOSFileTransferError) as outside:
        normalize_agent_file_download_path("/workspace/a.pdf")
    assert outside.value.code == "BAD_REQUEST"
    assert "/home/agentos" in str(outside.value)
    with pytest.raises(AgentOSFileTransferError) as traversal:
        normalize_agent_file_download_path("/home/agentos/../etc/passwd")
    assert traversal.value.code == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_router_upload_propagates_backend_file_too_large() -> None:
    router = _make_router_with_runtime()
    backend: _FileBackend = router._backend  # type: ignore[assignment]

    async def _too_large(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise AgentFileError(
            "payload too large",
            http_status=413,
            error_code="file_too_large",
        )

    backend.upload_agent_file = _too_large  # type: ignore[method-assign]
    with pytest.raises(AgentOSFileTransferError) as exc:
        await router.upload_container_file(
            user_id="user-1",
            path="big.pdf",
            content=b"x" * 1024,
            session_id="sess-1",
        )
    assert exc.value.code == "file_too_large"


@pytest.mark.asyncio
async def test_router_upload_container_file_resolves_instance_and_forwards_auth() -> None:
    router = _make_router_with_runtime()
    backend: _FileBackend = router._backend  # type: ignore[assignment]

    payload = await router.upload_container_file(
        user_id="user-1",
        path="docs/report.md",
        content=b"# hello",
        session_id="sess-1",
        auth_headers={"Authorization": "Bearer token-1"},
    )

    assert payload == {
        "success": True,
        "path": "/home/agentos/docs/report.md",
        "size": 7,
    }
    assert backend.upload_calls == [
        {
            "instance_id": "inst-1",
            "path": "/home/agentos/docs/report.md",
            "data": b"# hello",
            "auth_headers": {"Authorization": "Bearer token-1"},
        }
    ]


@pytest.mark.asyncio
async def test_router_download_container_file_returns_chunk_payload() -> None:
    router = _make_router_with_runtime()
    backend: _FileBackend = router._backend  # type: ignore[assignment]

    chunk = await router.download_container_file(
        user_id="user-1",
        path="/home/agentos/uploads/docs/report.pdf",
        offset=0,
        limit=1024,
        instance_id="inst-1",
    )

    assert chunk.path == "/home/agentos/uploads/docs/report.pdf"
    assert chunk.data == b"hello-file"
    assert chunk.eof is True
    assert backend.download_calls[0]["instance_id"] == "inst-1"


@pytest.mark.asyncio
async def test_router_list_container_files_forwards_dir_and_auth() -> None:
    router = _make_router_with_runtime()
    backend: _FileBackend = router._backend  # type: ignore[assignment]

    items = await router.list_container_files(
        user_id="user-1",
        dir_path="/home/agentos",
        recursive=True,
        max_depth=3,
        session_id="sess-1",
        auth_headers={"Authorization": "Bearer token-1"},
    )

    assert [item["name"] for item in items] == ["README.md", "logs", "app.py"]
    assert backend.list_calls == [
        {
            "instance_id": "inst-1",
            "path": "/home/agentos",
            "recursive": True,
            "max_depth": 3,
            "auth_headers": {"Authorization": "Bearer token-1"},
        }
    ]
    with pytest.raises(AgentOSFileTransferError) as relative:
        await router.list_container_files(user_id="user-1", dir_path="docs")
    assert relative.value.code == "BAD_REQUEST"
    with pytest.raises(AgentOSFileTransferError) as depth:
        await router.list_container_files(
            user_id="user-1",
            dir_path="/home/agentos",
            max_depth=-1,
        )
    assert depth.value.code == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_router_mkdir_container_dir_forwards_path_mode_and_auth() -> None:
    router = _make_router_with_runtime()
    backend: _FileBackend = router._backend  # type: ignore[assignment]

    payload = await router.mkdir_container_dir(
        user_id="user-1",
        path="/home/agentos/sub",
        mode="0755",
        recursive=True,
        session_id="sess-1",
        auth_headers={"Authorization": "Bearer token-1"},
    )

    assert payload == {
        "success": True,
        "path": "/home/agentos/sub",
        "created": True,
    }
    assert backend.mkdir_calls == [
        {
            "instance_id": "inst-1",
            "path": "/home/agentos/sub",
            "mode": "0755",
            "recursive": True,
            "auth_headers": {"Authorization": "Bearer token-1"},
        }
    ]
    with pytest.raises(AgentOSFileTransferError) as relative:
        await router.mkdir_container_dir(user_id="user-1", path="sub")
    assert relative.value.code == "BAD_REQUEST"
    with pytest.raises(AgentOSFileTransferError) as outside:
        await router.mkdir_container_dir(user_id="user-1", path="/workspace/sub")
    assert outside.value.code == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_router_mkdir_container_dir_idempotent_created_false() -> None:
    router = _make_router_with_runtime()
    backend: _FileBackend = router._backend  # type: ignore[assignment]
    backend.mkdir_result = {
        "success": True,
        "path": "/home/agentos/sub",
        "created": False,
    }

    payload = await router.mkdir_container_dir(
        user_id="user-1",
        path="/home/agentos/sub",
        recursive=True,
        session_id="sess-1",
    )

    assert payload["created"] is False


@pytest.mark.asyncio
async def test_router_third_party_upload_acquires_and_releases_runtime() -> None:
    """Third-party path must acquire so idle reaper cannot reap mid-transfer."""
    router = _make_router_with_runtime()
    manager = router._agent_manager  # noqa: SLF001 - test helper
    backend: _FileBackend = router._backend  # type: ignore[assignment]
    key = next(iter(manager._runtimes))  # noqa: SLF001 - test helper
    seen_counts: list[int] = []

    original_upload = backend.upload_agent_file

    async def _upload_checking_hold(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen_counts.append(manager._runtimes[key].task_count)  # noqa: SLF001
        return await original_upload(*args, **kwargs)

    backend.upload_agent_file = _upload_checking_hold  # type: ignore[method-assign]

    payload = await router.upload_container_file(
        user_id="user-1",
        path="docs/report.md",
        content=b"# hello",
        agent_type="claude",
        session_id="sess-1",
    )

    assert payload["success"] is True
    assert seen_counts == [1]
    assert manager._runtimes[key].task_count == 0  # noqa: SLF001 - released in finally


@pytest.mark.asyncio
async def test_router_resolve_instance_missing_runtime_raises() -> None:
    router = _make_router_with_runtime()
    with pytest.raises(AgentOSFileTransferError) as exc:
        await router.resolve_instance_id_for_files(
            user_id="missing-user",
            agent_type="claude",
            session_id="sess-1",
        )
    assert exc.value.code == "instance_not_found"


@pytest.mark.asyncio
async def test_router_jiuwenswarm_creates_sandbox_then_uploads(tmp_path) -> None:
    backend = _FileBackend()
    workspace_root = tmp_path / "ws"
    (workspace_root / "user-1").mkdir(parents=True)
    router = AgentOSRouterClient(
        backend,  # type: ignore[arg-type]
        _FakeRegistry(),  # type: ignore[arg-type]
        AgentManager(creating_timeout_seconds=1.0, key_fields=("user_id", "agent_type", "session_id")),
        workspace_root=str(workspace_root),
    )

    payload = await router.upload_container_file(
        user_id="user-1",
        path="docs/report.md",
        content=b"# hello",
        agent_type=BUILTIN_AGENT_TYPE,
        session_id="sess-1",
    )

    assert backend.create_calls == 1
    assert payload == {
        "success": True,
        "path": "/home/agentos/docs/report.md",
        "size": 7,
    }
    assert backend.upload_calls[0]["instance_id"] == "sbx-1"
    assert backend.upload_calls[0]["path"] == "/home/agentos/docs/report.md"


@pytest.mark.asyncio
async def test_router_jiuwenswarm_default_agent_type_creates_sandbox(tmp_path) -> None:
    backend = _FileBackend()
    workspace_root = tmp_path / "ws"
    (workspace_root / "user-1").mkdir(parents=True)
    router = AgentOSRouterClient(
        backend,  # type: ignore[arg-type]
        _FakeRegistry(),  # type: ignore[arg-type]
        AgentManager(creating_timeout_seconds=1.0, key_fields=("user_id", "agent_type", "session_id")),
        workspace_root=str(workspace_root),
    )

    instance_id = await router.resolve_instance_id_for_files(
        user_id="user-1",
        session_id="sess-1",
    )

    assert instance_id == "sbx-1"
    assert backend.create_calls == 1


@asynccontextmanager
async def _adx_files(handle, invoke):
    config = AdxRouterConfig(
        ingress_url="https://adx.test", tenant="agentos", token="platform",
        router=RouterConfig(registry=RegistryConfig()),
    )
    control = AdxControlClient(config, transport=httpx.MockTransport(handle))
    router = AdxRouterClient(config, control_client=control)
    router._resolve_file_runtime = AsyncMock(return_value=("inst-1", None))
    control.invoke = AsyncMock(side_effect=invoke)
    try:
        yield router
    finally:
        await control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "code", "expected"), [
    (404, "FILE_NOT_FOUND", "file_not_found"),
    (404, "INSTANCE_NOT_FOUND", "instance_not_found"),
    (413, "", "FILE_TOO_LARGE"),
])
async def test_adx_transfer_preserves_backend_error(status, code, expected):
    def handle(request):
        return httpx.Response(status, json={"error": {"code": code}})
    async with _adx_files(handle, lambda *_: {"size": 1, "type": "file"}) as router:
        with pytest.raises(AgentOSFileTransferError) as exc:
            if status == 413:
                await router.upload_container_file(user_id="user-1", path="report", content=b"x")
            else:
                await router.download_container_file(user_id="user-1", path="/home/agentos/report")
    assert exc.value.code == expected
    assert exc.value.status_code == status


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [0, 10, 1000])
async def test_adx_download_range_and_eof(offset):
    path = "/home/agentos/docs/report.pdf"

    def handle(request):
        assert request.url.path == "/direct/inst-1/download"
        assert request.url.params["path"] == path
        assert request.headers["authorization"] == "Bearer platform"
        assert offset == 0 and request.headers["range"] == "bytes=0-4"
        return httpx.Response(206, content=b"12345", headers={"Content-Range": "bytes 0-4/10"})

    async with _adx_files(handle, lambda *_: {"size": 10, "type": "file"}) as router:
        chunk = await router.download_container_file(user_id="user-1", path=path, offset=offset, limit=5)
    assert chunk.data == (b"12345" if offset == 0 else b"")
    assert chunk.offset == offset
    assert chunk.size == 10
    assert chunk.eof is (offset >= 10)
    assert chunk.content_type == "application/pdf"


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_size", [2, 3])
async def test_adx_upload_publishes_only_after_confirming_size(reported_size):
    def handle(request):
        assert request.method == "POST" and request.url.path == "/direct/inst-1/upload"
        assert request.headers["authorization"] == "Bearer platform"
        assert request.content == b"abc"
        return httpx.Response(200, json={"size": reported_size})

    async with _adx_files(handle, lambda *_: {"exit_code": 0}) as router:
        upload = router.upload_container_file(user_id="user-1", path="a.md", content=b"abc",
                                            auth_headers={"Authorization": "Bearer business-token"})
        if reported_size == 3:
            assert await upload == {"success": True, "path": "/home/agentos/a.md", "size": 3}
        else:
            with pytest.raises(AgentOSFileTransferError, match="Short ADX upload"):
                await upload
        actions = [call.args[1] for call in router._control.invoke.await_args_list]
        assert ("file.rename" in actions) == (reported_size == 3)
        assert actions[-1] == "file.remove"


@pytest.mark.asyncio
async def test_adx_list_uses_requested_directory_and_depth():
    def invoke(environment, action, args):
        assert environment == "inst-1" and action == "file.list"
        assert args == {"path": "/home/agentos/docs", "depth": 2}
        return {"entries": [{"path": "/home/agentos/docs/a.md", "name": "a.md", "type": "file"}]}

    async with _adx_files(lambda _: pytest.fail("listing uses invoke"), invoke) as router:
        entries = await router.list_container_files(user_id="u", dir_path="/home/agentos/docs",
                                                    recursive=True, max_depth=2)
    assert [entry["name"] for entry in entries] == ["a.md"]
    assert entries[0]["is_directory"] is False


class _RecordingChannel:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.responses: list[dict[str, Any]] = []
        self.container_file_client = None

    def register_method(self, method: str, handler, *, local_only=False) -> None:
        self.handlers[method] = handler

    def on_connect(self, _handler) -> None:
        return None

    async def send_response(self, ws, req_id, *, ok: bool, payload=None, error=None, code=None) -> None:
        self.responses.append(
            {
                "req_id": req_id,
                "ok": ok,
                "payload": payload or {},
                "error": error,
                "code": code,
            }
        )


@pytest.mark.asyncio
async def test_register_web_handlers_binds_container_file_client_for_agentos_router() -> None:
    router = _make_router_with_runtime()
    router_channel = _RecordingChannel()
    app_web_handlers._register_web_handlers(  # noqa: SLF001 - test helper
        app_web_handlers.WebHandlersBindParams(
            channel=router_channel,
            agent_client=router,
        )
    )
    assert router_channel.container_file_client is router
    assert "file.raw.read" not in router_channel.handlers
    assert "file.raw.write" not in router_channel.handlers
    assert "file.content.write" not in router_channel.handlers

    ws_client = WebSocketAgentServerClient()
    plain_channel = _RecordingChannel()
    app_web_handlers._register_web_handlers(  # noqa: SLF001 - test helper
        app_web_handlers.WebHandlersBindParams(
            channel=plain_channel,
            agent_client=ws_client,
        )
    )
    assert plain_channel.container_file_client is None


@pytest.mark.asyncio
async def test_container_file_http_raw_file_returns_binary_and_json() -> None:
    import httpx

    from agent_gateway.channel_manager.base import RobotMessageRouter
    from agent_gateway.channel_manager.web.web_channel_app import build_web_channel_app
    from agent_gateway.channel_manager.web.web_connect import WebChannelConfig

    router = _make_router_with_runtime()
    router.download_container_file = AsyncMock(  # type: ignore[method-assign]
        return_value=AgentFileDownloadChunk(
            data=b"abc",
            path="/home/agentos/uploads/a.bin",
            offset=0,
            chunk_size=3,
            size=3,
            content_type="application/octet-stream",
            eof=True,
        )
    )
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.container_file_client = router
    app = build_web_channel_app(channel)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        binary = await client.get(
            "/file-api/raw-file",
            params={
                "path": "/home/agentos/uploads/a.bin",
                "user_id": "user-1",
            },
            headers={"Authorization": "Bearer tok-1"},
        )
        json_resp = await client.get(
            "/file-api/raw-file",
            params={
                "path": "/home/agentos/uploads/a.bin",
                "user_id": "user-1",
                "format": "json",
            },
            headers={"Authorization": "Bearer tok-1"},
        )
    assert binary.status_code == 200
    assert binary.content == b"abc"
    assert json_resp.status_code == 200
    body = json_resp.json()
    assert body["data_base64"] == base64.b64encode(b"abc").decode("ascii")
    assert body["eof"] is True
    kwargs = router.download_container_file.await_args.kwargs
    assert kwargs["user_id"] == "user-1"
    assert kwargs["session_id"] == ""
    assert "auth_headers" not in kwargs


@pytest.mark.asyncio
async def test_container_file_http_raw_file_offset_beyond_eof_returns_empty() -> None:
    import httpx

    from agent_gateway.channel_manager.base import RobotMessageRouter
    from agent_gateway.channel_manager.web.web_channel_app import build_web_channel_app
    from agent_gateway.channel_manager.web.web_connect import WebChannelConfig

    router = _make_router_with_runtime()
    router.download_container_file = AsyncMock(  # type: ignore[method-assign]
        return_value=AgentFileDownloadChunk(
            data=b"",
            path="/home/agentos/uploads/a.bin",
            offset=10,
            chunk_size=0,
            size=10,
            content_type="application/octet-stream",
            eof=True,
        )
    )
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.container_file_client = router
    app = build_web_channel_app(channel)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        binary = await client.get(
            "/file-api/raw-file",
            params={
                "path": "/home/agentos/uploads/a.bin",
                "user_id": "user-1",
                "offset": 10,
                "limit": 5,
            },
            headers={"Authorization": "Bearer tok-1"},
        )
        json_resp = await client.get(
            "/file-api/raw-file",
            params={
                "path": "/home/agentos/uploads/a.bin",
                "user_id": "user-1",
                "offset": 10,
                "limit": 5,
                "format": "json",
            },
            headers={"Authorization": "Bearer tok-1"},
        )

    assert binary.status_code == 200
    assert binary.content == b""
    assert json_resp.status_code == 200
    body = json_resp.json()
    assert body["chunk_size"] == 0
    assert body["data_base64"] == ""
    assert body["eof"] is True
    assert body["size"] == 10
    assert body["offset"] == 10


@pytest.mark.asyncio
async def test_container_file_http_raw_file_missing_path_is_file_not_found() -> None:
    import httpx

    from agent_gateway.channel_manager.base import RobotMessageRouter
    from agent_gateway.channel_manager.web.web_channel_app import build_web_channel_app
    from agent_gateway.channel_manager.web.web_connect import WebChannelConfig

    router = _make_router_with_runtime()
    router.download_container_file = AsyncMock(  # type: ignore[method-assign]
        side_effect=AgentOSFileTransferError("directory not found", code="file_not_found")
    )
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.container_file_client = router
    app = build_web_channel_app(channel)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/file-api/raw-file",
            params={"path": "/home/agentos/missing.bin", "user_id": "user-1"},
            headers={"Authorization": "Bearer tok-1"},
        )
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "file_not_found"
    assert body["code"] != "instance_not_found"


@pytest.mark.asyncio
async def test_container_file_http_downloads_sent_file_from_token() -> None:
    import httpx

    from agent_gateway.channel_manager.base import RobotMessageRouter
    from agent_gateway.channel_manager.web.web_channel_app import build_web_channel_app
    from agent_gateway.channel_manager.web.web_connect import WebChannelConfig

    router = _make_router_with_runtime()
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
    payload = base64.urlsafe_b64encode(
        json.dumps({"path": "/home/agentos/reports/result.txt", "sid": "session-1"}).encode()
    ).decode().rstrip("=")
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.container_file_client = router
    app = build_web_channel_app(channel)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        head = await client.head(
            f"/file-api/download?token={payload}.signature&user_id=user-1",
            headers={"Authorization": "Bearer tok-1"},
        )
        content = await client.get(
            f"/file-api/download?token={payload}.signature&user_id=user-1",
            headers={"Authorization": "Bearer tok-1"},
        )

    assert head.status_code == 200
    assert head.headers["content-length"] == "6"
    assert content.status_code == 200
    assert content.content == b"report"
    assert content.headers["content-disposition"].startswith("attachment;")
    kwargs = router.download_container_file.await_args.kwargs
    assert kwargs["user_id"] == "user-1"
    assert kwargs["session_id"] == "session-1"
    assert kwargs["path"] == "/home/agentos/reports/result.txt"
    assert "auth_headers" not in kwargs


@pytest.mark.asyncio
async def test_container_file_http_routes_verified_download_through_e2a(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from agent_gateway.channel_manager.base import RobotMessageRouter
    from agent_gateway.channel_manager.web.web_channel_app import build_web_channel_app
    from agent_gateway.channel_manager.web.web_connect import WebChannelConfig
    from agent_gateway.routing import e2a_proxy

    content = b"x" * (512 * 1024 + 17)
    calls: list[dict[str, Any]] = []

    async def _fetch(**kwargs: Any) -> tuple[bool, dict[str, Any]]:
        calls.append(kwargs)
        params = kwargs["params"]
        offset = int(params["offset"])
        limit = int(params["limit"])
        data = content[offset:offset + limit]
        return True, {
            "data_base64": base64.b64encode(data).decode("ascii"),
            "offset": offset,
            "chunk_size": len(data),
            "size": len(content),
            "eof": offset + len(data) >= len(content),
            "name": "approved report.txt",
            "mime_type": "text/plain",
        }

    monkeypatch.setattr(e2a_proxy, "fetch_agent_unary", _fetch)
    router = _make_router_with_runtime()
    router.download_container_file = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("verified download must not use generic container path")
    )
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "kind": "verified_asset_v1",
                "path": "/tmp/sealed.txt",
                "sid": "session-1",
            }
        ).encode()
    ).decode().rstrip("=")
    channel = WebChannel(
        WebChannelConfig(enabled=True),
        RobotMessageRouter(),
        agent_client=router,
    )
    channel.container_file_client = router
    app = build_web_channel_app(channel)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        head = await client.head(
            f"/file-api/download?token={payload}.signature&user_id=user-a"
        )
        partial = await client.get(
            f"/file-api/download?token={payload}.signature&user_id=user-a",
            headers={"Range": "bytes=2-7"},
        )
        full = await client.get(
            f"/file-api/download?token={payload}.signature&user_id=user-a"
        )

    assert head.status_code == 200
    assert head.headers["content-disposition"] == (
        "attachment; filename*=UTF-8''approved%20report.txt"
    )
    assert partial.status_code == 206
    assert partial.content == content[2:8]
    assert partial.headers["content-range"] == f"bytes 2-7/{len(content)}"
    assert partial.headers["cache-control"] == "no-store"
    assert full.status_code == 200
    assert full.content == content
    assert [int(call["params"]["limit"]) for call in calls[-3:]] == [
        1,
        512 * 1024,
        17,
    ]
    assert {call["user_id"] for call in calls} == {"user-a"}
    assert {call["session_id"] for call in calls} == {"session-1"}
    router.download_container_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_container_file_http_rejects_verified_chunk_past_declared_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from agent_gateway.channel_manager.base import RobotMessageRouter
    from agent_gateway.channel_manager.web.web_channel_app import build_web_channel_app
    from agent_gateway.channel_manager.web.web_connect import WebChannelConfig
    from agent_gateway.routing import e2a_proxy

    async def _fetch(**kwargs: Any) -> tuple[bool, dict[str, Any]]:
        offset = int(kwargs["params"]["offset"])
        data = b"ab"
        return True, {
            "data_base64": base64.b64encode(data).decode("ascii"),
            "offset": offset,
            "chunk_size": len(data),
            "size": 1,
            "eof": True,
            "name": "report.txt",
            "mime_type": "text/plain",
        }

    monkeypatch.setattr(e2a_proxy, "fetch_agent_unary", _fetch)
    router = _make_router_with_runtime()
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "kind": "verified_asset_v1",
                "path": "/tmp/sealed.txt",
                "sid": "session-1",
            }
        ).encode()
    ).decode().rstrip("=")
    channel = WebChannel(
        WebChannelConfig(enabled=True),
        RobotMessageRouter(),
        agent_client=router,
    )
    channel.container_file_client = router
    app = build_web_channel_app(channel)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/file-api/download?token={payload}.signature&user_id=user-a"
        )

    assert response.status_code == 502
    assert response.json()["error"] == "invalid verified download response"


@pytest.mark.asyncio
async def test_container_file_http_verified_cross_user_rejection_has_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from agent_gateway.channel_manager.base import RobotMessageRouter
    from agent_gateway.channel_manager.web.web_channel_app import build_web_channel_app
    from agent_gateway.channel_manager.web.web_connect import WebChannelConfig
    from agent_gateway.routing import e2a_proxy

    async def _reject(**kwargs: Any) -> tuple[bool, dict[str, Any]]:
        assert kwargs["user_id"] == "user-b"
        return False, {"error": "verified download token rejected", "code": "FORBIDDEN"}

    monkeypatch.setattr(e2a_proxy, "fetch_agent_unary", _reject)
    router = _make_router_with_runtime()
    router.download_container_file = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("rejected verified token must not fall back")
    )
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "kind": "verified_asset_v1",
                "path": "/tmp/user-a.txt",
                "sid": "session-a",
            }
        ).encode()
    ).decode().rstrip("=")
    channel = WebChannel(
        WebChannelConfig(enabled=True),
        RobotMessageRouter(),
        agent_client=router,
    )
    channel.container_file_client = router
    app = build_web_channel_app(channel)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/file-api/download?token={payload}.signature&user_id=user-b"
        )

    assert response.status_code == 403
    router.download_container_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_container_file_http_upload_multipart() -> None:
    import httpx

    from agent_gateway.channel_manager.base import RobotMessageRouter
    from agent_gateway.channel_manager.web.web_channel_app import build_web_channel_app
    from agent_gateway.channel_manager.web.web_connect import WebChannelConfig

    router = _make_router_with_runtime()
    router.upload_container_file = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "success": True,
            "path": "/home/agentos/a.pdf",
            "size": 4,
        }
    )
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.container_file_client = router
    app = build_web_channel_app(channel)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/file-api/upload",
            data={"user_id": "user-1"},
            files={"file": ("a.pdf", b"%PDF", "application/pdf")},
            headers={"Authorization": "Bearer tok-1"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["files"][0]["path"] == "/home/agentos/a.pdf"
    assert body["files"][0]["filename"] == "a.pdf"
    assert body["errors"] == []
    kwargs = router.upload_container_file.await_args.kwargs
    assert kwargs["content"] == b"%PDF"
    assert kwargs["path"] == "a.pdf"
    assert kwargs["dir_prefix"] == ""
    assert kwargs["user_id"] == "user-1"
    assert kwargs["session_id"] == ""


@pytest.mark.asyncio
async def test_container_file_http_upload_multipart_with_dir() -> None:
    import httpx

    from agent_gateway.channel_manager.base import RobotMessageRouter
    from agent_gateway.channel_manager.web.web_channel_app import build_web_channel_app
    from agent_gateway.channel_manager.web.web_connect import WebChannelConfig

    router = _make_router_with_runtime()
    router.upload_container_file = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "success": True,
            "path": "/home/agentos/docs/a.pdf",
            "size": 4,
        }
    )
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.container_file_client = router
    app = build_web_channel_app(channel)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/file-api/upload",
            data={"user_id": "user-1", "dir": "docs"},
            files={"file": ("a.pdf", b"%PDF", "application/pdf")},
            headers={"Authorization": "Bearer tok-1"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["files"][0]["path"] == "/home/agentos/docs/a.pdf"
    kwargs = router.upload_container_file.await_args.kwargs
    assert kwargs["dir_prefix"] == "docs"
    assert kwargs["path"] == "a.pdf"


@pytest.mark.asyncio
async def test_container_file_http_file_content_text() -> None:
    import httpx

    from agent_gateway.channel_manager.base import RobotMessageRouter
    from agent_gateway.channel_manager.web.web_channel_app import build_web_channel_app
    from agent_gateway.channel_manager.web.web_connect import WebChannelConfig

    router = _make_router_with_runtime()
    router.upload_container_file = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "success": True,
            "path": "/home/agentos/notes.md",
            "size": 7,
        }
    )
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.container_file_client = router
    app = build_web_channel_app(channel)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/file-api/file-content",
            json={
                "path": "notes.md",
                "content": "# Hello",
                "user_id": "user-1",
            },
            headers={"X-Token": "tok-2"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    kwargs = router.upload_container_file.await_args.kwargs
    assert kwargs["content"] == b"# Hello"
    assert kwargs["session_id"] == ""
    assert "auth_headers" not in kwargs


@pytest.mark.asyncio
async def test_container_file_http_absent_without_router_client() -> None:
    import httpx

    from agent_gateway.channel_manager.base import RobotMessageRouter
    from agent_gateway.channel_manager.web.web_channel_app import build_web_channel_app
    from agent_gateway.channel_manager.web.web_connect import WebChannelConfig

    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    app = build_web_channel_app(channel)
    paths = {getattr(r, "path", None) for r in app.router.routes}
    assert "/file-api/upload" not in paths

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/file-api/raw-file",
            params={"path": "/x", "user_id": "u"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_container_file_http_list_files_and_markdown() -> None:
    import httpx

    from agent_gateway.channel_manager.base import RobotMessageRouter
    from agent_gateway.channel_manager.web.container_file_http import (
        map_container_list_files,
        map_container_list_markdown,
    )
    from agent_gateway.channel_manager.web.web_channel_app import build_web_channel_app
    from agent_gateway.channel_manager.web.web_connect import WebChannelConfig

    backend_items = [
        {
            "name": "README.md",
            "path": "/home/agentos/README.md",
            "size": 256,
            "is_directory": False,
            "modified_time": "2026-08-08T10:31:00.000000",
            "type": ".md",
        },
        {
            "name": "logs",
            "path": "/home/agentos/logs",
            "size": 0,
            "is_directory": True,
            "modified_time": "2026-08-08T10:29:00.000000",
            "type": None,
        },
        {
            "name": "app.py",
            "path": "/home/agentos/app.py",
            "size": 1024,
            "is_directory": False,
            "modified_time": "2026-08-08T10:30:00.000000",
            "type": ".py",
        },
        {
            "name": "notes.MDX",
            "path": "/home/agentos/notes.MDX",
            "size": 12,
            "is_directory": False,
            "modified_time": "2026-08-08T10:32:00.000000",
            "type": ".MDX",
        },
    ]
    mapped = map_container_list_files(backend_items)
    assert [item["name"] for item in mapped] == ["logs", "app.py", "notes.MDX", "README.md"]
    assert mapped[0]["isDirectory"] is True
    assert mapped[0]["isMarkdown"] is False
    assert mapped[-1]["isMarkdown"] is True
    markdown = map_container_list_markdown(backend_items)
    assert [item["name"] for item in markdown] == ["notes.MDX", "README.md"]
    assert "isDirectory" not in markdown[0]

    router = _make_router_with_runtime()
    router.list_container_files = AsyncMock(return_value=backend_items)  # type: ignore[method-assign]
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.container_file_client = router
    app = build_web_channel_app(channel)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing_dir = await client.get(
            "/file-api/list-files",
            params={"user_id": "user-1"},
            headers={"Authorization": "Bearer tok-1"},
        )
        listed = await client.get(
            "/file-api/list-files",
            params={
                "dir": "/home/agentos",
                "user_id": "user-1",
                "recursive": "true",
                "max_depth": "3",
            },
            headers={"Authorization": "Bearer tok-1"},
        )
        markdown_resp = await client.get(
            "/file-api/list-markdown",
            params={"dir": "/home/agentos", "user_id": "user-1"},
            headers={"X-User-Id": "user-1", "Authorization": "Bearer tok-1"},
        )

    assert missing_dir.status_code == 400
    assert missing_dir.json()["error"] == "missing_dir"
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()["files"]] == [
        "logs",
        "app.py",
        "notes.MDX",
        "README.md",
    ]
    assert markdown_resp.status_code == 200
    assert [item["name"] for item in markdown_resp.json()["files"]] == ["notes.MDX", "README.md"]
    kwargs = router.list_container_files.await_args_list[0].kwargs
    assert kwargs["dir_path"] == "/home/agentos"
    assert kwargs["recursive"] is True
    assert kwargs["max_depth"] == 3
    assert kwargs["user_id"] == "user-1"
    assert kwargs["session_id"] == ""
    assert "auth_headers" not in kwargs


@pytest.mark.asyncio
async def test_container_file_http_list_missing_dir_is_file_not_found() -> None:
    import httpx

    from agent_gateway.channel_manager.base import RobotMessageRouter
    from agent_gateway.channel_manager.web.web_channel_app import build_web_channel_app
    from agent_gateway.channel_manager.web.web_connect import WebChannelConfig

    router = _make_router_with_runtime()
    router.list_container_files = AsyncMock(  # type: ignore[method-assign]
        side_effect=AgentOSFileTransferError("directory not found", code="file_not_found")
    )
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.container_file_client = router
    app = build_web_channel_app(channel)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(
            "/file-api/list-files",
            params={"dir": "/home/agentos/missing", "user_id": "user-1"},
            headers={"Authorization": "Bearer tok-1"},
        )
        markdown = await client.get(
            "/file-api/list-markdown",
            params={"dir": "/home/agentos/missing", "user_id": "user-1"},
            headers={"Authorization": "Bearer tok-1"},
        )
    assert listed.status_code == 404
    assert listed.json()["code"] == "file_not_found"
    assert markdown.status_code == 404
    assert markdown.json()["code"] == "file_not_found"


@pytest.mark.asyncio
async def test_container_file_http_mkdir() -> None:
    import httpx

    from agent_gateway.channel_manager.base import RobotMessageRouter
    from agent_gateway.channel_manager.web.web_channel_app import build_web_channel_app
    from agent_gateway.channel_manager.web.web_connect import WebChannelConfig

    router = _make_router_with_runtime()
    router.mkdir_container_dir = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "success": True,
            "path": "/home/agentos/sub",
            "created": True,
        }
    )
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.container_file_client = router
    app = build_web_channel_app(channel)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing_path = await client.post(
            "/file-api/mkdir",
            params={"user_id": "user-1"},
            headers={"Authorization": "Bearer tok-1"},
        )
        created = await client.post(
            "/file-api/mkdir",
            params={
                "path": "/home/agentos/sub",
                "mode": "0755",
                "recursive": "true",
                "user_id": "user-1",
                "session_id": "sess-1",
            },
            headers={"Authorization": "Bearer tok-1"},
        )

    assert missing_path.status_code == 400
    assert missing_path.json()["error"] == "missing_path"
    assert created.status_code == 200
    assert created.json() == {
        "success": True,
        "path": "/home/agentos/sub",
        "created": True,
    }
    kwargs = router.mkdir_container_dir.await_args.kwargs
    assert kwargs["path"] == "/home/agentos/sub"
    assert kwargs["mode"] == "0755"
    assert kwargs["recursive"] is True
    assert kwargs["user_id"] == "user-1"
    assert kwargs["session_id"] == "sess-1"
    assert "auth_headers" not in kwargs
