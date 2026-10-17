# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the temp file the xiaoyi phone file tools download into."""

from __future__ import annotations

import asyncio
import os
import re
import stat
import tempfile
import time
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools import file_tools


class _FakeResponse:
    ok = True
    status = 200
    reason = "OK"

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _FakeSession:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def get(self, url, timeout=None):  # noqa: ARG002 - signature parity with aiohttp
        return _FakeResponse(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


@pytest.fixture
def fake_http(monkeypatch):
    """Serve a fixed payload for every download, with no network access."""

    def _install(payload: bytes = b"payload"):
        monkeypatch.setattr(
            file_tools.aiohttp,
            "ClientSession",
            lambda *args, **kwargs: _FakeSession(payload),
        )

    return _install


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """Redirect the system temporary directory into the test's own tmp_path."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return tmp_path


def test_repeated_downloads_of_one_url_do_not_collide(temp_root, fake_http):
    """A same-second retry used to reuse the timestamped name and overwrite."""
    fake_http(b"payload")
    url = "https://example.invalid/files/report.pdf"

    first = asyncio.run(file_tools._download_remote_file(url))
    second = asyncio.run(file_tools._download_remote_file(url))

    assert first != second
    assert Path(first).exists()
    assert Path(second).exists()
    assert Path(first).read_bytes() == b"payload"


def test_downloaded_name_keeps_the_stem_and_extension(temp_root, fake_http):
    """Regression guard: the file stays recognisable in a directory listing."""
    fake_http()

    local_path = Path(asyncio.run(file_tools._download_remote_file("https://example.invalid/a/report.pdf")))

    assert local_path.parent == temp_root
    assert local_path.name.startswith("report_")
    assert local_path.suffix == ".pdf"


def test_url_supplied_name_is_reduced_to_a_safe_alphabet(temp_root, fake_http):
    """Nothing from the URL reaches the name except letters, digits and ``_.-``."""
    fake_http()

    local_path = Path(
        asyncio.run(file_tools._download_remote_file("https://example.invalid/x/%2e%2e%2f%2e%2e%2fetc%2fpasswd"))
    )

    assert local_path.parent == temp_root
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", local_path.name)
    assert not local_path.name.startswith(".")


def test_an_overlong_url_name_still_yields_a_usable_path(temp_root, fake_http):
    """An unbounded stem used to exceed NAME_MAX and fail the download outright."""
    fake_http()

    local_path = Path(
        asyncio.run(file_tools._download_remote_file(f"https://example.invalid/x/{'a' * 400}.pdf"))
    )

    assert local_path.parent == temp_root
    assert len(local_path.name) <= 255
    assert local_path.suffix == ".pdf"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks and permission bits")
def test_a_symlink_planted_at_the_target_is_not_followed(temp_root, fake_http):
    """A pre-placed symlink must not turn the download into a write-through."""
    fake_http(b"payload")
    victim = temp_root / "victim"
    victim.write_bytes(b"original")

    # Reproduce the names the old scheme would have picked -- stem, underscore,
    # one-second timestamp, extension -- the way a co-tenant able to guess them
    # would. Both sides of the second boundary are covered so the plant cannot
    # miss by a tick.
    now = int(time.time())
    planted = [temp_root / f"report_{now + offset}.pdf" for offset in (0, 1)]
    for link in planted:
        link.symlink_to(victim)

    local_path = Path(asyncio.run(file_tools._download_remote_file("https://example.invalid/a/report.pdf")))

    assert local_path not in planted
    assert not local_path.is_symlink()
    assert victim.read_bytes() == b"original"
    assert local_path.read_bytes() == b"payload"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_downloaded_file_is_private(temp_root, fake_http):
    """The allocated file is 0600; the plain open used the process umask."""
    fake_http()

    local_path = Path(asyncio.run(file_tools._download_remote_file("https://example.invalid/a/report.pdf")))

    assert stat.S_IMODE(local_path.stat().st_mode) == 0o600
