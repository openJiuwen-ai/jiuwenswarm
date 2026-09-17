# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real-socket HTTP/SSE mTLS checks for the Gateway internal clients."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import ssl
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from jiuwenswarm.common.e2a.constants import E2A_WIRE_SERVER_PUSH_KEY
from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.e2a.wire_codec import encode_agent_chunk_for_wire
from jiuwenswarm.common.schema.agent import AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.security.link_mtls import (
    MTLSDeploymentIdentity,
    LinkMTLSConfig,
    LinkMTLSError,
    LinkMTLSMode,
)
from jiuwenswarm.gateway.routing.http_agent_client import HttpSseAgentServerClient

_EXT_DIR = (
    Path(__file__).resolve().parents[2]
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

_IDENTITY = MTLSDeploymentIdentity("deployment-local-e2e", "binding-local-e2e", 7)


@pytest.fixture(autouse=True)
def reset_sse_test_process_shutdown(monkeypatch):
    # Tests run several Uvicorn servers sequentially in one interpreter;
    # production roles each have their own process. sse-starlette keeps this
    # global flag after the previous test's server exits.
    from sse_starlette.sse import AppStatus

    monkeypatch.setattr(AppStatus, "should_exit", False)


def _write_private_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def _new_ca(
    directory: Path, name: str
) -> tuple[Path, rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / f"{name}.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key, cert


def _new_leaf(
    directory: Path,
    name: str,
    ca_key: rsa.RSAPrivateKey,
    ca_cert: x509.Certificate,
    *,
    server: bool = False,
    client: bool = False,
    include_ip_san: bool = True,
) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    usages = []
    if server:
        usages.append(ExtendedKeyUsageOID.SERVER_AUTH)
    if client:
        usages.append(ExtendedKeyUsageOID.CLIENT_AUTH)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
    )
    if server:
        subject_names: list[x509.GeneralName] = [x509.DNSName("localhost")]
        if include_ip_san:
            subject_names.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
        builder = builder.add_extension(
            x509.SubjectAlternativeName(subject_names),
            critical=False,
        )
    cert = builder.sign(ca_key, hashes.SHA256())
    cert_path = directory / f"{name}.crt"
    key_path = directory / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    _write_private_key(key_path, key)
    return cert_path, key_path


def _profile_config(certs, *, role, identity=_IDENTITY, wrong=False, dns=False):
    from openjiuwen_runtime.foundation.security.link_profile import (
        LinkProfile,
        cert_fingerprint,
    )

    from tests.fixtures.link_mtls import atomic_json

    prefix = (
        "wrong"
        if wrong
        else (
            "dns_only"
            if dns
            else {"agentserver": "server", "runtime": "runtime", "gateway": "gateway"}[
                role
            ]
        )
    )
    path = (
        certs["server_ca"].parent
        / f"{role}-{identity.mtls_binding_epoch}-{prefix}.json"
    )
    pins = {
        r: [cert_fingerprint(str(certs[p + "_cert"]))]
        for r, p in (
            ("agentserver", "server"),
            ("runtime", "runtime"),
            ("gateway", "gateway"),
        )
    }
    if wrong:
        pins["gateway"] = [cert_fingerprint(str(certs["wrong_cert"]))]
    if dns:
        pins[role] = [cert_fingerprint(str(certs["dns_only_cert"]))]
    atomic_json(
        path,
        {
            "version": 1,
            "status": "active",
            "role": role,
            "mtls_deployment_id": identity.mtls_deployment_id,
            "mtls_binding_id": identity.mtls_binding_id,
            "mtls_binding_epoch": identity.mtls_binding_epoch,
            "endpoints": {},
            "peers": pins,
            "tls": {
                "ca_file": str(certs["server_ca"]),
                "cert_file": str(certs[prefix + "_cert"]),
                "key_file": str(certs[prefix + "_key"]),
            },
        },
    )
    p = LinkProfile.load(str(path))
    return LinkMTLSConfig(
        mode=LinkMTLSMode.ENFORCE,
        profile=p,
        ca_file=p.ca_file,
        cert_file=p.cert_file,
        key_file=p.key_file,
        identity=identity,
    )


def _server_config(certs, *, role="agentserver"):
    return _profile_config(certs, role=role)


def _client_config(certs, *, identity=_IDENTITY, wrong_role=False):
    return _profile_config(certs, role="gateway", identity=identity, wrong=wrong_role)


@pytest.fixture
def link_certificates(tmp_path: Path) -> dict[str, Path]:
    server_ca, server_ca_key, server_ca_cert = _new_ca(tmp_path, "server-ca")
    gateway_ca, gateway_ca_key, gateway_ca_cert = (
        server_ca,
        server_ca_key,
        server_ca_cert,
    )
    runtime_cert, runtime_key = _new_leaf(
        tmp_path, "runtime", server_ca_key, server_ca_cert, server=True, client=True
    )
    wrong_ca, wrong_ca_key, wrong_ca_cert = _new_ca(tmp_path, "wrong-role-ca")
    server_cert, server_key = _new_leaf(
        tmp_path, "server", server_ca_key, server_ca_cert, server=True
    )
    dns_only_cert, dns_only_key = _new_leaf(
        tmp_path,
        "server-dns-only",
        server_ca_key,
        server_ca_cert,
        server=True,
        include_ip_san=False,
    )
    gateway_cert, gateway_key = _new_leaf(
        tmp_path, "gateway", gateway_ca_key, gateway_ca_cert, client=True
    )
    wrong_cert, wrong_key = _new_leaf(
        tmp_path, "wrong", wrong_ca_key, wrong_ca_cert, client=True
    )
    return {
        "runtime_cert": runtime_cert,
        "runtime_key": runtime_key,
        "server_ca": server_ca,
        "gateway_ca": gateway_ca,
        "wrong_ca": wrong_ca,
        "server_cert": server_cert,
        "server_key": server_key,
        "dns_only_cert": dns_only_cert,
        "dns_only_key": dns_only_key,
        "gateway_cert": gateway_cert,
        "gateway_key": gateway_key,
        "wrong_cert": wrong_cert,
        "wrong_key": wrong_key,
    }


@asynccontextmanager
async def _serve(
    app: FastAPI,
    *,
    ssl_kwargs: dict[str, object] | None = None,
) -> AsyncIterator[str]:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        access_log=False,
        log_level="warning",
        lifespan="off",
        **(ssl_kwargs or {}),
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started and server.servers:
            break
        if task.done():
            await task
        await asyncio.sleep(0.02)
    else:
        server.should_exit = True
        await task
        raise RuntimeError("uvicorn did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    scheme = "https" if ssl_kwargs else "http"
    try:
        yield f"{scheme}://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)


def _binding_guard(app: FastAPI, config: LinkMTLSConfig) -> None:
    @app.middleware("http")
    async def guard(request: Request, call_next):  # noqa: ANN001, ANN202
        try:
            config.authorize_request(request)
        except LinkMTLSError as exc:
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": {"message": str(exc)}},
            )
        return await call_next(request)


def _runtime_app(config: LinkMTLSConfig, agent_url: str) -> FastAPI:
    app = FastAPI()
    _binding_guard(app, config)

    @app.post("/api/session/route")
    async def route() -> dict:
        return {
            "ok": True,
            "rawdata": {
                "pod_sse_url": agent_url,
                "pod_id": "agent-1",
                **_IDENTITY.headers(),
                "mtls_deployment_id": _IDENTITY.mtls_deployment_id,
                "mtls_binding_id": _IDENTITY.mtls_binding_id,
                "mtls_binding_epoch": _IDENTITY.mtls_binding_epoch,
            },
        }

    @app.post("/api/session/touch")
    async def touch() -> dict:
        return {"ok": True, "rawdata": {"touched": True}}

    return app


def _agent_app(config: LinkMTLSConfig) -> FastAPI:
    app = FastAPI()
    _binding_guard(app, config)

    @app.get("/api/v1/health")
    async def health() -> dict:
        return {"ok": True, "data": {"status": "ready"}}

    @app.get("/api/v1/sessions")
    async def sessions() -> dict:
        return {"request_id": "unary-1", "ok": True, "data": {"sessions": []}}

    @app.post("/api/v1/chat/completions")
    async def chat() -> StreamingResponse:
        async def frames() -> AsyncIterator[str]:
            for sequence, (content, complete) in enumerate(
                (("hello", False), ("", True))
            ):
                wire = encode_agent_chunk_for_wire(
                    AgentResponseChunk(
                        request_id="stream-1",
                        channel_id="web",
                        payload={"content": content},
                        is_complete=complete,
                    ),
                    response_id="stream-1",
                    sequence=sequence,
                )
                yield f"event: e2a.chunk\ndata: {json.dumps(wire)}\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")

    @app.get("/api/v1/events/stream")
    async def events() -> StreamingResponse:
        async def frames() -> AsyncIterator[str]:
            payload = {
                "request_id": "push-1",
                "metadata": {E2A_WIRE_SERVER_PUSH_KEY: True},
                "data": {"event": "ready"},
            }
            yield f"event: e2a.chunk\ndata: {json.dumps(payload)}\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")

    return app


