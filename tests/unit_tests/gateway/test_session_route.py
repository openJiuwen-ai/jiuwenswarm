# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RuntimeSessionRouteClient 单测：httpx mock，不启真 Manager。"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from jiuwenswarm.common.security.link_mtls import (
    MTLSDeploymentIdentity,
    LinkMTLSConfig,
    LinkMTLSMode,
)

_EXT_DIR = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "jiuwenclaw-ee"
    / "gateway"
    / "extensions"
    / "runtime_management_extension"
)
if str(_EXT_DIR) not in sys.path:
    sys.path.insert(0, str(_EXT_DIR))

from session_route_client import (  # noqa: E402
    FatalRouteError,
    RetryableRouteError,
    RuntimeSessionRouteClient,
)

_ROUTE_KW = {
    "session_id": "sess-1",
    "group_id": "grp-1",
    "bot_id": "bot-1",
    "request_id": "req-1",
    "user_id": "user-1",
}


def _ok_body() -> dict:
    return {
        "type": "route",
        "metadata": {"request_id": "req-1"},
        "rawdata": {
            "pod_sse_url": "http://10.42.1.23:8080/sse",
            "pod_id": "agentserver-abc",
        },
        "ok": True,
    }


@pytest_asyncio.fixture
async def client_factory():
    clients: list = []
    https: list[httpx.AsyncClient] = []

    def factory(
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        base_url: str = "http://runtime-manager:8091",
        link_mtls_config: LinkMTLSConfig | None = None,
    ) -> RuntimeSessionRouteClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        https.append(http)
        client = RuntimeSessionRouteClient(
            base_url=base_url,
            http_client=http,
            link_mtls_config=link_mtls_config,
        )
        clients.append(client)
        return client

    yield factory
    for client in clients:
        await client.aclose()
    for http in https:
        await http.aclose()


@pytest.mark.asyncio
async def test_route_success(client_factory) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_ok_body())

    result = await client_factory(handler).route(**_ROUTE_KW)
    assert result.pod_sse_url == "http://10.42.1.23:8080/sse"
    assert result.pod_id == "agentserver-abc"
    assert result.request_id == "req-1"
    assert captured["url"] == "http://runtime-manager:8091/api/session/route"
    assert captured["body"]["type"] == "route"
    assert captured["body"]["rawdata"] == {}
    assert captured["body"]["metadata"]["session_id"] == "sess-1"
    assert captured["body"]["metadata"]["bot_id"] == "bot-1"
    assert captured["body"]["metadata"]["user_id"] == "user-1"
    assert captured["body"]["metadata"]["extra"]["group_id"] == "grp-1"
    assert captured["body"]["metadata"]["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_enforce_sends_binding_headers_and_validates_route(
    client_factory,
) -> None:
    identity = MTLSDeploymentIdentity("deployment-1", "binding-1", 3)
    config = LinkMTLSConfig(mode=LinkMTLSMode.ENFORCE, identity=identity)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Jiuwenswarm-Mtls-Binding-Id"] == "binding-1"
        assert request.headers["X-Jiuwenswarm-Mtls-Binding-Epoch"] == "3"
        body = _ok_body()
        body["rawdata"].update(
            {
                "pod_sse_url": "https://agent.example:8766/api/v1",
                "mtls_deployment_id": "deployment-1",
                "mtls_binding_id": "binding-1",
                "mtls_binding_epoch": 3,
            }
        )
        return httpx.Response(200, json=body)

    result = await client_factory(
        handler,
        base_url="https://runtime.example:8091",
        link_mtls_config=config,
    ).route(**_ROUTE_KW)
    assert result.mtls_binding_epoch == 3


@pytest.mark.asyncio
async def test_enforce_rejects_stale_route_binding(client_factory) -> None:
    config = LinkMTLSConfig(
        mode=LinkMTLSMode.ENFORCE,
        identity=MTLSDeploymentIdentity("deployment-1", "binding-1", 3),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = _ok_body()
        body["rawdata"].update(
            {
                "pod_sse_url": "https://agent.example:8766/api/v1",
                "mtls_deployment_id": "deployment-1",
                "mtls_binding_id": "binding-1",
                "mtls_binding_epoch": 2,
            }
        )
        return httpx.Response(200, json=body)

    with pytest.raises(FatalRouteError, match="binding mismatch"):
        await client_factory(
            handler,
            base_url="https://runtime.example:8091",
            link_mtls_config=config,
        ).route(**_ROUTE_KW)


@pytest.mark.asyncio
async def test_route_strips_base_url_slash(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://runtime-manager:8091/api/session/route"
        return httpx.Response(200, json=_ok_body())

    await client_factory(handler, base_url="http://runtime-manager:8091/").route(
        **_ROUTE_KW
    )


@pytest.mark.asyncio
async def test_route_missing_args_skips_http(client_factory) -> None:
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json=_ok_body())

    with pytest.raises(FatalRouteError) as exc:
        await client_factory(handler).route(
            session_id="", group_id="g", bot_id="b", request_id="r"
        )
    assert exc.value.code == "VALIDATION"
    assert called["n"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "retry_after", "exc_type"),
    [
        (503, "SCOPE_QUEUE_FULL", 2, RetryableRouteError),
        (504, "SCOPE_FULL_TIMEOUT", 1, RetryableRouteError),
        (503, "NO_POD_AVAILABLE", 1, RetryableRouteError),
        (503, "CONFIG_NOT_FOUND", None, FatalRouteError),
        (400, "VALIDATION", None, FatalRouteError),
    ],
)
async def test_route_error_codes(
    client_factory, status, code, retry_after, exc_type
) -> None:
    body: dict = {"ok": False, "error_code": code, "error_message": code}
    if retry_after is not None:
        body["retry_after"] = retry_after

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    with pytest.raises(exc_type) as exc:
        await client_factory(handler).route(**_ROUTE_KW)
    assert exc.value.code == code
    assert exc.value.retry_after == retry_after


