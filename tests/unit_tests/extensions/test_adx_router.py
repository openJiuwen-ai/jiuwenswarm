# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.extensions.adx.config import load_router_config
from jiuwenswarm.extensions.adx.router_client import AdxRouterClient, AdxRoutingError


def _config(monkeypatch, **overrides):
    monkeypatch.setenv("TEST_ADX_KEY", "platform-secret")
    settings = {
        "ingress_url": "https://adx.example",
        "tenant": "appliance",
        "template": "jiuwen",
        "version": "v1",
        "token_env": "TEST_ADX_KEY",
    }
    settings.update(overrides)
    return load_router_config({"gateway": {"adx": settings}})


class FakeClient:
    def __init__(self):
        self.server_ready = False
        self.disconnect = AsyncMock(side_effect=self._disconnect)
        self.calls = []
        self.push = None
        self.closed = None

    async def _disconnect(self):
        self.server_ready = False

    def set_server_push_handler(self, handler):
        self.push = handler

    def set_disconnect_handler(self, handler):
        self.closed = handler

    async def connect(self, uri, extra_headers=None):
        await asyncio.sleep(0)
        self.uri = uri
        self.headers = extra_headers
        self.server_ready = True

    async def send_request(self, envelope, *, timeout=None):
        self.calls.append(envelope)
        return AgentResponse(envelope.request_id, envelope.channel, payload={"user": envelope.user_id})

    async def send_request_stream(self, envelope):
        self.calls.append(envelope)
        for index in range(3):
            yield AgentResponseChunk(envelope.request_id, envelope.channel, {"index": index}, index == 2)


def _router(config):
    clients = []

    def factory():
        client = FakeClient()
        clients.append(client)
        return client

    return AdxRouterClient(config, ws_client_factory=factory), clients


def _request(user="alice", rid="same-id", **params):
    return E2AEnvelope(request_id=rid, user_id=user, channel="web", method="chat.send", params=params)


async def test_users_are_isolated_and_concurrent_first_connections_are_coalesced(monkeypatch):
    router, clients = _router(_config(monkeypatch))
    await router.connect("")
    responses = await asyncio.gather(*[
        router.send_request(_request(user, str(index)))
        for index, user in enumerate(["alice", "alice", "bob", "alice"])
    ])
    assert len(clients) == 2
    assert [response.payload["user"] for response in responses] == ["alice", "alice", "bob", "alice"]
    targets = [parse_qs(urlsplit(client.uri).query)["target"][0] for client in clients]
    assert len(set(targets)) == 2
    assert all(target.startswith("urn:adx:binding:jiuwen:v1:jiuwen-") for target in targets)
    assert all(client.headers["Authorization"] == "Bearer platform-secret" for client in clients)
    assert all(urlsplit(client.uri).path == "/agent/ws" for client in clients)
    parameters = [parse_qs(client.headers["X-ADX-Parameters"]) for client in clients]
    assert len({item["workspace"][0] for item in parameters}) == 2
    assert all("API_KEY" not in item for item in parameters)
    await router.disconnect()


async def test_stream_reuses_connection_and_keeps_order(monkeypatch):
    router, clients = _router(_config(monkeypatch))
    await router.connect("")
    await router.send_request(_request(rid="first"))
    chunks = [chunk async for chunk in router.send_request_stream(_request(rid="stream"))]
    assert len(clients) == 1
    assert [chunk.payload["index"] for chunk in chunks] == [0, 1, 2]
    assert chunks[-1].is_complete
    await router.disconnect()


async def test_cold_handshake_timeout_retries_before_sending_business_request(monkeypatch):
    client = FakeClient()
    attempts = []

    async def cold_connect(uri, extra_headers=None):
        attempts.append((uri, dict(extra_headers or {})))
        if len(attempts) == 1:
            raise TimeoutError("ADX is still activating the sandbox")
        await FakeClient.connect(client, uri, extra_headers)

    client.connect = cold_connect
    router = AdxRouterClient(_config(monkeypatch), ws_client_factory=lambda: client)
    await router.connect("")
    try:
        response = await router.send_request(_request(rid="cold-first"))
        assert response.payload["user"] == "alice"
        assert len(attempts) == 2
        assert attempts[0] == attempts[1]
        assert [call.request_id for call in client.calls] == ["cold-first"]
    finally:
        await router.shutdown()