@pytest.mark.asyncio
async def test_enforce_real_mtls_runtime_and_agent_http_sse(
    link_certificates: dict[str, Path],
) -> None:
    certs = link_certificates
    server_config = _server_config(certs)
    client_config = _client_config(certs)

    async with _serve(
        _agent_app(server_config), ssl_kwargs=server_config.uvicorn_ssl_kwargs()
    ) as agent_url:
        async with _serve(
            _runtime_app(_server_config(certs, role="runtime"), agent_url),
            ssl_kwargs=_server_config(certs, role="runtime").uvicorn_ssl_kwargs(),
        ) as runtime_url:
            route_client = RuntimeSessionRouteClient(
                base_url=runtime_url, link_mtls_config=client_config
            )
            result = await route_client.route(
                session_id="session-1",
                group_id="group-1",
                bot_id="bot-1",
                request_id="route-1",
                user_id="user-1",
            )
            assert result.pod_sse_url == agent_url
            assert result.mtls_binding_epoch == _IDENTITY.mtls_binding_epoch
            assert await route_client.touch(
                session_id="session-1", request_id="touch-1"
            )
            await route_client.aclose()

            push_received = asyncio.Event()
            agent_client = HttpSseAgentServerClient(link_mtls_config=client_config)

            async def on_push(payload: dict) -> None:
                assert payload["request_id"] == "push-1"
                push_received.set()

            agent_client.set_server_push_handler(on_push)
            await agent_client.connect(agent_url)
            unary = e2a_from_agent_fields(
                request_id="unary-1",
                channel_id="web",
                session_id="session-1",
                req_method=ReqMethod.SESSION_LIST,
                params={},
                is_stream=False,
                user_id="user-1",
            )
            assert (await agent_client.send_request(unary)).ok is True
            stream = e2a_from_agent_fields(
                request_id="stream-1",
                channel_id="web",
                session_id="session-1",
                req_method=ReqMethod.CHAT_SEND,
                params={"query": "hello"},
                is_stream=True,
                user_id="user-1",
            )
            chunks = [chunk async for chunk in agent_client.send_request_stream(stream)]
            assert chunks[0].payload["content"] == "hello"
            assert chunks[-1].is_complete is True
            await asyncio.wait_for(push_received.wait(), timeout=3)
            await agent_client.disconnect()


