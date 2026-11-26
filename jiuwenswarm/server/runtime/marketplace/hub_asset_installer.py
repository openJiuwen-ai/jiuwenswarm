# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared acquisition and commit flow for installable Hub asset packages."""

from __future__ import annotations

import logging
import json
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
    HubAssetDetail,
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
    normalize_install_source_id,
)


PackageNameValidator = Callable[[str], str]
PackageValidator = Callable[[Path, str], None]
PackageConflictValidator = Callable[[str], None]
# Hub keeps every publisher's asset under its own asset_id, so two of them may
# carry the same package name; local packages are keyed by directory name. The
# resolver maps (Hub package name, asset id) to the id that will be installed.
PackageIdResolver = Callable[[str, str], str]
PackageCommittedHook = Callable[[HubInstallRecord], None]
PackageRollbackHook = Callable[[str], None]
# Maps (extracted root, Hub package name) to the single directory to install.
PackageRootLocator = Callable[[Path, str], Path]

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
    detail: HubAssetDetail | None = None


def _find_package_root(
    extracted: Path,
    kind: HubAssetKind,
    package_name: str = "",
    locator: PackageRootLocator | None = None,
) -> Path:
    """Locate the package root among the extracted manifests.

    Packages may ship sub-assets with their own ``manifest.json`` (skills under
    ``skills/<name>/``, nested experts under ``agents/<name>/``). Only the
    shallowest manifest is the package root; deeper ones belong to sub-assets
    and must not make a valid package look like it has several roots (#6602).
    Sibling roots at the same depth stay ambiguous and are rejected.
    """
    if locator is not None:
        return locator(extracted, package_name)
    candidates = sorted({path.parent for path in extracted.rglob("manifest.json")})
    if kind == "agent_group":
        candidates = [
            path for path in candidates
            if _is_group_manifest(path / "manifest.json")
        ]
    if not candidates:
        raise ValueError(f"Hub {kind} ZIP must contain exactly one manifest.json")
    depths = {path: len(path.relative_to(extracted).parts) for path in candidates}
    shallowest = min(depths.values())
    roots = [path for path in candidates if depths[path] == shallowest]
    if len(roots) != 1:
        raise ValueError(f"Hub {kind} ZIP must contain exactly one manifest.json")
    return roots[0]


def _is_group_manifest(path: Path) -> bool:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("package_type") == "agent_group"
    except (OSError, ValueError, AttributeError):
        return False


def _assert_no_symlinks(package_root: Path) -> None:
    if package_root.is_symlink() or any(path.is_symlink() for path in package_root.rglob("*")):
        raise ValueError("Hub package must not contain symbolic links")


async def install_hub_asset_package(
    *,
    kind: HubAssetKind,
    asset_id: str,
    destination_root: Path,
    state_store: HubInstallStateStore | None = None,
    package_name_validator: PackageNameValidator,
    package_validator: PackageValidator,
    package_id_resolver: PackageIdResolver | None = None,
    conflict_validator: PackageConflictValidator | None = None,
    on_committed: PackageCommittedHook | None = None,
    on_rollback: PackageRollbackHook | None = None,
    hub_port: HubAssetPort | None = None,
    downloader: HubPackageExtractor | None = None,
    version: str = "",
    expected_sha256: str = "",
    package_root_locator: PackageRootLocator | None = None,
    source_id: str | None = None,
) -> HubPackageInstallResult:
    """Resolve, validate, download, atomically commit, and record one Hub package."""
    asset = str(asset_id or "").strip()
    if not asset:
        raise ValueError(f"Hub {kind} asset id is required")
    resolved_source_id = normalize_install_source_id(source_id)
    port = hub_port or create_default_hub_asset_port()
    detail = await port.query_asset(
        HubAssetQuery(kind=kind, asset_id=asset, version=str(version or "").strip())
    )
    if detail.kind != kind:
        raise ValueError(f"Hub {kind} package type mismatch")
    if detail.asset_id != asset:
        raise ValueError(
            f"Hub {kind} asset id mismatch: expected {asset!r}, "
            f"got {detail.asset_id!r}"
        )
    package_id = package_name_validator(detail.package_name)
    # Aliased installs keep the Hub package name in the artifact and only rename
    # the local directory id, so the artifact check below must compare against
    # the Hub name rather than the resolved id.
    hub_package_id = package_id
    if package_id_resolver is not None:
        package_id = package_name_validator(
            package_id_resolver(hub_package_id, asset)
        )
    if conflict_validator is not None:
        conflict_validator(package_id)
    if state_store is not None:
        state_store.assert_package_id_available(
            package_id,
            asset_id=asset,
            source_id=resolved_source_id,
            kind=kind,
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
    expected = str(expected_sha256 or "").strip().lower()
    if expected and artifact.checksum_sha256.lower() != expected:
        raise ValueError(
            f"Hub {kind} artifact changed: expected checksum {expected!r}, "
            f"got {artifact.checksum_sha256.lower()!r}"
        )
    if artifact.package_name != package_id:
        # An aliased install still ships the Hub package name inside the archive;
        # any other mismatch means the Hub served another package.
        aliased = (
            package_id != hub_package_id
            and artifact.package_name == hub_package_id
        )
        if not aliased:
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
        package_root = _find_package_root(
            extracted, kind, hub_package_id, package_root_locator
        )
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
        source_id=resolved_source_id,
    )
    try:
        if state_store is not None:
            state_store.upsert(record)
        if on_committed is not None:
            on_committed(record)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        if state_store is not None:
            try:
                state_store.remove(asset, source_id=resolved_source_id)
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
        detail=detail,
    )


__all__ = ["HubPackageInstallResult", "install_hub_asset_package"]
