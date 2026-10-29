# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio

import pytest

import jiuwenbox.supervisor.win_proxy as win_proxy
from jiuwenbox.models.policy import NetworkRulePolicy
from jiuwenbox.supervisor.win_proxy import (
    EgressFilter,
    _connect_via_upstream,
    _upstream_authority,
    handle_http_forward,
)
from jiuwenbox.supervisor.win_upstream import (
    UpstreamProxy,
    parse_proxy_server_list,
    resolve_upstream,
)

def _no_system(*_args):
    return None


def test_no_proxy_stays_direct():
    assert resolve_upstream(
        "example.com", 443, environ={}, system_lookup=_no_system,
    ) is None


def test_env_proxy_is_used_for_https():
    env = {"HTTPS_PROXY": "http://proxy.example:8080"}
    assert resolve_upstream(
        "example.com", 443, environ=env, system_lookup=_no_system,
    ) == UpstreamProxy("proxy.example", 8080)


def test_no_proxy_star_forces_direct():
    env = {"HTTPS_PROXY": "http://proxy.example:8080", "NO_PROXY": "*"}
    assert resolve_upstream(
        "example.com", 443, environ=env, system_lookup=_no_system,
    ) is None


def test_explicit_direct_ignores_env_and_system():
    env = {
        "JIUWENBOX_UPSTREAM_PROXY": "direct",
        "HTTPS_PROXY": "http://proxy.example:8080",
    }
    assert resolve_upstream(
        "example.com", 443, environ=env, system_lookup=lambda *_a: UpstreamProxy("ignored.example", 1),
    ) is None


def test_own_listen_port_falls_through_to_system_proxy():
    env = {"HTTPS_PROXY": "http://127.0.0.1:60080"}
    found = resolve_upstream(
        "example.com", 443, environ=env,
        system_lookup=lambda *_a: UpstreamProxy("proxy.example", 8080),
    )
    assert found == UpstreamProxy("proxy.example", 8080)


def test_loopback_target_is_direct():
    env = {"HTTP_PROXY": "http://proxy.example:8080"}
    assert resolve_upstream(
        "127.0.0.1", 80, environ=env, system_lookup=_no_system,
    ) is None


def test_registry_proxy_list_picks_https_entry():
    text = "http=proxy-http.example:8080;https=proxy-https.example:8443"
    assert parse_proxy_server_list(text, 443) == UpstreamProxy("proxy-https.example", 8443)
    assert parse_proxy_server_list("PROXY proxy-list.example:8080; DIRECT", 80) == UpstreamProxy(
        "proxy-list.example", 8080,
    )


def test_connect_via_upstream_sends_connect():
    async def scenario():
        seen = {}

        async def handle(reader, writer):
            blob = await reader.readuntil(b"\r\n\r\n")
            seen["request"] = blob
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            _reader, writer = await _connect_via_upstream(
                UpstreamProxy("127.0.0.1", port), "example.com", 443,
            )
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
        assert b"CONNECT example.com:443 HTTP/1.1" in seen["request"]

    asyncio.run(scenario())


def test_upstream_non_200_raises():
    async def scenario():
        async def handle(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with pytest.raises(OSError, match="status="):
                await _connect_via_upstream(
                    UpstreamProxy("127.0.0.1", port), "example.com", 443,
                )
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_upstream_authority_brackets_ipv6():
    assert _upstream_authority("::1", 443) == "[::1]:443"
    assert _upstream_authority("[::1]", 443) == "[::1]:443"
    assert _upstream_authority("example.com", 80) == "example.com:80"


def test_upstream_authority_rejects_crlf_and_space():
    with pytest.raises(OSError, match="rejected"):
        _upstream_authority("allowed.example\r\n\r\nCONNECT blocked.example", 443)
    with pytest.raises(OSError, match="rejected"):
        _upstream_authority("bad example", 80)


def test_connect_via_upstream_brackets_ipv6():
    async def scenario():
        seen = {}

        async def handle(reader, writer):
            blob = await reader.readuntil(b"\r\n\r\n")
            seen["request"] = blob
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            _reader, writer = await _connect_via_upstream(
                UpstreamProxy("127.0.0.1", port), "::1", 443,
            )
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
        assert b"CONNECT [::1]:443 HTTP/1.1" in seen["request"]
        assert b"Host: [::1]:443" in seen["request"]

    asyncio.run(scenario())


def test_connect_via_upstream_rejects_crlf_before_write():
    async def scenario():
        seen = {}

        async def handle(reader, writer):
            seen["hit"] = await reader.read(200)
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with pytest.raises(OSError, match="rejected"):
                await _connect_via_upstream(
                    UpstreamProxy("127.0.0.1", port),
                    "allowed.example\r\n\r\nCONNECT blocked.example",
                    443,
                )
            await asyncio.sleep(0.05)
        finally:
            server.close()
            await server.wait_closed()
        assert "hit" not in seen

    asyncio.run(scenario())


def test_bad_proxy_port_falls_through():
    env = {
        "HTTPS_PROXY": "http://proxy.example:70000",
        "HTTP_PROXY": "http://backup.example:8080",
    }
    assert resolve_upstream(
        "example.com", 443, environ=env, system_lookup=_no_system,
    ) == UpstreamProxy("backup.example", 8080)


def test_bad_explicit_proxy_stays_direct():
    def boom(*_args):
        raise AssertionError("system lookup")

    env = {
        "JIUWENBOX_UPSTREAM_PROXY": "http://proxy.example:8080abc",
        "HTTPS_PROXY": "http://backup.example:8080",
    }
    assert resolve_upstream(
        "example.com", 443, environ=env, system_lookup=boom,
    ) is None


class _MemWriter:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        return None

    def close(self):
        return None


def test_http_forward_sends_absolute_form_to_upstream():
    async def scenario():
        seen = {}

        async def handle(reader, writer):
            blob = await reader.readuntil(b"\r\n\r\n")
            seen["request"] = blob
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        original = win_proxy.resolve_upstream

        def fake_resolve(*_args, **_kwargs):
            return UpstreamProxy("127.0.0.1", port, "Basic dGVzdA==")

        win_proxy.resolve_upstream = fake_resolve
        try:
            client_reader = asyncio.StreamReader()
            client_reader.feed_data(b"Host: example.com\r\n\r\n")
            task = asyncio.create_task(handle_http_forward(
                client_reader,
                _MemWriter(),
                b"GET http://example.com/a?q=1 HTTP/1.1",
                EgressFilter(NetworkRulePolicy(default="allow")),
            ))
            for _ in range(100):
                if "request" in seen:
                    break
                await asyncio.sleep(0.02)
            client_reader.feed_eof()
            await asyncio.wait_for(task, timeout=3)
        finally:
            win_proxy.resolve_upstream = original
            server.close()
            await server.wait_closed()
        request = seen["request"]
        assert request.startswith(b"GET http://example.com:80/a?q=1 HTTP/1.1\r\n")
        assert b"Proxy-Authorization: Basic dGVzdA==\r\n" in request
        assert b"Host: example.com\r\n" in request
        assert b"CONNECT " not in request

    asyncio.run(scenario())