@pytest.mark.asyncio
async def test_enforce_reconnects_across_multiple_agentservers_in_one_binding(
    link_certificates: dict[str, Path],
) -> None:
    certs = link_certificates
    server_config = _server_config(certs)
    client_config = _client_config(certs)
    async with _serve(
        _agent_app(server_config), ssl_kwargs=server_config.uvicorn_ssl_kwargs()
    ) as first_url:
        async with _serve(
            _agent_app(server_config), ssl_kwargs=server_config.uvicorn_ssl_kwargs()
        ) as second_url:
            client = HttpSseAgentServerClient(link_mtls_config=client_config)
            try:
                for index, url in enumerate((first_url, second_url), start=1):
                    await client.connect(url)
                    request = e2a_from_agent_fields(
                        request_id=f"multi-{index}",
                        channel_id="web",
                        session_id="session-1",
                        req_method=ReqMethod.SESSION_LIST,
                        params={},
                        is_stream=False,
                        user_id="user-1",
                    )
                    assert (await client.send_request(request)).ok is True
                    await client.disconnect()
            finally:
                await client.disconnect()


@pytest.mark.asyncio
async def test_enforce_rejects_missing_wrong_role_and_stale_binding(
    link_certificates: dict[str, Path],
) -> None:
    certs = link_certificates
    server_config = _server_config(certs, role="runtime")
    async with _serve(
        _runtime_app(server_config, "https://agent.invalid"),
        ssl_kwargs=server_config.uvicorn_ssl_kwargs(),
    ) as runtime_url:
        server_trust = ssl.create_default_context(cafile=str(certs["server_ca"]))
        async with httpx.AsyncClient(verify=server_trust, trust_env=False) as client:
            with pytest.raises(httpx.HTTPError):
                await client.post(f"{runtime_url}/api/session/route", json={})

        wrong = _client_config(certs, wrong_role=True)
        async with httpx.AsyncClient(
            verify=wrong.client_ssl_context(), trust_env=False
        ) as client:
            with pytest.raises(httpx.HTTPError):
                await client.post(
                    f"{runtime_url}/api/session/route",
                    json={},
                    headers=wrong.binding_headers(),
                )

        stale = _client_config(
            certs,
            identity=MTLSDeploymentIdentity(
                _IDENTITY.mtls_deployment_id,
                _IDENTITY.mtls_binding_id,
                _IDENTITY.mtls_binding_epoch - 1,
            ),
        )
        route_client = RuntimeSessionRouteClient(
            base_url=runtime_url, link_mtls_config=stale
        )
        with pytest.raises(FatalRouteError):
            await route_client.route(
                session_id="session-1",
                group_id="group-1",
                bot_id="bot-1",
                request_id="stale-1",
            )
        await route_client.aclose()

    with pytest.raises(LinkMTLSError, match="explicitly use https"):
        RuntimeSessionRouteClient(
            base_url="http://127.0.0.1:8091",
            link_mtls_config=_client_config(certs),
        )


