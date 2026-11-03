# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``ops.http.fetch``: allowlist, budgets, redirects and error codes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx

from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig, HttpPolicy
from jiuwenswarm.gateway.extensions_v2.loader import LoadedPackage
from jiuwenswarm.gateway.extensions_v2.models import (
    EVENT_GATEWAY_REQUEST,
    GatewayRequestOutput,
    HttpFetchInput,
)
from jiuwenswarm.gateway.extensions_v2.registry import HandlerDeclaration
from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime


class Channel:
    """Minimal WebChannel stand-in; this test only needs the ops catalog."""

    channel_id = "web"

    def __init__(self) -> None:
        self.responses: list[dict] = []
        self.events: list[tuple[str, dict]] = []
        self._ids: dict[int, str] = {}

    async def ensure_ws_id(self, ws) -> str:
        key = id(ws)
        if key not in self._ids:
            self._ids[key] = f"ws-{len(self._ids) + 1}"
            setattr(ws, "_jiuwen_ws_id", self._ids[key])
        return self._ids[key]

    def connection_user_id(self, ws):
        return None

    async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append({"id": req_id, "ok": ok, "payload": payload or {}})

    async def send_event(self, ws, event, payload):
        self.events.append((event, payload))


class FakeResponse:
    def __init__(self, status=200, content=b"{}", headers=None) -> None:
        self.status_code = status
        self.content = content
        self.headers = headers or {"content-type": "application/json"}
        self.is_redirect = 300 <= status < 400

    async def aiter_bytes(self):
        if self.content:
            yield self.content


class FakeStream:
    async def __aenter__(self):
        if FakeClient.error is not None:
            raise FakeClient.error
        return FakeClient.responses.pop(0)

    async def __aexit__(self, *exc_info):
        return False


class FakeClient:
    """Record every request and replay queued responses."""

    requests: list[dict] = []
    responses: list[FakeResponse] = []
    error: Exception | None = None
    trust_env: bool = True

    def __init__(self, timeout=None, follow_redirects=False, trust_env=True) -> None:
        self.timeout = timeout
        self.follow_redirects = follow_redirects
        self.trust_env = trust_env
        FakeClient.trust_env = trust_env

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def stream(self, method, url, headers=None, content=None, timeout=None):
        FakeClient.requests.append(
            {"method": method, "url": url, "headers": headers, "content": content, "timeout": self.timeout}
        )
        return FakeStream()


def _package(handler) -> LoadedPackage:
    return LoadedPackage(
        package_id="auth",
        version="1.0.0",
        root=Path("/tmp/auth"),
        handlers=(HandlerDeclaration(EVENT_GATEWAY_REQUEST, handler, {}, "local-1"),),
    )


async def _runtime(tmp_path: Path, policy: HttpPolicy) -> GatewayRuntime:
    runtime = GatewayRuntime(
        channel=Channel(),
        config=GatewayExtensionConfig(install_dir=tmp_path, http=policy),
    )
    await runtime.start()
    return runtime


def _fetch_handler(record: dict):
    async def handler(event, ops, next_handler):
        record["output"] = await ops.http.fetch(record["request"])
        return GatewayRequestOutput(handled=True, route="consume", response={})

    return handler


def _install_fakes(monkeypatch, *responses: FakeResponse, error: Exception | None = None) -> None:
    FakeClient.requests = []
    FakeClient.responses = list(responses)
    FakeClient.error = error
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)


async def test_empty_allowlist_denies_every_host(tmp_path, monkeypatch):
    _install_fakes(monkeypatch)
    runtime = await _runtime(tmp_path, HttpPolicy())
    record: dict = {"request": HttpFetchInput("https://auth.example.com/login")}
    runtime.publish(_package(_fetch_handler(record)))

    await runtime.intercept(SimpleNamespace(closed=False), "r1", "chat.send", {})

    assert record["output"].code == "HTTP_HOST_DENIED"
    assert record["output"].status == 0
    assert FakeClient.requests == []


