# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime import enterprise_attachment_materializer as mod


def test_enterprise_files_need_download_true_when_url_without_path():
    files = [{"name": "a.png", "url": "http://minio/default/a.png"}]
    assert mod.enterprise_files_need_download(files) is True


def test_enterprise_files_need_download_false_when_local_path_exists(tmp_path: Path):
    local = tmp_path / "a.png"
    local.write_bytes(b"png")
    files = [{"name": "a.png", "url": "http://minio/default/a.png", "path": str(local)}]
    assert mod.enterprise_files_need_download(files) is False


@pytest.mark.asyncio
async def test_materialize_url_attachments_writes_to_workspace_uploads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "session"
    workspace.mkdir()
    captured: dict[str, object] = {}

    def _fake_download(url: str, dest: Path, *, max_file_size: int, timeout: int) -> None:
        captured["url"] = url
        captured["dest"] = dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-image")

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setattr(mod, "_download_http_to_path", _fake_download)

    files = [{"name": "screenshot.png", "url": "http://minio/default/screenshot.png"}]
    result = await mod.materialize_url_attachments(files, str(workspace), request_id="req_1")

    assert result is not files
    assert result[0]["path"] == str(workspace / "uploads" / "screenshot.png")
    assert Path(result[0]["path"]).is_file()
    assert captured["url"] == "http://minio/default/screenshot.png"


@pytest.mark.asyncio
async def test_materialize_skips_when_not_enterprise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    files = [{"name": "a.png", "url": "http://minio/default/a.png"}]
    result = await mod.materialize_url_attachments(files, str(tmp_path))
    assert result is files
    assert "path" not in result[0]


def test_download_http_to_path_rejects_non_minio_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class _Cfg:
        endpoint = "127.0.0.1:9000"
        public_base_url = ""

    monkeypatch.setattr(
        "jiuwenswarm.channels.web.minio_upload.load_minio_upload_config",
        lambda: _Cfg(),
    )
    dest = tmp_path / "uploads" / "x.bin"
    with pytest.raises(ValueError, match="not allowed"):
        mod._download_http_to_path(
            "http://169.254.169.254/latest/meta-data",
            dest,
            max_file_size=1024,
            timeout=5,
        )
    assert not dest.exists()