@pytest.mark.asyncio
async def test_enforce_rejects_wrong_server_dns_san(
    link_certificates: dict[str, Path],
) -> None:
    certs = link_certificates
    server_config = _profile_config(certs, role="runtime", dns=True)
    async with _serve(
        _runtime_app(server_config, "https://agent.invalid"),
        ssl_kwargs=server_config.uvicorn_ssl_kwargs(),
    ) as runtime_url:
        route_client = RuntimeSessionRouteClient(
            base_url=runtime_url,
            link_mtls_config=_client_config(certs),
        )
        try:
            with pytest.raises(RetryableRouteError, match="request failed"):
                await route_client.route(
                    session_id="session-1",
                    group_id="group-1",
                    bot_id="bot-1",
                    request_id="wrong-san-1",
                )
        finally:
            await route_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [LinkMTLSMode.OFF, LinkMTLSMode.OBSERVE])
async def test_off_and_observe_keep_plain_http_compatible(
    link_certificates: dict[str, Path], mode: LinkMTLSMode
) -> None:
    certs = link_certificates
    config = LinkMTLSConfig(mode=mode)
    if mode is LinkMTLSMode.OBSERVE:
        config = LinkMTLSConfig(
            mode=mode,
            ca_file=str(certs["server_ca"]),
            cert_file=str(certs["gateway_cert"]),
            key_file=str(certs["gateway_key"]),
            identity=_IDENTITY,
        )
    async with _serve(_agent_app(config)) as agent_url:
        async with _serve(_runtime_app(config, agent_url)) as runtime_url:
            route_client = RuntimeSessionRouteClient(
                base_url=runtime_url, link_mtls_config=config
            )
            result = await route_client.route(
                session_id="session-1",
                group_id="group-1",
                bot_id="bot-1",
                request_id=f"{mode.value}-1",
            )
            assert result.pod_sse_url == agent_url
            await route_client.aclose()

            agent_client = HttpSseAgentServerClient(link_mtls_config=config)
            await agent_client.connect(agent_url)
            await agent_client.disconnect()


