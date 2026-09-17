from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import time
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.tools import web_file_download
from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    WebFileDownloadManager,
)
from jiuwenswarm.channels.web.app_web import _SpaStaticHandler


class _DownloadHandlerStub:
    def __init__(
        self,
        *,
        command: str = "HEAD",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.headers = headers or {}
        self.wfile = io.BytesIO()
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}
        self.headers_ended = False

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.response_headers[name] = value

    def end_headers(self) -> None:
        self.headers_ended = True

    def _write_json(self, status: int, payload: dict) -> None:
        raise AssertionError(f"unexpected JSON response: {status} {payload}")

    def log_error(self, message: str, *args: object) -> None:
        raise AssertionError(message % args)


def _serve_file(
    monkeypatch: pytest.MonkeyPatch,
    file_path: Path,
    query: dict[str, str],
    *,
    command: str = "HEAD",
    headers: dict[str, str] | None = None,
) -> _DownloadHandlerStub:
    monkeypatch.setattr(
        web_file_download,
        "validate_file_download_token",
        lambda _token: {"path": str(file_path)},
    )
    handler = _DownloadHandlerStub(command=command, headers=headers)
    _SpaStaticHandler._handle_file_download(handler, query)
    return handler


def _signed_token(secret: str, payload: object) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    signature = hmac.new(
        secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{encoded}.{signature}"


def test_valid_token_is_accepted() -> None:
    manager = WebFileDownloadManager(secret="s" * 32)
    token = manager.generate_token("/tmp/report.xlsx", "session-1", expires_in=60)

    payload = manager.validate_token(token)
    assert payload is not None
    assert payload["path"] == "/tmp/report.xlsx"
    assert payload["exp"] == pytest.approx(int(time.time()) + 60, abs=1)
    assert payload["sid"] == "session-1"


def test_expiration_is_not_required_for_download() -> None:
    secret = "s" * 32
    manager = WebFileDownloadManager(secret=secret)
    token = _signed_token(
        secret,
        {"path": "/tmp/report.xlsx", "sid": "session-1"},
    )

    assert manager.validate_token(token) == {
        "path": "/tmp/report.xlsx",
        "sid": "session-1",
    }


def test_tampered_token_is_rejected() -> None:
    manager = WebFileDownloadManager(secret="s" * 32)
    token = manager.generate_token("/tmp/report.xlsx", expires_in=60)
    encoded, signature = token.split(".")

    assert manager.validate_token(f"{encoded}x.{signature}") is None


@pytest.mark.parametrize("inline_value", ["1", "true", "TRUE"])
def test_download_handler_uses_inline_disposition_for_preview(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    inline_value: str,
) -> None:
    file_path = tmp_path / "preview sample.pdf"
    file_path.write_bytes(b"%PDF-1.7")
    handler = _serve_file(
        monkeypatch,
        file_path,
        {"token": "signed-token", "inline": inline_value},
    )

    assert handler.status == 200
    assert handler.headers_ended is True
    assert handler.response_headers["Content-Type"] == "application/pdf"
    assert handler.response_headers["Accept-Ranges"] == "bytes"
    assert handler.response_headers["Content-Disposition"] == (
        "inline; filename*=UTF-8''preview%20sample.pdf"
    )


@pytest.mark.parametrize(
    "query", [{"token": "signed-token"}, {"token": "signed-token", "inline": "0"}]
)
def test_download_handler_keeps_attachment_disposition_for_download(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    query: dict[str, str],
) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF-1.7")
    handler = _serve_file(monkeypatch, file_path, query)

    assert handler.status == 200
    assert handler.response_headers["Content-Disposition"] == (
        "attachment; filename*=UTF-8''report.pdf"
    )


@pytest.mark.parametrize(
    ("range_header", "expected_body", "expected_content_range"),
    [
        ("bytes=2-5", b"2345", "bytes 2-5/10"),
        ("bytes=7-", b"789", "bytes 7-9/10"),
        ("bytes=-3", b"789", "bytes 7-9/10"),
        ("bytes=7-20", b"789", "bytes 7-9/10"),
    ],
)
def test_download_handler_serves_single_byte_range(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    range_header: str,
    expected_body: bytes,
    expected_content_range: str,
) -> None:
    file_path = tmp_path / "media.bin"
    file_path.write_bytes(b"0123456789")
    handler = _serve_file(
        monkeypatch,
        file_path,
        {"token": "signed-token", "inline": "1"},
        command="GET",
        headers={"Range": range_header},
    )

    assert handler.status == 206
    assert handler.response_headers["Content-Length"] == str(len(expected_body))
    assert handler.response_headers["Accept-Ranges"] == "bytes"
    assert handler.response_headers["Content-Range"] == expected_content_range
    assert handler.wfile.getvalue() == expected_body


@pytest.mark.parametrize(
    "range_header",
    [
        "items=0-1",
        "bytes=10-12",
        "bytes=3-2",
        "bytes=-0",
        "bytes=0-1,3-4",
    ],
)
def test_download_handler_rejects_invalid_byte_range(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    range_header: str,
) -> None:
    file_path = tmp_path / "media.bin"
    file_path.write_bytes(b"0123456789")
    handler = _serve_file(
        monkeypatch,
        file_path,
        {"token": "signed-token", "inline": "1"},
        command="GET",
        headers={"Range": range_header},
    )

    assert handler.status == 416
    assert handler.response_headers["Content-Range"] == "bytes */10"
    assert handler.wfile.getvalue() == b""
