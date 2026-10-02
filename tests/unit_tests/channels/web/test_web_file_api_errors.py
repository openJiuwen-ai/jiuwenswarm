"""Regression tests for malformed /file-api requests.

Each case used to raise inside the request handler, escape ``do_GET`` /
``do_POST`` and drop the connection without a response (the client sees an
empty reply and the gateway logs a traceback per request).  They must now be
answered with a ``400`` JSON body instead.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest

from jiuwenswarm.channels.web import app_web
from jiuwenswarm.channels.web.app_web import _BadRequestError, _SpaStaticHandler


class _FileApiHandlerHarness(_SpaStaticHandler):
    """Bind the real handlers onto a minimal request double.

    ``SimpleHTTPRequestHandler.__init__`` needs a socket/request triad, so the
    constructor is replaced; the file-api helpers only rely on the attributes
    set below.
    """

    workspace_root = Path("/nonexistent")
    agent_teams_root = Path("/nonexistent")
    logs_root = Path("/nonexistent")
    auto_harness_root = Path("/nonexistent")

    def __init__(
        self,
        *,
        project_root: Path,
        path: str,
        command: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.project_root = project_root
        self.command = command
        self.path = path
        self.headers = headers or {}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.logger = logging.getLogger("test-web-file-api")
        self.responses: list[tuple[int, dict | None]] = []

    def send_response(self, status: int) -> None:
        self.responses.append((status, None))

    def send_header(self, name: str, value: str) -> None:
        return None

    def end_headers(self) -> None:
        return None

    def _write_json(self, status: int, payload: dict) -> None:
        self.responses.append((status, payload))


def _make_handler(tmp_path: Path, path: str, **kwargs) -> _FileApiHandlerHarness:
    # Every allowed root must be an absolute path on the same drive, otherwise
    # ``_is_path_under_allowed_root`` mixes roots and ``os.path.commonpath``
    # raises ValueError (which the real class catches and reports as denied).
    root = tmp_path.resolve()
    _FileApiHandlerHarness.workspace_root = root
    _FileApiHandlerHarness.agent_teams_root = (root / ".agent_teams").resolve()
    _FileApiHandlerHarness.logs_root = (root / ".logs").resolve()
    _FileApiHandlerHarness.auto_harness_root = (root / "auto-harness").resolve()
    return _FileApiHandlerHarness(project_root=tmp_path, path=path, **kwargs)


def _last(handler: _FileApiHandlerHarness) -> tuple[int, dict | None]:
    assert handler.responses, "handler produced no response"
    return handler.responses[-1]


def test_resolve_project_path_rejects_embedded_nul(tmp_path: Path) -> None:
    handler = _make_handler(tmp_path, "/file-api/list-files")
    with pytest.raises(_BadRequestError) as excinfo:
        handler._resolve_project_path("bad\x00name")
    assert str(excinfo.value) == "invalid_path"


def test_parse_content_length_variants() -> None:
    handler = _FileApiHandlerHarness(project_root=Path("/tmp"), path="/")
    assert handler._parse_content_length() == 0
    handler.headers = {"Content-Length": "12"}
    assert handler._parse_content_length() == 12
    handler.headers = {"Content-Length": "abc"}
    with pytest.raises(_BadRequestError) as excinfo:
        handler._parse_content_length()
    assert str(excinfo.value) == "invalid_content_length"


def test_read_request_body_rejects_invalid_content_length() -> None:
    handler = _FileApiHandlerHarness(
        project_root=Path("/tmp"),
        path="/file-api/skills/upload-temp",
        command="POST",
        headers={"Content-Length": "not-a-number"},
    )
    with pytest.raises(_BadRequestError):
        handler._read_request_body()


def test_file_api_get_nul_path_returns_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)
    handler = _make_handler(tmp_path, "/file-api/list-files?dir=bad%00dir")

    handler.do_GET()

    status, payload = _last(handler)
    assert status == 400
    assert payload == {"error": "invalid_path"}


def test_file_api_get_unknown_encoding_returns_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)
    (tmp_path / "note.md").write_text("hello", encoding="utf-8")
    handler = _make_handler(tmp_path, "/file-api/file-content?path=note.md&encoding=not-a-codec")

    handler.do_GET()

    status, payload = _last(handler)
    assert status == 400
    assert payload is not None and payload["error"] == "invalid_encoding"


def test_file_api_get_undecodable_content_returns_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)
    (tmp_path / "note.md").write_bytes(b"\xff\xfe\x00binary")
    handler = _make_handler(tmp_path, "/file-api/file-content?path=note.md&encoding=ascii")

    handler.do_GET()

    status, payload = _last(handler)
    assert status == 400
    assert payload is not None and payload["error"] == "invalid_encoding"


def test_file_api_get_valid_encoding_returns_200(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)
    (tmp_path / "note.md").write_text("hello", encoding="utf-8")
    handler = _make_handler(tmp_path, "/file-api/file-content?path=note.md&encoding=utf-8")

    handler.do_GET()

    status, _payload = _last(handler)
    assert status == 200


def test_file_api_post_invalid_content_length_returns_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)
    handler = _make_handler(
        tmp_path,
        "/file-api/ws-debug-config",
        command="POST",
        headers={"Content-Length": "abc"},
    )

    handler.do_POST()

    status, payload = _last(handler)
    assert status == 400
    assert payload == {"error": "invalid_content_length"}


def test_file_api_post_valid_ws_debug_config_returns_200(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)
    body = b'{"wsDisableCompress": true}'
    handler = _make_handler(
        tmp_path,
        "/file-api/ws-debug-config",
        command="POST",
        headers={"Content-Length": str(len(body))},
        body=body,
    )

    handler.do_POST()

    status, _payload = _last(handler)
    assert status == 200