@pytest.mark.asyncio
async def test_actual_agent_http_routes_guard_and_ext_over_tls(
    link_certificates, monkeypatch
):
    from jiuwenswarm.common.request_ext import (
        INTERNAL_HEADER_NAME,
        encode_internal_header,
    )
    from jiuwenswarm.server.agent_http_routes import build_fastapi_app

    config = _server_config(link_certificates)
    monkeypatch.setattr(
        LinkMTLSConfig, "from_env", classmethod(lambda cls, **kwargs: config)
    )
    seen = []

    class BusinessProbe:
        async def invoke_unary(self, method, params, **kwargs):
            seen.append(kwargs)
            return {"ok": True, "data": {"sessions": []}}, 200

    app = build_fastapi_app(BusinessProbe())
    client_config = _client_config(link_certificates)
    async with _serve(app, ssl_kwargs=config.uvicorn_ssl_kwargs()) as url:
        async with httpx.AsyncClient(
            **client_config.client_kwargs(role="agentserver")
        ) as client:
            headers = {
                **client_config.binding_headers(),
                INTERNAL_HEADER_NAME: encode_internal_header(
                    {"X-Tenant-Id": "tenant-a"}
                ),
            }
            assert (
                await client.get(url + "/api/v1/health", headers=headers)
            ).status_code == 200
            assert (
                await client.get(url + "/api/v1/sessions", headers=headers)
            ).status_code == 200
            assert seen[0]["request_ext"] == {"X-Tenant-Id": "tenant-a"}
            assert (await client.get(url + "/api/v1/sessions")).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "remove_pin"])
@pytest.mark.parametrize("idle", [False, True])
async def test_actual_chat_stream_rechecks_binding_and_cleans_up(
    link_certificates, monkeypatch, change, idle
):
    from jiuwenswarm.server.agent_http_routes import build_fastapi_app
    from tests.fixtures.link_mtls import atomic_json

    config = _server_config(link_certificates)
    gateway = _client_config(link_certificates)
    release, cleaned = asyncio.Event(), asyncio.Event()

    class Business:
        async def iter_stream(self, *args, **kwargs):
            try:
                yield {"event": "probe.ready", "data": "ready"}
                await release.wait()
                yield {"event": "must-not-leak", "data": "after revoke"}
            finally:
                cleaned.set()

    monkeypatch.setattr(
        LinkMTLSConfig, "from_env", classmethod(lambda cls, **kwargs: config)
    )
    app = build_fastapi_app(Business())
    async with _serve(app, ssl_kwargs=config.uvicorn_ssl_kwargs()) as url:
        async with httpx.AsyncClient(
            trust_env=False, timeout=3, **gateway.client_kwargs(role="agentserver")
        ) as client:
            async with client.stream(
                "POST",
                url + "/api/v1/chat/completions",
                headers=gateway.binding_headers(),
                json={"query": "probe", "enable_streaming": True},
            ) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                async for line in lines:
                    if line == "event: probe.ready":
                        break
                else:
                    pytest.fail("missing first event")
                data = json.loads(config.profile.path.read_text())
                if change == "revoke":
                    data["status"] = "revoked"
                else:
                    data["peers"]["gateway"] = ["0" * 64]
                atomic_json(config.profile.path, data)
                if not idle:
                    release.set()

                async def consume():
                    return [line async for line in lines]

                remaining = await asyncio.wait_for(consume(), 3)
                assert not any("must-not-leak" in line for line in remaining)
                await asyncio.wait_for(cleaned.wait(), 1)
                assert (
                    release.is_set() is not idle
                )  # idle case did not need another chunk


