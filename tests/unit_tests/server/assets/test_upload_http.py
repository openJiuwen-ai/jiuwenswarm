# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""/file-api/assets/upload-temp HTTP 面与暂存目录单测。"""

from __future__ import annotations

import io
import os
import time
import zipfile
from pathlib import Path

import pytest

from jiuwenswarm.common.safe_archive import ArchiveLimits
from jiuwenswarm.server.runtime.assets import staging
from jiuwenswarm.server.runtime.assets.upload_http import (
    handle_assets_upload_temp_http,
)

_BOUNDARY = "----assetboundary"


def _multipart(
    fields: dict[str, str] | None = None,
    files: dict[str, tuple[str, bytes]] | None = None,
) -> tuple[str, bytes]:
    buffer = io.BytesIO()
    for name, value in (fields or {}).items():
        buffer.write(
            f'--{_BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    for name, (filename, content) in (files or {}).items():
        buffer.write(
            (
                f'--{_BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
        )
        buffer.write(content)
        buffer.write(b"\r\n")
    buffer.write(f"--{_BOUNDARY}--\r\n".encode())
    return f"multipart/form-data; boundary={_BOUNDARY}", buffer.getvalue()


def _zip_bytes(entry: str = "root/f.txt") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(entry, "x")
    return buffer.getvalue()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr("jiuwenswarm.common.utils.get_workspace_dir", lambda: root)
    return root


# ------------------------------------------------------------------ 上传落盘


def test_upload_stages_zip_under_controlled_root(workspace):
    content_type, body = _multipart({}, {"file": ("my-expert.zip", _zip_bytes())})
    status, payload = handle_assets_upload_temp_http(content_type=content_type, body=body)
    assert status == 200
    assert set(payload) == {"path"}
    dest = Path(payload["path"])
    assert dest.name == "my-expert.zip"
    assert dest.read_bytes() == _zip_bytes()
    assert dest.is_relative_to(workspace / "temp" / "asset_uploads")
    # 每次上传独立子目录
    assert dest.parent != workspace / "temp" / "asset_uploads"


def test_upload_same_filename_does_not_collide(workspace):
    content_type, body = _multipart({}, {"file": ("a.zip", _zip_bytes())})
    first = handle_assets_upload_temp_http(content_type=content_type, body=body)[1]["path"]
    second = handle_assets_upload_temp_http(content_type=content_type, body=body)[1]["path"]
    assert first != second
    assert Path(first).is_file() and Path(second).is_file()


# ------------------------------------------------------------------ 入参拒绝


def test_upload_requires_file_field(workspace):
    content_type, body = _multipart({"kind": "skill"}, {})
    status, payload = handle_assets_upload_temp_http(content_type=content_type, body=body)
    assert status == 400
    assert payload["code"] == "ASSET_INVALID_PACKAGE"


def test_upload_requires_multipart(workspace):
    status, payload = handle_assets_upload_temp_http(
        content_type="application/json", body=b"{}"
    )
    assert status == 400
    assert payload["code"] == "ASSET_INVALID_PACKAGE"


@pytest.mark.parametrize(
    "filename",
    [
        "../evil.zip",
        "a/b.zip",
        # email 解析器对 quoted-string 做反斜杠转义还原，头部需写双反斜杠，
        # 到达校验层的才是单 backslash
        "a\\\\b.zip",
        "",
        ".",
        "..",
        "CON.zip",
    ],
)
def test_upload_rejects_unsafe_filename(workspace, filename):
    content_type, body = _multipart({}, {"file": (filename, _zip_bytes())})
    status, payload = handle_assets_upload_temp_http(content_type=content_type, body=body)
    assert status == 400
    assert payload["code"] == "ASSET_UNSAFE_PATH"


@pytest.mark.parametrize("filename", ["pack.tar.gz", "pack.zip.exe", "pack"])
def test_upload_rejects_non_zip(workspace, filename):
    content_type, body = _multipart({}, {"file": (filename, b"data")})
    status, payload = handle_assets_upload_temp_http(content_type=content_type, body=body)
    assert status == 400
    assert payload["code"] == "ASSET_INVALID_PACKAGE"


def test_upload_rejects_unknown_kind(workspace):
    content_type, body = _multipart(
        {"kind": "expert"}, {"file": ("a.zip", _zip_bytes())}
    )
    status, payload = handle_assets_upload_temp_http(content_type=content_type, body=body)
    assert status == 400
    assert payload["code"] == "ASSET_INVALID_PACKAGE"


@pytest.mark.parametrize("kind", ["skill", "agent_templates", "agent-group", "plugin"])
def test_upload_accepts_contract_kind_aliases(workspace, kind):
    content_type, body = _multipart({"kind": kind}, {"file": ("a.zip", _zip_bytes())})
    status, payload = handle_assets_upload_temp_http(content_type=content_type, body=body)
    assert status == 200
    assert Path(payload["path"]).is_file()


def test_upload_rejects_oversized_content(workspace):
    content_type, body = _multipart({}, {"file": ("big.zip", b"0" * 100)})
    status, payload = handle_assets_upload_temp_http(
        content_type=content_type,
        body=body,
        limits=ArchiveLimits(zip_bytes=10),
    )
    assert status == 413
    assert payload["code"] == "ASSET_LIMIT_EXCEEDED"


# -------------------------------------------------------------- 暂存与 TTL


def test_staging_roots_under_workspace_temp(workspace):
    assert staging.assets_export_root() == workspace / "temp" / "asset_exports"
    assert staging.assets_upload_root() == workspace / "temp" / "asset_uploads"
    assert staging.assets_export_root().is_dir()
    assert staging.assets_upload_root().is_dir()


def test_prune_stale_entries_keeps_fresh(workspace, tmp_path):
    root = tmp_path / "stale-root"
    fresh_dir = root / "fresh"
    fresh_dir.mkdir(parents=True)
    (fresh_dir / "f.zip").write_bytes(b"x")
    old_dir = root / "old"
    old_dir.mkdir()
    (old_dir / "f.zip").write_bytes(b"x")
    old_file = root / "old.zip"
    old_file.write_bytes(b"x")
    stale_ts = time.time() - staging.STAGING_TTL_SECONDS - 60
    os.utime(old_dir, (stale_ts, stale_ts))
    os.utime(old_file, (stale_ts, stale_ts))

    assert staging.prune_stale_entries(root) == 2
    assert (fresh_dir / "f.zip").is_file()
    assert not old_dir.exists()
    assert not old_file.exists()


def test_prune_missing_root_is_noop(tmp_path):
    assert staging.prune_stale_entries(tmp_path / "absent") == 0


def test_prune_symlink_to_dir_only_removes_link(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.zip").write_bytes(b"x")
    link = root / "link"
    link.symlink_to(target)
    stale_ts = time.time() - staging.STAGING_TTL_SECONDS - 60
    os.utime(link, (stale_ts, stale_ts), follow_symlinks=False)

    assert staging.prune_stale_entries(root) == 1
    assert not link.exists()
    assert target.is_dir() and (target / "keep.zip").is_file()


def test_upload_prunes_stale_staging(workspace):
    root = staging.assets_upload_root()
    stale = root / "stale-upload"
    stale.mkdir()
    (stale / "a.zip").write_bytes(b"x")
    stale_ts = time.time() - staging.STAGING_TTL_SECONDS - 60
    os.utime(stale, (stale_ts, stale_ts))

    content_type, body = _multipart({}, {"file": ("new.zip", _zip_bytes())})
    status, _ = handle_assets_upload_temp_http(content_type=content_type, body=body)
    assert status == 200
    assert not stale.exists()
