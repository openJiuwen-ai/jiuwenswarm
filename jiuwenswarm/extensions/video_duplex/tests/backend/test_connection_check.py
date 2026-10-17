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
    ('{"type":"session.created"}', True),
    ('{"type":"error","error":{"message":"secret detail"}}', False),
])
async def test_qwen_checks_session_and_closes_socket(monkeypatch, event, success):
    monkeypatch.setenv("VIDEO_LIVE_MODE", "realtime")
    monkeypatch.setenv("VIDEO_REALTIME_PROVIDER", "qwen_omni")
    monkeypatch.setenv("QWEN_OMNI_REALTIME_URL", "wss://example.test/realtime")
    monkeypatch.setenv("QWEN_OMNI_MODEL_NAME", "test-model")
    monkeypatch.setenv("QWEN_OMNI_API_KEY", "test-key")
    connection = AsyncContext(SimpleNamespace(recv=AsyncMock(return_value=event)))
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


@pytest.mark.parametrize("models,success", [([{"id": "test-model"}], True), ([], False)])
async def test_joyai_checks_configured_model_and_closes_client(monkeypatch, models, success):
    monkeypatch.setattr(check, "settings_payload", lambda **_: {"values": {
        "video_live_provider": "joyai", "joyai_api_base": "https://example.test/v1/",
        "joyai_model": "test-model",
    }})
    monkeypatch.setenv("JOYAI_API_KEY", "test-key")
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": models})
    client = SimpleNamespace(get=AsyncMock(return_value=response))
    connection = AsyncContext(client)
    monkeypatch.setattr(check.httpx, "AsyncClient", lambda **_: connection)
    if success:
        await check.check_saved_connection()
    else:
        with pytest.raises(ValueError, match="not listed"):
            await check.check_saved_connection()
    assert connection.closed
    client.get.assert_awaited_once_with(
        "https://example.test/v1/models", headers={"Authorization": "Bearer test-key"},
    )
