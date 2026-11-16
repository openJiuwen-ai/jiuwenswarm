# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contracts for TTL-owned verified download assets."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.tools.verified_download_assets import (
    VerifiedDownloadAssetOwner,
)
from jiuwenswarm.common import utils, verified_download_assets


def _owner(root: Path, now: list[float]) -> VerifiedDownloadAssetOwner:
    return VerifiedDownloadAssetOwner(
        root=root,
        now_fn=lambda: now[0],
        orphan_grace_seconds=10.0,
        start_sweeper=False,
    )


def test_windows_default_root_is_stable_and_follows_user_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIUWENSWARM_DOWNLOAD_ASSET_ROOT", raising=False)
    simulated_os = SimpleNamespace(getenv=os.getenv, getpid=lambda: 123)
    monkeypatch.setattr(verified_download_assets, "os", simulated_os)
    first_data = tmp_path / "first-data"
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(first_data))
    monkeypatch.setattr(utils, "_workspace_base_dir", None)
    first_root = verified_download_assets._default_asset_root()
    assert first_root == first_data / "download-assets"
    simulated_os.getpid = lambda: 456
    assert verified_download_assets._default_asset_root() == first_root

    second_data = tmp_path / "second-data"
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(second_data))
    monkeypatch.setattr(utils, "_workspace_base_dir", None)
    second_root = verified_download_assets._default_asset_root()
    assert second_root == second_data / "download-assets"
    assert second_root != first_root
    assert not first_root.exists() and not second_root.exists()


def test_default_root_preserves_uid_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_DOWNLOAD_ASSET_ROOT", raising=False)
    monkeypatch.setattr(
        verified_download_assets, "os", SimpleNamespace(getenv=os.getenv, getuid=lambda: 42),
    )
    assert verified_download_assets._default_asset_root() == (
        Path(tempfile.gettempdir()) / "jiuwenswarm-download-assets-42"
    )


@pytest.mark.parametrize("has_getuid", [False, True])
def test_default_root_keeps_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_getuid: bool,
) -> None:
    configured = tmp_path / "configured-assets"
    monkeypatch.setenv("JIUWENSWARM_DOWNLOAD_ASSET_ROOT", str(configured))
    simulated_os = SimpleNamespace(getenv=os.getenv)
    if has_getuid:
        simulated_os.getuid = lambda: 42
    monkeypatch.setattr(verified_download_assets, "os", simulated_os)
    assert verified_download_assets._default_asset_root() == configured
    assert not configured.exists()


