# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared acquisition and commit flow for installable Hub asset packages."""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from jiuwenswarm.server.runtime.marketplace.hub_package_downloader import (
    HubPackageDownloader,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetPort,
    HubAssetQuery,
    HubDownloadRequest,
    HubAssetKind,
    HubResolvedDownload,
    create_default_hub_asset_port,
)
from jiuwenswarm.server.runtime.marketplace.hub_install_state import (
    HubInstallRecord,
    HubInstallStateStore,
)


PackageNameValidator = Callable[[str], str]
PackageValidator = Callable[[Path, str], None]
PackageConflictValidator = Callable[[str], None]
PackageCommittedHook = Callable[[HubInstallRecord], None]
PackageRollbackHook = Callable[[str], None]

logger = logging.getLogger(__name__)


class HubPackageExtractor(Protocol):
    """Minimal downloader contract needed by the shared installation flow."""

    async def download_and_extract(
        self, artifact: HubResolvedDownload, destination: Path
    ) -> None:
        ...


@dataclass(frozen=True, slots=True)
class HubPackageInstallResult:
    """One package committed locally with its persisted Hub provenance."""

    package_id: str
    destination: Path
    record: HubInstallRecord


def _find_package_root(extracted: Path, kind: HubAssetKind) -> Path:
    candidates = sorted({path.parent for path in extracted.rglob("manifest.json")})
    if len(candidates) != 1:
        raise ValueError(f"Hub {kind} ZIP must contain exactly one manifest.json")
    return candidates[0]


def _assert_no_symlinks(package_root: Path) -> None:
    if package_root.is_symlink() or any(path.is_symlink() for path in package_root.rglob("*")):
        raise ValueError("Hub package must not contain symbolic links")


async def install_hub_asset_package(
    *,
    kind: HubAssetKind,
    asset_id: str,
    destination_root: Path,
    state_store: HubInstallStateStore,
    package_name_validator: PackageNameValidator,
    package_validator: PackageValidator,
    conflict_validator: PackageConflictValidator | None = None,
    on_committed: PackageCommittedHook | None = None,
    on_rollback: PackageRollbackHook | None = None,
    hub_port: HubAssetPort | None = None,
    downloader: HubPackageExtractor | None = None,
) -> HubPackageInstallResult:
    """Resolve, validate, download, atomically commit, and record one Hub package."""
    asset = str(asset_id or "").strip()
    if not asset:
        raise ValueError(f"Hub {kind} asset id is required")
    port = hub_port or create_default_hub_asset_port()
    detail = await port.query_asset(HubAssetQuery(kind=kind, asset_id=asset))
    if detail.kind != kind:
        raise ValueError(f"Hub {kind} package type mismatch")
    if detail.asset_id != asset:
        raise ValueError(
            f"Hub {kind} asset id mismatch: expected {asset!r}, "
            f"got {detail.asset_id!r}"
        )
    package_id = package_name_validator(detail.package_name)
    if conflict_validator is not None:
        conflict_validator(package_id)
    owner = state_store.get_by_package_id(package_id)
    if owner is not None and owner.asset_id != asset:
        raise ValueError(
            f"Hub {kind} package id {package_id!r} is owned by another Hub asset"
        )

    artifact = await port.resolve_download(
        HubDownloadRequest(kind=kind, asset_id=asset, version=detail.version)
    )
    if artifact.kind != kind:
        raise ValueError(f"Hub {kind} artifact type mismatch")
    if artifact.asset_id != asset:
        raise ValueError(
            f"Hub {kind} artifact asset id mismatch: expected {asset!r}, "
            f"got {artifact.asset_id!r}"
        )
    if artifact.version != detail.version:
        raise ValueError(
            f"Hub {kind} artifact version mismatch: expected "
            f"{detail.version!r}, got {artifact.version!r}"
        )
    if artifact.package_name != package_id:
        raise ValueError(
            f"Hub {kind} artifact package name mismatch: expected "
            f"{package_id!r}, got {artifact.package_name!r}"
        )

    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / package_id
    if destination.exists():
        raise ValueError(f"Hub {kind} destination already exists: {package_id}")
    package_downloader = downloader or HubPackageDownloader()
    with tempfile.TemporaryDirectory(prefix=f"jiuwenswarm_hub_{kind}_") as temp:
        extracted = Path(temp)
        await package_downloader.download_and_extract(artifact, extracted)
        package_root = _find_package_root(extracted, kind)
        _assert_no_symlinks(package_root)

        staging_parent = Path(
            tempfile.mkdtemp(prefix=f".{package_id}.staging-", dir=destination_root)
        )
        staged = staging_parent / package_id
        try:
            shutil.copytree(package_root, staged)
            package_validator(staged, package_id)
            staged.replace(destination)
        finally:
            shutil.rmtree(staging_parent, ignore_errors=True)

    record = HubInstallRecord(
        asset_id=asset,
        package_id=package_id,
        kind=kind,
        version=artifact.version,
        checksum_sha256=artifact.checksum_sha256,
        installed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    try:
        state_store.upsert(record)
        if on_committed is not None:
            on_committed(record)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        try:
            state_store.remove(asset)
        except Exception:
            logger.exception("Failed to remove Hub install state during rollback")
        if on_rollback is not None:
            try:
                on_rollback(package_id)
            except Exception:
                logger.exception("Failed to run Hub package rollback hook")
        raise
    return HubPackageInstallResult(
        package_id=package_id,
        destination=destination,
        record=record,
    )


__all__ = ["HubPackageInstallResult", "install_hub_asset_package"]
