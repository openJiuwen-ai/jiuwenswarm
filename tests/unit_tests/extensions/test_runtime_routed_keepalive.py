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
