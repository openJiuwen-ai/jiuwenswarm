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


class _UploadHandlerHarness(_SpaStaticHandler):
    """Real ``/file-api`` POST routing over an in-memory request."""

    def __init__(self, *, headers: dict[str, str], body: bytes) -> None:
        self.path = _PATH
        self.headers = headers
        self.rfile = io.BytesIO(body)
        self.logger = logging.getLogger("test-web-custom-agent-upload")
        self.sent: list[tuple[int, dict | None]] = []

    def _write_json(self, status: int, payload: dict | None) -> None:
        self.sent.append((status, payload))


def _post(body: bytes) -> _UploadHandlerHarness:
    handler = _UploadHandlerHarness(
        headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(body))},
        body=body,
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

    handler = _post(b"0" * (_LIMIT * 2))

    assert handler.sent[-1] == (413, {"error": "upload too large"})
    # The body must be consumed before responding, otherwise closing the
    # socket resets the connection and browsers report "Failed to fetch".
    assert handler.rfile.read() == b""
    assert calls == []


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
