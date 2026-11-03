"""Security tests for the bounded file-import downloader."""

from __future__ import annotations

import socket

import pytest

from jiuwenswarm.server.runtime.attachments import remote_fetch


def _public_dns(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def _private_dns(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self._offset = 0
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._body):
            return b""
        end = len(self._body) if size < 0 else self._offset + size
        chunk = self._body[self._offset:end]
        self._offset = end
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


class _FakeOpener:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.requested_url = ""

    def open(self, request, timeout=None):  # noqa: A002
        self.requested_url = request.full_url
        return self.response


@pytest.mark.parametrize(
    "raw",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "..\\..\\windows\\system32\\config",
        "a/b/c.txt",
        "bad\x00name.txt",
        "con:file",
        ".",
        "..",
    ],
)
def test_sanitize_filename_keeps_only_a_safe_basename(raw: str) -> None:
    name = remote_fetch.sanitize_filename(raw)

    assert name not in {"", ".", ".."}
    assert "/" not in name
    assert "\\" not in name
    assert "\x00" not in name
    assert ":" not in name


def test_sanitize_filename_truncates_long_names() -> None:
    name = remote_fetch.sanitize_filename("a" * 500 + ".zip")

    assert len(name) <= remote_fetch._MAX_FILENAME_LENGTH
    assert name.endswith(".zip")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a.txt",
        "https:///missing-host",
        "https://user:pass@example.com/a.txt",
        "https://example.com:not-a-port/a.txt",
    ],
)
def test_validate_import_url_rejects_unsafe_forms(url: str) -> None:
    with pytest.raises(remote_fetch.UnsafeImportUrlError):
        remote_fetch.validate_import_url(url)


def test_validate_import_url_rejects_private_addresses(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _private_dns)

    with pytest.raises(remote_fetch.UnsafeImportUrlError):
        remote_fetch.validate_import_url("http://internal.example/a.txt")


def test_validate_import_url_accepts_public_address(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)

    parsed = remote_fetch.validate_import_url("http://example.com/a.txt")

    assert parsed.hostname == "example.com"


def test_validate_import_url_rejects_when_any_address_is_private(monkeypatch) -> None:
    def mixed_dns(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mixed_dns)

    with pytest.raises(remote_fetch.UnsafeImportUrlError):
        remote_fetch.validate_import_url("http://example.com/a.txt")


def test_download_stays_inside_workspace(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    opener = _FakeOpener(_FakeResponse(b"payload"))
    monkeypatch.setattr(remote_fetch, "build_opener", lambda *_args: opener)

    target = remote_fetch.download_url_to_workspace(
        "http://example.com/a.txt",
        tmp_path,
        preferred_name="../../escape.txt",
    )

    assert target.parent == tmp_path.resolve()
    assert target.name == "escape.txt"
    assert target.read_bytes() == b"payload"


def test_download_rejects_oversize_body_and_removes_partial(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    opener = _FakeOpener(_FakeResponse(b"x" * 32))
    monkeypatch.setattr(remote_fetch, "build_opener", lambda *_args: opener)

    with pytest.raises(RuntimeError, match="exceeds"):
        remote_fetch.download_url_to_workspace(
            "http://example.com/a.txt",
            tmp_path,
            preferred_name="a.txt",
            max_bytes=16,
        )

    assert list(tmp_path.iterdir()) == []


def test_download_rejects_oversize_content_length(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    opener = _FakeOpener(_FakeResponse(b"", {"Content-Length": "4096"}))
    monkeypatch.setattr(remote_fetch, "build_opener", lambda *_args: opener)

    with pytest.raises(RuntimeError, match="too large"):
        remote_fetch.download_url_to_workspace(
            "http://example.com/a.txt",
            tmp_path,
            preferred_name="a.txt",
            max_bytes=16,
        )

    assert list(tmp_path.iterdir()) == []


def test_redirect_handler_revalidates_every_hop(monkeypatch) -> None:
    handler = remote_fetch._ValidatingRedirectHandler()
    monkeypatch.setattr(socket, "getaddrinfo", _private_dns)

    with pytest.raises(remote_fetch.UnsafeImportUrlError):
        handler.redirect_request(
            None, None, 302, "Found", {}, "http://127.0.0.1/secret"
        )
