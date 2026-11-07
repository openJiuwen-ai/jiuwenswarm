# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HttpSseAgentServerClient：并发流不得吞掉同 session 父流 chunk。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.extensions.runtime_management_extension import pod_http_client as pod_http
from jiuwenswarm.extensions.runtime_management_extension.pod_http_client import (
    HttpSseAgentServerClient,
)


def _legacy_chunk(request_id: str, event_type: str, **extra: Any) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "channel_id": "web",
        "payload": {"event_type": event_type, **extra},
        "is_complete": False,
        "metadata": {},
        "sequence": 0,
    }


def _envelope(request_id: str, *, input_mode: str | None = None) -> E2AEnvelope:
    params: dict[str, Any] = {"session_id": "sess-1", "content": "x"}
    if input_mode:
        params["input_mode"] = input_mode
        params["expected_execution_id"] = "exec-1"
    return E2AEnvelope(
        method="chat.send",
        session_id="sess-1",
        request_id=request_id,
        channel="web",
        user_id="u1",
        params=params,
        is_stream=True,
    )


class _FakeResponse:
    def __init__(
        self,
        frames: list[dict[str, Any]],
        *,
        gate: asyncio.Event | None = None,
        entered: asyncio.Event | None = None,
    ) -> None:
        self.status_code = 200
        self._frames = frames
        self._gate = gate
        self._entered = entered

    async def aiter_lines(self) -> AsyncIterator[str]:
        import json

        if self._entered is not None:
            self._entered.set()
        if self._gate is not None:
            await self._gate.wait()
        for frame in self._frames:
            yield f"data: {json.dumps(frame, ensure_ascii=False)}"
            yield ""

    async def aread(self) -> bytes:
        return b""


@pytest.mark.asyncio
async def test_steer_stream_does_not_supersede_parent_input_received(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同 session 开 steer 短流时，父流随后到达的 chat.input_received 不得被丢弃。"""
    parent_rid = "req_parent_1"
    steer_rid = "req_steer_2"
    parent_gate = asyncio.Event()
    parent_entered = asyncio.Event()
    parent_frames = [
        _legacy_chunk(parent_rid, "chat.output_phase", output_phase_id="p1"),
        _legacy_chunk(
            parent_rid,
            "chat.input_received",
            input_request_id=steer_rid,
            content="2",
            timestamp=1_793_671_642_014,
        ),
        _legacy_chunk(parent_rid, "chat.reasoning", content="..."),
    ]
    steer_frames = [
        _legacy_chunk(steer_rid, "runtime.accepted", input_boundary="stream"),
        {
            "request_id": steer_rid,
            "channel_id": "web",
            "payload": None,
            "is_complete": True,
            "metadata": {},
            "sequence": 1,
        },
    ]

    client = HttpSseAgentServerClient(timeout_s=30.0)
    client._base_url = "http://agentserver.test"
    client._api_root = "http://agentserver.test/api/v1"
    client._server_ready = True
    client._running = True

    streams: dict[str, _FakeResponse] = {
        parent_rid: _FakeResponse(
            parent_frames, gate=parent_gate, entered=parent_entered
        ),
        steer_rid: _FakeResponse(steer_frames),
    }

    @asynccontextmanager
    async def fake_stream(verb: str, url: str, **kwargs: Any):
        body = kwargs.get("json") or {}
        rid = str(body.get("request_id") or "")
        yield streams[rid]

    fake_http = AsyncMock()
    fake_http.stream = fake_stream
    client._http = fake_http
    client._owns_http = False

    async def _assemble(envelope: E2AEnvelope, api_root: str):
        return SimpleNamespace(
            verb="POST",
            url=f"{api_root}/chat/completions",
            headers={},
            json_body={"request_id": envelope.request_id},
            query=None,
            used_rpc_fallback=False,
        )

    monkeypatch.setattr(pod_http, "_assemble_with_allowlist", _assemble)
    monkeypatch.setattr(
        client,
        "_request_headers",
        lambda headers=None: dict(headers or {}),
    )

    parent_events: list[str] = []

    async def read_parent() -> None:
        async for chunk in client.send_request_stream(_envelope(parent_rid)):
            payload = chunk.payload if isinstance(chunk.payload, dict) else {}
            parent_events.append(str(payload.get("event_type") or ""))

    parent_task = asyncio.create_task(read_parent())
    await asyncio.wait_for(parent_entered.wait(), timeout=2)

    steer_events: list[str] = []
    async for chunk in client.send_request_stream(
        _envelope(steer_rid, input_mode="steer")
    ):
        payload = chunk.payload if isinstance(chunk.payload, dict) else {}
        if payload.get("event_type"):
            steer_events.append(str(payload["event_type"]))

    assert steer_events == ["runtime.accepted"]
    assert not client._should_drop_stream_chunk(
        envelope_rid=parent_rid, chunk_rid=parent_rid
    )

    parent_gate.set()
    await parent_task
    assert parent_events == [
        "chat.output_phase",
        "chat.input_received",
        "chat.reasoning",
    ]


@pytest.mark.asyncio
async def test_finished_stream_still_drops_only_its_residual_chunks() -> None:
    client = HttpSseAgentServerClient(timeout_s=30.0)
    rid = "req_done_1"
    peer = "req_peer_2"

    await client._mark_stream_cancelled(rid)

    assert client._should_drop_stream_chunk(envelope_rid=rid, chunk_rid=rid)
    assert not client._should_drop_stream_chunk(envelope_rid=peer, chunk_rid=peer)
