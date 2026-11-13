# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""RuntimeRoutedAgentClient：流式保活 touch + interrupt 多播。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.extensions.runtime_management_extension.runtime_routed_client import (
    RuntimeRoutedAgentClient,
)
from jiuwenswarm.extensions.runtime_management_extension.session_route_client import (
    RouteResult,
)


def _envelope(*, method: str, session_id: str = "sess-cron-1", request_id: str = "req-1") -> E2AEnvelope:
    return E2AEnvelope(
        method=method,
        session_id=session_id,
        request_id=request_id,
        channel="web",
        user_id="u1",
        channel_context={"routing": {"group_id": "g1", "bot_id": "b1", "user_id": "u1"}},
        params={"session_id": session_id},
    )


def _chunk(request_id: str = "req-1") -> AgentResponseChunk:
    return AgentResponseChunk(
        request_id=request_id,
        channel_id="web",
        payload={"event_type": "chat.delta", "content": "x"},
        is_complete=False,
    )


class _FakeRoute:
    def __init__(self, *, touch_results: list[bool | None] | None = None) -> None:
        self.touch_calls: list[tuple[str, str]] = []
        self.route_calls: list[dict[str, Any]] = []
        self._touch_results = list(touch_results or [])
        self._default_pod = "http://10.244.102.148:8080"

    async def route(
        self,
        *,
        session_id: str,
        group_id: str,
        bot_id: str,
        request_id: str,
        user_id: str | None = None,
    ) -> RouteResult:
        self.route_calls.append(
            {
                "session_id": session_id,
                "group_id": group_id,
                "bot_id": bot_id,
                "request_id": request_id,
                "user_id": user_id,
            }
        )
        return RouteResult(
            pod_sse_url=f"{self._default_pod}/sse",
            pod_id="pod-148",
            request_id=request_id,
        )

    async def touch(self, *, session_id: str, request_id: str) -> bool:
        self.touch_calls.append((session_id, request_id))
        if self._touch_results:
            result = self._touch_results.pop(0)
            if result is None:
                raise RuntimeError("touch transport failed")
            return bool(result)
        return True

    async def aclose(self) -> None:
        return None


class _FakeHttp:
    def __init__(self, *, stream_delay: float = 0.0) -> None:
        self.stream_delay = stream_delay
        self.stream_bases: list[str] = []
        self.send_bases: list[str] = []
        self.send_payloads: dict[str, AgentResponse] = {}

    async def send_request_stream(self, envelope: E2AEnvelope, *, base_url: str):
        self.stream_bases.append(base_url)
        if self.stream_delay > 0:
            await asyncio.sleep(self.stream_delay)
        yield _chunk(str(envelope.request_id or "req"))
        yield AgentResponseChunk(
            request_id=str(envelope.request_id or "req"),
            channel_id="web",
            payload={"event_type": "chat.final"},
            is_complete=True,
        )

    async def send_request(self, envelope: E2AEnvelope, *, base_url: str) -> AgentResponse:
        self.send_bases.append(base_url)
        if base_url in self.send_payloads:
            return self.send_payloads[base_url]
        return AgentResponse(
            request_id=str(envelope.request_id or ""),
            channel_id="web",
            ok=True,
            payload={"event_type": "chat.interrupt_result", "success": True, "killed": 0},
        )

    async def disconnect(self) -> None:
        return None


@pytest.mark.asyncio
async def test_stream_keepalive_touches_while_waiting_for_chunks(monkeypatch):
    """长时间无 chunk 时后台保活仍会周期性 touch。"""
    monkeypatch.setattr(
        "jiuwenswarm.extensions.runtime_management_extension.runtime_routed_client.authorize_agent",
        AsyncMock(return_value=("g1", "b1", "u1")),
    )
    route = _FakeRoute()
    http = _FakeHttp(stream_delay=0.18)
    client = RuntimeRoutedAgentClient(
        route_client=route,
        http_client=http,
        touch_interval_seconds=0.05,
    )
    await client.connect("http://unused")
    monkeypatch.setattr(client, "_ensure_pod_push", AsyncMock())

    env = _envelope(method="chat.send")
    chunks = []
    async for chunk in client.send_request_stream(env):
        chunks.append(chunk)

    assert len(chunks) == 2
    # 0.18s 等待 + interval 0.05 → 至少 2 次后台 touch（再加流结束 finally 一次）
    assert len(route.touch_calls) >= 2
    assert all(sid == "sess-cron-1" for sid, _ in route.touch_calls)
    assert "sess-cron-1" not in client._session_stream_bases