@pytest.mark.asyncio
async def test_idle_push_stream_revocation_unregisters_subscriber(
    link_certificates, monkeypatch
):
    from jiuwenswarm.server.agent_http_routes import build_fastapi_app
    from jiuwenswarm.server.transports import push_registry
    from tests.fixtures.link_mtls import atomic_json

    registry = push_registry.PushRegistry()
    monkeypatch.setattr(push_registry, "get_push_registry", lambda: registry)
    config = _server_config(link_certificates)
    gateway = _client_config(link_certificates)
    monkeypatch.setattr(
        LinkMTLSConfig, "from_env", classmethod(lambda cls, **kwargs: config)
    )
    app = build_fastapi_app(object())
    async with _serve(app, ssl_kwargs=config.uvicorn_ssl_kwargs()) as url:
        async with httpx.AsyncClient(
            trust_env=False, timeout=3, **gateway.client_kwargs(role="agentserver")
        ) as client:
            async with client.stream(
                "GET",
                url + "/api/v1/events/stream",
                headers={
                    **gateway.binding_headers(),
                    "X-Jiuwen-Push-Consumer": "gateway",
                },
            ) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                async for line in lines:
                    if line == "event: gateway.push_ready":
                        break
                assert len(registry._subscribers) == 1
                data = json.loads(config.profile.path.read_text())
                data["status"] = "revoked"
                atomic_json(config.profile.path, data)

                async def consume():
                    return [line async for line in lines]

                await asyncio.wait_for(consume(), 3)
                assert not registry._subscribers
                assert registry._reverse_rpc_owner_id is None