async def test_disconnect_reconnects_same_binding_without_replaying_request(monkeypatch):
    router, clients = _router(_config(monkeypatch))
    await router.connect("")
    await router.send_request(_request(rid="first"))
    first = clients[0]
    first.send_request = AsyncMock(side_effect=RuntimeError("connection closed"))
    first.server_ready = False
    await first.closed(RuntimeError("connection closed"))
    await router.send_request(_request(rid="next"))
    assert len(clients) == 2
    assert clients[1].uri == first.uri
    assert clients[1].headers == first.headers
    assert len(first.calls) == 1
    # A late callback for the retired socket cannot evict its replacement.
    await first.closed(RuntimeError("late close"))
    await router.send_request(_request(rid="third"))
    assert len(clients) == 2
    await router.disconnect()


async def test_failed_inflight_request_is_not_replayed(monkeypatch):
    router, clients = _router(_config(monkeypatch))
    await router.connect("")
    await router.send_request(_request(rid="first"))
    clients[0].send_request = AsyncMock(side_effect=OSError("broken pipe"))
    with pytest.raises(AdxRoutingError) as error:
        await router.send_request(_request(rid="unknown"))
    assert error.value.code == "ADX_CONNECTION_LOST"
    assert len(clients) == 1
    assert clients[0].send_request.await_count == 1
    await router.send_request(_request(rid="next"))
    assert len(clients) == 2
    await router.disconnect()


async def test_stream_failure_after_delta_is_not_replayed(monkeypatch):
    router, clients = _router(_config(monkeypatch))
    await router.connect("")
    await router.send_request(_request(rid="first"))

    async def broken_stream(envelope):
        yield AgentResponseChunk(envelope.request_id, "web", {"content": "partial"})
        raise OSError("stream lost")

    clients[0].send_request_stream = broken_stream
    stream = router.send_request_stream(_request(rid="stream"))
    assert (await anext(stream)).payload["content"] == "partial"
    with pytest.raises(AdxRoutingError, match="unknown"):
        await anext(stream)
    assert len(clients) == 1
    await router.disconnect()


async def test_cancelled_first_waiter_does_not_cancel_shared_connection(monkeypatch):
    router, clients = _router(_config(monkeypatch))
    started, release = asyncio.Event(), asyncio.Event()
    factory = router._factory

    def blocking_factory():
        client = factory()
        original = client.connect

        async def blocked(uri, extra_headers=None):
            started.set()
            await release.wait()
            await original(uri, extra_headers)

        client.connect = blocked
        return client

    router._factory = blocking_factory
    await router.connect("")
    first = asyncio.create_task(router.send_request(_request(rid="first")))
    await started.wait()
    second = asyncio.create_task(router.send_request(_request(rid="second")))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert (await second).ok
    assert len(clients) == 1
    await router.disconnect()


async def test_shutdown_cancels_opening_connections_and_rejects_new_requests(monkeypatch):
    router, clients = _router(_config(monkeypatch))
    started = asyncio.Event()
    factory = router._factory

    def blocking_factory():
        client = factory()

        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        client.connect = blocked
        return client

    router._factory = blocking_factory
    await router.connect("")
    waiting = asyncio.create_task(router.send_request(_request()))
    await started.wait()
    await router.disconnect()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert clients[0].disconnect.await_count == 1
    assert not router._clients and not router._connecting
    with pytest.raises(AdxRoutingError) as error:
        await router.send_request(_request())
    assert error.value.code == "ADX_ROUTER_CLOSED"


async def test_failed_open_is_cleaned_up_and_next_request_can_connect(monkeypatch):
    router, clients = _router(_config(monkeypatch))
    factory = router._factory

    def factory_with_first_failure():
        client = factory()
        if len(clients) == 1:
            client.connect = AsyncMock(side_effect=OSError("handshake failed"))
        return client

    router._factory = factory_with_first_failure
    await router.connect("")
    with pytest.raises(AdxRoutingError) as error:
        await router.send_request(_request())
    assert error.value.code == "ADX_CONNECT_FAILED"
    assert clients[0].disconnect.await_count == 1
    assert (await router.send_request(_request(rid="next"))).ok
    assert len(clients) == 2
    await router.disconnect()


async def test_connection_capacity_protects_online_users(monkeypatch):
    router, clients = _router(replace(_config(monkeypatch), max_connections=1))
    await router.connect("")
    await router.send_request(_request())
    await router._on_channel_event(SimpleNamespace(channel_type="web", user_id="alice", event_type="connected"))
    with pytest.raises(AdxRoutingError) as error:
        await router.send_request(_request("bob"))
    assert error.value.code == "ADX_CAPACITY_EXCEEDED"
    assert len(clients) == 1 and clients[0].disconnect.await_count == 0
    await router.disconnect()