@pytest.mark.parametrize("reference_only", [False, True])
def test_windows_default_registration_restores_in_another_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference_only: bool,
) -> None:
    source = tmp_path / "report.md"
    source.write_text("approved", encoding="utf-8")
    data = tmp_path / "data"
    monkeypatch.delenv("JIUWENSWARM_DOWNLOAD_ASSET_ROOT", raising=False)
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(data))
    script = """
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from jiuwenswarm.common import verified_download_assets as assets
from jiuwenswarm.common.file_download_token import WebFileDownloadManager

assets.os = SimpleNamespace(**{name: getattr(os, name) for name in dir(os) if name != 'getuid'})
owner = assets.VerifiedDownloadAssetOwner(start_sweeper=False)
manager = WebFileDownloadManager('s' * 32, asset_owner=owner)
if sys.argv[1] == 'stage':
    reference = sys.argv[3] == 'True'
    source = Path(sys.argv[2])
    asset = owner.stage(source, file_name=source.name,
                        expires_at=0 if reference else time.time() + 600,
                        reference_only=reference)
    owner.commit(asset)
    result = {'token': manager.generate_verified_asset_token(asset, file_name=source.name, session_id='restart')}
else:
    payload = manager.validate_token(sys.argv[2], session_id='restart')
    assert payload is not None
    result = {'content': Path(payload['path']).read_text(encoding='utf-8'), 'digest': payload['digest']}
result.update(pid=os.getpid(), root=str(owner.root))
print(json.dumps(result))
"""
    staged = subprocess.run(
        [sys.executable, "-c", script, "stage", str(source), str(reference_only)],
        check=True, capture_output=True, text=True,
    )
    first = json.loads(staged.stdout.splitlines()[-1])
    restored = subprocess.run(
        [sys.executable, "-c", script, "restore", first["token"]],
        check=True, capture_output=True, text=True,
    )
    second = json.loads(restored.stdout.splitlines()[-1])
    assert first["pid"] != second["pid"]
    assert first["root"] == second["root"] == str((data / "download-assets").resolve())
    assert second["content"] == "approved"
    assert second["digest"] == "sha256:" + hashlib.sha256(b"approved").hexdigest()


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
@pytest.mark.parametrize("reference_only", [False, True])
def test_stage_rejects_non_regular_source_without_residue(
    tmp_path: Path,
    source_kind: str,
    reference_only: bool,
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
        owner.stage(
            source,
            file_name="report.bin",
            expires_at=0 if reference_only else 160.0,
            reference_only=reference_only,
        )

    assert list(root.iterdir()) == []


def test_reference_survives_expiry_and_owner_reload_without_copying(tmp_path: Path) -> None:
    now = [100.0]
    source = tmp_path / "report.md"
    source.write_bytes(b"delivered")
    original_mode = stat.S_IMODE(source.stat().st_mode)
    owner = _owner(tmp_path / "assets", now)
    asset = owner.stage(source, file_name=source.name, expires_at=0, reference_only=True)
    owner.commit(asset)

    assert asset.sealed_path == source
    assert asset.content_digest == "sha256:" + hashlib.sha256(b"delivered").hexdigest()
    assert [p.name for p in owner.root.iterdir()] == [f"{asset.asset_id}.json"]
    now[0] = 100_000.0
    source.write_bytes(b"changed after delivery")
    recovered = _owner(owner.root, now)
    recovered.prune()
    assert recovered.is_active(
        asset_id=asset.asset_id, sealed_path=source, expires_at=0,
        size_bytes=asset.size_bytes, content_digest=asset.content_digest,
    )
    recovered.revoke(asset)
    assert source.read_bytes() == b"changed after delivery"
    assert stat.S_IMODE(source.stat().st_mode) == original_mode
    assert list(owner.root.iterdir()) == []


def test_failed_reference_registration_preserves_source(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "report.md"
    source.write_bytes(b"original")
    original_mode = stat.S_IMODE(source.stat().st_mode)
    owner = _owner(tmp_path / "assets", [100.0])

    def fail_write(*_args):
        raise OSError("registration unavailable")

    monkeypatch.setattr(owner, "_write_sidecar_atomic", fail_write)
    with pytest.raises(OSError, match="registration unavailable"):
        owner.stage(source, file_name=source.name, expires_at=0, reference_only=True)
    assert source.read_bytes() == b"original"
    assert stat.S_IMODE(source.stat().st_mode) == original_mode
    assert list(owner.root.iterdir()) == []


@pytest.mark.parametrize("field,value", [("state", "invalid"), ("expires_at", "invalid"), ("asset_id", "invalid")])
def test_prune_invalid_reference_removes_only_registration(tmp_path: Path, field, value) -> None:
    source = tmp_path / "report.md"
    source.write_bytes(b"original")
    owner = _owner(tmp_path / "assets", [100.0])
    asset = owner.stage(source, file_name=source.name, expires_at=0, reference_only=True)
    sidecar = owner.root / f"{asset.asset_id}.json"
    payload = json.loads(sidecar.read_text())
    payload[field] = value
    sidecar.write_text(json.dumps(payload))
    owner.prune(now=100_000)
    assert source.read_bytes() == b"original"
    assert not sidecar.exists()


def test_reference_rejects_cache_source_before_pruning(tmp_path: Path) -> None:
    owner = _owner(tmp_path / "assets", [100.0])
    owner.root.mkdir()
    source = owner.root / "preserve.md"
    source.write_bytes(b"original")
    os.utime(source, (0, 0))
    with pytest.raises(ValueError, match="download_asset_reference_in_asset_root"):
        owner.stage(source, file_name=source.name, expires_at=0, reference_only=True)
    assert source.read_bytes() == b"original"


@pytest.mark.parametrize("expires_at", [0, -1, 160, True, None])
def test_reference_expiry_is_explicit_and_zero_only(tmp_path: Path, expires_at) -> None:
    source = tmp_path / "report.md"
    source.write_bytes(b"original")
    owner = _owner(tmp_path / "assets", [100.0])
    if expires_at == 0:
        with pytest.raises(ValueError, match="download_asset_expired"):
            owner.stage(source, file_name=source.name, expires_at=expires_at)
    else:
        with pytest.raises(ValueError, match="download_asset_reference_expiry_invalid"):
            owner.stage(source, file_name=source.name, expires_at=expires_at, reference_only=True)
    assert not owner.root.exists()
