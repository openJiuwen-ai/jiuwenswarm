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


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks are unavailable on this platform")


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
    if os.name != "nt":
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
    if os.name == "nt":
        asset.sealed_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    asset.sealed_path.unlink()
    _symlink(asset.sealed_path, source)

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
    _symlink(symlink_root, real_root, directory=True)

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
        _symlink(source, target)
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


def test_stage_uses_authorized_descriptor_instead_of_reopening_path(tmp_path: Path) -> None:
    approved = tmp_path / "approved.bin"
    content = b"approved\r\nbinary\x1acontent"
    approved.write_bytes(content)
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"outside the authorized file")
    owner = _owner(tmp_path / "assets", [100.0])

    with approved.open("rb") as stream:
        stream.read(3)
        asset = owner.stage(
            replacement, file_name=approved.name, expires_at=160.0, source_fd=stream.fileno(),
        )
        assert not stream.closed

    assert asset.sealed_path.read_bytes() == content
    assert asset.size_bytes == len(content)


def test_default_windows_asset_root_is_shared_by_processes_for_the_same_user(tmp_path, monkeypatch):
    from jiuwenswarm.common import verified_download_assets as assets

    monkeypatch.delenv("JIUWENSWARM_DOWNLOAD_ASSET_ROOT", raising=False)
    monkeypatch.delattr(assets.os, "getuid", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    monkeypatch.setattr(assets.os, "getpid", lambda: 101)
    first = assets._default_asset_root()
    monkeypatch.setattr(assets.os, "getpid", lambda: 202)

    assert assets._default_asset_root() == first
