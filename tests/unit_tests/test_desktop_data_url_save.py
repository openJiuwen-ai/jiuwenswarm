import base64
import errno
import os
import tempfile
from pathlib import Path

import pytest

from jiuwenswarm.channels.desktop import desktop_app


def _runtime() -> desktop_app.DesktopRuntime:
    return desktop_app.DesktopRuntime(
        frontend_host="127.0.0.1",
        ports={
            "app": 19001,
            "web": 19000,
            "frontend": 5173,
            "tui": 19002,
            "third_party": 19003,
        },
    )


def _data_url(mime_type: str, content: bytes, *, charset: bool = False) -> str:
    charset_parameter = ";charset=utf-8" if charset else ""
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{mime_type}{charset_parameter};base64,{encoded}"


@pytest.mark.parametrize(
    ("data_url", "filename", "expected_content", "expected_file_types"),
    [
        (
            _data_url("image/png", desktop_app.PNG_SIGNATURE + b"png-content"),
            "diagram.png",
            desktop_app.PNG_SIGNATURE + b"png-content",
            ("PNG Image (*.png)",),
        ),
        (
            _data_url(
                "image/svg+xml",
                b'<svg xmlns="http://www.w3.org/2000/svg"/>',
                charset=True,
            ),
            "diagram.svg",
            b'<svg xmlns="http://www.w3.org/2000/svg"/>',
            ("SVG Image (*.svg)",),
        ),
        (
            _data_url("text/plain", b"flowchart LR\nA --> B", charset=True),
            "diagram.mmd",
            b"flowchart LR\nA --> B",
            ("Mermaid Diagram (*.mmd)",),
        ),
    ],
)
def test_save_data_url_supports_diagram_exports(
    monkeypatch,
    tmp_path,
    data_url,
    filename,
    expected_content,
    expected_file_types,
):
    runtime = _runtime()
    target_path = tmp_path / filename
    selections = []

    def select_save_path(suggested_name: str, file_types: tuple[str, ...]) -> Path:
        selections.append((suggested_name, file_types))
        return target_path

    monkeypatch.setattr(runtime, "_select_save_path", select_save_path)

    result = runtime.save_data_url(data_url, filename)

    assert result == {"ok": True, "cancelled": False}
    assert selections == [(filename, expected_file_types)]
    assert target_path.read_bytes() == expected_content
    assert list(tmp_path.glob(".*.part")) == []


def test_save_data_url_reports_user_cancellation(monkeypatch):
    runtime = _runtime()
    monkeypatch.setattr(runtime, "_select_save_path", lambda filename, file_types: None)

    result = runtime.save_data_url(
        _data_url("text/plain", b"flowchart LR", charset=True),
        "diagram.mmd",
    )

    assert result == {"ok": False, "cancelled": True}


@pytest.mark.parametrize(
    ("data_url", "filename"),
    [
        ("data:text/html;base64,PGgxPm5vPC9oMT4=", "diagram.html"),
        (_data_url("image/svg+xml", b"<svg/>", charset=True), "diagram.png"),
        ("data:text/plain;charset=utf-8,not-base64", "diagram.mmd"),
        ("data:text/plain;charset=gbk;base64,QQ==", "diagram.mmd"),
        ("data:text/plain;charset=utf-8;base64,%%%", "diagram.mmd"),
        ("data:text/plain;charset=utf-8;base64,你好", "diagram.mmd"),
        (_data_url("image/png", b"not-a-png"), "diagram.png"),
    ],
)
def test_save_data_url_rejects_invalid_or_mismatched_payloads(
    monkeypatch, data_url, filename
):
    runtime = _runtime()

    def unexpected_dialog(*args, **kwargs):
        raise AssertionError("invalid exports must be rejected before showing a dialog")

    monkeypatch.setattr(runtime, "_select_save_path", unexpected_dialog)

    assert runtime.save_data_url(data_url, filename) == {
        "ok": False,
        "cancelled": False,
    }


def test_save_data_url_preserves_existing_file_when_atomic_replace_fails(
    monkeypatch, tmp_path
):
    runtime = _runtime()
    target_path = tmp_path / "diagram.svg"
    target_path.write_bytes(b"existing")
    monkeypatch.setattr(
        runtime, "_select_save_path", lambda filename, file_types: target_path
    )

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    result = runtime.save_data_url(
        _data_url("image/svg+xml", b"<svg/>", charset=True),
        "diagram.svg",
    )

    assert result == {"ok": False, "cancelled": False}
    assert target_path.read_bytes() == b"existing"
    assert list(tmp_path.glob(".*.part")) == []


def test_save_data_url_closes_and_removes_temp_file_when_fdopen_fails(
    monkeypatch, tmp_path
):
    runtime = _runtime()
    target_path = tmp_path / "diagram.svg"
    created_fd = None
    original_mkstemp = tempfile.mkstemp

    def tracked_mkstemp(*args, **kwargs):
        nonlocal created_fd
        created_fd, temp_name = original_mkstemp(*args, **kwargs)
        return created_fd, temp_name

    def fail_fdopen(fd: int, mode: str):
        raise OSError("fdopen failed")

    monkeypatch.setattr(
        runtime, "_select_save_path", lambda filename, file_types: target_path
    )
    monkeypatch.setattr(desktop_app.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(os, "fdopen", fail_fdopen)

    result = runtime.save_data_url(
        _data_url("image/svg+xml", b"<svg/>", charset=True),
        "diagram.svg",
    )

    assert result == {"ok": False, "cancelled": False}
    assert created_fd is not None
    with pytest.raises(OSError) as exc_info:
        os.fstat(created_fd)
    assert exc_info.value.errno == errno.EBADF
    assert list(tmp_path.glob(".*.part")) == []
