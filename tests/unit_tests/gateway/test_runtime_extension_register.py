# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""runtime 扩展只在环境变量写成 runtime 时注册。"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_gateway.extensions.loader import ExtensionLoader
from starlette.requests import Request

from e2a.models import E2AEnvelope
from jiuwenswarm.extensions.runtime_management_extension.extension import (
    register_extensions,
    runtime_client_requested,
)
from jiuwenswarm.extensions.runtime_management_extension.invoke_ids import (
    routing_triple_from_envelope,
)
from jiuwenswarm.extensions.runtime_management_extension.pod_rest_map import (
    identity_headers,
)
from jiuwenswarm.server.agent_http_routes import _rebuild_identity_from_headers

_EXTENSION_ROOT = (
    Path(__file__).resolve().parents[3]
    / "jiuwenswarm"
    / "extensions"
    / "runtime_management_extension"
)


class _Registry:
    def __init__(self) -> None:
        self.client = None

    def register_agent_server_client(self, extension) -> None:
        self.client = extension


def test_extension_loader_can_import_the_entry() -> None:
    module = ExtensionLoader._import_module(_EXTENSION_ROOT)
    assert callable(module.register_extensions)


def test_runtime_client_requested_only_for_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_AGENT_CLIENT_MODE", raising=False)
    assert runtime_client_requested() is False
    assert runtime_client_requested({}) is False
    assert runtime_client_requested({"GATEWAY_AGENT_CLIENT_MODE": "websocket"}) is False
    assert runtime_client_requested({"GATEWAY_AGENT_CLIENT_MODE": "http"}) is False
    assert runtime_client_requested({"GATEWAY_AGENT_CLIENT_MODE": " runtime "}) is True


@pytest.mark.asyncio
async def test_register_skips_unless_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_AGENT_CLIENT_MODE", "websocket")
    registry = _Registry()
    assert await register_extensions(registry) == []
    assert registry.client is None


@pytest.mark.asyncio
async def test_register_uses_runtime_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_AGENT_CLIENT_MODE", "runtime")
    registry = _Registry()
    registered = await register_extensions(registry)
    assert len(registered) == 1
    assert registry.client is registered[0]
    assert type(registry.client.get_client()).__name__ == "RuntimeRoutedAgentClient"


def test_identity_headers_come_from_the_extension() -> None:
    envelope = E2AEnvelope(
        request_id="req-1",
        channel="web",
        session_id="sess-1",
        user_id="user-1",
        channel_context={"routing": {"group_id": "g1", "bot_id": "b1"}},
    )
    headers = identity_headers(envelope, accept="application/json")
    assert headers["X-Request-Id"] == "req-1"
    assert headers["X-Session-Id"] == "sess-1"
    assert headers["X-User-Id"] == "user-1"
    assert headers["X-Group-Id"] == "g1"
    assert headers["X-Bot-Id"] == "b1"
    assert "X-Cron-Targets" not in headers
    assert routing_triple_from_envelope(envelope) == ("g1", "b1", "user-1")


def test_identity_headers_forward_cron_targets() -> None:
    envelope = E2AEnvelope(
        request_id="req-cron",
        channel="__cron__",
        session_id="__cron___sess",
        user_id="user-1",
        channel_context={
            "routing": {"group_id": "g1", "bot_id": "b1"},
            "targets": "web",
        },
    )
    headers = identity_headers(envelope, accept="application/json")
    assert headers["X-Channel-Id"] == "__cron__"
    assert headers["X-Cron-Targets"] == "web"


def _http_request(headers: dict[str, str]) -> Request:
    raw = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in headers.items()
    ]
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": raw,
            "client": ("127.0.0.1", 123),
            "server": ("127.0.0.1", 80),
        }
    )


def test_rebuild_identity_restores_cron_targets_metadata() -> None:
    _user_id, metadata = _rebuild_identity_from_headers(
        _http_request(
            {
                "X-User-Id": "user-1",
                "X-Channel-Id": "__cron__",
                "X-Cron-Targets": "web",
            }
        )
    )
    assert _user_id == "user-1"
    assert metadata is not None
    assert metadata["targets"] == "web"
