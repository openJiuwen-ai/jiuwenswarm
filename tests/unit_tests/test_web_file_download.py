# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jiuwenswarm.agents.harness.common.tools import web_file_download
from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    WebFileDownloadManager,
)
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_http_app import create_web_http_app
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig


def _download_client() -> TestClient:
    channel = WebChannel(WebChannelConfig(host="127.0.0.1", port=0), RobotMessageRouter())
    return TestClient(create_web_http_app(channel))


def _get_download(
    monkeypatch: pytest.MonkeyPatch,
    file_path: Path,
    *,
    inline: str | None = None,
    range_header: str | None = None,
    method: str = "GET",
):
    monkeypatch.setenv("JIUWENSWARM_WEB_RECEIVED_FILES", str(file_path.parent))
    monkeypatch.setattr(
        web_file_download,
        "validate_file_download_token",
        lambda _token: {"path": str(file_path)},
    )
    client = _download_client()
    params: dict[str, str] = {"token": "signed-token"}
    if inline is not None:
        params["inline"] = inline
    headers = {"Range": range_header} if range_header else None
    if method == "HEAD":
        return client.head("/file-api/download", params=params, headers=headers)
    return client.get("/file-api/download", params=params, headers=headers)


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
    token = manager.generate_token(
        "/tmp/1788249651_222.txt",
        "session-1",
        expires_in=60,
        file_name="222.txt",
    )

    payload = manager.validate_token(token)
    assert payload is not None
    assert payload["path"] == "/tmp/1788249651_222.txt"
    assert payload["name"] == "222.txt"
    assert payload["exp"] == pytest.approx(int(time.time()) + 60, abs=1)
    assert payload["sid"] == "session-1"


def test_download_handler_prefers_token_display_name(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """磁盘名带时间戳时，Content-Disposition 仍用 token.name。"""
    file_path = tmp_path / "1788249651_222.txt"
    file_path.write_bytes(b"888")
    monkeypatch.setenv("JIUWENSWARM_WEB_RECEIVED_FILES", str(file_path.parent))
    monkeypatch.setattr(
        web_file_download,
        "validate_file_download_token",
        lambda _token: {"path": str(file_path), "name": "222.txt"},
    )
    client = _download_client()
    response = client.head("/file-api/download", params={"token": "signed-token"})

    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        "attachment; filename*=UTF-8''222.txt"
    )


def test_missing_expiration_is_rejected() -> None:
    """payload 无 exp 时按 get('exp', 0) 视为已过期，validate_token 返回 None。"""
    secret = "s" * 32
    manager = WebFileDownloadManager(secret=secret)
    token = _signed_token(
        secret,
        {"path": "/tmp/report.xlsx", "sid": "session-1"},
    )

    assert manager.validate_token(token) is None


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
    response = _get_download(
        monkeypatch,
        file_path,
        inline=inline_value,
        method="HEAD",
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-disposition"] == (
        "inline; filename*=UTF-8''preview%20sample.pdf"
    )


@pytest.mark.parametrize(
    "inline",
    [None, "0"],
)
def test_download_handler_keeps_attachment_disposition_for_download(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    inline: str | None,
) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF-1.7")
    response = _get_download(monkeypatch, file_path, inline=inline, method="HEAD")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
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
    response = _get_download(
        monkeypatch,
        file_path,
        inline="1",
        range_header=range_header,
    )

    assert response.status_code == 206
    assert response.headers["content-length"] == str(len(expected_body))
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"] == expected_content_range
    assert response.content == expected_body


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
    response = _get_download(
        monkeypatch,
        file_path,
        inline="1",
        range_header=range_header,
    )

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"
    assert response.content == b""
