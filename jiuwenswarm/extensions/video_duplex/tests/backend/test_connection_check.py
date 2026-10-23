"""Connection checks use saved settings and never request media."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.extensions.video_duplex.backend import connection_check as check


class AsyncContext:
    def __init__(self, value):
        self.value = value
        self.closed = False

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        self.closed = True


@pytest.mark.parametrize("event,success", [
    ('{"type":"session.updated"}', True),
    ('{"type":"error","error":{"message":"secret detail"}}', False),
])
async def test_qwen_checks_session_and_closes_socket(monkeypatch, event, success):
    monkeypatch.setenv("VIDEO_LIVE_MODE", "realtime")
    monkeypatch.setenv("VIDEO_REALTIME_PROVIDER", "qwen_omni")
    monkeypatch.setenv("QWEN_OMNI_REALTIME_URL", "wss://example.test/realtime")
    monkeypatch.setenv("QWEN_OMNI_MODEL_NAME", "test-model")
    monkeypatch.setenv("QWEN_OMNI_API_KEY", "test-key")
    socket = SimpleNamespace(send=AsyncMock(), recv=AsyncMock(side_effect=['{"type":"session.created"}', event]))
    connection = AsyncContext(socket)
    calls = []

    def connect(url, **kwargs):
        calls.append((url, kwargs))
        return connection

    monkeypatch.setattr(check.websockets, "connect", connect)
    if success:
        await check.check_saved_connection()
    else:
        with pytest.raises(ValueError, match="provider rejected"):
            await check.check_saved_connection()
    assert connection.closed
    assert socket.recv.await_count == 2
    assert 'session.update' in socket.send.call_args.args[0]
    assert calls[0][0] == "wss://example.test/realtime?model=test-model"
    assert calls[0][1]["additional_headers"] == {"Authorization": "Bearer test-key"}


async def test_qwen_created_does_not_hide_later_untyped_access_denied(monkeypatch):
    monkeypatch.setattr(check, "settings_payload", lambda **_: {"values": {"video_live_provider": "qwen_omni"}})
    monkeypatch.setenv("QWEN_OMNI_REALTIME_URL", "wss://example.test/realtime")
    monkeypatch.setenv("QWEN_OMNI_MODEL_NAME", "test-model")
    monkeypatch.setenv("QWEN_OMNI_API_KEY", "test-key")
    socket = SimpleNamespace(send=AsyncMock(), recv=AsyncMock(side_effect=[
        '{"type":"session.created"}',
        '{"code":"AccessDenied.Unpurchased","message":"Model denied test-key"}',
    ]))
    connection = AsyncContext(socket)
    monkeypatch.setattr(check.websockets, "connect", lambda *args, **kwargs: connection)
    with pytest.raises(ValueError, match="AccessDenied.Unpurchased") as error:
        await check.check_saved_connection()
    assert "test-key" not in str(error.value)
    assert connection.closed


@pytest.mark.parametrize("choices,success", [([{"message": {"content": "OK"}}], True), ([], False)])
async def test_joyai_checks_chat_completion_and_closes_client(monkeypatch, choices, success):
    monkeypatch.setattr(check, "settings_payload", lambda **_: {"values": {
        "video_live_provider": "joyai", "joyai_api_base": "https://example.test/v1/",
        "joyai_model": "test-model",
    }})
    monkeypatch.setenv("JOYAI_API_KEY", "test-key")
    monkeypatch.setattr(check.uuid, "uuid4", lambda: SimpleNamespace(hex="session-id"))
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"choices": choices})
    client = SimpleNamespace(post=AsyncMock(return_value=response))
    connection = AsyncContext(client)
    monkeypatch.setattr(check.httpx, "AsyncClient", lambda **_: connection)
    if success:
        await check.check_saved_connection()
    else:
        with pytest.raises(ValueError, match="invalid chat response"):
            await check.check_saved_connection()
    assert connection.closed
    client.post.assert_awaited_once_with(
        "https://example.test/v1/chat/completions",
        headers={
            "Authorization": "Bearer test-key",
            "x-streaming-session": "connection-check-session-id",
            "x-system-prompt-key": "DEFAULT_SYSTEM_PROMPT_EN",
        },
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 8,
            "temperature": 0,
            "stream": False,
        },
    )