@pytest.mark.asyncio
async def test_agent_enforce_port_conflict_refuses_automatic_port_drift(
    link_certificates, monkeypatch
):
    import socket

    from jiuwenswarm.server.agent_http_server import AgentHTTPServer

    config = _server_config(link_certificates)
    monkeypatch.setattr(
        LinkMTLSConfig, "from_env", classmethod(lambda cls, **kwargs: config)
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = AgentHTTPServer(object(), host="127.0.0.1", port=port)
    try:
        with pytest.raises(LinkMTLSError, match="occupied"):
            await server.start()
        assert server.port == port
        assert server._server is None
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_actual_gateway_receiver_requires_manager_role(tmp_path, monkeypatch):
    from openjiuwen_runtime.foundation.security import link_profile as profiles
    from openjiuwen_runtime.foundation.security.link_profile import LinkProfile

    from tests.fixtures.link_mtls import install, provision

    extensions = (
        Path(__file__).resolve().parents[2]
        / "packages/jiuwenclaw-ee/gateway/extensions"
    )
    monkeypatch.syspath_prepend(str(extensions))
    from manager_config_receiver.http.app import create_app

    provision(
        tmp_path / "bundle",
        mtls_deployment_id="gateway-guard",
        endpoints={"gateway": "127.0.0.1:8775"},
    )
    gateway = LinkProfile.load(str(tmp_path / "bundle/gateway/profile.json"))
    manager = LinkProfile.load(str(tmp_path / "bundle/manager/profile.json"))
    monkeypatch.setattr(profiles, "DEFAULT_IDENTITY_ROOT", tmp_path / "installed")
    install(tmp_path / "bundle", "gateway")
    monkeypatch.delenv("JIUWENSWARM_LINK_MTLS_PROFILE", raising=False)
    monkeypatch.setenv("JIUWENSWARM_LINK_MTLS_MODE", "enforce")
    config = LinkMTLSConfig.from_env()
    app = create_app()

    @app.post("/review-probe")
    async def probe():
        return {"ok": True}

    async with _serve(app, ssl_kwargs=config.uvicorn_ssl_kwargs()) as url:
        async with httpx.AsyncClient(**manager.client_kwargs(role="gateway")) as client:
            assert (await client.get(url + "/api/health")).status_code == 200
            assert (
                await client.post(url + "/review-probe", headers=manager.headers())
            ).status_code == 200
        async with httpx.AsyncClient(
            **gateway.client_kwargs(role="gateway")
        ) as wrong_role:
            assert (
                await wrong_role.post(url + "/review-probe", headers=gateway.headers())
            ).status_code == 403


@pytest.mark.asyncio
async def test_agent_handler_initializes_without_plaintext_websocket(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import websockets.legacy.server

    from jiuwenswarm.server import agent_ws_server as module

    serve_spy = AsyncMock(
        side_effect=AssertionError("plaintext listener must not start")
    )
    monkeypatch.setattr(websockets.legacy.server, "serve", serve_spy)
    monkeypatch.setattr(module, "reset_harness_packages_state", lambda: None)
    monkeypatch.setattr(
        module, "get_config", lambda: {"startup": {"warmup": {"enabled": False}}}
    )
    server = SimpleNamespace(
        _server=None,
        _trigger_before_ws_server_start_hook=AsyncMock(),
        _bootstrap_internal_jiuwenbox=AsyncMock(),
        _start_loop_lag_monitor=AsyncMock(),
    )
    await module.AgentWebSocketServer.start(server, listen=False)
    serve_spy.assert_not_awaited()
    server._bootstrap_internal_jiuwenbox.assert_awaited_once()


@pytest.mark.asyncio
async def test_installed_agent_and_gateway_identities_without_profile_env(
    tmp_path, monkeypatch
):
    from openjiuwen_runtime.foundation.security import link_profile as profiles

    from jiuwenswarm.server.agent_http_routes import build_fastapi_app
    from tests.fixtures.link_mtls import install, provision

    for key in (
        "JIUWENSWARM_LINK_MTLS_PROFILE",
        "JIUWENSWARM_LINK_MTLS_CA_FILE",
        "JIUWENSWARM_LINK_MTLS_CERT_FILE",
        "JIUWENSWARM_LINK_MTLS_KEY_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    bundle = tmp_path / "bundle"
    provision(
        bundle,
        mtls_deployment_id="no-handwritten-state",
        endpoints={"agentserver": "127.0.0.1:8766"},
    )
    monkeypatch.setattr(profiles, "DEFAULT_IDENTITY_ROOT", tmp_path / "installed")
    install(bundle, "agentserver")
    install(bundle, "gateway")
    monkeypatch.setenv("JIUWENSWARM_LINK_MTLS_MODE", "enforce")
    gateway = LinkMTLSConfig.from_env()
    agent = LinkMTLSConfig.from_env(role="agentserver")
    assert gateway.profile.role == "gateway" and agent.profile.role == "agentserver"

    class BusinessProbe:
        async def invoke_unary(self, method, params, **kwargs):
            return {"ok": True, "data": {"sessions": []}}, 200

    app = build_fastapi_app(BusinessProbe())
    async with _serve(app, ssl_kwargs=agent.uvicorn_ssl_kwargs()) as url:
        actual_gateway_client = HttpSseAgentServerClient()
        try:
            await actual_gateway_client.connect(url)
            async with httpx.AsyncClient(
                **gateway.client_kwargs(role="agentserver")
            ) as client:
                assert (
                    await client.get(
                        url + "/api/v1/sessions", headers=gateway.binding_headers()
                    )
                ).status_code == 200
                assert (await client.get(url + "/api/v1/sessions")).status_code == 403
        finally:
            await actual_gateway_client.disconnect()