async def test_host_port_entry_matches_only_that_port(tmp_path, monkeypatch):
    _install_fakes(monkeypatch, FakeResponse())
    policy = HttpPolicy(allowed_hosts=("auth.example.com:9443",))
    allowed: dict = {"request": HttpFetchInput("https://auth.example.com:9443/login")}
    runtime = await _runtime(tmp_path, policy)
    runtime.publish(_package(_fetch_handler(allowed)))

    await runtime.intercept(SimpleNamespace(closed=False), "r1", "chat.send", {})

    assert allowed["output"].code is None
    assert allowed["output"].status == 200

    denied: dict = {"request": HttpFetchInput("https://auth.example.com:9444/login")}
    other = await _runtime(tmp_path, policy)
    other.publish(_package(_fetch_handler(denied)))

    await other.intercept(SimpleNamespace(closed=False), "r2", "chat.send", {})

    assert denied["output"].code == "HTTP_HOST_DENIED"


async def test_allowed_host_returns_the_parsed_json_body(tmp_path, monkeypatch):
    _install_fakes(monkeypatch, FakeResponse(content=b'{"user_id": "u_1"}'))
    runtime = await _runtime(tmp_path, HttpPolicy(allowed_hosts=("auth.example.com",)))
    record: dict = {
        "request": HttpFetchInput(
            "https://auth.example.com/login",
            method="POST",
            body={"username": "u1", "password": "p1"},
            timeout_ms=2000,
        )
    }
    runtime.publish(_package(_fetch_handler(record)))

    await runtime.intercept(SimpleNamespace(closed=False), "r1", "chat.send", {})

    output = record["output"]
    assert output.code is None
    assert output.status == 200
    assert output.body == {"user_id": "u_1"}
    assert FakeClient.requests[0]["method"] == "POST"
    assert FakeClient.requests[0]["content"] == b'{"username": "u1", "password": "p1"}'
    assert FakeClient.requests[0]["timeout"] == 2.0


async def test_response_over_the_body_budget_is_reported_not_truncated(tmp_path, monkeypatch):
    _install_fakes(monkeypatch, FakeResponse(content=b"x" * 32))
    runtime = await _runtime(
        tmp_path,
        HttpPolicy(allowed_hosts=("auth.example.com",), max_body_bytes=8),
    )
    record: dict = {"request": HttpFetchInput("https://auth.example.com/login")}
    runtime.publish(_package(_fetch_handler(record)))

    await runtime.intercept(SimpleNamespace(closed=False), "r1", "chat.send", {})

    assert record["output"].code == "HTTP_BODY_TOO_LARGE"
    assert record["output"].body is None
    assert record["output"].status == 200


async def test_timeout_is_mapped_to_a_code(tmp_path, monkeypatch):
    _install_fakes(monkeypatch, error=httpx.TimeoutException("slow"))
    runtime = await _runtime(tmp_path, HttpPolicy(allowed_hosts=("auth.example.com",)))
    record: dict = {"request": HttpFetchInput("https://auth.example.com/login")}
    runtime.publish(_package(_fetch_handler(record)))

    await runtime.intercept(SimpleNamespace(closed=False), "r1", "chat.send", {})

    assert record["output"].code == "HTTP_TIMEOUT"


async def test_redirect_to_a_denied_host_is_refused(tmp_path, monkeypatch):
    _install_fakes(
        monkeypatch,
        FakeResponse(
            status=302,
            content=b"",
            headers={"location": "https://evil.example.com/take", "content-type": "text/plain"},
        ),
    )
    runtime = await _runtime(tmp_path, HttpPolicy(allowed_hosts=("auth.example.com",)))
    record: dict = {"request": HttpFetchInput("https://auth.example.com/login", follow_redirects=True)}
    runtime.publish(_package(_fetch_handler(record)))

    await runtime.intercept(SimpleNamespace(closed=False), "r1", "chat.send", {})

    assert record["output"].code == "HTTP_HOST_DENIED"
    assert record["output"].status == 302
    assert len(FakeClient.requests) == 1


