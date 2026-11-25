# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Project extension packages into the card shape used by the extension page."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from openjiuwen.core.extension.bundle import (
    BundleMember,
    bundle_version_dir,
    is_bundle_document,
    read_bundle_spec,
)
from openjiuwen.core.extension.data.json_values import valid_package_id
from openjiuwen.core.extension.errors import ExtensionFailure
from openjiuwen.core.extension.versions import parse_version

from jiuwenswarm.extensions_v2.management.containers import bundle_overall_status
from jiuwenswarm.extensions_v2.manifest_io import (
    DOMAIN_AGENT_SERVER,
    DOMAIN_GATEWAY,
    has_manifest,
    package_domain,
    read_manifest_document,
)
from jiuwenswarm.server.runtime.marketplace.extension_install_state import (
    ExtensionBundleRecord,
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
    members: tuple[BundleMember, ...] = ()

    @property
    def is_bundle(self) -> bool:
        return bool(self.members)


@dataclass(frozen=True, slots=True)
class StateIndex:
    """Active and installed facts keyed by package id."""

    active: dict[str, Any]
    installed: dict[str, Path]
    roots: dict[str, tuple[Path, ...]] = field(default_factory=dict)


def build_state_index(state: Any, store: ExtensionInstallState | None = None) -> StateIndex:
    """Return standalone install facts, ignoring roots owned by bundle copies.

    Bundle members live inside a bundle root and are projected through the
    bundle container; they must never make a standalone package with the same
    id look installed or enabled.
    """

    member_roots = _bundle_member_roots(store)
    active = {
        record.package_id: record
        for record in state.active
        if _is_standalone_root(getattr(record, "root", None), member_roots)
    }
    installed: dict[str, Path] = {}
    installed_keys: dict[str, tuple[int, int, int, str]] = {}
    roots: dict[str, list[Path]] = {}
    for root in state.installed:
        if not _is_standalone_root(root, member_roots):
            continue
        entry = entry_from_root(root)
        package_id = entry.package_id if entry is not None else root.parent.name
        version_key = _root_version_key(root, entry)
        roots.setdefault(package_id, []).append(root)
        current = installed_keys.get(package_id)
        if current is None or version_key > current:
            installed[package_id] = root
            installed_keys[package_id] = version_key
    return StateIndex(
        active=active,
        installed=installed,
        roots={
            package_id: tuple(sorted(items, key=_root_sort_key))
            for package_id, items in roots.items()
        },
    )


def _bundle_member_roots(store: ExtensionInstallState | None) -> set[Path]:
    if store is None:
        return set()
    return {
        Path(member.root).expanduser().resolve()
        for member in store.bundle_members()
        if member.root
    }


def _is_standalone_root(root: Any, member_roots: set[Path]) -> bool:
    if root is None or not member_roots:
        return True
    return Path(root).expanduser().resolve() not in member_roots


def _root_version_key(root: Path, entry: MarketEntry | None) -> tuple[int, int, int, str]:
    if entry is not None:
        return _version_text_key(entry.version)
    return (*_version_key(root), root.name)


def _root_sort_key(root: Path) -> tuple[int, int, int, str]:
    return _root_version_key(root, entry_from_root(root))


def _matches_domain(entry: MarketEntry, domain: str | None) -> bool:
    """Return True when the entry belongs to the domain or is a bundle."""

    return domain is None or entry.domain == domain or entry.is_bundle


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
        if domain is not None and not _matches_domain(entry, domain):
            continue
        entries.append(entry)
    return entries


def entry_from_root(root: Path) -> MarketEntry | None:
    """Build an entry from one package root; invalid manifests return None."""

    try:
        document = read_manifest_document(root)
    except (OSError, ValueError):
        return None
    package = document["package"]
    if is_bundle_document(document):
        try:
            spec = read_bundle_spec(root)
        except ExtensionFailure:
            return None
        return MarketEntry(
            package_id=spec.bundle_id,
            version=spec.version,
            root=root,
            domain="",
            document=document,
            members=spec.members,
        )
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

    store = _install_state(install_dir)
    index = build_state_index(state, store)
    result = []
    for entry in scan_examples(examples_dir, domain=domain):
        record = _record(store, entry.package_id, installed_version(index, entry.package_id))
        result.append(
            _summary(
                entry, index, origin=record.origin if record else "preset",
                origin_record=record, store=store, install_dir=install_dir,
            )
        )
    return result


def list_mine(
    examples_dir: Path,
    state: Any,
    *,
    domain: str | None = None,
    install_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return installed packages, including ones currently kept disabled."""

    store = _install_state(install_dir)
    index = build_state_index(state, store)
    examples = {
        entry.package_id: entry for entry in scan_examples(examples_dir, exposed_only=False, domain=domain)
    }
    packages: list[dict[str, Any]] = []
    package_ids = set(index.active) | set(index.installed)
    if store is not None:
        package_ids.update(record.package_id for record in store.packages())
    for package_id in sorted(package_ids):
        active_record = index.active.get(package_id)
        root = active_record.root if active_record is not None else index.installed.get(package_id)
        if root is None:
            stored = store.get(package_id) if store is not None else None
            if stored is not None:
                root = install_dir / package_id / stored.version if install_dir is not None else None
        entry = (entry_from_root(root) if root is not None else None) or examples.get(package_id)
        if entry is not None and entry.is_bundle:
            # Bundle roots share the store layout with packages but are projected
            # by ``_installed_containers`` as one card per container.
            continue
        if entry is not None and _matches_domain(entry, domain):
            card_index = index
            card_record = _record(store, package_id, installed_version(index, package_id))
            stored = store.get(package_id) if store is not None else None
            if stored is not None and install_dir is not None:
                standalone_root = install_dir / package_id / stored.version
                standalone_entry = (
                    entry_from_root(standalone_root) if standalone_root.is_dir() else None
                )
                if standalone_entry is not None:
                    entry = standalone_entry
                    card_record = stored
                    runtime_record = index.active.get(package_id)
                    standalone_active = (
                        runtime_record is not None
                        and Path(runtime_record.root).resolve() == standalone_root.resolve()
                    )
                    card_index = StateIndex(
                        active=(
                            {package_id: SimpleNamespace(
                                package_id=package_id, version=stored.version, root=standalone_root,
                            )}
                            if standalone_active else {}
                        ),
                        installed={package_id: standalone_root},
                        roots={package_id: (standalone_root,)},
                    )
            origin = card_record.origin if card_record else ("preset" if package_id in examples else "user")
            packages.append(
                _summary(
                    entry, card_index, origin=origin, origin_record=card_record,
                    store=store, install_dir=install_dir,
                )
            )
    if store is not None:
        packages.extend(_installed_containers(store, index, examples, install_dir))
    return packages


def find_entry(
    package_id: str,
    examples_dir: Path,
    state: Any,
    *,
    domain: str | None = None,
    install_dir: Path | None = None,
    accepted_roots: Sequence[Path] = (),
    version: str = "",
) -> MarketEntry:
    """Resolve one package, preferring the installed copy over the preset."""

    store = (
        ExtensionInstallState.for_install_dir(install_dir)
        if install_dir is not None
        else None
    )
    index = build_state_index(state, store)
    if install_dir is not None and version:
        bundle = store.get_bundle(package_id, version)
        if bundle is not None:
            root = bundle_version_dir(install_dir, bundle.bundle_id, bundle.version)
            resolved = entry_from_root(root) if root.is_dir() else None
            if resolved is not None:
                return resolved
        stored = install_dir / package_id / version
        if stored.is_dir():
            resolved = entry_from_root(stored)
            if resolved is not None and _matches_domain(resolved, domain):
                return resolved
        for bundle in store.bundles():
            for member in bundle.member_records:
                if member.package_id != package_id or member.version != version:
                    continue
                member_root = Path(member.root) if member.root else (
                    bundle_version_dir(install_dir, bundle.bundle_id, bundle.version)
                    / "plugins" / package_id
                )
                resolved = entry_from_root(member_root) if member_root.is_dir() else None
                if resolved is not None:
                    return resolved
        examples = {
            entry.package_id: entry for entry in scan_examples(examples_dir, exposed_only=False, domain=domain)
        }
        entry = examples.get(package_id)
        if entry is not None and entry.version == version:
            return entry
        raise PackageNotFound(package_id)
    if install_dir is not None:
        standalone = store.get(package_id)
        if standalone is not None:
            standalone_root = install_dir / package_id / standalone.version
            resolved = (
                entry_from_root(standalone_root) if standalone_root.is_dir() else None
            )
            if resolved is not None and _matches_domain(resolved, domain):
                return resolved
    record = index.active.get(package_id)
    roots = [record.root] if record is not None else []
    installed = index.installed.get(package_id)
    if installed is not None:
        roots.append(installed)
    for root in roots:
        resolved = entry_from_root(root)
        if resolved is not None and _matches_domain(resolved, domain):
            return resolved
    accepted = sorted(
        (root for root in accepted_roots if root.parent.name == package_id),
        key=_version_key,
        reverse=True,
    )
    for root in accepted:
        resolved = entry_from_root(root)
        if resolved is None or resolved.package_id != package_id:
            continue
        if domain is not None and not _matches_domain(resolved, domain):
            continue
        return resolved
    if install_dir is not None:
        for bundle in store.bundles():
            for member in bundle.member_records:
                if member.package_id != package_id:
                    continue
                if version and member.version != version:
                    continue
                member_root = Path(member.root) if member.root else (
                    bundle_version_dir(install_dir, bundle.bundle_id, bundle.version)
                    / "plugins" / member.package_id
                )
                resolved = entry_from_root(member_root) if member_root.is_dir() else None
                if resolved is not None:
                    return resolved
        bundle_record = store.active_bundle(package_id)
        if bundle_record is not None:
            bundle_root = bundle_version_dir(install_dir, bundle_record.bundle_id, bundle_record.version)
            resolved = entry_from_root(bundle_root)
            if resolved is not None:
                return resolved
        resolved = _installed_entry(package_id, install_dir)
        if resolved is not None and _matches_domain(resolved, domain):
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

    store = _install_state(install_dir)
    if store is None:
        store = _install_state(_entry_install_dir(entry, build_state_index(state)))
    index = build_state_index(state, store)
    display = _display(entry.document)
    installed = installed_version(index, entry.package_id)
    record = _record(store, entry.package_id, installed)
    bundle_record = (
        store.get_bundle(entry.package_id, entry.version)
        if entry.is_bundle and store is not None
        else None
    )
    detail = _summary(
        entry,
        index,
        origin=(
            record.origin if record is not None
            else bundle_record.origin if bundle_record is not None
            else "preset"
        ),
        origin_record=record,
        store=store,
        install_dir=install_dir,
        bundle_record=bundle_record,
    )
    detail.update(
        {
            "details": _localized(display.get("details"), ""),
            "readme": _readme(entry.root),
            "preview": _preview_image(entry.root),
            "events": [] if entry.is_bundle else _events(display),
            "systemPlugins": _system_plugins(entry, store, bundle_record),
        }
    )
    return detail


def resolve_install_source(
    package_id: str,
    *,
    examples_dir: Path,
    install_dir: Path,
    version: str = "",
) -> Path:
    """Resolve a package id to the requested or newest local source directory."""

    if not valid_package_id(package_id):
        raise ValueError("package_id is invalid")
    store = ExtensionInstallState.for_install_dir(install_dir)
    bundle_record = (
        store.get_bundle(package_id, version)
        if version
        else store.active_bundle(package_id)
    )
    if bundle_record is not None:
        root = bundle_version_dir(install_dir, bundle_record.bundle_id, bundle_record.version)
        if root.is_dir():
            return root
    for entry in scan_examples(examples_dir, exposed_only=False):
        if entry.package_id == package_id:
            return entry.root
    entry = _installed_entry(package_id, install_dir, version)
    if entry is not None:
        return entry.root
    raise PackageNotFound(package_id)


def _installed_entry(package_id: str, install_dir: Path, version: str = "") -> MarketEntry | None:
    """Return the requested or newest installed copy of a package id, if any."""

    package_dir = install_dir / package_id
    if not package_dir.is_dir():
        return None
    if version:
        root = package_dir / version
        if root.is_dir() and has_manifest(root):
            entry = entry_from_root(root)
            if entry is not None and entry.package_id == package_id:
                return entry
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
    store: ExtensionInstallState | None = None,
    install_dir: Path | None = None,
    bundle_record: ExtensionBundleRecord | None = None,
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
    recorded = _record(store, entry.package_id, installed_version_value) if store is not None else None
    if recorded is not None:
        installed = True
        if entry.domain != DOMAIN_AGENT_SERVER:
            # Gateway members are activated by the Gateway runtime, not this one.
            active = bool(recorded.active)
        installed_version_value = installed_version_value or recorded.version
        if recorded.install_path:
            managed_root = Path(recorded.install_path)
        elif managed_root is None and install_dir is not None:
            managed_root = Path(install_dir) / recorded.package_id / recorded.version
        if recorded.version not in versions:
            versions = sort_versions([*versions, recorded.version])
    if recorded is not None:
        if recorded.status == "failed":
            enabled = False
        elif recorded.status in {"active", "standby"} and recorded.enabled and recorded.install_path:
            # The record owns this standalone copy. Its manifest may still say
            # disabled when a same-id bundle member root was updated instead.
            enabled = True
        elif entry.domain == DOMAIN_GATEWAY:
            enabled = bool(recorded.enabled)
        else:
            enabled = _enabled(entry, managed_root)
    else:
        enabled = _enabled(entry, managed_root)
    if entry.is_bundle:
        records = (
            [record for record in store.bundles() if record.bundle_id == entry.package_id]
            if store is not None
            else []
        )
        versions = sort_versions([*versions, *(record.version for record in records), entry.version])
        active_record = store.active_bundle(entry.package_id) if store is not None else None
        container = bundle_record or active_record
        if container is None and records:
            container = records[0]
        overall = bundle_overall_status(container, store) if store is not None else "not-installed"
        if container is not None and not container.active:
            overall = "disabled"
        installed = bool(records)
        active = bool(
            container and container.active and container.status in {"active", "partial"}
        )
        enabled = active and overall in {"enabled", "partial"}
        if container is not None:
            installed_version_value = (active_record or container).version
    elif not installed:
        overall = "not-installed"
    elif recorded is not None and recorded.status == "pending":
        overall = "partial"
    elif recorded is not None and recorded.status == "failed":
        overall = "failed"
    elif recorded is not None and recorded.status == "standby":
        overall = "enabled" if enabled else "disabled"
    else:
        overall = "enabled" if enabled else "disabled"
    return {
        "id": entry.package_id,
        "packageId": entry.package_id,
        "assetId": (
            origin_record.asset_id if origin_record is not None
            else bundle_record.asset_id if bundle_record is not None
            else ""
        ),
        "version": entry.version,
        "installedVersion": installed_version_value,
        "versions": versions,
        "displayName": _localized(display.get("name"), entry.package_id),
        "displayDescription": _localized(display.get("description"), ""),
        "tags": _localized_list(display.get("tags")),
        "avatar": _avatar(entry.root, display),
        "source": "hub" if origin == "hub" else "local",
        "origin": origin,
        "kind": "bundle" if entry.is_bundle else "plugin",
        "membersCount": len(entry.members) if entry.is_bundle else 1,
        "overallStatus": overall,
        "installed": installed,
        "active": active,
        "enabled": enabled,
    }


def _installed_containers(
    store: ExtensionInstallState,
    index: StateIndex,
    examples: Mapping[str, MarketEntry],
    install_dir: Path | None,
) -> list[dict[str, Any]]:
    """Project one card per installed container, using its active version."""

    packages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in store.bundles():
        if record.bundle_id in seen:
            continue
        seen.add(record.bundle_id)
        selected = store.active_bundle(record.bundle_id) or record
        entry = None
        if install_dir is not None:
            root = bundle_version_dir(install_dir, selected.bundle_id, selected.version)
            entry = entry_from_root(root) if root.is_dir() else None
        if entry is None:
            entry = examples.get(selected.bundle_id)
        if entry is None:
            continue
        card = _summary(
            entry,
            index,
            origin=selected.origin,
            store=store,
            install_dir=install_dir,
            bundle_record=selected,
        )
        card["assetId"] = selected.asset_id
        packages.append(card)
    return packages


def _system_plugins(
    entry: MarketEntry,
    store: ExtensionInstallState | None,
    bundle_record: ExtensionBundleRecord | None = None,
) -> list[dict[str, Any]]:
    """Project bundle members for the detail page."""

    if not entry.is_bundle:
        return []
    if bundle_record is None and store is not None:
        bundle_record = store.get_bundle(entry.package_id, entry.version)
    bundle_active = bool(bundle_record.active) if bundle_record is not None else False
    bundle_installed = bundle_record is not None
    result: list[dict[str, Any]] = []
    for member in entry.members:
        name = {"zh": member.package_id, "en": member.package_id}
        description = {"zh": "", "en": ""}
        try:
            display = _display(read_manifest_document(member.root))
            name = _localized(display.get("name"), member.package_id)
            description = _localized(display.get("description"), "")
        except (OSError, ValueError):
            declared_name, declared_description = _declared_member_display(entry.document, member.package_id)
            if declared_name["zh"] or declared_name["en"]:
                name = declared_name
            description = declared_description
        record = (
            store.get_bundle_member(entry.package_id, entry.version, member.package_id)
            if store is not None
            else None
        )
        if not bundle_installed:
            status, active, enabled = "pending", False, False
        elif not bundle_active:
            status, active, enabled = "disabled", False, False
        else:
            status = record.status if record is not None else "pending"
            active = bool(record.active) if record is not None else False
            enabled = bool(record.enabled) if record is not None else False
        result.append(
            {
                "packageId": member.package_id,
                "displayName": name,
                "displayDescription": description,
                "version": member.version,
                "domain": member.domain,
                "status": status,
                "active": active,
                "enabled": enabled,
                "error": record.error if record is not None else "",
                "installedVersion": record.version if record is not None else "",
            }
        )
    return result


def _declared_member_display(
    document: Mapping[str, Any],
    package_id: str,
) -> tuple[dict[str, str], dict[str, str]]:
    display = document.get("display")
    raw = display.get("system-plugins") if isinstance(display, Mapping) else None
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, Mapping) and str(item.get("packageId") or "").strip() == package_id:
                return _localized(item.get("name"), package_id), _localized(item.get("description"), "")
    return {"zh": "", "en": ""}, {"zh": "", "en": ""}


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
        except (OSError, ValueError):
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
