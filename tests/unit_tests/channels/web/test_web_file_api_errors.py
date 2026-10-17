"""Regression tests for malformed /file-api and malformed static-path requests.

Each case used to raise inside the request handler, escape ``do_GET`` /
``do_POST`` / ``send_head`` and drop the connection without a response (the
client sees an empty reply and the gateway logs a traceback per request).
They must now be answered with a ``400`` JSON body instead.
"""

from __future__ import annotations

import io
import logging
import sys
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
        self.directory = str(project_root)
        self.command = command
        self.path = path
        self.headers = headers or {}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.logger = logging.getLogger("test-web-file-api")
        # 不要叫 ``responses``：那会遮蔽 BaseHTTPRequestHandler.responses
        # （状态码 -> 报文 的类属性字典），让走到 send_error() 的分支变成
        # IndexError 而掩盖真实错误。
        self.sent_responses: list[tuple[int, dict | None]] = []

    def send_response(self, status: int) -> None:
        self.sent_responses.append((status, None))

    def send_header(self, name: str, value: str) -> None:
        return None

    def end_headers(self) -> None:
        return None

    def _write_json(self, status: int, payload: dict) -> None:
        self.sent_responses.append((status, payload))


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
    assert handler.sent_responses, "handler produced no response"
    return handler.sent_responses[-1]


def test_resolve_project_path_rejects_embedded_nul(tmp_path: Path) -> None:
    handler = _make_handler(tmp_path, "/file-api/list-files")
    with pytest.raises(_BadRequestError) as excinfo:
        handler._resolve_project_path("bad\x00name")
    assert str(excinfo.value) == "invalid_path"


def test_resolve_project_path_rejects_nul_without_resolve_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NUL 判定不能依赖 ``resolve()``：Windows 各 Python 版本行为不一致。"""
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: self)
    handler = _make_handler(tmp_path, "/file-api/list-files")
    with pytest.raises(_BadRequestError):
        handler._resolve_project_path("bad\x00name")


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


def test_send_head_embedded_nul_returns_400(tmp_path: Path) -> None:
    """``/%00`` must not escape ``send_head``.

    ``handle_one_request`` only catches ``TimeoutError``; a ``ValueError`` from
    ``Path.resolve()`` / ``os.path.commonpath`` reaches ``socketserver``'s error
    handler and the connection is closed with no HTTP response at all. NUL is
    rejected explicitly because ``resolve()`` only raises on POSIX.
    """
    handler = _make_handler(tmp_path, "/%00")

    assert handler.send_head() is None
    assert _last(handler) == (400, {"error": "invalid_path"})


def test_send_head_nul_rejected_even_when_resolve_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不能依赖 ``Path.resolve()`` 抛异常：Windows 各 Python 版本行为不一致。

    3.11 在 NUL 处静默截断、3.13 原样返回含 NUL 的路径，只有 POSIX 一定抛
    ValueError。把 resolve 打桩成不抛异常后，NUL 仍必须被挡成 400。
    """
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: self)
    handler = _make_handler(tmp_path, "/%00")

    assert handler.send_head() is None
    assert _last(handler) == (400, {"error": "invalid_path"})


@pytest.mark.skipif(sys.platform != "win32", reason="cross-drive paths only exist on Windows")
def test_send_head_cross_drive_path_returns_400(tmp_path: Path) -> None:
    """``/D:/x`` resolves onto another drive, so ``commonpath`` raises ValueError."""
    handler = _make_handler(tmp_path, "/D:/x")

    assert handler.send_head() is None
    assert _last(handler) == (400, {"error": "invalid_path"})


def test_send_head_clean_path_is_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed path must still fall through to the static branch, unguarded."""

    def _stub(_self):
        return io.BytesIO()

    monkeypatch.setattr(app_web.SimpleHTTPRequestHandler, "send_head", _stub)
    (tmp_path / "note.md").write_text("hi", encoding="utf-8")
    handler = _make_handler(tmp_path, "/note.md")

    assert handler.send_head() is not None
    assert handler.sent_responses == []


def test_harness_does_not_shadow_base_response_table() -> None:
    """``responses`` 必须仍是 BaseHTTPRequestHandler 的状态码表。

    测试桩曾用 ``self.responses = []`` 遮蔽它，使 send_error() 分支炸
    IndexError 而掩盖真实错误。
    """
    handler = _FileApiHandlerHarness(project_root=Path("/tmp"), path="/")
    assert isinstance(handler.responses, dict)
    assert handler.responses is _SpaStaticHandler.responses
    assert handler.sent_responses == []
