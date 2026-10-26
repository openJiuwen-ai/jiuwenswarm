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
    connection = AsyncContext(SimpleNamespace(send=AsyncMock(), recv=AsyncMock(return_value=event)))
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
    assert calls[0][0] == "wss://example.test/realtime?model=test-model"
    assert calls[0][1]["additional_headers"] == {"Authorization": "Bearer test-key"}


@pytest.mark.parametrize("status,body,error", [
    (200, {"choices": [{"message": {"content": "OK"}}]}, None),
    (200, {"data": [{"id": "test-model"}]}, RuntimeError),
    (401, {"error": {"message": "Unauthorized"}}, RuntimeError),
    (429, {"error": {"message": "Too many requests"}}, check.joyai_provider.JoyAIRateLimitError),
])
async def test_joyai_checks_completion_and_closes_client(monkeypatch, status, body, error):
    monkeypatch.setenv("VIDEO_LIVE_MODE", "joyai")
    monkeypatch.setenv("JOYAI_API_BASE", "https://example.test/v1/")
    monkeypatch.setenv("JOYAI_MODEL_NAME", "test-model")
    monkeypatch.setenv("JOYAI_API_KEY", "test-key")
    monkeypatch.setenv("JOYAI_SYSTEM_PROMPT_KEY", "test-prompt")
    response = SimpleNamespace(status_code=status, text="provider error", json=lambda: body)
    client = SimpleNamespace(post=AsyncMock(return_value=response))
    connection = AsyncContext(client)
    monkeypatch.setattr(check.joyai_provider.httpx, "AsyncClient", lambda **_: connection)
    if error is None:
        await check.check_saved_connection()
    else:
        with pytest.raises(error):
            await check.check_saved_connection()
    assert connection.closed
    client.post.assert_awaited_once()
    call = client.post.await_args
    assert call.args == ("https://example.test/v1/chat/completions",)
    assert call.kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert call.kwargs["headers"]["x-system-prompt-key"] == "test-prompt"
    assert call.kwargs["headers"]["x-streaming-session"].startswith("connection-check-")
    payload = call.kwargs["json"]
    assert payload["model"] == "test-model"
    assert payload["max_tokens"] == 8
    assert payload["stream"] is False
    content = payload["messages"][0]["content"]
    assert all(item["type"] == "text" for item in content)
    assert "Reply with OK." in content[0]["text"]


async def test_joyai_connection_checks_use_distinct_sessions(monkeypatch):
    monkeypatch.setenv("VIDEO_LIVE_MODE", "joyai")
    monkeypatch.setenv("JOYAI_API_BASE", "https://example.test/v1")
    monkeypatch.setenv("JOYAI_MODEL_NAME", "test-model")
    monkeypatch.setenv("JOYAI_API_KEY", "test-key")
    completion = AsyncMock()
    monkeypatch.setattr(check.joyai_provider, "request_completion", completion)
    await check.check_saved_connection()
    await check.check_saved_connection()
    first, second = completion.await_args_list
    assert first.args[2] != second.args[2]


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
