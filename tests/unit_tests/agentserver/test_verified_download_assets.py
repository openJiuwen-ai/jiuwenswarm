# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contracts for TTL-owned verified download assets."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.tools.verified_download_assets import (
    VerifiedDownloadAssetOwner,
)


def _owner(root: Path, now: list[float]) -> VerifiedDownloadAssetOwner:
    return VerifiedDownloadAssetOwner(
        root=root,
        now_fn=lambda: now[0],
        orphan_grace_seconds=10.0,
        start_sweeper=False,
    )


def test_stage_is_durable_private_and_commit_keeps_same_asset(
    tmp_path: Path,
) -> None:
    now = [100.0]
    source = tmp_path / "report.md"
    source.write_text("approved", encoding="utf-8")
    root = tmp_path / "assets"
    owner = _owner(root, now)

    asset = owner.stage(
        source,
        file_name=source.name,
        expires_at=160.0,
    )

    assert asset.sealed_path.read_text(encoding="utf-8") == "approved"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(asset.sealed_path.stat().st_mode) == 0o400
    assert owner.is_active(
        asset_id=asset.asset_id,
        sealed_path=asset.sealed_path,
        expires_at=asset.expires_at,
        size_bytes=asset.size_bytes,
        content_digest=asset.content_digest,
    )

    owner.commit(asset)

    assert owner.is_active(
        asset_id=asset.asset_id,
        sealed_path=asset.sealed_path,
        expires_at=asset.expires_at,
        size_bytes=asset.size_bytes,
        content_digest=asset.content_digest,
    )


def test_restart_recovery_keeps_asset_until_ttl_then_reclaims_it(
    tmp_path: Path,
) -> None:
    now = [100.0]
    source = tmp_path / "report.pdf"
    source.write_bytes(b"approved")
    root = tmp_path / "assets"
    first_owner = _owner(root, now)
    asset = first_owner.stage(
        source,
        file_name=source.name,
        expires_at=110.0,
    )

    recovered_owner = _owner(root, now)
    recovered_owner.prune()
    assert asset.sealed_path.exists()
    assert recovered_owner.is_active(
        asset_id=asset.asset_id,
        sealed_path=asset.sealed_path,
        expires_at=asset.expires_at,
        size_bytes=asset.size_bytes,
        content_digest=asset.content_digest,
    )

    now[0] = 111.0
    recovered_owner.prune()

    assert not asset.sealed_path.exists()
    assert not (root / f"{asset.asset_id}.json").exists()
    assert not recovered_owner.is_active(
        asset_id=asset.asset_id,
        sealed_path=asset.sealed_path,
        expires_at=asset.expires_at,
        size_bytes=asset.size_bytes,
        content_digest=asset.content_digest,
    )


def test_revoke_removes_unexposed_staged_asset(tmp_path: Path) -> None:
    now = [100.0]
    source = tmp_path / "report.md"
    source.write_text("approved", encoding="utf-8")
    root = tmp_path / "assets"
    owner = _owner(root, now)
    asset = owner.stage(
        source,
        file_name=source.name,
        expires_at=160.0,
    )

    owner.revoke(asset)

    assert not asset.sealed_path.exists()
    assert not (root / f"{asset.asset_id}.json").exists()


def test_prune_uses_grace_for_unregistered_orphans(tmp_path: Path) -> None:
    now = [100.0]
    root = tmp_path / "assets"
    owner = _owner(root, now)
    owner.prune()
    recent = root / "recent.bin"
    old = root / "old.bin"
    recent.write_bytes(b"recent")
    old.write_bytes(b"old")
    os.utime(recent, (95.0, 95.0))
    os.utime(old, (80.0, 80.0))

    owner.prune()

    assert recent.exists()
    assert not old.exists()


def test_symlink_claim_is_never_active_or_followed(tmp_path: Path) -> None:
    now = [100.0]
    source = tmp_path / "report.md"
    source.write_text("approved", encoding="utf-8")
    root = tmp_path / "assets"
    owner = _owner(root, now)
    asset = owner.stage(
        source,
        file_name=source.name,
        expires_at=160.0,
    )
    asset.sealed_path.unlink()
    asset.sealed_path.symlink_to(source)

    assert not owner.is_active(
        asset_id=asset.asset_id,
        sealed_path=asset.sealed_path,
        expires_at=asset.expires_at,
        size_bytes=asset.size_bytes,
        content_digest=asset.content_digest,
    )
    owner.revoke(asset)
    assert source.read_text(encoding="utf-8") == "approved"


def test_asset_claim_cannot_escape_managed_root(tmp_path: Path) -> None:
    now = [100.0]
    source = tmp_path / "report.md"
    source.write_text("approved", encoding="utf-8")
    owner = _owner(tmp_path / "assets", now)
    asset = owner.stage(
        source,
        file_name=source.name,
        expires_at=160.0,
    )

    assert not owner.is_active(
        asset_id=asset.asset_id,
        sealed_path=tmp_path / "outside.md",
        expires_at=asset.expires_at,
        size_bytes=asset.size_bytes,
        content_digest=asset.content_digest,
    )


def test_asset_owner_rejects_symlink_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-assets"
    real_root.mkdir()
    symlink_root = tmp_path / "asset-link"
    symlink_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(ValueError, match="download_asset_root_invalid"):
        VerifiedDownloadAssetOwner(
            root=symlink_root,
            start_sweeper=False,
        )


@pytest.mark.parametrize("source_kind", ["symlink", "directory", "fifo"])
def test_stage_rejects_non_regular_source_without_residue(
    tmp_path: Path,
    source_kind: str,
) -> None:
    now = [100.0]
    source = tmp_path / "source"
    if source_kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"data")
        source.symlink_to(target)
    elif source_kind == "directory":
        source.mkdir()
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO is not supported on this platform")
        os.mkfifo(source)
    root = tmp_path / "assets"
    owner = _owner(root, now)

    with pytest.raises(ValueError, match="download_asset_source_not_file"):
        owner.stage(source, file_name="report.bin", expires_at=160.0)

    assert list(root.iterdir()) == []
