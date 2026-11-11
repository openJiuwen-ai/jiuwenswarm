# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Project extension packages into the card shape used by the extension page."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openjiuwen.core.extension.data.json_values import valid_package_id
from openjiuwen.core.extension.versions import parse_version

from jiuwenswarm.extensions_v2.manifest_io import (
    has_manifest,
    package_domain,
    read_manifest_document,
)
from jiuwenswarm.server.runtime.marketplace.extension_install_state import (
    ExtensionInstallRecord,
    ExtensionInstallState,
)

_AVATAR_MIME = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_AVATAR_LIMIT = 1024 * 1024
_PREVIEW_IMAGE = "assets/preview.png"
_PREVIEW_IMAGE_LIMIT = 5 * 1024 * 1024
_README_LIMIT = 64 * 1024


class PackageNotFound(LookupError):
    """Raised when a package id cannot be resolved to a package root."""


@dataclass(frozen=True, slots=True)
class MarketEntry:
    """One package directory with its parsed manifest."""

    package_id: str
    version: str
    root: Path
    domain: str
    document: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class StateIndex:
    """Active and installed facts keyed by package id."""

    active: dict[str, Any]
    installed: dict[str, Path]
    roots: dict[str, tuple[Path, ...]] = field(default_factory=dict)


def build_state_index(state: Any) -> StateIndex:
    active = {record.package_id: record for record in state.active}
    installed: dict[str, Path] = {}
    roots: dict[str, list[Path]] = {}
    for root in state.installed:
        roots.setdefault(root.parent.name, []).append(root)
        current = installed.get(root.parent.name)
        if current is None or _version_key(root) > _version_key(current):
            installed[root.parent.name] = root
    return StateIndex(
        active=active,
        installed=installed,
        roots={package_id: tuple(sorted(items, key=_version_key)) for package_id, items in roots.items()},
    )


def scan_examples(
    examples_dir: Path,
    *,
    exposed_only: bool = True,
    domain: str | None = None,
) -> list[MarketEntry]:
    """Read example packages, skipping invalid ones."""

    entries: list[MarketEntry] = []
    if not examples_dir.is_dir():
        return entries
    for root in sorted(examples_dir.iterdir(), key=lambda path: path.name):
        if not root.is_dir() or root.name.startswith(".") or not has_manifest(root):
            continue
        entry = entry_from_root(root)
        if entry is None:
            continue
        if exposed_only and entry.document.get("expose") is not True:
            continue
        if domain is not None and entry.domain != domain:
            continue
        entries.append(entry)
    return entries


def entry_from_root(root: Path) -> MarketEntry | None:
    """Build an entry from one package root; invalid manifests return None."""

    try:
        document = read_manifest_document(root)
    except (OSError, UnicodeError, ValueError):
        return None
    package = document["package"]
    return MarketEntry(
        package_id=package["id"],
        version=package["version"].strip(),
        root=root,
        domain=package_domain(document),
        document=document,
    )


