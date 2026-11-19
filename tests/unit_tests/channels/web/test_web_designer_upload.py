"""Tokenless multipart ``POST /file-api/upload`` stores designer canvas media locally."""

from __future__ import annotations

import io
import logging
from pathlib import Path
from urllib.parse import urlparse

import pytest

from jiuwenswarm.channels.web import app_web, designer_upload_http
from jiuwenswarm.channels.web.app_web import _SpaStaticHandler
from jiuwenswarm.channels.web.designer_upload_http import handle_designer_upload_http

_BOUNDARY = "----designer-upload"


class _UploadHandlerHarness(_SpaStaticHandler):
    """Real ``/file-api`` POST routing over an in-memory request."""

    def __init__(self, *, workspace_root: Path, path: str, headers: dict[str, str], body: bytes) -> None:
        self.workspace_root = workspace_root
        self.path = path
        self.headers = headers
        self.rfile = io.BytesIO(body)
        self.logger = logging.getLogger("test-web-designer-upload")
        self.sent: list[tuple[int, dict | None]] = []

    def _write_json(self, status: int, payload: dict | None) -> None:
        self.sent.append((status, payload))


def _multipart(filename: str, content: bytes, mime_type: str = "image/png") -> bytes:
    return (
        f"--{_BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode() + content + f"\r\n--{_BOUNDARY}--\r\n".encode()


def _upload(tmp_path: Path, body: bytes, *, length: int | None = None) -> tuple[int, dict]:
    return handle_designer_upload_http(
        content_type=f"multipart/form-data; boundary={_BOUNDARY}",
        body=io.BytesIO(body),
        length=len(body) if length is None else length,
        workspace_root=tmp_path,
    )


def _post(
    tmp_path: Path,
    body: bytes,
    *,
    path: str = "/file-api/upload",
    multipart: bool = True,
) -> _UploadHandlerHarness:
    content_type = f"multipart/form-data; boundary={_BOUNDARY}" if multipart else "application/octet-stream"
    handler = _UploadHandlerHarness(
        workspace_root=tmp_path,
        path=path,
        headers={"Content-Type": content_type, "Content-Length": str(len(body))},
        body=body,
    )
    handler._handle_file_api_post(urlparse(handler.path))
    return handler


def test_tokenless_multipart_upload_is_stored_under_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)

    handler = _post(tmp_path, _multipart("cat.png", b"\x89PNG-bytes"))

    status, payload = handler.sent[-1]
    assert status == 200
    assert payload is not None and payload["errors"] == []
    (stored,) = payload["files"]
    stored_path = Path(stored["path"])
    assert stored_path.parent == tmp_path / "uploads" / "designer"
    assert stored_path.name.endswith("_cat.png")
    assert stored_path.read_bytes() == b"\x89PNG-bytes"
    assert stored["filename"] == "cat.png"
    assert stored["mime_type"] == "image/png"
    assert stored["size"] == len(b"\x89PNG-bytes")


def test_token_upload_still_uses_the_agent_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    routed: list[str] = []
    monkeypatch.setattr(
        _SpaStaticHandler,
        "_handle_file_upload_proxy",
        lambda self, parsed: routed.append(parsed.query),
    )

    _post(tmp_path, b"raw-bytes", path="/file-api/upload?token=abc", multipart=False)
    _post(tmp_path, _multipart("cat.png", b"x"), path="/file-api/upload?token=abc")

    assert routed == ["token=abc", "token=abc"]
    assert not (tmp_path / "uploads").exists()


def test_tokenless_raw_upload_keeps_missing_token_error(tmp_path: Path) -> None:
    handler = _post(tmp_path, b"raw-bytes", multipart=False)

    assert handler.sent[-1] == (400, {"error": "missing_token"})


def test_oversized_request_is_drained_and_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)
    monkeypatch.setattr(designer_upload_http, "MAX_DESIGNER_UPLOAD_REQUEST_BYTES", 64)

    handler = _post(tmp_path, _multipart("big.mp4", b"0" * 128, "video/mp4"))

    status, payload = handler.sent[-1]
    assert status == 413
    assert payload is not None and payload["error"] == "file_too_large"
    assert handler.rfile.read() == b""
    assert not (tmp_path / "uploads").exists()


def test_video_gets_the_larger_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)
    monkeypatch.setattr(designer_upload_http, "MAX_DESIGNER_UPLOAD_BYTES", 8)
    monkeypatch.setattr(designer_upload_http, "MAX_DESIGNER_VIDEO_UPLOAD_BYTES", 16)

    image = _post(tmp_path, _multipart("big.png", b"0123456789", "image/png"))
    video = _post(tmp_path, _multipart("clip.mp4", b"0123456789", "video/mp4"))

    assert image.sent[-1][0] == 413
    assert image.rfile.read() == b""
    status, payload = video.sent[-1]
    assert status == 200
    assert payload is not None
    (stored,) = payload["files"]
    assert stored["mime_type"] == "video/mp4"
    assert Path(stored["path"]).read_bytes() == b"0123456789"
    assert [p.name for p in (tmp_path / "uploads" / "designer").iterdir()] == [Path(stored["path"]).name]


def test_video_over_its_limit_is_rejected_and_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)
    monkeypatch.setattr(designer_upload_http, "MAX_DESIGNER_VIDEO_UPLOAD_BYTES", 8)

    handler = _post(tmp_path, _multipart("clip.mp4", b"0123456789", "video/mp4"))

    status, payload = handler.sent[-1]
    assert status == 413
    assert payload is not None and payload["error"] == "file_too_large"
    assert handler.rfile.read() == b""
    assert not any((tmp_path / "uploads" / "designer").iterdir())


def test_agentos_routing_rejects_local_designer_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: True)

    handler = _post(tmp_path, _multipart("cat.png", b"x"))

    assert handler.sent[-1] == (503, {"error": "agentserver_file_rpc_required"})
    assert handler.rfile.read() == b""
    assert not (tmp_path / "uploads").exists()


def test_missing_file_field_is_rejected(tmp_path: Path) -> None:
    body = (
        f"--{_BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="note"\r\n\r\n'
        f"hello\r\n--{_BOUNDARY}--\r\n"
    ).encode()

    status, payload = _upload(tmp_path, body)

    assert status == 400
    assert payload["error"] == "missing_file"


def test_truncated_body_is_rejected_and_removed(tmp_path: Path) -> None:
    body = _multipart("cat.png", b"0123456789")

    status, payload = _upload(tmp_path, body[:-20], length=len(body))

    assert status == 400
    assert payload["error"] == "missing_file"
    assert not any((tmp_path / "uploads" / "designer").iterdir())


def test_unsafe_filename_is_sanitized(tmp_path: Path) -> None:
    status, payload = _upload(tmp_path, _multipart("../evil:name?.png", b"x"))

    assert status == 200
    stored_path = Path(payload["files"][0]["path"])
    assert stored_path.parent == tmp_path / "uploads" / "designer"
    assert stored_path.name.endswith("_evil_name_.png")


def test_utf8_filename_is_kept(tmp_path: Path) -> None:
    status, payload = _upload(tmp_path, _multipart("分镜 01.png", b"x"))

    assert status == 200
    (stored,) = payload["files"]
    assert stored["filename"] == "分镜 01.png"
    assert Path(stored["path"]).name.endswith("_分镜 01.png")
