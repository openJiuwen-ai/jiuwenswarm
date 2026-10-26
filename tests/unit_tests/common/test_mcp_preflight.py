from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from openjiuwen.core.foundation.tool import McpServerConfig

from jiuwenswarm.common.mcp_config import preflight_mcp_server_reachable


class _FakeStreamResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        return None


class _FakeAsyncClient:
    calls: list[tuple[str, str, dict]] = []
    status_code = 200

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return _FakeStreamResponse(self.status_code)

    async def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return SimpleNamespace(status_code=self.status_code)


def _config(client_type: str) -> McpServerConfig:
    return McpServerConfig(
        server_name="demo",
        client_type=client_type,
        server_path="https://example.com/mcp",
        auth_headers={"Authorization": "Bearer token"},
    )


@pytest.fixture(autouse=True)
def _patch_http_client(monkeypatch):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.status_code = 200
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)


@pytest.mark.asyncio
async def test_sse_preflight_uses_get_stream_with_auth_header():
    ok, _reason = await preflight_mcp_server_reachable(_config("sse"))

    assert ok is True
    method, _url, kwargs = _FakeAsyncClient.calls[0]
    assert method == "GET"
    assert kwargs["headers"]["Accept"] == "text/event-stream"
    assert kwargs["headers"]["Authorization"] == "Bearer token"


@pytest.mark.asyncio
async def test_streamable_http_preflight_posts_initialize_payload():
    ok, _reason = await preflight_mcp_server_reachable(
        _config("streamable-http")
    )

    assert ok is True
    method, _url, kwargs = _FakeAsyncClient.calls[0]
    assert method == "POST"
    assert kwargs["json"]["method"] == "initialize"
    assert kwargs["headers"]["Authorization"] == "Bearer token"


@pytest.mark.asyncio
async def test_sse_preflight_rejects_auth_failure():
    _FakeAsyncClient.status_code = 401

    ok, reason = await preflight_mcp_server_reachable(_config("sse"))

    assert ok is False
    assert reason == "http 401 from server"
