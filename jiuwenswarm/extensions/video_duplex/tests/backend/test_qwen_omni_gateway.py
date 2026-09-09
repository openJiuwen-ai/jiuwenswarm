from __future__ import annotations

import asyncio
import json

import pytest
from starlette.websockets import WebSocketState

from jiuwenswarm.extensions.video_duplex.backend import qwen_omni_gateway


def test_qwen_config_adds_model_query_and_preserves_existing_query(monkeypatch) -> None:
    monkeypatch.setenv(
        "QWEN_OMNI_REALTIME_URL",
        "wss://workspace.example/api-ws/v1/realtime?region=beijing",
    )
    monkeypatch.setenv("QWEN_OMNI_API_KEY", "secret")
    monkeypatch.setenv("QWEN_OMNI_MODEL_NAME", "qwen3.5-omni-flash-realtime")

    config = qwen_omni_gateway.QwenOmniRealtimeConfig.from_environment()

    assert config.upstream_with_model() == (
        "wss://workspace.example/api-ws/v1/realtime"
        "?region=beijing&model=qwen3.5-omni-flash-realtime"
    )


class _BrowserSocket:
    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTING
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None
        self._receive_count = 0

    async def accept(self) -> None:
        self.client_state = WebSocketState.CONNECTED

    async def receive(self) -> dict:
        if self._receive_count == 0:
            self._receive_count += 1
            return {"type": "websocket.receive", "text": json.dumps({"type": "session.update"})}
        await asyncio.Future()

    async def send_text(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def send_bytes(self, _message: bytes) -> None:
        raise AssertionError("unexpected binary upstream event")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)
        self.client_state = WebSocketState.DISCONNECTED


class _UpstreamSocket:
    def __init__(self) -> None:
        self.sent: list[str | bytes] = []
        self._sent = asyncio.Event()
        self._yielded = False

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)
        self._sent.set()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._yielded:
            raise StopAsyncIteration
        await self._sent.wait()
        self._yielded = True
        return json.dumps({"type": "session.created"})


class _UpstreamContext:
    def __init__(self, socket: _UpstreamSocket) -> None:
        self.socket = socket

    async def __aenter__(self) -> _UpstreamSocket:
        return self.socket

    async def __aexit__(self, *_args) -> None:
        return None


@pytest.mark.asyncio
async def test_gateway_injects_authorization_and_relays_both_directions(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_OMNI_REALTIME_URL", "wss://workspace.example/realtime")
    monkeypatch.setenv("QWEN_OMNI_API_KEY", "private-key")
    monkeypatch.setenv("QWEN_OMNI_MODEL_NAME", "qwen3.5-omni-flash-realtime")
    browser = _BrowserSocket()
    upstream = _UpstreamSocket()
    connect_call = {}

    def fake_connect(url, **kwargs):
        connect_call.update({"url": url, **kwargs})
        return _UpstreamContext(upstream)

    monkeypatch.setattr(qwen_omni_gateway.websockets, "connect", fake_connect)

    await qwen_omni_gateway.serve_qwen_omni_websocket(browser)

    assert connect_call["url"].endswith("?model=qwen3.5-omni-flash-realtime")
    assert connect_call["additional_headers"] == {"Authorization": "Bearer private-key"}
    assert json.loads(upstream.sent[0]) == {"type": "session.update"}
    assert browser.sent == [{"type": "session.created"}]
    assert browser.closed == (1000, "")
