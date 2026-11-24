# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""workspace preview / download 上限：512KiB 缺省、1MiB 硬顶、超 E2A inline 改 token。"""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.enterprise.capabilities.workspace.service import (
    WorkspaceService,
    _DEFAULT_PREVIEW_MAX_BYTES,
    _PREVIEW_HARD_MAX_BYTES,
)
from jiuwenswarm.server.runtime.gateway_adapter import workspace_file_adapter as mod


@pytest.fixture
def svc(tmp_path: Path) -> WorkspaceService:
    return WorkspaceService(tmp_path)


def test_preview_default_is_512kib(svc: WorkspaceService, tmp_path: Path):
    data = b"a" * (_DEFAULT_PREVIEW_MAX_BYTES + 100)
    (tmp_path / "big.txt").write_bytes(data)
    out = svc.preview_file("big.txt")
    assert out["truncated"] is True
    assert len(out["content"].encode("utf-8")) == _DEFAULT_PREVIEW_MAX_BYTES


def test_preview_hard_cap_1mib(svc: WorkspaceService, tmp_path: Path):
    data = b"b" * (_PREVIEW_HARD_MAX_BYTES + 50)
    (tmp_path / "huge.txt").write_bytes(data)
    out = svc.preview_file("huge.txt", max_bytes=_PREVIEW_HARD_MAX_BYTES + 9999)
    assert out["truncated"] is True
    assert len(out["content"].encode("utf-8")) == _PREVIEW_HARD_MAX_BYTES


def test_preview_exact_1mib_boundary(svc: WorkspaceService, tmp_path: Path):
    data = b"c" * _PREVIEW_HARD_MAX_BYTES
    (tmp_path / "exact.txt").write_bytes(data)
    out = svc.preview_file("exact.txt", max_bytes=_PREVIEW_HARD_MAX_BYTES)
    assert out["truncated"] is False
    assert len(out["content"].encode("utf-8")) == _PREVIEW_HARD_MAX_BYTES


def test_workspace_download_inline_under_limit(monkeypatch):
    monkeypatch.setattr(mod, "_workspace_e2a_inline_max_bytes", lambda: 1024)
    download_svc = SimpleNamespace(
        download_payload=lambda _rel: {
            "filename": "a.txt",
            "content_type": "text/plain",
            "content": b"hello",
        }
    )
    out = mod._workspace_download_response(
        download_svc, "a.txt", session_id="s", user_id="u"
    )
    assert out["content_b64"] == base64.b64encode(b"hello").decode("ascii")
    assert "download_url" not in out


def test_workspace_download_token_over_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_workspace_e2a_inline_max_bytes", lambda: 8)
    big = b"0123456789abcdef"
    download_svc = SimpleNamespace(
        download_payload=lambda _rel: {
            "filename": "big.bin",
            "content_type": "application/octet-stream",
            "content": big,
        },
        resolve=lambda _rel: (_ for _ in ()).throw(FileNotFoundError()),
    )
    staged = SimpleNamespace(
        asset_id="aid",
        sealed_path=tmp_path / "aid.bin",
        expires_at=99.0,
        size_bytes=len(big),
        content_digest="d" * 64,
    )
    owner = MagicMock()
    owner.stage.return_value = staged
    monkeypatch.setattr(
        "jiuwenswarm.common.verified_download_assets.get_verified_download_asset_owner",
        lambda: owner,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.file_download_token.build_verified_asset_download_info",
        lambda *_a, **_k: {
            "download_token": "tok",
            "download_url": "/file-api/download?token=tok",
            "name": "big.bin",
            "size": len(big),
            "mime_type": "application/octet-stream",
        },
    )
    out = mod._workspace_download_response(
        download_svc, "big.bin", session_id="s", user_id="u"
    )
    assert "content_b64" not in out
    assert out["download_url"] == "/file-api/download?token=tok"
    assert out["size_bytes"] == len(big)
    owner.commit.assert_called_once()
