"""`/file-api/file-content` 大小上限与 ``tail_bytes`` 语义。

无上限的全量读取曾让大文件预览与日志面板轮询把整个文件（原始字节 +
解码 str + 重编码 body）整份搬进内存；上限与尾部读取把每次请求的内存
峰值限制在常数级。
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.channels.web import app_web
from jiuwenswarm.channels.web.app_web import MAX_FILE_CONTENT_BYTES, _SpaStaticHandler


class _FileContentHarness(_SpaStaticHandler):
    """Bind the real GET handler onto a minimal request double."""

    workspace_root = Path("/nonexistent")
    agent_teams_root = Path("/nonexistent")
    logs_root = Path("/nonexistent")
    auto_harness_root = Path("/nonexistent")

    def __init__(self, *, project_root: Path, path: str, query: str) -> None:
        self.project_root = project_root
        self.directory = str(project_root)
        self.command = "GET"
        self.path = path
        self.headers = {}
        self.rfile = io.BytesIO()
        self.wfile = io.BytesIO()
        self.logger = logging.getLogger("test-web-file-content")
        self.sent_responses: list[tuple[int, dict | None]] = []
        self.sent_headers: dict[str, str] = {}
        self._parsed = SimpleNamespace(path=path, query=query)

    def send_response(self, status: int) -> None:
        self.sent_responses.append((status, None))

    def send_header(self, name: str, value: str) -> None:
        self.sent_headers[name] = value

    def end_headers(self) -> None:
        return None

    def _write_json(self, status: int, payload: dict) -> None:
        self.sent_responses.append((status, payload))

    def call(self) -> None:
        self._handle_file_api_get(self._parsed)


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_web, "_uses_agentos_routing", lambda: False)

    def make(relative_path: str, payload: bytes, query: str = "") -> _FileContentHarness:
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        # _is_path_under_allowed_root is a classmethod reading class attrs;
        # bind the test roots the same way test_web_file_api_errors does.
        # Relative paths must NOT start with "agent/": _resolve_project_path
        # rewrites that prefix to "domain/agent/", which would move the target
        # outside ``workspace_root`` and trip the allowed-root check.
        cls = _FileContentHarness
        cls.workspace_root = tmp_path.resolve()
        cls.agent_teams_root = (tmp_path / ".agent_teams").resolve()
        cls.logs_root = (tmp_path / ".logs").resolve()
        cls.auto_harness_root = (tmp_path / "auto-harness").resolve()
        cls.organizations_root = (tmp_path / ".organizations").resolve()
        handler = _FileContentHarness(
            project_root=tmp_path,
            path="/file-api/file-content",
            query="path=" + relative_path.replace("\\", "/") + (("&" + query) if query else ""),
        )
        return handler

    return make


def test_small_file_without_tail_returns_full_content(harness) -> None:
    handler = harness("workspace/small.md", "hello 预览".encode("utf-8"))
    handler.call()
    assert handler.sent_responses[-1][0] == 200
    assert handler.wfile.getvalue() == "hello 预览".encode("utf-8")
    assert handler.sent_headers["X-Truncated"] == "0"


def test_oversized_file_without_tail_returns_413(harness) -> None:
    payload = b"x" * (MAX_FILE_CONTENT_BYTES + 1)
    handler = harness("workspace/big.json", payload)
    handler.call()
    status, body = handler.sent_responses[-1]
    assert status == 413
    assert body is not None and body["error"] == "file_too_large"
    assert body["fileSize"] == len(payload)
    assert body["maxBytes"] == MAX_FILE_CONTENT_BYTES


def test_tail_bytes_returns_trailing_window_and_marks_truncation(harness) -> None:
    payload = "".join("line-%06d\n" % i for i in range(60000)).encode("utf-8")
    handler = harness("workspace/data.log", payload, query="tail_bytes=1024")
    handler.call()
    assert handler.sent_responses[-1][0] == 200
    body = handler.wfile.getvalue()
    # The window may start mid-line; the handler aligns it to the next
    # newline, so at most one line is dropped and every returned line is
    # complete.
    assert len(body) <= 1024
    lines = body.decode("utf-8").splitlines()
    assert len(lines) > 0
    assert all(line.startswith("line-") and len(line) == 11 for line in lines)
    assert lines[-1] == "line-%06d" % 59999
    assert handler.sent_headers["X-Truncated"] == "1"


def test_tail_bytes_larger_than_file_returns_everything(harness) -> None:
    payload = "small log line\n".encode("utf-8") * 10
    handler = harness(".logs/ws-dev.log", payload, query="tail_bytes=262144")
    handler.call()
    assert handler.sent_responses[-1][0] == 200
    assert handler.wfile.getvalue() == payload
    assert handler.sent_headers["X-Truncated"] == "0"


def test_tail_bytes_on_oversized_file_is_clamped_to_limit(harness) -> None:
    # A huge tail_bytes must not reopen the whole-file read: the window is
    # clamped to MAX_FILE_CONTENT_BYTES before reading.
    line = b"L" * 60 + b"\n"
    total = MAX_FILE_CONTENT_BYTES + 4096
    payload = (line * (total // len(line) + 1))[:total]
    handler = harness(
        "workspace/huge.log", payload, query="tail_bytes=999999999999"
    )
    handler.call()
    assert handler.sent_responses[-1][0] == 200
    body = handler.wfile.getvalue()
    assert len(body) <= MAX_FILE_CONTENT_BYTES
    assert len(body) > MAX_FILE_CONTENT_BYTES - 2 * len(line)
    lines = body.split(b"\n")
    assert all(part == b"L" * 60 for part in lines[:-1])
    assert 0 < len(lines[-1]) < len(line)
    assert handler.sent_headers["X-Truncated"] == "1"


def test_tail_digits_beyond_int_limit_is_treated_as_no_tail(harness) -> None:
    # CPython caps int() at 4300 digits; a longer digit string must not raise
    # out of the handler, just fall back to the plain (gate-protected) read.
    payload = "small log line\n".encode("utf-8") * 10
    handler = harness(".logs/ws-dev.log", payload, query="tail_bytes=" + "9" * 5000)
    handler.call()
    assert handler.sent_responses[-1][0] == 200
    assert handler.wfile.getvalue() == payload
    assert handler.sent_headers["X-Truncated"] == "0"


def test_tail_offset_inside_utf8_char_aligns_to_line_start(harness) -> None:
    # "中文日志行\n" is 16 bytes in UTF-8; tail_bytes=1454 puts the seek at
    # byte 2 of a 3-byte character, which strict decoding would reject.
    payload = "中文日志行\n".encode("utf-8") * 100
    assert len(payload) == 1600
    handler = harness(".logs/utf8-cut.log", payload, query="tail_bytes=1454")
    handler.call()
    assert handler.sent_responses[-1][0] == 200
    lines = handler.wfile.getvalue().decode("utf-8").splitlines()
    assert len(lines) > 1
    assert all(line == "中文日志行" for line in lines)
    assert handler.sent_headers["X-Truncated"] == "1"


def test_tail_offset_inside_gbk_char_aligns_to_line_start(harness) -> None:
    # Same cut through GBK double-byte characters with an explicit encoding;
    # strict decoding used to answer 400 invalid_encoding here.
    payload = "中文日志行\n".encode("gbk") * 100
    assert len(payload) == 1100
    handler = harness(
        ".logs/gbk-cut.log", payload, query="tail_bytes=989&encoding=gbk"
    )
    handler.call()
    assert handler.sent_responses[-1][0] == 200
    lines = handler.wfile.getvalue().decode("utf-8").splitlines()
    assert len(lines) > 1
    assert all(line == "中文日志行" for line in lines)
    assert handler.sent_headers["X-Truncated"] == "1"


def test_tail_with_explicit_encoding_decodes_trailing_window(harness) -> None:
    payload = ("中文日志行\n".encode("gbk")) * 200
    handler = harness(".logs/gbk.log", payload, query="tail_bytes=1024&encoding=gbk")
    handler.call()
    assert handler.sent_responses[-1][0] == 200
    text = handler.wfile.getvalue().decode("utf-8")
    # The window is aligned to a line boundary before decoding, so every
    # line - including the first - must be intact.
    lines = text.splitlines()
    assert len(lines) > 1
    assert all(line == "中文日志行" for line in lines)


def test_auto_encoding_still_detected_for_small_file(harness) -> None:
    # Repeat long enough for charset_normalizer to reach a confident GBK verdict.
    payload = "中文内容, 自动探测。\n".encode("gbk") * 60
    handler = harness("workspace/gbk.md", payload, query="encoding=auto")
    handler.call()
    assert handler.sent_responses[-1][0] == 200
    text = handler.wfile.getvalue().decode("utf-8")
    assert "中文内容, 自动探测。" in text
    assert handler.sent_headers["X-Original-Encoding"].lower().startswith("gb")
