from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from jiuwenswarm.agents.harness.common.tools import web_file_download
from jiuwenswarm.agents.harness.common.tools.verified_download_assets import (
    VerifiedDownloadAssetOwner,
)
from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    WebFileDownloadManager,
)
from jiuwenswarm.channels.web import app_web
from jiuwenswarm.channels.web.app_web import _SpaStaticHandler
from jiuwenswarm.server.agent_ws_server import _parse_single_byte_range


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
        self.logger = logging.getLogger("test-web-file-download")

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

    # 复用真实 Gateway 代理逻辑（stub 提供其依赖的 self 接口）
    _proxy_file_download = _SpaStaticHandler._proxy_file_download


class _UploadHandlerStub:
    def __init__(self, body: bytes) -> None:
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.logger = logging.getLogger("test-web-file-upload")
        self.response: tuple[int, dict] | None = None

    def _write_json(self, status: int, payload: dict) -> None:
        self.response = (status, payload)


class _FakeAgentDownloadServer(BaseHTTPRequestHandler):
    """模拟目标 AgentServer 的 ``/file-api/download`` 端点。

    复用真实的 ``_parse_single_byte_range``（AgentServer 侧解析函数），其余
    响应构造保持与 AgentServer 端点（inline/RFC5987/Range/206/416）一致，
    用于验证 Gateway 代理的透传行为。
    """

    file_path: Path | None = None

    def do_GET(self) -> None:  # noqa: N802
        query = parse_qs(urlsplit(self.path).query)
        if "token" not in query:
            self._plain(400, b"missing_token")
            return
        if self.file_path is None or not self.file_path.is_file():
            self._plain(404, b"file_not_found")
            return
        file_size = self.file_path.stat().st_size
        mime_type, _ = mimetypes.guess_type(self.file_path.name)
        raw_inline = query.get("inline")
        if isinstance(raw_inline, list):
            raw_inline = raw_inline[0] if raw_inline else ""
        inline = str(raw_inline or "").strip().lower() in {"1", "true"}
        disposition = "inline" if inline else "attachment"
        encoded = quote(self.file_path.name, safe="")
        base_headers = {
            "Content-Type": mime_type or "application/octet-stream",
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded}",
            "Accept-Ranges": "bytes",
        }
        range_header = self.headers.get("Range")
        byte_range = _parse_single_byte_range(range_header, file_size) if range_header else None
        if range_header and byte_range is None:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{file_size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = byte_range or (0, max(0, file_size - 1))
        with open(self.file_path, "rb") as f:
            f.seek(start)
            body = f.read(end - start + 1)
        self.send_response(206 if byte_range is not None else 200)
        for name, value in base_headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        if byte_range is not None:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()
        self.wfile.write(body)

    def _plain(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture
def fake_agent_server(tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAgentDownloadServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _serve_file(
    monkeypatch: pytest.MonkeyPatch,
    fake_agent_server: ThreadingHTTPServer,
    file_path: Path,
    query: dict[str, str],
    *,
    command: str = "HEAD",
    headers: dict[str, str] | None = None,
) -> _DownloadHandlerStub:
    """Phase 2 迁移后：Gateway 为薄代理，stub token 校验失败走纯代理路径。"""
    monkeypatch.setattr(
        web_file_download,
        "validate_file_download_token",
        lambda _token: None,
    )
    _FakeAgentDownloadServer.file_path = file_path
    monkeypatch.setattr(
        app_web,
        "_resolve_agent_http_base",
        lambda: f"http://127.0.0.1:{fake_agent_server.server_address[1]}",
    )
    handler = _DownloadHandlerStub(command=command, headers=headers)
    _SpaStaticHandler._handle_file_download(handler, query)
    return handler


def test_valid_token_is_accepted() -> None:
    manager = WebFileDownloadManager(secret="s" * 32)
    token = manager.generate_token("/tmp/report.xlsx", "session-1", expires_in=60)

    payload = manager.validate_token(token)
    assert payload is not None
    assert payload["path"] == "/tmp/report.xlsx"
    assert payload["exp"] == pytest.approx(int(time.time()) + 60, abs=1)
    assert payload["sid"] == "session-1"


def test_non_expiring_token_omits_exp() -> None:
    """send_file_to_user 交付产物应签发不过期令牌（payload 无 exp）。"""
    manager = WebFileDownloadManager(secret="s" * 32)
    token = manager.generate_token("/tmp/report.xlsx", "session-1", expires_in=None)

    payload = manager.validate_token(token)
    assert payload is not None
    assert payload["path"] == "/tmp/report.xlsx"
    assert "exp" not in payload
    assert payload["sid"] == "session-1"


def test_non_expiring_download_info_stays_valid(tmp_path: Path) -> None:
    file_path = tmp_path / "deliverable.txt"
    file_path.write_text("ok", encoding="utf-8")
    info = web_file_download.build_file_download_info(
        str(file_path), "deliverable.txt", "session-1"
    )
    payload = web_file_download.validate_file_download_token(info["download_token"])
    assert payload is not None
    assert "exp" not in payload


def test_expired_token_is_rejected() -> None:
    manager = WebFileDownloadManager(secret="s" * 32)
    token = manager.generate_token("/tmp/report.xlsx", "session-1", expires_in=-1)

    assert manager.validate_token(token) is None
    assert manager.validate_token(token, check_expiry=False) is not None


def test_tampered_token_is_rejected() -> None:
    manager = WebFileDownloadManager(secret="s" * 32)
    token = manager.generate_token("/tmp/report.xlsx", expires_in=60)
    encoded, signature = token.split(".")

    assert manager.validate_token(f"{encoded}x.{signature}") is None


def _signed_token(secret: str, payload: object) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    signature = hmac.new(
        secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{encoded}.{signature}"


def test_strict_legacy_token_without_expiration_remains_compatible() -> None:
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


@pytest.mark.parametrize(
    "payload",
    [
        {"path": "/tmp/report.xlsx"},
        {"path": "", "sid": "session-1"},
        {"path": "/tmp/report.xlsx", "sid": ""},
        {"path": 1, "sid": "session-1"},
        {"path": "/tmp/report.xlsx", "sid": 1},
        {"path": "/tmp/report.xlsx", "sid": "session-1", "extra": "claim"},
        {
            "path": "/tmp/report.xlsx",
            "sid": "session-1",
            "download_http_base": "",
        },
        {
            "path": "/tmp/report.xlsx",
            "sid": "session-1",
            "download_http_base": "http://test.invalid",
            "extra": "claim",
        },
        {
            "kind": "verified_asset_v1",
            "asset_id": "asset-1",
            "path": "/tmp/report.xlsx",
            "size": 1,
            "digest": "sha256:digest",
            "name": "report.xlsx",
            "sid": "session-1",
        },
    ],
)
def test_only_exact_legacy_no_expiration_payload_is_accepted(
    payload: dict[str, object],
) -> None:
    secret = "s" * 32
    manager = WebFileDownloadManager(secret=secret)

    assert manager.validate_token(_signed_token(secret, payload)) is None


def test_agent_http_bases_are_embedded_per_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """独立 Web 静态进程可由 AgentServer 签发的 token 路由到用户 sandbox。"""
    monkeypatch.setenv(
        "JIUWENSWARM_AGENT_DOWNLOAD_HTTP_BASE", "http://agent-a.invalid:18092"
    )
    monkeypatch.setenv(
        "JIUWENSWARM_AGENT_UPLOAD_HTTP_BASE", "http://agent-a.invalid:18093"
    )
    download_token = web_file_download.generate_file_download_token("/tmp/a.txt", "s1")
    upload_token = web_file_download.generate_file_upload_token("agent/workspace/a.txt", "s1")

    download_payload = web_file_download.validate_file_download_token(download_token)
    assert download_payload is not None
    assert download_payload["download_http_base"] == "http://agent-a.invalid:18092"

    assert (
        app_web.resolve_agent_http_base_for_token(
            download_token, endpoint="download"
        )
        == "http://agent-a.invalid:18092"
    )
    assert (
        app_web.resolve_agent_http_base_for_token(
            upload_token, endpoint="upload"
        )
        == "http://agent-a.invalid:18093"
    )


def test_upload_base_does_not_reuse_download_http_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_AGENT_UPLOAD_HTTP_BASE", raising=False)
    monkeypatch.delenv("JIUWENSWARM_AGENT_HTTP_PORT", raising=False)
    monkeypatch.setenv(
        "JIUWENSWARM_AGENT_HTTP_BASE", "http://agent-a.invalid:18092"
    )
    monkeypatch.setenv("AGENT_SERVER_PORT", "18092")

    assert app_web._resolve_agent_upload_base() == "http://127.0.0.1:18093"

    token = web_file_download.generate_file_upload_token("agent/workspace/a.txt", "s1")
    payload = WebFileDownloadManager.get_instance().validate_token(token)
    assert payload is not None
    assert "upload_http_base" not in payload


@pytest.mark.parametrize("inline_value", ["1"])
def test_download_handler_uses_inline_disposition_for_preview(
    tmp_path,
    fake_agent_server,
    monkeypatch: pytest.MonkeyPatch,
    inline_value: str,
) -> None:
    file_path = tmp_path / "preview sample.pdf"
    file_path.write_bytes(b"%PDF-1.7")
    handler = _serve_file(
        monkeypatch,
        fake_agent_server,
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


def test_download_handler_keeps_attachment_disposition_for_download(
    tmp_path,
    fake_agent_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF-1.7")
    handler = _serve_file(
        monkeypatch, fake_agent_server, file_path, {"token": "signed-token"}
    )

    assert handler.status == 200
    assert handler.response_headers["Content-Disposition"] == (
        "attachment; filename*=UTF-8''report.pdf"
    )


@pytest.mark.parametrize(
    ("range_header", "expected_body", "expected_content_range"),
    [
        ("bytes=2-5", b"2345", "bytes 2-5/10"),
        ("bytes=-3", b"789", "bytes 7-9/10"),
    ],
)
def test_download_handler_serves_single_byte_range(
    tmp_path,
    fake_agent_server,
    monkeypatch: pytest.MonkeyPatch,
    range_header: str,
    expected_body: bytes,
    expected_content_range: str,
) -> None:
    file_path = tmp_path / "media.bin"
    file_path.write_bytes(b"0123456789")
    handler = _serve_file(
        monkeypatch,
        fake_agent_server,
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
        "bytes=0-1,3-4",
    ],
)
def test_download_handler_rejects_invalid_byte_range(
    tmp_path,
    fake_agent_server,
    monkeypatch: pytest.MonkeyPatch,
    range_header: str,
) -> None:
    file_path = tmp_path / "media.bin"
    file_path.write_bytes(b"0123456789")
    handler = _serve_file(
        monkeypatch,
        fake_agent_server,
        file_path,
        {"token": "signed-token", "inline": "1"},
        command="GET",
        headers={"Range": range_header},
    )

    assert handler.status == 416
    assert handler.response_headers["Content-Range"] == "bytes */10"
    assert handler.wfile.getvalue() == b""


def test_download_handler_proxies_agent_server_403(
    tmp_path,
    fake_agent_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游 403（越界/token 无效）原样透传，不误报 503。"""
    file_path = tmp_path / "forbidden.txt"
    file_path.write_bytes(b"secret")

    class _ForbiddenServer(_FakeAgentDownloadServer):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(b"path_outside_workspace")))
            self.end_headers()
            self.wfile.write(b"path_outside_workspace")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _ForbiddenServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(
            web_file_download,
            "validate_file_download_token",
            lambda _token: None,
        )
        monkeypatch.setattr(
            app_web,
            "_resolve_agent_http_base",
            lambda: f"http://127.0.0.1:{server.server_address[1]}",
        )
        handler = _DownloadHandlerStub(command="GET")
        _SpaStaticHandler._handle_file_download(
            handler, {"token": "signed-token"}
        )
        assert handler.status == 403
        assert handler.wfile.getvalue() == b"path_outside_workspace"
    finally:
        server.shutdown()
        server.server_close()


def test_verified_single_user_download_fallback_keeps_range_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_path = tmp_path / "legacy-range.txt"
    file_path.write_bytes(b"legacy-content")
    monkeypatch.setattr(web_file_download, "validate_file_download_token", lambda _token: {"path": str(file_path)})
    monkeypatch.setattr(web_file_download, "is_path_within_user_dirs", lambda _path: True)
    def _unexpected_proxy(*_args, **_kwargs):
        raise AssertionError("legacy local download must not proxy to the WS port")

    monkeypatch.setattr(_SpaStaticHandler, "_proxy_file_download", _unexpected_proxy)
    handler = _DownloadHandlerStub(command="GET")
    handler.headers["Range"] = "bytes=0-5"

    _SpaStaticHandler._handle_file_download(handler, {"token": "legacy-token"})

    assert handler.status == 206
    assert handler.wfile.getvalue() == b"legacy"


def test_verified_agentos_token_uses_bridge_when_gateway_cannot_see_user_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共享密钥不应让 Gateway 把容器内路径误判为本地 404。"""
    container_only_path = tmp_path / "not-mounted-in-gateway" / "report.txt"
    monkeypatch.setenv(
        "JIUWENSWARM_AGENT_DOWNLOAD_HTTP_BASE",
        "http://agentserver-for-user.invalid:18092",
    )
    token = web_file_download.generate_file_download_token(
        str(container_only_path), "session-1"
    )
    calls: list[tuple[str, bool]] = []

    def _proxy(_self, token: str, *, inline: bool = False) -> bool:
        calls.append((token, inline))
        return True

    monkeypatch.setattr(_DownloadHandlerStub, "_proxy_file_download", _proxy)
    handler = _DownloadHandlerStub(command="GET")

    _SpaStaticHandler._handle_file_download(handler, {"token": token})

    assert calls == [(token, False)]


def test_verified_single_user_upload_persists_without_http_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)
    monkeypatch.setattr(
        web_file_download,
        "validate_file_download_token",
        lambda _token: {"path": "agent/sessions/s1/uploads/report.txt"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path
    )
    handler = _UploadHandlerStub(b"legacy upload")

    _SpaStaticHandler._handle_file_upload_proxy(
        handler, SimpleNamespace(query="token=legacy-upload-token")
    )

    target = tmp_path / "agent" / "sessions" / "s1" / "uploads" / "report.txt"
    assert target.read_bytes() == b"legacy upload"
    assert handler.response == (200, {"path": str(target), "size": 13})


def test_verified_asset_retry_range_and_concurrent_get_use_same_sealed_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "media.bin"
    source.write_bytes(b"0123456789")
    owner = VerifiedDownloadAssetOwner(
        root=tmp_path / "assets",
        start_sweeper=False,
    )
    asset = owner.stage(
        source,
        file_name=source.name,
        expires_at=time.time() + 60,
    )
    manager = WebFileDownloadManager("s" * 32, asset_owner=owner)
    monkeypatch.setattr(WebFileDownloadManager, "_instance", manager)
    token = manager.generate_verified_asset_token(
        asset,
        file_name=source.name,
        session_id="session-a",
    )
    source.write_bytes(b"changed-after-capture")

    def fetch(headers: dict[str, str] | None = None) -> _DownloadHandlerStub:
        handler = _DownloadHandlerStub(command="GET", headers=headers)
        _SpaStaticHandler._handle_file_download(handler, {"token": token})
        return handler

    first = fetch()
    retry = fetch()
    partial = fetch({"Range": "bytes=2-5"})
    with ThreadPoolExecutor(max_workers=4) as executor:
        concurrent_bodies = tuple(
            executor.map(lambda _index: fetch().wfile.getvalue(), range(4))
        )

    assert first.status == retry.status == 200
    assert first.wfile.getvalue() == retry.wfile.getvalue() == b"0123456789"
    assert partial.status == 206
    assert partial.wfile.getvalue() == b"2345"
    assert concurrent_bodies == (b"0123456789",) * 4