@pytest.mark.asyncio
async def test_stream_keepalive_restores_affinity_when_touch_expires(monkeypatch):
    """touch 返回 False 时触发重新 route（亲和恢复路径）。"""
    monkeypatch.setattr(
        "jiuwenswarm.extensions.runtime_management_extension.runtime_routed_client.authorize_agent",
        AsyncMock(return_value=("g1", "b1", "u1")),
    )
    route = _FakeRoute(touch_results=[False, True, True, True, True, True])
    http = _FakeHttp(stream_delay=0.12)
    client = RuntimeRoutedAgentClient(
        route_client=route,
        http_client=http,
        touch_interval_seconds=0.04,
    )
    await client.connect("http://unused")
    monkeypatch.setattr(client, "_ensure_pod_push", AsyncMock())

    env = _envelope(method="chat.send", request_id="req-restore")
    async for _ in client.send_request_stream(env):
        pass

    # 初始 route + 至少一次 affinity restore route
    assert len(route.route_calls) >= 2


@pytest.mark.asyncio
async def test_interrupt_fanout_hits_stream_base_and_pooled_pods(monkeypatch):
    """interrupt：优先打推流 Pod，再扫本 GW 已连接的其它 AgentServer。"""
    route = _FakeRoute()
    # route 故意指向「错」的 Pod（亲和漂走场景）
    route._default_pod = "http://10.244.102.251:8080"
    http = _FakeHttp()
    stream_base = "http://10.244.102.148:8080"
    other_base = "http://10.244.102.200:8080"
    http.send_payloads[stream_base] = AgentResponse(
        request_id="irq-1",
        channel_id="web",
        ok=True,
        payload={
            "event_type": "chat.interrupt_result",
            "success": True,
            "killed": 2,
            "cancelled_tools": [{"tool_call_id": "t1"}],
        },
    )
    client = RuntimeRoutedAgentClient(
        route_client=route,
        http_client=http,
        touch_interval_seconds=20.0,
    )
    await client.connect("http://unused")
    monkeypatch.setattr(client, "_ensure_pod_push", AsyncMock())
    client._session_stream_bases["sess-cron-1"] = stream_base
    # 模拟本 GW 已热连接过的 Pod（含真正跑 bash 的 148 与旁路 200）
    client._pod_clients[stream_base] = MagicMock()
    client._pod_clients[other_base] = MagicMock()
    client._pod_clients["http://10.244.102.251:8080"] = MagicMock()

    env = _envelope(method="chat.interrupt", request_id="irq-1")
    resp = await client.send_request(env)

    assert resp.ok is True
    assert resp.payload.get("killed") == 2
    # 推流 Pod 必须在目标列表里，且因 killed>0 可提前结束（不必打完所有）
    assert stream_base in http.send_bases
    assert http.send_bases[0] == stream_base


