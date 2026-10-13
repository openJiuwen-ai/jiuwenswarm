# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import base64
import subprocess
import sys

import pytest

from jiuwenbox.server.routes.sandbox import _download_headers
from jiuwenbox.server.sandbox_manager import (
    _DOWNLOAD_NOFOLLOW_SCRIPT,
    _UPLOAD_NOFOLLOW_SCRIPT,
)


_MAX_BYTES = str(1024 * 1024)


def _run(
    script: str, path, data: bytes | None = None, *extra: str
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-S", "-c", script, str(path), *extra],
        input=data,
        capture_output=True,
        check=False,
    )


def test_download_headers_are_attachment_and_do_not_inject():
    headers = _download_headers("/sandbox/secret/报告\r\nX-Evil: 1.txt")
    disposition = headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert "%0D" in disposition
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "/sandbox/secret" not in disposition


def test_upload_and_download_fallback_round_trip(tmp_path):
    target = tmp_path / "nested" / "file.bin"
    payload = b"\x00\xffhello"
    uploaded = _run(_UPLOAD_NOFOLLOW_SCRIPT, target, payload)
    assert uploaded.returncode == 0
    assert target.read_bytes() == payload

    downloaded = _run(_DOWNLOAD_NOFOLLOW_SCRIPT, target, None, _MAX_BYTES)
    assert downloaded.returncode == 0
    assert downloaded.stdout == base64.b64encode(payload)


def test_download_fallback_script_rejects_file_over_limit(tmp_path):
    target = tmp_path / "big.bin"
    target.write_bytes(b"x" * 11)
    downloaded = _run(_DOWNLOAD_NOFOLLOW_SCRIPT, target, None, "10")
    assert downloaded.returncode == 47
    assert downloaded.stdout == b""

    at_limit = _run(_DOWNLOAD_NOFOLLOW_SCRIPT, target, None, "11")
    assert at_limit.returncode == 0


def test_fallback_refuses_symlink(tmp_path):
    target = tmp_path / "secret.bin"
    target.write_bytes(b"secret")
    link = tmp_path / "link.bin"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink is not permitted in this environment")

    uploaded = _run(_UPLOAD_NOFOLLOW_SCRIPT, link, b"overwrite")
    assert uploaded.returncode == 46
    assert target.read_bytes() == b"secret"

    downloaded = _run(_DOWNLOAD_NOFOLLOW_SCRIPT, link, None, _MAX_BYTES)
    assert downloaded.returncode == 46
    assert downloaded.stdout == b""