async def test_push_is_forwarded_unchanged_and_retired_socket_is_ignored(monkeypatch):
    router, clients = _router(_config(monkeypatch))
    pushes = []

    async def push_handler(wire):
        pushes.append(wire)

    router.set_server_push_handler(push_handler)
    await router.connect("")
    await router.send_request(_request())
    client = clients[0]
    wire = {"request_id": "same-id", "session_id": "session-1", "metadata": {"ws_id": "alice-ws"}}
    await client.push(wire)
    assert pushes == [wire]
    await client.closed(RuntimeError("closed"))
    await client.push({"request_id": "same-id"})
    assert len(pushes) == 1
    await router.disconnect()


async def test_responses_and_stream_chunks_keep_the_existing_metadata(monkeypatch):
    router, _ = _router(_config(monkeypatch))
    await router.connect("")
    client = await router._get_client("alice")
    response = AgentResponse("request", "web", payload={"content": "reply"}, metadata={"ws_id": "alice-ws"})
    chunks = [AgentResponseChunk("stream", "web", {"content": "reply"}, True, metadata={"trace_id": "trace"})]
    client.send_request = AsyncMock(return_value=response)

    async def stream(envelope):
        for chunk in chunks:
            yield chunk

    client.send_request_stream = stream
    assert await router.send_request(_request(rid="request")) == response
    received = [chunk async for chunk in router.send_request_stream(_request(rid="stream"))]
    assert received == chunks
    assert response.metadata == {"ws_id": "alice-ws"}
    assert received[0].metadata == {"trace_id": "trace"}
    await router.shutdown()


@pytest.mark.parametrize("user,params", [(None, {}), ("", {}), (" alice ", {}), ("alice", {"user_id": "bob"}), ("alice", {"agent_type": "other"})])
async def test_invalid_identity_or_agent_is_rejected_before_connect(monkeypatch, user, params):
    router, clients = _router(_config(monkeypatch))
    await router.connect("")
    with pytest.raises(AdxRoutingError):
        await router.send_request(_request(user, **params))
    assert clients == []
    await router.disconnect()


@pytest.mark.parametrize("url", ["http://adx.example", "https://u:p@adx.example", "https://adx.example/path", "https://adx.example?token=x"])
def test_invalid_ingress_url_is_rejected(monkeypatch, url):
    with pytest.raises(ValueError):
        _config(monkeypatch, ingress_url=url)


def test_missing_platform_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("MISSING_ADX_TOKEN", raising=False)
    with pytest.raises(ValueError):
        _config(monkeypatch, token_env="MISSING_ADX_TOKEN")


def test_binding_identity_and_urn_encoding_match_adx_contract(monkeypatch):
    config = _config(monkeypatch, template="jiuwen:办公", version="v1/测试", service_port=18092)
    router, _ = _router(config)
    uri = router._uri("alice")
    query = parse_qs(urlsplit(uri).query)
    # Fixed compatibility vector: changing the mapping would select another Binding.
    digest = "66b277ab9ebf0338ef66928801f5770b57718b76f66f951e6b83c41cc3d0a17e"
    assert query["target"] == [f"urn:adx:binding:jiuwen%3A%E5%8A%9E%E5%85%AC:v1%2F%E6%B5%8B%E8%AF%95:jiuwen-{digest}"]
    assert query["port"] == ["18092"]
    assert "platform-secret" not in uri
    assert "platform-secret" not in repr(config)


async def test_unsupported_channel_cannot_claim_an_authorized_user(monkeypatch):
    router, clients = _router(_config(monkeypatch))
    await router.connect("")
    with pytest.raises(AdxRoutingError) as error:
        await router.send_request(replace(_request(), channel="acp"))
    assert error.value.code == "ADX_CHANNEL_UNSUPPORTED" and clients == []
    await router.disconnect()


async def test_factory_failure_does_not_leave_a_stuck_connection_slot(monkeypatch):
    router, clients = _router(_config(monkeypatch))
    factory = router._factory
    router._factory = lambda: (_ for _ in ()).throw(RuntimeError("factory failed"))
    await router.connect("")
    with pytest.raises(AdxRoutingError):
        await router.send_request(_request())
    assert not router._connecting
    router._factory = factory
    assert (await router.send_request(_request(rid="next"))).ok
    assert len(clients) == 1
    await router.disconnect()