def list_catalog(
    examples_dir: Path,
    state: Any,
    *,
    domain: str | None = None,
    install_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return the preset packages shown in the market tab, regardless of install state."""

    index = build_state_index(state)
    store = _install_state(install_dir)
    result = []
    for entry in scan_examples(examples_dir, domain=domain):
        record = _record(store, entry.package_id, installed_version(index, entry.package_id))
        result.append(_summary(entry, index, origin=record.origin if record else "preset", origin_record=record))
    return result


def list_mine(
    examples_dir: Path,
    state: Any,
    *,
    domain: str | None = None,
    install_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return installed packages, including ones currently kept disabled."""

    index = build_state_index(state)
    examples = {
        entry.package_id: entry for entry in scan_examples(examples_dir, exposed_only=False, domain=domain)
    }
    store = _install_state(install_dir)
    packages: list[dict[str, Any]] = []
    package_ids = set(index.active) | set(index.installed)
    for package_id in sorted(package_ids):
        record = index.active.get(package_id)
        root = record.root if record is not None else index.installed[package_id]
        entry = entry_from_root(root) or examples.get(package_id)
        if entry is not None and (domain is None or entry.domain == domain):
            installed = installed_version(index, package_id)
            record = _record(store, package_id, installed)
            origin = record.origin if record else ("preset" if package_id in examples else "user")
            packages.append(_summary(entry, index, origin=origin, origin_record=record))
    return packages


def find_entry(
    package_id: str,
    examples_dir: Path,
    state: Any,
    *,
    domain: str | None = None,
    install_dir: Path | None = None,
) -> MarketEntry:
    """Resolve one package, preferring the installed copy over the preset."""

    index = build_state_index(state)
    record = index.active.get(package_id)
    roots = [record.root] if record is not None else []
    installed = index.installed.get(package_id)
    if installed is not None:
        roots.append(installed)
    for root in roots:
        resolved = entry_from_root(root)
        if resolved is not None and (domain is None or resolved.domain == domain):
            return resolved
    if install_dir is not None:
        resolved = _installed_entry(package_id, install_dir)
        if resolved is not None and (domain is None or resolved.domain == domain):
            return resolved
    examples = {
        entry.package_id: entry for entry in scan_examples(examples_dir, exposed_only=False, domain=domain)
    }
    entry = examples.get(package_id)
    if entry is not None:
        return entry
    raise PackageNotFound(package_id)


def project_detail(
    entry: MarketEntry,
    state: Any,
    *,
    install_dir: Path | None = None,
) -> dict[str, Any]:
    """Return the detail payload for one package."""

    index = build_state_index(state)
    display = _display(entry.document)
    store = _install_state(install_dir or _entry_install_dir(entry, index))
    installed = installed_version(index, entry.package_id)
    record = _record(store, entry.package_id, installed)
    detail = _summary(entry, index, origin=record.origin if record else "preset", origin_record=record)
    detail.update(
        {
            "details": _localized(display.get("details"), ""),
            "readme": _readme(entry.root),
            "preview": _preview_image(entry.root),
            "events": _events(display),
        }
    )
    return detail


def resolve_install_source(package_id: str, *, examples_dir: Path, install_dir: Path) -> Path:
    """Resolve a package id to the newest local source directory."""

    if not valid_package_id(package_id):
        raise ValueError("package_id is invalid")
    for entry in scan_examples(examples_dir, exposed_only=False):
        if entry.package_id == package_id:
            return entry.root
    entry = _installed_entry(package_id, install_dir)
    if entry is not None:
        return entry.root
    raise PackageNotFound(package_id)


def _installed_entry(package_id: str, install_dir: Path) -> MarketEntry | None:
    """Return the newest installed copy of a package id, if any."""

    package_dir = install_dir / package_id
    if not package_dir.is_dir():
        return None
    roots = sorted(
        (path for path in package_dir.iterdir() if path.is_dir() and has_manifest(path)),
        key=_version_key,
        reverse=True,
    )
    if not roots and has_manifest(package_dir):
        roots = [package_dir]
    for root in roots:
        entry = entry_from_root(root)
        if entry is not None and entry.package_id == package_id:
            return entry
    return None


def _summary(
    entry: MarketEntry,
    index: StateIndex,
    *,
    origin: str = "preset",
    origin_record: ExtensionInstallRecord | None = None,
) -> dict[str, Any]:
    document = entry.document
    display = _display(document)
    active_record = index.active.get(entry.package_id)
    installed_root = index.installed.get(entry.package_id)
    managed_root = active_record.root if active_record is not None else installed_root
    installed = active_record is not None or installed_root is not None
    installed_version_value = installed_version(index, entry.package_id)
    versions = installed_versions(index, entry.package_id)
    if entry.version not in versions:
        versions.append(entry.version)
    versions = sort_versions(versions)
    active = active_record is not None
    enabled = active_record is not None or (
        origin_record.enabled if origin_record is not None else _enabled(entry, managed_root)
    )
    return {
        "id": entry.package_id,
        "packageId": entry.package_id,
        "assetId": origin_record.asset_id if origin_record is not None else "",
        "version": entry.version,
        "installedVersion": installed_version_value,
        "versions": versions,
        "displayName": _localized(display.get("name"), entry.package_id),
        "displayDescription": _localized(display.get("description"), ""),
        "tags": _localized_list(display.get("tags")),
        "avatar": _avatar(entry.root, display),
        "source": "hub" if origin == "hub" else "local",
        "origin": origin,
        "installed": installed,
        "active": active,
        "enabled": enabled,
    }


def installed_version(index: StateIndex, package_id: str) -> str:
    record = index.active.get(package_id)
    if record is not None:
        version = getattr(record, "version", "")
        if version:
            return str(version)
    root = index.installed.get(package_id)
    if root is None:
        return ""
    entry = entry_from_root(root)
    return entry.version if entry is not None else root.name


def installed_versions(index: StateIndex, package_id: str) -> list[str]:
    """Return every package version present in the managed install store."""

    versions: list[str] = []
    for root in index.roots.get(package_id, ()):
        entry = entry_from_root(root)
        versions.append(entry.version if entry is not None else root.name)
    return versions


def sort_versions(versions: Sequence[str]) -> list[str]:
    """Return unique versions in ascending order for the version selector."""

    return sorted({version for version in versions if version}, key=_version_text_key)


def merge_versions(*groups: Sequence[str]) -> list[str]:
    """Merge available and locally installed versions without duplicates."""

    return sort_versions([version for group in groups for version in group])


def _install_state(install_dir: Path | None) -> ExtensionInstallState | None:
    return ExtensionInstallState.for_install_dir(install_dir) if install_dir is not None else None


def _record(
    store: ExtensionInstallState | None,
    package_id: str,
    version: str,
) -> ExtensionInstallRecord | None:
    if store is None:
        return None
    return store.get(package_id, version) or store.get(package_id)


def _entry_install_dir(entry: MarketEntry, index: StateIndex) -> Path | None:
    root = index.installed.get(entry.package_id)
    if root is not None:
        return root.parent.parent
    record = index.active.get(entry.package_id)
    if record is not None:
        return record.root.parent.parent
    return None


def _enabled(entry: MarketEntry, installed_root: Path | None) -> bool:
    """Read the persisted state from the managed copy when one exists."""
    if installed_root is None:
        value = entry.document.get("enable")
    else:
        try:
            value = read_manifest_document(installed_root).get("enable")
        except (OSError, UnicodeError, ValueError):
            value = entry.document.get("enable")
    return value if isinstance(value, bool) else True


def _display(document: Mapping[str, Any]) -> Mapping[str, Any]:
    value = document.get("display")
    return value if isinstance(value, Mapping) else {}


def _localized(value: object, fallback: str) -> dict[str, str]:
    if isinstance(value, str):
        text = value.strip()
        return {"zh": text or fallback, "en": text or fallback}
    if isinstance(value, Mapping):
        zh = value.get("zh")
        en = value.get("en")
        zh_text = zh.strip() if isinstance(zh, str) and zh.strip() else ""
        en_text = en.strip() if isinstance(en, str) and en.strip() else ""
        return {"zh": zh_text or en_text or fallback, "en": en_text or zh_text or fallback}
    return {"zh": fallback, "en": fallback}


def _localized_list(value: object) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append({"zh": item.strip(), "en": item.strip()})
        elif isinstance(item, Mapping):
            text = _localized(item, "")
            if text["zh"] or text["en"]:
                result.append(text)
    return result


def _readme(root: Path) -> dict[str, str]:
    """Return both README languages so the client can switch without refetching."""

    zh = _read_limited(root, "README.md")
    en = _read_limited(root, "README_EN.md")
    return {"zh": zh or en, "en": en or zh}


def _read_limited(root: Path, name: str) -> str:
    path = root / name
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        return ""
    try:
        return path.read_text(encoding="utf-8")[:_README_LIMIT].strip()
    except (OSError, UnicodeError):
        return ""


def _data_url(root: Path, path: Path, limit: int) -> str | None:
    """Return a package file as a data URL, or None when unusable."""

    mime = _AVATAR_MIME.get(path.suffix.lower())
    if mime is None or path.is_symlink() or not path.is_file():
        return None
    if not path.resolve().is_relative_to(root.resolve()) or path.stat().st_size > limit:
        return None
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _preview_image(root: Path) -> str | None:
    """Return the fixed package preview image as a data URL."""

    return _data_url(root, root / _PREVIEW_IMAGE, _PREVIEW_IMAGE_LIMIT)


def _events(display: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = display.get("events")
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        result.append({"name": name.strip(), "description": _localized(item.get("description"), "")})
    return result


def _avatar(root: Path, display: Mapping[str, Any]) -> str | None:
    raw = display.get("avatar")
    if not isinstance(raw, str) or not raw.strip():
        return None
    relative = Path(raw.strip())
    if relative.is_absolute() or ".." in relative.parts:
        return None
    return _data_url(root, root / relative, _AVATAR_LIMIT)


def _version_key(root: Path) -> tuple[int, int, int]:
    try:
        version = parse_version(root.name)
    except (TypeError, ValueError):
        return (0, 0, 0)
    return (version.major, version.minor, version.patch)


def _version_text_key(version: str) -> tuple[int, int, int, str]:
    try:
        parsed = parse_version(version)
    except (TypeError, ValueError):
        return (0, 0, 0, version)
    return (parsed.major, parsed.minor, parsed.patch, version)
