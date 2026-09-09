# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contracts for signed legacy and verified-asset download tokens."""

from __future__ import annotations

import json
from pathlib import Path

from jiuwenswarm.agents.harness.common.tools.verified_download_assets import (
    VerifiedDownloadAsset,
    VerifiedDownloadAssetOwner,
)
from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    WebFileDownloadManager,
    build_verified_asset_download_info,
)


def _stage_asset(
    tmp_path: Path,
    *,
    now: float,
    expires_at: float,
) -> tuple[VerifiedDownloadAssetOwner, VerifiedDownloadAsset]:
    source = tmp_path / "approved report.md"
    source.write_text("approved", encoding="utf-8")
    owner = VerifiedDownloadAssetOwner(
        root=tmp_path / "assets",
        now_fn=lambda: now,
        start_sweeper=False,
    )
    asset = owner.stage(
        source,
        file_name=source.name,
        expires_at=expires_at,
    )
    return owner, asset


def test_legacy_token_is_rejected_after_expiry(
    monkeypatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_file_download.time.time",
        lambda: now[0],
    )
    manager = WebFileDownloadManager("s" * 32)
    token = manager.generate_token("/tmp/report.md", "session-a", expires_in=10)

    assert manager.validate_token(token) == {
        "path": "/tmp/report.md",
        "exp": 110,
        "sid": "session-a",
    }

    now[0] = 111.0
    assert manager.validate_token(token) is None


def test_verified_token_binds_durable_asset_claims(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_file_download.time.time",
        lambda: now[0],
    )
    owner, asset = _stage_asset(tmp_path, now=now[0], expires_at=160.0)
    manager = WebFileDownloadManager("s" * 32, asset_owner=owner)

    token = manager.generate_verified_asset_token(
        asset,
        file_name="../approved report.md",
        session_id="session-a",
    )

    assert manager.validate_token(token) == {
        "kind": "verified_asset_v1",
        "asset_id": asset.asset_id,
        "path": asset.sealed_path.as_posix(),
        "exp": 160.0,
        "size": asset.size_bytes,
        "digest": asset.content_digest,
        "name": "approved report.md",
        "sid": "session-a",
    }

    owner.revoke(asset)
    assert manager.validate_token(token) is None


def test_verified_token_or_sidecar_claim_tamper_is_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_file_download.time.time",
        lambda: now,
    )
    owner, asset = _stage_asset(tmp_path, now=now, expires_at=160.0)
    manager = WebFileDownloadManager("s" * 32, asset_owner=owner)
    token = manager.generate_verified_asset_token(
        asset,
        file_name="approved report.md",
        session_id="session-a",
    )

    replacement = "0" if token[-1] != "0" else "1"
    assert manager.validate_token(token[:-1] + replacement) is None

    sidecar = owner.root / f"{asset.asset_id}.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["content_digest"] = "sha256:" + ("0" * 64)
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    assert manager.validate_token(token) is None


def test_verified_token_rejects_sidecar_size_claim_tamper(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_file_download.time.time",
        lambda: now,
    )
    owner, asset = _stage_asset(tmp_path, now=now, expires_at=160.0)
    manager = WebFileDownloadManager("s" * 32, asset_owner=owner)
    token = manager.generate_verified_asset_token(
        asset,
        file_name="approved report.md",
        session_id="session-a",
    )
    sidecar = owner.root / f"{asset.asset_id}.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["size_bytes"] = asset.size_bytes + 1
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    assert manager.validate_token(token) is None


def test_verified_download_info_uses_sealed_asset_and_original_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_file_download.time.time",
        lambda: now,
    )
    owner, asset = _stage_asset(tmp_path, now=now, expires_at=160.0)
    manager = WebFileDownloadManager("s" * 32, asset_owner=owner)
    monkeypatch.setattr(WebFileDownloadManager, "_instance", manager)
    guessed_names: list[str] = []

    def guess_markdown_type(file_name: str) -> tuple[str, None]:
        guessed_names.append(file_name)
        return "text/markdown", None

    monkeypatch.setattr("mimetypes.guess_type", guess_markdown_type)

    info = build_verified_asset_download_info(
        asset,
        "../approved report.md",
        "session-a",
    )

    assert info["name"] == "approved report.md"
    assert info["size"] == len(b"approved")
    assert info["mime_type"] == "text/markdown"
    assert guessed_names == ["../approved report.md"]
    payload = manager.validate_token(info["download_token"])
    assert payload is not None
    assert payload["path"] == asset.sealed_path.as_posix()
    assert payload["name"] == "approved report.md"