async def test_full_pool_reclaims_oldest_offline_connection_without_deleting_binding(monkeypatch):
    router, clients = _router(replace(_config(monkeypatch), max_connections=2))
    router._control.delete_binding = AsyncMock()
    await router.connect("")
    await router.send_request(_request("alice", "alice-1"))
    await router.send_request(_request("bob", "bob-1"))
    await router.send_request(_request("alice", "alice-2"))
    assert (await router.send_request(_request("charlie"))).ok
    assert set(router._clients) == {"alice", "charlie"}
    clients[0].disconnect.assert_not_awaited()
    clients[1].disconnect.assert_awaited_once()
    router._control.delete_binding.assert_not_awaited()
    await router.shutdown()
    assert not router._last_used and not router._pending_cleanups


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_capacity_and_idle_cleanup_protect_inflight_requests(monkeypatch, streaming, cancel):
    from jiuwenswarm.extensions.adx import router_client

    monkeypatch.setattr(router_client, "_DISCONNECT_CLEANUP_SECONDS", 0)
    router, clients = _router(replace(_config(monkeypatch), max_connections=1))
    await router.connect("")
    await router.send_request(_request())
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_request(envelope, *, timeout=None):
        entered.set()
        await release.wait()
        return AgentResponse(envelope.request_id, envelope.channel, payload={})

    async def slow_stream(envelope):
        entered.set()
        await release.wait()
        yield AgentResponseChunk(envelope.request_id, envelope.channel, {}, True)

    clients[0].send_request = slow_request
    clients[0].send_request_stream = slow_stream

    async def consume():
        if streaming:
            return [chunk async for chunk in router.send_request_stream(_request(rid="held"))]
        return await router.send_request(_request(rid="held"))

    task = asyncio.create_task(consume())
    await entered.wait()
    router._last_used["alice"] -= 1000
    await router._reap_idle_once()
    await router._on_channel_event(SimpleNamespace(channel_type="web", user_id="alice", event_type="connected"))
    await router._on_channel_event(SimpleNamespace(channel_type="web", user_id="alice", event_type="disconnected"))
    await router._pending_cleanups["alice"]
    with pytest.raises(AdxRoutingError) as error:
        await router.send_request(_request("bob"))
    assert error.value.code == "ADX_CAPACITY_EXCEEDED"
    clients[0].disconnect.assert_not_awaited()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        await task
    assert router._active_requests == {}
    assert (await router.send_request(_request("bob"))).ok
    clients[0].disconnect.assert_awaited_once()
    await router.shutdown()


async def test_disconnect_cleanup_waits_for_all_frontends_and_reconnect_cancels_it(monkeypatch):
    from jiuwenswarm.extensions.adx import router_client

    monkeypatch.setattr(router_client, "_DISCONNECT_CLEANUP_SECONDS", 0)
    router, clients = _router(_config(monkeypatch))
    await router.connect("")
    await router.send_request(_request())
    for channel in ("web", "tui"):
        await router._on_channel_event(SimpleNamespace(channel_type=channel, user_id="alice", event_type="connected"))
    await router._on_channel_event(SimpleNamespace(channel_type="web", user_id="alice", event_type="disconnected"))
    assert not router._pending_cleanups
    router._last_used["alice"] -= 1000
    await router._reap_idle_once()
    clients[0].disconnect.assert_not_awaited()
    await router._on_channel_event(SimpleNamespace(channel_type="tui", user_id="alice", event_type="disconnected"))
    cleanup = router._pending_cleanups["alice"]
    await router._on_channel_event(SimpleNamespace(channel_type="web", user_id="alice", event_type="connected"))
    await asyncio.gather(cleanup, return_exceptions=True)
    clients[0].disconnect.assert_not_awaited()
    await router._on_channel_event(SimpleNamespace(channel_type="web", user_id="alice", event_type="disconnected"))
    await router._pending_cleanups["alice"]
    clients[0].disconnect.assert_awaited_once()
    assert not router._frontend_connections and not router._last_used and not router._pending_cleanups
    await router.shutdown()


async def test_idle_timeout_reclaims_only_offline_idle_connections(monkeypatch):
    router, clients = _router(_config(monkeypatch))
    await router.connect("")
    for user in ("alice", "bob", "charlie"):
        await router.send_request(_request(user))
    router._last_used["alice"] -= 1000
    router._last_used["bob"] -= 1000
    await router._on_channel_event(SimpleNamespace(channel_type="web", user_id="bob", event_type="connected"))
    await router._reap_idle_once()
    assert set(router._clients) == {"bob", "charlie"}
    clients[0].disconnect.assert_awaited_once()
    clients[1].disconnect.assert_not_awaited()
    clients[2].disconnect.assert_not_awaited()
    await router.shutdown()
