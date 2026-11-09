# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Project Gateway packages into the card shape served to the extension page."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.core.extension.data.json_values import valid_package_id
from openjiuwen.core.extension.versions import parse_version

from jiuwenswarm.gateway.extensions_v2.manifest import (
    DOMAIN_GATEWAY,
    has_manifest,
    package_domain,
    read_manifest_document,
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
_PREVIEW_IMAGE_LIMIT = 10 * 1024 * 1024
_README_LIMIT = 64 * 1024


class PackageNotFound(LookupError):
    """Raised when a package id cannot be resolved to a package root."""


@dataclass(frozen=True, slots=True)
class MarketEntry:
    """One Gateway package directory with its parsed manifest."""

    package_id: str
    version: str
    root: Path
    document: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _Facts:
    """Active and installed Gateway facts keyed by package id."""

    active: dict[str, Mapping[str, Any]]
    installed: dict[str, Path]


def _facts(state: Mapping[str, Any]) -> _Facts:
    active = {
        str(record.get("package_id")): record
        for record in state.get("active") or []
        if isinstance(record, Mapping) and record.get("package_id")
    }
    installed: dict[str, Path] = {}
    for root in state.get("installed") or []:
        if not isinstance(root, str) or not root:
            continue
        path = Path(root)
        current = installed.get(path.parent.name)
        if current is None or _version_key(path) > _version_key(current):
            installed[path.parent.name] = path
    return _Facts(active=active, installed=installed)


def list_presets(examples_dir: Path, state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the Gateway presets shown in the market tab, installed or not."""

    facts = _facts(state)
    return [_summary(entry, facts) for entry in _scan(examples_dir, exposed_only=True)]


def list_installed(examples_dir: Path, state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return installed Gateway packages, including ones kept disabled."""

    facts = _facts(state)
    presets = {entry.package_id: entry for entry in _scan(examples_dir, exposed_only=False)}
    packages: list[dict[str, Any]] = []
    package_ids = set(facts.active) | set(facts.installed)
    for package_id in sorted(package_ids):
        record = facts.active.get(package_id)
        roots: list[Path] = []
        active_root = record.get("root") if record is not None else None
        if isinstance(active_root, str) and active_root:
            roots.append(Path(active_root))
        installed_root = facts.installed.get(package_id)
        if installed_root is not None and installed_root not in roots:
            roots.append(installed_root)
        entry = presets.get(package_id)
        for root in roots:
            if entry is not None:
                break
            entry = _entry(root)
        if entry is not None:
            packages.append(_summary(entry, facts))
    return packages


def package_detail(package_id: str, examples_dir: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the detail payload for one Gateway package."""

    entry = _find(package_id, examples_dir, state)
    facts = _facts(state)
    display = _display(entry.document)
    detail = _summary(entry, facts)
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
    """Resolve a package id of either domain to the newest local source directory."""

    if not valid_package_id(package_id):
        raise ValueError("package_id is invalid")
    for entry in _scan(examples_dir, exposed_only=False, domain=None):
        if entry.package_id == package_id:
            return entry.root
    package_dir = install_dir / package_id
    if package_dir.is_dir():
        versions = [path for path in package_dir.iterdir() if path.is_dir() and has_manifest(path)]
        for root in sorted(versions, key=_version_key, reverse=True):
            return root
    raise PackageNotFound(package_id)


def _scan(examples_dir: Path, *, exposed_only: bool, domain: str | None = DOMAIN_GATEWAY) -> list[MarketEntry]:
    entries: list[MarketEntry] = []
    if not examples_dir.is_dir():
        return entries
    for root in sorted(examples_dir.iterdir(), key=lambda path: path.name):
        if not root.is_dir() or root.name.startswith(".") or not has_manifest(root):
            continue
        entry = _entry(root)
        if entry is None or (domain is not None and package_domain(entry.document) != domain):
            continue
        if exposed_only and entry.document.get("expose") is not True:
            continue
        entries.append(entry)
    return entries


def _entry(root: Path) -> MarketEntry | None:
    try:
        document = read_manifest_document(root)
    except (OSError, UnicodeError, ValueError):
        return None
    package = document["package"]
    return MarketEntry(package["id"], package["version"].strip(), root, document)


def _find(package_id: str, examples_dir: Path, state: Mapping[str, Any]) -> MarketEntry:
    presets = {entry.package_id: entry for entry in _scan(examples_dir, exposed_only=False)}
    entry = presets.get(package_id)
    if entry is not None:
        return entry
    facts = _facts(state)
    record = facts.active.get(package_id)
    roots = [record["root"]] if record is not None and isinstance(record.get("root"), str) else []
    installed = facts.installed.get(package_id)
    if installed is not None:
        roots.append(str(installed))
    for root in roots:
        resolved = _entry(Path(root))
        if resolved is not None:
            return resolved
    raise PackageNotFound(package_id)


def _summary(entry: MarketEntry, facts: _Facts) -> dict[str, Any]:
    document = entry.document
    display = _display(document)
    record = facts.active.get(entry.package_id)
    installed_root = facts.installed.get(entry.package_id)
    installed = record is not None or installed_root is not None
    return {
        "id": entry.package_id,
        "version": entry.version,
        "displayName": _localized(display.get("name"), entry.package_id),
        "displayDescription": _localized(display.get("description"), ""),
        "tags": _localized_list(display.get("tags")),
        "avatar": _avatar(entry.root, display),
        "installed": installed,
        "active": record is not None,
        "enabled": _enabled(entry, installed_root),
    }


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