async def test_redirects_are_not_followed_by_default(tmp_path, monkeypatch):
    _install_fakes(
        monkeypatch,
        FakeResponse(status=302, content=b"", headers={"location": "https://auth.example.com/next"}),
    )
    runtime = await _runtime(tmp_path, HttpPolicy(allowed_hosts=("auth.example.com",)))
    record: dict = {"request": HttpFetchInput("https://auth.example.com/login")}
    runtime.publish(_package(_fetch_handler(record)))

    await runtime.intercept(SimpleNamespace(closed=False), "r1", "chat.send", {})

    assert record["output"].code is None
    assert record["output"].status == 302
    assert len(FakeClient.requests) == 1


async def test_request_failure_is_reported_as_a_code(tmp_path, monkeypatch):
    _install_fakes(monkeypatch, error=httpx.ConnectError("refused"))
    runtime = await _runtime(tmp_path, HttpPolicy(allowed_hosts=("auth.example.com",)))
    record: dict = {"request": HttpFetchInput("https://auth.example.com/login")}
    runtime.publish(_package(_fetch_handler(record)))

    await runtime.intercept(SimpleNamespace(closed=False), "r1", "chat.send", {})

    assert record["output"].code == "HTTP_REQUEST_FAILED"


async def test_client_ignores_proxy_environment(tmp_path, monkeypatch):
    _install_fakes(monkeypatch, FakeResponse())
    runtime = await _runtime(tmp_path, HttpPolicy(allowed_hosts=("auth.example.com",)))
    record: dict = {"request": HttpFetchInput("https://auth.example.com/login")}
    runtime.publish(_package(_fetch_handler(record)))

    await runtime.intercept(SimpleNamespace(closed=False), "r1", "chat.send", {})

    assert FakeClient.trust_env is False


async def test_cross_host_redirect_drops_credentials_and_switches_to_get(tmp_path, monkeypatch):
    _install_fakes(
        monkeypatch,
        FakeResponse(status=302, content=b"", headers={"location": "https://sso.example.com/next"}),
        FakeResponse(content=b'{"ok": true}'),
    )
    policy = HttpPolicy(allowed_hosts=("auth.example.com", "sso.example.com"))
    record: dict = {
        "request": HttpFetchInput(
            "https://auth.example.com/login",
            method="POST",
            headers={"Authorization": "Bearer t", "content-type": "application/json"},
            body={"password": "p1"},
            follow_redirects=True,
        )
    }
    runtime = await _runtime(tmp_path, policy)
    runtime.publish(_package(_fetch_handler(record)))

    await runtime.intercept(SimpleNamespace(closed=False), "r1", "chat.send", {})

    assert record["output"].code is None
    second = FakeClient.requests[1]
    assert second["url"] == "https://sso.example.com/next"
    assert second["method"] == "GET"
    assert second["content"] is None
    assert second["headers"] == {"content-type": "application/json"}


async def test_ipv6_and_default_port_entries_match(tmp_path, monkeypatch):
    _install_fakes(monkeypatch, FakeResponse(), FakeResponse())
    policy = HttpPolicy(allowed_hosts=("[::1]:8080", "auth.example.com:443"))
    ipv6: dict = {"request": HttpFetchInput("http://[::1]:8080/login")}
    runtime = await _runtime(tmp_path, policy)
    runtime.publish(_package(_fetch_handler(ipv6)))

    await runtime.intercept(SimpleNamespace(closed=False), "r1", "chat.send", {})

    default_port: dict = {"request": HttpFetchInput("https://auth.example.com/login")}
    other = await _runtime(tmp_path, policy)
    other.publish(_package(_fetch_handler(default_port)))

    await other.intercept(SimpleNamespace(closed=False), "r2", "chat.send", {})

    assert ipv6["output"].code is None
    assert ipv6["output"].status == 200
    assert default_port["output"].code is None
    assert default_port["output"].status == 200
