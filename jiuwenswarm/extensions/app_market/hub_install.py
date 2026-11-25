# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Install, update and uninstall Hub-distributed application packages.

The shared Hub installer owns download/extract/atomic commit. This module adds
the application contract on top of it: package-root discovery, distribution
shape checks, local registration through the existing ``publish_app`` path,
``install.json`` provenance, and lifecycle operations.
"""

from __future__ import annotations

import asyncio
import hashlib
import html.parser
import io
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from jiuwenswarm.enterprise.edition import is_enterprise
from jiuwenswarm.server.runtime.marketplace.hub_asset_installer import (
    HubPackageInstallResult,
    install_hub_asset_package,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetDetail,
    HubAssetQuery,
    HubAssetPort,
    create_default_hub_asset_port,
)
from jiuwenswarm.server.runtime.marketplace.hub_client import (
    HubNotFoundError,
    HubProtocolError,
)
from jiuwenswarm.server.runtime.marketplace.hub_package_downloader import (
    HubDownloadError,
    HubPackageDownloader,
)

from .app_market.models import AppManifest
from .app_market.storage import MarketStorage
from .editor_sessions import CoreRequest

logger = logging.getLogger(__name__)

MAX_APP_ZIP_BYTES = 600 * 1024 * 1024
MAX_APP_SOURCE_BYTES = 512 * 1024 * 1024
MAX_APP_SINGLE_FILE_BYTES = 64 * 1024 * 1024
MAX_APP_PACKAGE_NAME = 64
_PACKAGE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
_WINDOWS_DEVICE_NAME = re.compile(r"^(?:con|prn|aux|nul|(?:com|lpt)[1-9])$")
_RESERVED_PACKAGE_PATHS = (
    "scripts/start.ps1",
    "scripts/start.sh",
    "scripts/stop.ps1",
    "scripts/stop.sh",
)
_RELEASE_MARKER = ".workswarm-app-release"
_RESERVED_PAYLOAD_ROOTS = frozenset({"data", ".git", _RELEASE_MARKER})
INSTALL_RECORD_SCHEMA_VERSION = 1

ProgressCallback = Callable[[str], None]


class AppInstallError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AppInstallCancelled(AppInstallError):
    def __init__(self, message: str = "Installation cancelled") -> None:
        super().__init__("cancelled", message)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def app_install_id(asset_id: str, owner_id: str, hub_url: str = "") -> str:
    """Stable local app id for one Hub asset and owner."""
    digest = hashlib.sha256(
        f"{hub_url}\0{asset_id}\0{owner_id}".encode("utf-8")
    ).hexdigest()[:24]
    safe_asset = re.sub(r"[^a-z0-9-]", "-", str(asset_id or "").lower())[:48]
    return f"hub-{safe_asset}-{digest}"


def validate_package_name(value: str) -> str:
    name = str(value or "").strip()
    if not name or len(name) > MAX_APP_PACKAGE_NAME:
        raise AppInstallError(
            "invalid_plugin_structure",
            f"Hub app package name is invalid: {value!r}",
        )
    if not _PACKAGE_NAME_PATTERN.fullmatch(name) or _WINDOWS_DEVICE_NAME.fullmatch(name):
        raise AppInstallError(
            "invalid_plugin_structure",
            f"Hub app package name is invalid: {value!r}",
        )
    return name


def app_package_root(extracted: Path, package_name: str) -> Path:
    """Locate ``<outer>/<package_name>/index.html`` and return the inner directory."""
    wanted = validate_package_name(package_name)
    for outer in extracted.iterdir():
        if not outer.is_dir() or not (outer / "plugin.yaml").is_file():
            continue
        root = outer / wanted
        if (root / "index.html").is_file() and not any(
            entry.name not in {"plugin.yaml", "icon.png", wanted}
            for entry in outer.iterdir()
        ) and len(list(extracted.iterdir())) == 1:
            return root
    raise AppInstallError(
        "invalid_plugin_structure",
        f"Hub app ZIP must contain one market wrapper with {wanted}/index.html",
    )


def app_package_validator(package_root: Path, package_name: str) -> None:
    """Reject packages that would replace market-owned files or pre-seed data."""
    if not (package_root / "index.html").is_file():
        raise AppInstallError(
            "invalid_plugin_structure", "Hub app package is missing index.html"
        )
    for entry in package_root.iterdir():
        if entry.name.casefold() in _RESERVED_PAYLOAD_ROOTS:
            raise AppInstallError(
                "invalid_plugin_structure", f"Hub app package contains reserved path {entry.name}",
            )
        if entry.name.casefold() == "scripts":
            if not entry.is_dir() or entry.name != "scripts":
                raise AppInstallError("invalid_plugin_structure", "Application scripts must be a scripts/ directory")
            for script in entry.iterdir():
                if f"scripts/{script.name.casefold()}" in _RESERVED_PACKAGE_PATHS:
                    raise AppInstallError(
                        "invalid_plugin_structure",
                        f"Hub app package must not ship market script {script.name}",
                    )


def _read_attached_icon(icon_uri: str) -> str:
    """Keep an embedded data-URL icon; leave remote URLs for the UI to fetch."""
    return str(icon_uri or "").strip()


class _HtmlMeta(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}
        self.icon_href = ""
        self.in_head = False
        self.head_complete = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "head":
            self.in_head = True
        elif tag == "body":
            self.in_head = False
            self.head_complete = True
        elif tag == "meta" and self.in_head:
            values = {key: value or "" for key, value in attrs}
            name = values.get("name", "").strip().lower()
            self.values[name] = values.get("content", "").strip()
        elif tag == "link" and self.in_head:
            values = {key: value or "" for key, value in attrs}
            if "icon" in values.get("rel", "").lower().split():
                self.icon_href = values.get("href", "").strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self.in_head = False
            self.head_complete = True


def read_entry_metadata(package_root: Path, version: str) -> dict[str, str]:
    entry = package_root / "index.html"
    limit = 64 * 1024
    with entry.open("rb") as source:
        raw = source.read(limit + 1)
    parser = _HtmlMeta()
    try:
        parser.feed(raw[:limit].decode("utf-8", errors="replace"))
    except (ValueError, AssertionError) as exc:
        raise AppInstallError("invalid_plugin_structure", "Invalid HTML application entry") from exc
    if not parser.head_complete and len(raw) > limit:
        raise AppInstallError("invalid_plugin_structure", "Application HTML head exceeds 64 KiB")
    declared = parser.values.get("application-version", "")
    if declared and declared != version:
        raise AppInstallError("app_artifact_changed", "HTML entry version differs from Hub version")
    return {**parser.values, "icon_href": parser.icon_href}


def _require_app_detail(detail: HubAssetDetail) -> None:
    if detail.kind != "app":
        raise AppInstallError(
            "invalid_plugin_structure",
            f"Hub asset is not an app (kind={detail.kind!r})",
        )


def _check_detail(detail: HubAssetDetail) -> None:
    if detail.file_size is not None and int(detail.file_size) > MAX_APP_ZIP_BYTES:
        raise AppInstallError(
            "file_too_large",
            "Hub app package exceeds the 600 MB transfer limit",
        )
    if detail.source_size is not None and int(detail.source_size) > MAX_APP_SOURCE_BYTES:
        raise AppInstallError(
            "file_too_large",
            "Hub app source files exceed the 512 MB limit",
        )


_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_WINDOWS_INVALID_CHARS = frozenset('<>:"|?*')


def _windows_unsafe_segment(segment: str) -> str | None:
    """Return why one path segment cannot be created on Windows, if so."""
    if not segment:
        return "empty path segment"
    if segment != segment.rstrip(" ."):
        return "ends with a space or dot"
    if any(char in _WINDOWS_INVALID_CHARS or ord(char) < 32 for char in segment):
        return "contains a Windows-illegal character"
    if segment.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
        return "uses a Windows reserved device name"
    return None


def validate_app_archive_paths(body: bytes) -> None:
    """Reject archive paths Windows cannot materialize or that collide when folded.

    Hub validation already checks traversal and links; this mirrors the
    cross-platform rules from the distribution contract on the installing side
    so a package accepted by one side cannot fail or overwrite files on the
    other.
    """
    if not body.startswith(b"PK"):
        raise AppInstallError("invalid_plugin_structure", "Hub app package is not a ZIP file")
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            entries: dict[str, tuple[str, bool]] = {}
            explicit: set[str] = set()
            for info in archive.infolist():
                raw = info.filename.replace("\\", "/").rstrip("/")
                parts = raw.split("/")
                if raw in explicit or len(parts) > 32:
                    raise AppInstallError("invalid_plugin_structure", f"Duplicate or overly deep ZIP path: {raw!r}")
                explicit.add(raw)
                if info.compress_size and info.file_size > info.compress_size * 50:
                    raise AppInstallError("invalid_plugin_structure", "ZIP compression ratio exceeds 50:1")
                for depth, segment in enumerate(parts, start=1):
                    path = "/".join(parts[:depth])
                    directory = depth < len(parts) or info.filename.endswith(("/", "\\"))
                    identity = (path, directory)
                    if entries.setdefault(path.casefold(), identity) != identity:
                        raise AppInstallError("invalid_plugin_structure", f"Conflicting ZIP paths: {path!r}")
                    reason = _windows_unsafe_segment(segment)
                    if reason:
                        raise AppInstallError(
                            "invalid_plugin_structure",
                            f"Hub app package path {raw!r} is not portable: {reason}",
                        )
    except zipfile.BadZipFile as exc:
        raise AppInstallError(
            "invalid_plugin_structure", "Hub app package is not a valid ZIP file"
        ) from exc


class _CancellableDownloader:
    """Wrap the shared downloader so cancel is honoured before extraction."""

    def __init__(
        self,
        downloader: HubPackageDownloader,
        cancelled: Callable[[], bool],
        progress: ProgressCallback | None = None,
    ) -> None:
        self._downloader = downloader
        self._cancelled = cancelled
        self._progress = progress or (lambda phase: None)

    async def download_and_extract(self, artifact: Any, destination: Path) -> None:
        if self._cancelled():
            raise AppInstallCancelled()
        body = await self._downloader.download_bytes(artifact)
        # Path checks must run before extraction: on Windows a reserved name
        # fails while writing, which would leave a half-extracted staging tree.
        await asyncio.to_thread(validate_app_archive_paths, body)
        self._progress("verifying")
        if self._cancelled():
            raise AppInstallCancelled()
        self._progress("extracting")
        await asyncio.to_thread(
            self._downloader.extract_zip,
            body,
            destination,
            max_total_uncompressed_bytes=MAX_APP_SOURCE_BYTES,
            max_single_file_bytes=MAX_APP_SINGLE_FILE_BYTES,
            verify_crc=False,
        )


def operation_path(operation_dir: Path, job_id: str) -> Path:
    return Path(operation_dir) / f"{job_id}.json"


def _write_operation(operation_dir: Path | None, payload: dict[str, Any]) -> None:
    if operation_dir is None:
        return
    target = operation_path(operation_dir, payload["job_id"])
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_operation(operation_dir: Path | None, job_id: str) -> None:
    if operation_dir is not None:
        operation_path(operation_dir, job_id).unlink(missing_ok=True)


def _directory_identity(path: Path) -> list[int] | None:
    if not path.is_dir() or path.resolve() != path:
        return None
    info = path.stat()
    return [info.st_dev, info.st_ino]


def _mark_release(path: Path, release_id: str) -> None:
    (path / _RELEASE_MARKER).write_text(release_id, encoding="ascii")


def _owns_release(path: Path, release_id: str) -> bool:
    if not release_id or _directory_identity(path) is None:
        return False
    marker = path / _RELEASE_MARKER
    if not marker.is_file() or marker.is_symlink():
        return False
    with marker.open("rb") as stream:
        return stream.read(33) == release_id.encode("ascii")


def _prepare_operation(
    operation_dir: Path | None,
    job_id: str,
    app_id: str,
    root: Path,
    destination: Path,
    kind: str,
) -> dict[str, Any]:
    release_id = uuid.uuid4().hex
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=root))
    operation = {
        "job_id": job_id or release_id,
        "kind": kind,
        "app_id": app_id,
        "install_root": str(root),
        "destination": str(destination),
        "stage": str(stage),
        "release_id": release_id,
    }
    try:
        _mark_release(stage, release_id)
        _write_operation(operation_dir, operation)
    except Exception:
        shutil.rmtree(stage)
        raise
    return operation


def _operation_destination(operation: dict[str, Any]) -> Path:
    root = Path(operation["install_root"])
    destination = Path(operation["destination"])
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("App operation paths are outside their installation root")
    if (
        destination.parent != root
        or validate_package_name(destination.name) != destination.name
    ):
        raise ValueError("App operation paths are outside their installation root")
    return destination


def _cleanup_stage(operation: dict[str, Any]) -> None:
    destination = _operation_destination(operation)
    stage = Path(operation["stage"])
    if stage.parent != destination.parent:
        raise ValueError("App staging directory is outside its installation root")
    if _owns_release(stage, operation["release_id"]):
        shutil.rmtree(stage)


def _same_release(value: Any, record: dict[str, Any]) -> bool:
    if isinstance(value, AppManifest):
        return (
            value.source == "hub"
            and value.owner_id == record["owner_id"]
            and value.artifact_sha256 == record["artifact_sha256"]
            and value.version == record["version"]
            and value.work_dir == record["work_dir"]
        )
    return isinstance(value, dict) and all(
        value.get(key) == record[key]
        for key in ("release_id", "artifact_sha256", "version", "work_dir")
    )


def _recover_operation(operation: dict[str, Any], storage: Any) -> None:
    destination = _operation_destination(operation)
    app_id = operation["app_id"]
    release_id = operation["release_id"]
    if not re.fullmatch(r"hub-[a-z0-9-]+", app_id):
        raise ValueError("Invalid application id in install operation")
    record = operation.get("record")
    if record is not None:
        manifest = storage.load_manifest(app_id)
        if manifest is not None and manifest.source == "local":
            _cleanup_stage(operation)
            return
        installed = storage.load_install_record(app_id)
        owns_destination = _owns_release(destination, release_id)
        committed = (
            owns_destination
            and _same_release(manifest, record)
            and _same_release(installed, record)
        )
        if not committed:
            if operation["kind"] == "update":
                old_identity = operation["old_identity"]
                backup = Path(operation["backup"])
                archive_root = destination.parent / ".app-archive"
                if backup.parent != archive_root or archive_root.resolve() != archive_root:
                    raise ValueError("App backup directory is outside its archive root")
                if old_identity and _directory_identity(backup) == old_identity:
                    if owns_destination:
                        shutil.rmtree(destination)
                    if destination.exists() or destination.is_symlink():
                        raise ValueError("App destination was replaced during recovery")
                    backup.replace(destination)
                elif _directory_identity(destination) != old_identity:
                    raise ValueError("Previous application directory could not be recovered")
                previous_manifest = AppManifest(**operation["old_manifest"])
                if manifest != previous_manifest:
                    storage.save_manifest(app_id, previous_manifest)
                if installed != operation["old_record"]:
                    storage.save_install_record(app_id, operation["old_record"])
            else:
                if owns_destination:
                    shutil.rmtree(destination)
                if _same_release(manifest, record):
                    storage.delete_app(app_id)
                    if storage.load_manifest(app_id) is not None:
                        raise OSError("Could not remove the partially registered application")
                if _same_release(installed, record):
                    storage.delete_install_record(app_id)
    _cleanup_stage(operation)


def _recover_after_failure(
    operation: dict[str, Any],
    storage: Any,
    operation_dir: Path | None,
) -> None:
    """Roll back a failed step, keeping the journal if rollback itself fails."""
    try:
        _recover_operation(operation, storage)
    except Exception:  # noqa: BLE001 - preserve the original install error
        logger.exception(
            "Failed to recover app operation %s; journal retained for startup recovery",
            operation.get("job_id"),
        )
        return
    _remove_operation(operation_dir, operation["job_id"])


def recover_interrupted_operations(
    operation_dir: Path, storage: Any,
) -> list[dict[str, Any]]:
    """Resolve each journal independently, retaining any operation that needs retry."""
    recovered = []
    for path in sorted(operation_dir.glob("*.json")):
        try:
            operation = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(operation, dict) or "release_id" not in operation:
                logger.warning("Retaining app journal without release ownership: %s", path.name)
                continue
            _recover_operation(operation, storage)
            path.unlink()
            recovered.append(operation)
        except Exception:  # noqa: BLE001 - recovery must not block startup
            logger.exception("Failed to recover interrupted app operation: %s", path.name)
    return recovered


def hub_app_downloader() -> HubPackageDownloader:
    return HubPackageDownloader(
        max_download_bytes=MAX_APP_ZIP_BYTES,
        max_total_uncompressed_bytes=MAX_APP_SOURCE_BYTES,
        max_single_file_bytes=MAX_APP_SINGLE_FILE_BYTES,
        verify_archive_crc=False,
    )


def default_install_root(configured_root: str = "", user_workspace: Path | None = None) -> Path:
    if configured_root.strip():
        return Path(configured_root).expanduser().resolve()
    if user_workspace is not None:
        return (Path(user_workspace) / "apps").resolve()
    from jiuwenswarm.common.paths import get_market_apps_dir

    return get_market_apps_dir()


async def install_hub_app(
    *,
    manager: Any,
    asset_id: str,
    version: str = "",
    install_root: Path,
    owner_id: str,
    expected_sha256: str = "",
    hub_port: HubAssetPort | None = None,
    downloader: HubPackageDownloader | None = None,
    on_progress: ProgressCallback | None = None,
    cancelled: Callable[[], bool] | None = None,
    operation_dir: Path | None = None,
    job_id: str = "",
    project_request: CoreRequest | None = None,
    on_committed: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Register a Hub app and report its committed result before project binding."""
    asset = str(asset_id or "").strip()
    if not asset:
        raise AppInstallError("invalid_plugin_structure", "asset_id is required")
    # Empty owner is the local single-user identity. Only enterprise deployments
    # require an authenticated user for app ownership.
    if not str(owner_id or "").strip() and is_enterprise():
        raise AppInstallError("permission_denied", "owner identity is required")
    cancel = cancelled or (lambda: False)
    progress = on_progress or (lambda phase: None)

    port = hub_port or create_default_hub_asset_port()
    hub_url = str(getattr(port, "base_url", "") or "").strip()
    app_id = app_install_id(asset, owner_id, hub_url)
    progress("resolving")
    try:
        detail = await port.query_asset(
            HubAssetQuery(kind="app", asset_id=asset, version=str(version or "").strip())
        )
    except HubNotFoundError as exc:
        raise AppInstallError(
            "not_found", f"Hub 上找不到该应用版本: {exc}"
        ) from exc
    except HubProtocolError as exc:
        raise AppInstallError("hub_unavailable", f"无法访问 Hub: {exc}") from exc
    _require_app_detail(detail)
    _check_detail(detail)

    root = Path(install_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    package_name = validate_package_name(detail.package_name)
    destination = root / package_name
    if os.name == "nt" and len(str(destination)) > 240:
        raise AppInstallError(
            "invalid_plugin_structure",
            f"Install path is too long for Windows: {destination}",
        )
    if destination.exists() or destination.is_symlink():
        raise AppInstallError(
            "install_path_conflict",
            f"Install destination already exists: {destination}. "
            "Choose another install directory or remove the existing folder first.",
        )
    _ensure_disk_space(root, detail.source_size or detail.file_size or 0)
    operation = _prepare_operation(operation_dir, job_id, app_id, root, destination, "install")
    try:
        progress("downloading")
        result: HubPackageInstallResult = await install_hub_asset_package(
            kind="app",
            asset_id=asset,
            version=detail.version,
            destination_root=Path(operation["stage"]),
            package_name_validator=validate_package_name,
            package_validator=app_package_validator,
            package_root_locator=app_package_root,
            hub_port=port,
            downloader=_CancellableDownloader(downloader or hub_app_downloader(), cancel, progress),
            expected_sha256=expected_sha256 or detail.checksum_sha256,
        )
        if cancel():
            raise AppInstallCancelled()
        if result.destination.name != package_name:
            raise AppInstallError("app_artifact_changed", "Application package name changed")
        record = {
            "schema_version": INSTALL_RECORD_SCHEMA_VERSION,
            "install_id": uuid.uuid4().hex,
            "release_id": operation["release_id"],
            "hub_url": hub_url,
            "asset_id": asset,
            "package_name": package_name,
            "version": result.record.version,
            "artifact_sha256": result.record.checksum_sha256,
            "installed_at": result.record.installed_at,
            "owner_id": owner_id,
            "install_root": str(root),
            "work_dir": str(destination),
            "requires_project": bool(detail.requires_project),
            "permissions": list(detail.permissions),
            "has_server_script": bool(detail.has_server_script),
        }
        _mark_release(result.destination, operation["release_id"])
        operation["record"] = record
        _write_operation(operation_dir, operation)
        progress("committing")
        if destination.exists() or destination.is_symlink():
            raise AppInstallError("install_path_conflict", f"Install destination already exists: {destination}")
        result.destination.rename(destination)
        progress("registering")
        manifest = await _register_installed_app(
            manager=manager,
            app_id=app_id,
            destination=destination,
            detail=detail,
            hub_url=hub_url,
            artifact_sha256=result.record.checksum_sha256,
            owner_id=owner_id,
        )
        manager.storage.save_install_record(app_id, record)
    except (Exception, asyncio.CancelledError) as exc:
        _recover_after_failure(operation, manager.storage, operation_dir)
        if isinstance(exc, HubDownloadError):
            # Downloader errors carry a stable code (checksum, size, structure).
            raise AppInstallError(getattr(exc, "code", "") or "checksum_mismatch", str(exc)) from exc
        if isinstance(exc, HubProtocolError):
            raise AppInstallError("hub_unavailable", str(exc)) from exc
        if isinstance(exc, ValueError):
            raise AppInstallError("app_artifact_changed", str(exc)) from exc
        raise

    _cleanup_stage(operation)
    _remove_operation(operation_dir, operation["job_id"])
    installed = {
        "app_id": app_id,
        "name": manifest.name,
        "version": manifest.version,
        "work_dir": str(destination),
        "install_root": str(root),
        "package_name": package_name,
        "project_id": manifest.project_id,
        "requires_project": bool(detail.requires_project),
        "permissions": list(detail.permissions),
        "has_server_script": bool(detail.has_server_script),
    }
    if on_committed is not None:
        on_committed(installed)
    if project_request is not None:
        # Binding is a start precondition, not part of the install transaction.
        # The package is already downloaded, verified and registered here, so a
        # failed resolve/create RPC must not discard it: the app stays installed
        # and unbound, and start_app binds it again before launch.
        try:
            await manager.bind_app_project(manifest, project_request)
            manager.storage.save_manifest(app_id, manifest)
        except Exception as exc:
            logger.warning("Project binding failed after installing %s: %s", app_id, exc)
        else:
            installed["project_id"] = manifest.project_id
    progress("done")
    return installed


async def _register_installed_app(
    *,
    manager: Any,
    app_id: str,
    destination: Path,
    detail: HubAssetDetail,
    hub_url: str,
    artifact_sha256: str,
    owner_id: str,
):
    meta = read_entry_metadata(destination, detail.version)
    options = {
        "app_id": app_id,
        "name": detail.display_name or meta.get("application-name") or destination.name,
        "version": detail.version,
        "run_type": "static",
        "page_entry_path": "index.html",
        "work_dir": str(destination),
        "description": detail.short_description or meta.get("description", ""),
        "icon_url": _read_attached_icon(detail.icon_uri),
        "owner_id": owner_id,
        "permissions": list(detail.permissions),
        "source_session": "",
        "source": "hub",
        "hub_asset_id": detail.asset_id,
        "hub_url": hub_url,
        "artifact_sha256": artifact_sha256,
        "requires_project": bool(detail.requires_project),
    }
    manifest = await manager.service.publish_app(str(destination), options)
    if manifest is None:
        raise AppInstallError(
            "app_register_failed",
            "Application adaptation or validation failed",
        )
    return manifest


def _ensure_disk_space(root: Path, source_size: int) -> None:
    required = int(source_size * 1.15) + 1024 * 1024 * 1024
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        return
    if free < required:
        raise AppInstallError(
            "insufficient_disk_space",
            f"Not enough disk space under {root}: need {required} bytes, free {free}",
        )


def load_install_record(manager: Any, app_id: str) -> dict[str, Any] | None:
    record = manager.storage.load_install_record(app_id)
    return record if isinstance(record, dict) else None


def _reject_busy_editor(
    manifest: AppManifest, editor_is_busy: Callable[[str], bool] | None,
) -> None:
    if editor_is_busy is not None and any(
        editor_is_busy(session_id) for session_id in manifest.iter_editor_session_ids()
    ):
        raise AppInstallError("app_operation_conflict", "Stop the active editor session before updating")


async def update_hub_app(
    *,
    manager: Any,
    app_id: str,
    owner_id: str,
    version: str = "",
    expected_sha256: str = "",
    hub_port: HubAssetPort | None = None,
    downloader: HubPackageDownloader | None = None,
    on_progress: ProgressCallback | None = None,
    cancelled: Callable[[], bool] | None = None,
    operation_dir: Path | None = None,
    editor_is_busy: Callable[[str], bool] | None = None,
    job_id: str = "",
) -> dict[str, Any]:
    """In-place update preserving ``data/``, ``.git`` and project binding."""
    progress = on_progress or (lambda phase: None)
    manifest = manager.service.get_app(app_id)
    if manifest is None or manifest.owner_id != owner_id:
        raise AppInstallError("app_work_dir_missing", "Application not found")
    record = load_install_record(manager, app_id)
    if not record:
        raise AppInstallError(
            "app_work_dir_missing", "Application was not installed from Hub"
        )
    install_root = Path(record["install_root"])
    package_name = validate_package_name(record["package_name"])
    destination = install_root / package_name
    old_identity = _directory_identity(destination)
    if not install_root.is_absolute() or install_root.resolve() != install_root:
        raise AppInstallError("app_work_dir_missing", "Application directory has changed or is unavailable")
    if str(destination) != manifest.work_dir or not old_identity:
        raise AppInstallError("app_work_dir_missing", "Application directory has changed or is unavailable")
    if (
        record.get("work_dir") != str(destination)
        or manifest.owner_id != record.get("owner_id")
    ):
        raise AppInstallError("app_work_dir_missing", "Application directory has changed or is unavailable")
    if record.get("release_id") and not _owns_release(destination, record["release_id"]):
        raise AppInstallError("app_work_dir_missing", "Application directory has changed or is unavailable")
    asset_id = str(record.get("asset_id") or "").strip()
    if not asset_id:
        raise AppInstallError("app_work_dir_missing", "Install record has no asset id")

    port = hub_port or create_default_hub_asset_port()
    origin = str(getattr(port, "base_url", "") or "").rstrip("/")
    if origin != str(record.get("hub_url") or "").rstrip("/"):
        raise AppInstallError("app_artifact_changed", "Select the original Hub before updating this application")
    progress("resolving")
    try:
        detail = await port.query_asset(
            HubAssetQuery(kind="app", asset_id=asset_id, version=str(version or "").strip())
        )
    except HubNotFoundError as exc:
        raise AppInstallError("not_found", f"Hub 上找不到该应用版本: {exc}") from exc
    except HubProtocolError as exc:
        raise AppInstallError("hub_unavailable", f"无法访问 Hub: {exc}") from exc
    _require_app_detail(detail)
    _check_detail(detail)
    expected = (expected_sha256 or detail.checksum_sha256).lower()
    if expected and detail.checksum_sha256 and expected != detail.checksum_sha256.lower():
        raise AppInstallError("app_artifact_changed", "Application artifact changed")
    if detail.version == manifest.version and expected == manifest.artifact_sha256.lower():
        return {"app_id": app_id, "version": manifest.version, "updated": False}

    if manager.storage.contains_local_app(destination):
        raise AppInstallError(
            "app_operation_conflict", "The installation directory is still used by a local application"
        )
    _ensure_disk_space(install_root, (detail.source_size or detail.file_size or 0) * 2)
    operation = _prepare_operation(operation_dir, job_id, app_id, install_root, destination, "update")
    cancel = cancelled or (lambda: False)
    try:
        progress("downloading")
        staged = await install_hub_asset_package(
            kind="app",
            asset_id=asset_id,
            version=detail.version,
            destination_root=Path(operation["stage"]),
            package_name_validator=validate_package_name,
            package_validator=app_package_validator,
            package_root_locator=app_package_root,
            hub_port=port,
            downloader=_CancellableDownloader(downloader or hub_app_downloader(), cancel, progress),
            expected_sha256=expected,
        )
        if cancel():
            raise AppInstallCancelled()
        read_entry_metadata(staged.destination, detail.version)
        current = manager.service.get_app(app_id)
        if current is None or current.artifact_sha256 != manifest.artifact_sha256:
            raise AppInstallError("app_operation_conflict", "Application changed during update")
        if _directory_identity(destination) != old_identity:
            raise AppInstallError("app_operation_conflict", "Application directory changed during update")
        _reject_busy_editor(current, editor_is_busy)

        progress("committing")
        for instance in manager.service.list_instances(app_id):
            if instance.state in ("starting", "running", "stopping"):
                if not await manager.service.stop_app(instance.instance_id):
                    raise AppInstallError("app_operation_conflict", "Failed to stop application instances")
        _reject_busy_editor(current, editor_is_busy)

        # Keep the old working tree intact in the backup, including any user edits.
        await asyncio.to_thread(_copy_preserved_paths, destination, staged.destination)
        _reject_busy_editor(current, editor_is_busy)
        if _directory_identity(destination) != old_identity:
            raise AppInstallError("app_operation_conflict", "Application directory changed during update")
        archive_root = _archive_root(install_root)
        backup = archive_root / f"{destination.name}-{operation['release_id']}"
        new_record = _updated_record(
            record, detail=detail, checksum=staged.record.checksum_sha256, destination=destination,
        )
        new_record["release_id"] = operation["release_id"]
        _mark_release(staged.destination, operation["release_id"])
        operation.update(
            record=new_record,
            backup=str(backup),
            old_identity=old_identity,
            old_manifest=asdict(current),
            old_record=record,
        )
        _write_operation(operation_dir, operation)
        destination.rename(backup)
        staged.destination.rename(destination)

        progress("registering")
        new_manifest = await manager.service.publish_app(str(destination), {
            "app_id": app_id,
            "name": detail.display_name or current.name,
            "version": detail.version,
            "description": detail.short_description or current.description,
            "icon_url": detail.icon_uri or current.icon_url,
            "artifact_sha256": staged.record.checksum_sha256,
            "requires_project": bool(detail.requires_project),
            "permissions": list(detail.permissions),
            "run_type": "static",
            "page_entry_path": "index.html",
            "work_dir": str(destination),
            "owner_id": owner_id,
            "source": "hub",
            "hub_asset_id": asset_id,
            "hub_url": current.hub_url or record.get("hub_url", ""),
            "project_id": current.project_id,
            "project_dir": current.project_dir,
            "project_work_mode": current.project_work_mode,
            "source_session": "",
        })
        if new_manifest is None:
            raise AppInstallError("app_register_failed", "Application adaptation or validation failed")
        # Updating replaces the package, not the market registration: keep the
        # pinned flag, creation time and editor bindings owned by this copy.
        new_manifest.pinned = current.pinned
        new_manifest.created_at = current.created_at
        new_manifest.editor_session_id = current.editor_session_id
        new_manifest.editor_create_token = current.editor_create_token
        new_manifest.editor_create_pending = current.editor_create_pending
        new_manifest.editor_session_ids = list(current.editor_session_ids)
        manager.storage.save_manifest(app_id, new_manifest)
        manager.storage.save_install_record(app_id, new_record)
    except (Exception, asyncio.CancelledError) as exc:
        _recover_after_failure(operation, manager.storage, operation_dir)
        if isinstance(exc, HubDownloadError):
            raise AppInstallError(getattr(exc, "code", "") or "checksum_mismatch", str(exc)) from exc
        if isinstance(exc, HubProtocolError):
            raise AppInstallError("hub_unavailable", str(exc)) from exc
        if isinstance(exc, ValueError):
            raise AppInstallError("app_artifact_changed", str(exc)) from exc
        raise

    _cleanup_stage(operation)
    _remove_operation(operation_dir, operation["job_id"])
    progress("done")
    return {"app_id": app_id, "version": detail.version, "updated": True, "backup_dir": str(backup)}


def _updated_record(
    record: dict[str, Any],
    *,
    detail: HubAssetDetail,
    checksum: str,
    destination: Path,
) -> dict[str, Any]:
    updated = dict(record)
    updated.update(
        {
            "version": detail.version,
            "artifact_sha256": checksum,
            "installed_at": now_iso(),
            "work_dir": str(destination),
            "requires_project": bool(detail.requires_project),
            "permissions": list(detail.permissions),
            "has_server_script": bool(detail.has_server_script),
        }
    )
    return updated


def _archive_root(install_root: Path) -> Path:
    root = install_root / ".app-archive"
    if root.resolve() != root:
        raise AppInstallError("install_path_conflict", "Application archive path was redirected")
    root.mkdir(exist_ok=True)
    return root


def _copy_preserved_paths(source: Path, target: Path) -> None:
    for name in ("data", ".git"):
        entry = source / name
        if entry.is_symlink() or entry.is_file():
            shutil.copy2(entry, target / name, follow_symlinks=False)
        elif entry.is_dir():
            shutil.copytree(entry, target / name, symlinks=True)


def _cleanup_uninstalled_files(
    manifest: AppManifest,
    record: dict[str, Any] | None,
) -> tuple[str, str, str]:
    preserved_dir = ""
    try:
        if not record:
            raise AppInstallError("install_path_conflict", "Cannot verify ownership of installation directory")
        install_root = Path(str(record.get("install_root") or ""))
        work_dir = install_root / validate_package_name(str(record.get("package_name") or ""))
        release_id = str(record.get("release_id") or "")
        if not install_root.is_absolute() or install_root.resolve() != install_root:
            raise AppInstallError("install_path_conflict", "Cannot verify ownership of installation directory")
        if (
            record.get("owner_id") != manifest.owner_id
            or str(work_dir) != manifest.work_dir
            or record.get("work_dir") != str(work_dir)
        ):
            raise AppInstallError("install_path_conflict", "Cannot verify ownership of installation directory")
        if not _owns_release(work_dir, release_id):
            raise AppInstallError("install_path_conflict", "Cannot verify ownership of installation directory")
        if any((work_dir / name).exists() or (work_dir / name).is_symlink() for name in ("data", ".git")):
            archive = _archive_root(install_root) / f"{work_dir.name}-{uuid.uuid4().hex}"
            archive.mkdir()
            _copy_preserved_paths(work_dir, archive)
            preserved_dir = str(archive)
        if not _owns_release(work_dir, release_id):
            raise AppInstallError("install_path_conflict", "Application directory changed during uninstall")
        shutil.rmtree(work_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("Local cleanup failed after uninstalling %s: %s", manifest.app_id, exc)
        return "", preserved_dir, str(exc) or "Local application files could not be removed"
    return str(work_dir), preserved_dir, ""


def cleanup_local_app_files(manifest: AppManifest, storage: MarketStorage) -> tuple[str, str, str]:
    """Remove an app-owned local program directory, archiving data/ and .git.

    Returns ``(removed_dir, preserved_dir, cleanup_warning)``. Local apps may
    point at a user source directory. A converted installation outside the
    default root needs the release ID retained from its install record.
    """
    if not manifest.work_dir:
        return "", "", ""
    work_dir = Path(manifest.work_dir).expanduser()
    preserved_dir = ""
    try:
        if not work_dir.exists() and not work_dir.is_symlink():
            return "", "", ""
        install_root = default_install_root(os.environ.get("APP_MARKET_INSTALL_ROOT", ""))
        identity = _directory_identity(work_dir)
        under_install_root = work_dir.is_relative_to(install_root)
        archived = any(part.casefold() == ".app-archive" for part in work_dir.parts)
        owned = (
            _owns_release(work_dir, manifest.local_release_id)
            if manifest.local_release_id else under_install_root
        )
        kept_message = f"The application directory is not owned by App Market and was kept: {work_dir}"
        if not identity or not owned:
            return "", "", kept_message
        if install_root.is_relative_to(work_dir) or archived:
            return "", "", kept_message
        if storage.has_overlapping_work_dir(work_dir):
            return "", "", "The application directory is still used by another application"
        if any((work_dir / name).exists() or (work_dir / name).is_symlink() for name in ("data", ".git")):
            archive_root = _archive_root(install_root if under_install_root else work_dir.parent)
            archive = archive_root / f"{work_dir.name}-{uuid.uuid4().hex}"
            archive.mkdir()
            _copy_preserved_paths(work_dir, archive)
            preserved_dir = str(archive)
        if _directory_identity(work_dir) != identity or (
            manifest.local_release_id and not _owns_release(work_dir, manifest.local_release_id)
        ):
            raise AppInstallError("install_path_conflict", "Application directory changed during cleanup")
        if storage.has_overlapping_work_dir(work_dir):
            return "", preserved_dir, "The application directory is still used by another application"
        shutil.rmtree(work_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("Local app cleanup failed for %s: %s", manifest.app_id, exc)
        return "", preserved_dir, str(exc) or "Local application files could not be removed"
    return str(work_dir), preserved_dir, ""


async def uninstall_hub_app(
    *,
    manager: Any,
    app_id: str,
    owner_id: str,
    remove_files: bool = False,
) -> dict[str, Any]:
    """Deregister the app, then attempt optional local cleanup."""
    manifest = manager.service.get_app(app_id)
    if manifest is None or manifest.owner_id != owner_id:
        raise AppInstallError("app_work_dir_missing", "Hub application not found")
    if manifest.source != "hub":
        raise AppInstallError(
            "app_not_hub_installed",
            "Application is no longer a Hub installation; delete the local app instead",
        )
    record = load_install_record(manager, app_id) if remove_files else None
    if not await manager.service.delete_app(app_id) or manager.service.get_app(app_id) is not None:
        raise AppInstallError("app_operation_conflict", "Failed to remove application from App Market")

    removed_dir = preserved_dir = cleanup_warning = ""
    if remove_files and manifest.work_dir and manager.storage.contains_local_app(Path(manifest.work_dir)):
        cleanup_warning = "The installation directory is still used by a local application"
    elif remove_files:
        removed_dir, preserved_dir, cleanup_warning = await asyncio.to_thread(
            _cleanup_uninstalled_files, manifest, record,
        )
    return {
        "app_id": app_id,
        "removed_files": bool(removed_dir),
        "removed_dir": removed_dir,
        "preserved_dir": preserved_dir,
        "project_retained": True,
        "work_dir": manifest.work_dir,
        "cleanup_warning": cleanup_warning,
    }


__all__ = [
    "AppInstallCancelled",
    "AppInstallError",
    "app_install_id",
    "app_package_root",
    "default_install_root",
    "hub_app_downloader",
    "install_hub_app",
    "load_install_record",
    "uninstall_hub_app",
    "update_hub_app",
    "validate_app_archive_paths",
]