@pytest.mark.asyncio
async def test_route_connect_error(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(RetryableRouteError) as exc:
        await client_factory(handler).route(**_ROUTE_KW)
    assert exc.value.code == "TRANSPORT"


@pytest.mark.asyncio
async def test_route_success_missing_pod_fields(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "rawdata": {}})

    with pytest.raises(FatalRouteError) as exc:
        await client_factory(handler).route(**_ROUTE_KW)
    assert exc.value.code == "VALIDATION"


@pytest.mark.asyncio
async def test_touch_true_and_false(client_factory) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "rawdata": {"touched": True}})

    client = client_factory(handler)
    assert await client.touch(session_id="sess-1", request_id="req-1") is True
    assert captured["url"] == "http://runtime-manager:8091/api/session/touch"
    assert captured["body"]["type"] == "touch"
    assert captured["body"]["metadata"]["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_touch_expired_returns_false(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "rawdata": {"touched": False}})

    assert (
        await client_factory(handler).touch(session_id="sess-gone", request_id="req-1")
        is False
    )


@pytest.mark.asyncio
async def test_touch_missing_args_skips_http(client_factory) -> None:
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"ok": True, "rawdata": {"touched": True}})

    with pytest.raises(FatalRouteError) as exc:
        await client_factory(handler).touch(session_id="", request_id="req-1")
    assert exc.value.code == "VALIDATION"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_route_accepts_flat_success_body(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"pod_sse_url": "http://10.0.0.1:8080/sse", "pod_id": "pod-1"},
        )

    result = await client_factory(handler).route(**_ROUTE_KW)
    assert result.pod_sse_url == "http://10.0.0.1:8080/sse"
    assert result.pod_id == "pod-1"


@pytest.mark.asyncio
async def test_route_ok_false_on_200(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "error_code": "SCOPE_QUEUE_FULL", "retry_after": 2},
        )

    with pytest.raises(RetryableRouteError) as exc:
        await client_factory(handler).route(**_ROUTE_KW)
    assert exc.value.code == "SCOPE_QUEUE_FULL"
    assert exc.value.retry_after == 2


@pytest.mark.asyncio
async def test_route_internal_5xx_is_retryable(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"ok": False, "error_code": "internal", "error_message": "boom"},
        )

    with pytest.raises(RetryableRouteError) as exc:
        await client_factory(handler).route(**_ROUTE_KW)
    assert exc.value.code == "internal"


@pytest.mark.asyncio
async def test_route_retry_after_header(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"ok": False, "error_code": "NO_POD_AVAILABLE"},
            headers={"Retry-After": "3"},
        )

    with pytest.raises(RetryableRouteError) as exc:
        await client_factory(handler).route(**_ROUTE_KW)
    assert exc.value.retry_after == 3


@pytest.mark.asyncio
async def test_injected_client_not_closed(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body())

    client = client_factory(handler)
    await client.route(**_ROUTE_KW)
    await client.aclose()
    assert client._http.is_closed is False


@pytest.mark.asyncio
async def test_reads_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_RUNTIME_MANAGER_URL", "http://rm.example:8091/")
    client = RuntimeSessionRouteClient()
    assert client._route_url == "http://rm.example:8091/api/session/route"
    assert client._touch_url == "http://rm.example:8091/api/session/touch"
    await client.aclose()


@pytest.mark.asyncio
async def test_blank_url_and_bad_timeout_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_RUNTIME_MANAGER_URL", "   ")
    monkeypatch.setenv("GATEWAY_RUNTIME_MANAGER_TIMEOUT", "not-a-number")
    client = RuntimeSessionRouteClient()
    assert client._route_url == "http://127.0.0.1:8091/api/session/route"
    await client.aclose()
