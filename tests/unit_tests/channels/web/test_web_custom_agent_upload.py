"""Oversized ``POST /file-api/external-cli/custom-agents`` rejects after draining the body."""

from __future__ import annotations

import io
import logging
from urllib.parse import urlparse

import pytest

from jiuwenswarm.channels.web import app_web, custom_agent_upload_http
from jiuwenswarm.channels.web.app_web import _SpaStaticHandler

_PATH = "/file-api/external-cli/custom-agents"
# Tiny stand-in for MAX_CUSTOM_AGENT_UPLOAD_BYTES so tests never build 512MB bodies.
_LIMIT = 64


class _StubConnection:
    """Minimal socket stand-in for the drain idle-timeout calls."""

    def __init__(self) -> None:
        self.timeout: float | None = None

    def gettimeout(self) -> float | None:
        return self.timeout

    def settimeout(self, value: float | None) -> None:
        self.timeout = value


class _StalledBody(io.BytesIO):
    """Body reader that stops delivering bytes mid-request."""

    def read(self, size: int = -1) -> bytes:
        raise TimeoutError("stalled client")


class _UploadHandlerHarness(_SpaStaticHandler):
    """Real ``/file-api`` POST routing over an in-memory request."""

    def __init__(self, *, headers: dict[str, str], rfile: io.BytesIO) -> None:
        self.path = _PATH
        self.headers = headers
        self.rfile = rfile
        self.connection = _StubConnection()
        self.logger = logging.getLogger("test-web-custom-agent-upload")
        self.sent: list[tuple[int, dict | None]] = []
        self.body_offsets_at_write: list[int] = []

    def _write_json(self, status: int, payload: dict | None) -> None:
        self.body_offsets_at_write.append(self.rfile.tell())
        self.sent.append((status, payload))


def _post(body: bytes, *, rfile: io.BytesIO | None = None) -> _UploadHandlerHarness:
    handler = _UploadHandlerHarness(
        headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(body))},
        rfile=rfile if rfile is not None else io.BytesIO(body),
    )
    handler._handle_file_api_post(urlparse(_PATH))
    return handler


def test_oversized_upload_is_drained_and_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)
    monkeypatch.setattr(custom_agent_upload_http, "MAX_CUSTOM_AGENT_UPLOAD_BYTES", _LIMIT)
    calls: list[bytes] = []

    def _handle(*, content_type: str, body: bytes) -> tuple[int, dict]:
        calls.append(body)
        return 200, {}

    monkeypatch.setattr(custom_agent_upload_http, "handle_custom_agent_upload_http", _handle)

    body = b"0" * (_LIMIT * 2)
    handler = _post(body)

    assert handler.sent[-1] == (413, {"error": "upload too large"})
    # The response must only be written once the body has been drained,
    # otherwise closing the socket resets the connection and browsers report
    # "Failed to fetch".
    assert handler.body_offsets_at_write == [len(body)]
    assert handler.rfile.read() == b""
    assert calls == []


def test_stalled_upload_drain_times_out_and_closes_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)
    monkeypatch.setattr(custom_agent_upload_http, "MAX_CUSTOM_AGENT_UPLOAD_BYTES", _LIMIT)

    handler = _post(b"0" * (_LIMIT * 2), rfile=_StalledBody())

    assert handler.sent[-1] == (413, {"error": "upload too large"})
    assert handler.close_connection is True
    # The previous socket timeout is restored after the bounded drain.
    assert handler.connection.gettimeout() is None


def test_upload_at_the_limit_is_processed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)
    monkeypatch.setattr(custom_agent_upload_http, "MAX_CUSTOM_AGENT_UPLOAD_BYTES", _LIMIT)
    received: list[bytes] = []

    def _handle(*, content_type: str, body: bytes) -> tuple[int, dict]:
        received.append(body)
        return 200, {"agent": {"name": "demo"}}

    monkeypatch.setattr(custom_agent_upload_http, "handle_custom_agent_upload_http", _handle)

    handler = _post(b"0" * _LIMIT)

    assert handler.sent[-1] == (200, {"agent": {"name": "demo"}})
    assert received == [b"0" * _LIMIT]


def test_upload_one_byte_over_the_limit_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)
    monkeypatch.setattr(custom_agent_upload_http, "MAX_CUSTOM_AGENT_UPLOAD_BYTES", _LIMIT)

    handler = _post(b"0" * (_LIMIT + 1))

    assert handler.sent[-1] == (413, {"error": "upload too large"})
    assert handler.rfile.read() == b""


def test_agentos_routing_drains_body_before_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: True)

    handler = _post(b"0" * 128)

    assert handler.sent[-1] == (
        503,
        {"error": "custom agents are not supported in AgentOS mode"},
    )
    assert handler.rfile.read() == b""
