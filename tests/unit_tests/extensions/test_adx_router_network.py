# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real WSS contract tests with a transparent Ingress double and E2A server."""

from __future__ import annotations

import asyncio
import json
import ssl
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from test_adx_router import EnvironmentAPI, _config
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.legacy.client import connect
from websockets.legacy.server import serve

from gateway.extensions.adx.control_client import AdxControlClient
from gateway.extensions.adx.router_client import AdxRouterClient
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
)
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk


def _certificate(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "ca.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context, str(cert_path)


@asynccontextmanager
async def _network(tmp_path, monkeypatch):
    monkeypatch.setattr(
        'gateway.extensions.agentos.agentos_router.router_client._WS_CONNECT_READY_TIMEOUT_SECONDS', 5,
    )
    tls, ca_path = _certificate(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", ca_path)
    requests, handshakes, targets = [], [], []
    expected_key = "platform-secret"

    async def backend(ws, path):
        targets.append(parse_qs(urlsplit(path).query)["target"][0])
        assert "Authorization" not in ws.request_headers
        await ws.send(json.dumps({
            "type": "event", "event": "connection.ack",
            "payload": {"status": "ready", "readiness": "AGENT_READY"},
        }))

        async def reply(request):
            requests.append(request)
            rid = request["request_id"]
            action = request["params"].get("action")
            if request["is_stream"]:
                for index in range(3):
                    chunk = AgentResponseChunk(rid, request["channel"], {"index": index}, index == 2)
                    await ws.send(json.dumps(encode_agent_chunk_for_wire(chunk, response_id=rid, sequence=index)))
                    if action == "drop" and index == 0:
                        await ws.close(code=1011, reason="test route withdrawn")
                        return
                    await asyncio.sleep(0)
            else:
                await ws.send(json.dumps(encode_agent_response_for_wire(
                    AgentResponse(rid, request["channel"], payload={"user": request["user_id"]}),
                    response_id=rid,
                )))

        running = set()
        try:
            async for raw in ws:
                task = asyncio.create_task(reply(json.loads(raw)))
                running.add(task)
        except ConnectionClosed:
            pass
        finally:
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)

    async with serve(backend, "127.0.0.1", 0) as agent_server:
        agent_port = agent_server.sockets[0].getsockname()[1]

        async def auth(path, headers):
            handshakes.append((path, dict(headers)))
            if urlsplit(path).path != "/agent/ws" or headers.get("Authorization") != f"Bearer {expected_key}":
                return 401, [], b"unauthorized"
            return None

        async def ingress(downstream, path):
            target = parse_qs(urlsplit(path).query)["target"][0]
            async with connect(f"ws://127.0.0.1:{agent_port}/?target={target}") as upstream:
                async def copy(source, destination):
                    try:
                        async for frame in source:
                            await destination.send(frame)
                    except ConnectionClosed:
                        return

                pipes = [asyncio.create_task(copy(downstream, upstream)), asyncio.create_task(copy(upstream, downstream))]
                try:
                    await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for pipe in pipes:
                        pipe.cancel()
                    await asyncio.gather(*pipes, return_exceptions=True)

        async with serve(ingress, "127.0.0.1", 0, ssl=tls, process_request=auth) as proxy:
            port = proxy.sockets[0].getsockname()[1]
            monkeypatch.setenv("ADX_TEST_PLATFORM", expected_key)
            config = _config(monkeypatch, ingress_url=f"https://localhost:{port}")
            api = EnvironmentAPI(config.tenant)
            control = AdxControlClient(config, transport=httpx.MockTransport(api.handle))
            router = AdxRouterClient(config, control_client=control)
            router.test_api = api
            await router.connect("")
            try:
                yield router, requests, handshakes, targets
            finally:
                await router.disconnect()


def _request(user="alice", rid="request", action=""):
    return E2AEnvelope(request_id=rid, user_id=user, channel="web", method="chat.send", params={"action": action})


async def test_wss_proxy_isolates_users_reuses_connections_and_multiplexes_streams(tmp_path, monkeypatch):
    async with _network(tmp_path, monkeypatch) as (router, requests, handshakes, targets):
        responses = await asyncio.gather(*[
            router.send_request(_request(user, f"unary-{index}"))
            for index, user in enumerate(["alice", "bob", "alice", "alice"])
        ])
        assert [response.payload["user"] for response in responses] == ["alice", "bob", "alice", "alice"]

        async def stream(user):
            return [chunk async for chunk in router.send_request_stream(_request(user, "same-stream-id"))]

        streams = await asyncio.gather(stream("alice"), stream("bob"))
        for chunks in streams:
            assert [chunk.payload["index"] for chunk in chunks] == [0, 1, 2]
            assert chunks[-1].is_complete
        assert len(handshakes) == 2 and len(set(targets)) == 2
        assert all(headers["authorization"] == "Bearer platform-secret" for _, headers in handshakes)
        assert len(requests) == 6


async def test_real_stream_disconnect_is_not_replayed_and_next_call_reauthenticates(tmp_path, monkeypatch):
    async with _network(tmp_path, monkeypatch) as (router, requests, handshakes, targets):
        stream = router.send_request_stream(_request(rid="lost", action="drop"))
        assert (await anext(stream)).payload["index"] == 0
        failure = await asyncio.wait_for(anext(stream), timeout=3)
        assert failure.is_complete and failure.payload.get("error")
        assert [request["request_id"] for request in requests] == ["lost"]
        assert await router._agent_manager.get_agent('alice', 'jiuwenswarm') is None
        assert (await router.send_request(_request(rid="next"))).ok
        assert len(router.test_api.creates) == 2
        assert len(handshakes) == 2 and targets[0] == targets[1]
        assert all(headers["authorization"] == "Bearer platform-secret" for _, headers in handshakes)


@pytest.mark.parametrize("failure", ["untrusted_ca", "invalid_key"])
async def test_tls_or_ingress_auth_failure_never_sends_business_requests(tmp_path, monkeypatch, failure):
    async with _network(tmp_path, monkeypatch) as (router, requests, _handshakes, targets):
        if failure == "untrusted_ca":
            monkeypatch.delenv("SSL_CERT_FILE")
            monkeypatch.setattr(
                'gateway.extensions.agentos.agentos_router.router_client._WS_CONNECT_READY_TIMEOUT_SECONDS', 0.1,
            )
        config = router._config if failure == "untrusted_ca" else replace(router._config, token="wrong-key")
        api = EnvironmentAPI(config.tenant)
        rejected = AdxRouterClient(config, control_client=AdxControlClient(
            router._config, transport=httpx.MockTransport(api.handle)))
        await rejected.connect("")
        try:
            if failure == "invalid_key":
                with pytest.raises(InvalidHandshake):
                    await rejected.send_request(_request())
            else:
                assert not (await rejected.send_request(_request())).ok
            assert not rejected._ws_clients
            assert not requests and not targets
        finally:
            await rejected.disconnect()