@pytest.mark.asyncio
async def test_interrupt_fanout_continues_when_primary_is_noop(monkeypatch):
    """route 到的错 Pod killed=0 时，继续打其它已连接 Pod。"""
    route = _FakeRoute()
    route._default_pod = "http://10.244.102.251:8080"
    http = _FakeHttp()
    wrong = "http://10.244.102.251:8080"
    right = "http://10.244.102.148:8080"
    http.send_payloads[wrong] = AgentResponse(
        request_id="irq-2",
        channel_id="web",
        ok=True,
        payload={"event_type": "chat.interrupt_result", "success": True, "killed": 0},
    )
    http.send_payloads[right] = AgentResponse(
        request_id="irq-2",
        channel_id="web",
        ok=True,
        payload={
            "event_type": "chat.interrupt_result",
            "success": True,
            "cancelled_tools": [{"tool_call_id": "bash1"}],
        },
    )
    client = RuntimeRoutedAgentClient(
        route_client=route,
        http_client=http,
        touch_interval_seconds=20.0,
    )
    await client.connect("http://unused")
    monkeypatch.setattr(client, "_ensure_pod_push", AsyncMock())
    client._pod_clients[wrong] = MagicMock()
    client._pod_clients[right] = MagicMock()

    env = _envelope(method="chat.interrupt", request_id="irq-2")
    resp = await client.send_request(env)

    assert wrong in http.send_bases
    assert right in http.send_bases
    # 主返回仍是第一枪（route 结果）；多播是副作用杀进程
    assert resp.payload.get("killed") == 0


class _DeferredHttp(_FakeHttp):
    async def send_request_stream(self, envelope: E2AEnvelope, *, base_url: str):
        self.stream_bases.append(base_url)
        rid = str(envelope.request_id or "req")
        yield AgentResponseChunk(
            request_id=rid,
            channel_id="web",
            payload={
                "event_type": "chat.processing_status_deferred",
                "session_id": str(envelope.session_id or ""),
            },
            is_complete=False,
        )
        yield AgentResponseChunk(
            request_id=rid,
            channel_id="web",
            payload=None,
            is_complete=True,
        )


def _push_wire(
    session_id: str,
    *,
    event_type: str = "chat.processing_status",
    is_processing: bool = False,
    idle: bool | None = None,
) -> dict[str, Any]:
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

    payload: dict[str, Any] = {
        "event_type": event_type,
        "session_id": session_id,
        "is_processing": is_processing,
        "is_complete": not is_processing,
    }
    if idle is not None:
        payload["idle"] = idle
    return build_server_push_wire(
        {
            "request_id": f"team-session-live:{session_id}",
            "channel_id": "web",
            "session_id": session_id,
            "payload": payload,
            "is_complete": False,
        }
    )


def _patch_authorize(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.extensions.runtime_management_extension.runtime_routed_client.authorize_agent",
        AsyncMock(return_value=("g1", "b1", "u1")),
    )


async def _drain(client: RuntimeRoutedAgentClient, env: E2AEnvelope) -> None:
    async for _ in client.send_request_stream(env):
        pass


@pytest.mark.asyncio
async def test_deferred_keeps_touching_without_reroute(monkeypatch):
    """短请求结束后仍 touch，且不再 route。disconnect 之后停止。"""
    _patch_authorize(monkeypatch)
    route = _FakeRoute()
    client = RuntimeRoutedAgentClient(
        route_client=route,
        http_client=_DeferredHttp(),
        touch_interval_seconds=0.05,
    )
    await client.connect("http://unused")
    monkeypatch.setattr(client, "_ensure_pod_push", AsyncMock())

    await _drain(client, _envelope(method="chat.send"))
    before = len(route.touch_calls)
    await asyncio.sleep(0.16)

    assert len(route.touch_calls) >= before + 2
    assert all(sid == "sess-cron-1" for sid, _ in route.touch_calls)
    assert len(route.route_calls) == 1
    assert "sess-cron-1" in client._session_keepalives
    await client.disconnect()
    stopped = len(route.touch_calls)
    await asyncio.sleep(0.16)
    assert len(route.touch_calls) == stopped
    assert client._session_keepalives == {}


