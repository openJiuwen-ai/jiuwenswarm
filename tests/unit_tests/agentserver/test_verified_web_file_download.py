# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contracts for signed legacy and verified-asset download tokens."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

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
    reference_only: bool = False,
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
        reference_only=reference_only,
    )
    return owner, asset


def test_legacy_token_is_rejected_after_expiry(
    monkeypatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "jiuwenswarm.common.file_download_token.time.time",
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
        "jiuwenswarm.common.file_download_token.time.time",
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
        "jiuwenswarm.common.file_download_token.time.time",
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
        "jiuwenswarm.common.file_download_token.time.time",
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
        "jiuwenswarm.common.file_download_token.time.time",
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


def test_reference_token_through_symlink_parent_survives_owner_reload(tmp_path: Path) -> None:
    directory = tmp_path / "real-directory"
    directory.mkdir()
    alias = tmp_path / "directory-alias"
    alias.symlink_to(directory, target_is_directory=True)
    owner, asset = _stage_asset(alias, now=100, expires_at=0, reference_only=True)
    manager = WebFileDownloadManager("s" * 32, asset_owner=owner)
    token = manager.generate_verified_asset_token(asset, file_name="report.md", session_id="alias")

    recovered_owner = VerifiedDownloadAssetOwner(
        root=owner.root, now_fn=lambda: 100_000, start_sweeper=False,
    )
    recovered = WebFileDownloadManager("s" * 32, asset_owner=recovered_owner)
    payload = recovered.validate_token(token, session_id="alias")

    assert payload is not None
    assert Path(payload["path"]) == directory / "approved report.md"
    sidecar = json.loads((owner.root / f"{asset.asset_id}.json").read_text())
    assert sidecar["sealed_path"] == payload["path"]
    recovered_owner.prune()
    assert recovered.validate_token(token, session_id="alias") is not None


def test_reference_token_and_digest_restore_from_durable_history(tmp_path: Path) -> None:
    from jiuwenswarm.server.control.history_service import load_history_page
    from jiuwenswarm.server.runtime.session.session_history import (
        append_history_record,
        flush_history_writes,
    )

    owner, asset = _stage_asset(tmp_path, now=100, expires_at=0, reference_only=True)
    manager = WebFileDownloadManager("s" * 32, asset_owner=owner)
    token = manager.generate_verified_asset_token(asset, file_name="report.md", session_id="reference-history")
    url = manager.generate_download_url(token)
    append_history_record(
        session_id="reference-history", request_id="reference-request", channel_id="web",
        role="assistant", event_type="chat.file", content="", timestamp=100,
        extra={"files": [{"download_token": token, "download_url": url}]},
    )
    flush_history_writes()
    asset.sealed_path.write_bytes(b"different current content")
    recovered_owner = VerifiedDownloadAssetOwner(root=owner.root, now_fn=lambda: 100_000, start_sweeper=False)
    recovered = WebFileDownloadManager("s" * 32, asset_owner=recovered_owner)
    records = load_history_page("reference-history", 1)["messages"]
    saved = next(record for record in records if record.get("event_type") == "chat.file")["files"][0]
    restored = parse_qs(urlsplit(saved["download_url"]).query)["token"][0]
    assert restored == saved["download_token"] == token
    payload = recovered.validate_token(restored, session_id="reference-history")
    assert set(payload) == {"kind", "asset_id", "path", "exp", "size", "digest", "name", "sid"}
    assert payload["exp"] == 0
    assert payload["digest"] == "sha256:" + hashlib.sha256(b"approved").hexdigest()
    assert recovered.validate_token(restored, session_id="other-session") is None
    other_owner = VerifiedDownloadAssetOwner(root=tmp_path / "other-assets", start_sweeper=False)
    assert WebFileDownloadManager("s" * 32, asset_owner=other_owner).validate_token(restored) is None
    recovered_owner.revoke(asset)
    assert recovered.validate_token(restored) is None
    asset.sealed_path.unlink()
    assert json.loads(base64.urlsafe_b64decode(restored.split(".")[0]))["digest"] == payload["digest"]


@pytest.mark.parametrize("expiry", [False, None, "0", float("nan"), float("inf")])
def test_invalid_reference_expiry_never_bypasses_registration(tmp_path: Path, expiry) -> None:
    owner, asset = _stage_asset(tmp_path, now=100, expires_at=0, reference_only=True)
    manager = WebFileDownloadManager("s" * 32, asset_owner=owner)
    token = manager.generate_verified_asset_token(asset, file_name="report.md", session_id="reference")
    payload = manager.validate_token(token)
    payload["exp"] = expiry
    invalid = manager._sign_payload(payload)
    assert manager.validate_token(invalid) is None
    assert manager.validate_token(invalid, check_expiry=False) is None