@pytest.mark.asyncio
async def test_deferred_stops_when_touch_misses_or_transport_fails(monkeypatch):
    """touch 返回 false 后停止且不重新 route；传输失败则下一轮再试。"""
    _patch_authorize(monkeypatch)
    route = _FakeRoute(touch_results=[True, False, True, True])
    client = RuntimeRoutedAgentClient(
        route_client=route,
        http_client=_DeferredHttp(),
        touch_interval_seconds=0.05,
    )
    await client.connect("http://unused")
    monkeypatch.setattr(client, "_ensure_pod_push", AsyncMock())
    await _drain(client, _envelope(method="chat.send"))
    await asyncio.sleep(0.25)
    assert len(route.touch_calls) == 2
    assert len(route.route_calls) == 1
    assert "sess-cron-1" not in client._session_keepalives

    route_retry = _FakeRoute(touch_results=[True, None, True])
    retry_client = RuntimeRoutedAgentClient(
        route_client=route_retry,
        http_client=_DeferredHttp(),
        touch_interval_seconds=0.05,
    )
    await retry_client.connect("http://unused")
    monkeypatch.setattr(retry_client, "_ensure_pod_push", AsyncMock())
    await _drain(retry_client, _envelope(method="chat.send"))
    await asyncio.sleep(0.18)
    assert len(route_retry.touch_calls) >= 3
    assert len(route_retry.route_calls) == 1
    await retry_client.disconnect()


@pytest.mark.asyncio
async def test_deferred_stops_on_round_end_frame(monkeypatch):
    """没有 idle 的收口帧停掉后续期。带 idle 的灯帧、team.idle 和 is_processing true 不停。"""
    _patch_authorize(monkeypatch)
    route = _FakeRoute()
    client = RuntimeRoutedAgentClient(
        route_client=route,
        http_client=_DeferredHttp(),
        touch_interval_seconds=0.2,
    )
    await client.connect("http://unused")
    monkeypatch.setattr(client, "_ensure_pod_push", AsyncMock())
    await _drain(client, _envelope(method="chat.send"))
    assert "sess-cron-1" in client._session_keepalives

    await client._handle_pod_push(
        "http://pod",
        _push_wire("sess-cron-1", event_type="team.idle", is_processing=False),
    )
    await client._handle_pod_push(
        "http://pod",
        _push_wire("sess-cron-1", is_processing=True),
    )
    assert "sess-cron-1" in client._session_keepalives

    await client._handle_pod_push(
        "http://pod",
        _push_wire("sess-cron-1", is_processing=False, idle=True),
    )
    assert "sess-cron-1" in client._session_keepalives

    await client._handle_pod_push(
        "http://pod",
        _push_wire("sess-cron-1", is_processing=False),
    )
    assert "sess-cron-1" not in client._session_keepalives
    await asyncio.sleep(0.25)
    assert len(route.touch_calls) == 1
    assert len(route.route_calls) == 1

    await _drain(client, _envelope(method="chat.send", request_id="req-again"))
    assert "sess-cron-1" in client._session_keepalives
    forwarded: list[dict[str, Any]] = []

    async def _on_push(wire: dict[str, Any]) -> None:
        forwarded.append(wire)

    client.set_server_push_handler(_on_push)
    done = _push_wire("sess-cron-1", is_processing=False)
    await client._handle_pod_push("http://pod", done)
    assert forwarded == [done]
    assert "sess-cron-1" not in client._session_keepalives
    await client.disconnect()
    assert client._session_keepalives == {}


@pytest.mark.asyncio
async def test_open_stream_still_restores_affinity(monkeypatch):
    """非 deferred 的流仍按原来的方式 touch，失败后重新 route。"""
    _patch_authorize(monkeypatch)
    route = _FakeRoute(touch_results=[False, True, True, True, True, True])
    client = RuntimeRoutedAgentClient(
        route_client=route,
        http_client=_FakeHttp(stream_delay=0.12),
        touch_interval_seconds=0.04,
    )
    await client.connect("http://unused")
    monkeypatch.setattr(client, "_ensure_pod_push", AsyncMock())
    await _drain(client, _envelope(method="chat.send", request_id="req-restore"))
    assert len(route.route_calls) >= 2
    assert client._session_keepalives == {}
    await client.disconnect()
