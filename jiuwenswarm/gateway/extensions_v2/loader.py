# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Discover and load Gateway extension packages.

Gateway and AgentServer share the install directory (``~/.jiuwenswarm/
extension_pkgs``); a package declares its host with ``package.domain``
(``agentserver`` or ``gateway``, default ``agentserver``).

Loading follows the Core loader: every entry file is executed inside a fresh
package module, then ``create_extension()`` hands a registrar to the package.
A broken package is skipped with a warning; it never blocks startup.
"""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import logging
import sys
import threading
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jiuwenswarm.gateway.extensions_v2.registry import GatewayRegistrar, HandlerDeclaration
from jiuwenswarm.gateway.extensions_v2.manifest import (
    DOMAIN_AGENT_SERVER,
    DOMAIN_GATEWAY,
    has_manifest,
    read_manifest_document,
)

logger = logging.getLogger(__name__)

DEFAULT_DOMAIN = DOMAIN_AGENT_SERVER
KNOWN_DOMAINS = frozenset({DOMAIN_AGENT_SERVER, DOMAIN_GATEWAY})


@dataclass(frozen=True, slots=True)
class LoadedPackage:
    """One package whose handlers are ready to publish."""

    package_id: str
    version: str
    root: Path
    handlers: tuple[HandlerDeclaration, ...]
    ui: Mapping[str, Any] = field(default_factory=dict)
    client: Mapping[str, Any] = field(default_factory=dict)
    #: ``display.name`` as ``{zh, en}`` (empty when absent); the host shows it in navigation, never as code.
    display_name: Mapping[str, str] = field(default_factory=dict)
    #: Root module names created by this load; removed from ``sys.modules`` once the activation is released.
    module_names: tuple[str, ...] = ()


def load_gateway_packages(install_dir: Path) -> list[LoadedPackage]:
    """Load every Gateway package installed under ``install_dir``."""

    loaded: list[LoadedPackage] = []
    for root in discover_package_roots(install_dir):
        try:
            manifest = _read_manifest(root)
            if manifest["domain"] != DOMAIN_GATEWAY:
                continue
            handlers, module_names = _load_handlers(root, manifest["modules"])
        except Exception as exc:  # noqa: BLE001 - one bad package must not block startup
            logger.warning("[GatewayExtensions] skipping package %s: %s", root, exc)
            continue
        loaded.append(_package_from(root, manifest, handlers, module_names))
    return loaded


def load_gateway_package(root: Path) -> LoadedPackage:
    """Load one installed version directory, refusing non-Gateway packages."""

    manifest = _read_manifest(Path(root))
    if manifest["domain"] != DOMAIN_GATEWAY:
        raise ValueError(f"{root} is not a gateway package")
    handlers, module_names = _load_handlers(Path(root), manifest["modules"])
    return _package_from(Path(root), manifest, handlers, module_names)


def release_package_modules(package: LoadedPackage) -> None:
    """Drop the modules a load created; safe to call more than once."""

    _release_modules(list(package.module_names))


def _package_from(
    root: Path,
    manifest: Mapping[str, Any],
    handlers: list[HandlerDeclaration],
    module_names: tuple[str, ...] = (),
) -> LoadedPackage:
    return LoadedPackage(
        package_id=manifest["package_id"],
        version=manifest["version"],
        root=root,
        handlers=tuple(handlers),
        ui=manifest["ui"],
        client=manifest["client"],
        display_name=manifest["display_name"],
        module_names=module_names,
    )


def discover_package_roots(install_dir: Path) -> list[Path]:
    """Return the highest installed version of every package id."""

    directory = Path(install_dir).expanduser()
    if not directory.is_dir():
        return []
    highest: dict[str, tuple[tuple[int, ...], Path]] = {}
    for package_dir in sorted(directory.iterdir()):
        if not package_dir.is_dir():
            continue
        candidates = [package_dir] if has_manifest(package_dir) else [
            child
            for child in package_dir.iterdir()
            if child.is_dir() and has_manifest(child)
        ]
        for root in candidates:
            key = _version_key(root.name)
            current = highest.get(package_dir.name)
            if current is None or key > current[0]:
                highest[package_dir.name] = (key, root)
    return [root for _, root in highest.values()]


def _read_manifest(root: Path) -> dict[str, Any]:
    raw = read_manifest_document(root)
    package = raw["package"]
    return {
        "package_id": package["id"],
        "version": str(package["version"]).strip(),
        "domain": _domain(package.get("domain")),
        "modules": _modules(raw.get("extensions"), root),
        "ui": _optional_mapping(raw.get("ui"), "ui"),
        "client": _optional_mapping(raw.get("client"), "client"),
        "display_name": _display_name(raw.get("display")),
    }


def _display_name(display: object) -> dict[str, str]:
    """Read ``display.name`` as ``{zh, en}``; an absent, empty or oversized name is ignored.

    A single string counts for both languages and a missing language falls back to the
    other one, like the package catalog does.
    """

    value = display.get("name") if isinstance(display, Mapping) else None
    if isinstance(value, str):
        value = {"zh": value, "en": value}
    if not isinstance(value, Mapping):
        return {}
    names: dict[str, str] = {}
    for language in ("zh", "en"):
        text = value.get(language)
        text = " ".join(text.split()) if isinstance(text, str) else ""
        if text and len(text.encode("utf-8")) <= 200:
            names[language] = text
    if not names:
        return {}
    return {"zh": names.get("zh") or names["en"], "en": names.get("en") or names["zh"]}


def _optional_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return dict(value)


def _domain(value: object) -> str:
    """Read the host split; an omitted field keeps the Core default (agentserver)."""

    if value is None:
        return DEFAULT_DOMAIN
    if not isinstance(value, str) or value.strip().lower() not in KNOWN_DOMAINS:
        raise ValueError(f"domain must be one of {sorted(KNOWN_DOMAINS)}")
    return value.strip().lower()


def _modules(value: object, root: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("extensions must be a non-empty list")
    modules: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("each extensions item must be a mapping")
        module = item.get("module")
        if not isinstance(module, str) or not module.strip():
            raise ValueError("each extensions item needs a module")
        _ensure_entry(root, module.strip())
        modules.append(module.strip())
    return tuple(modules)


def _ensure_entry(root: Path, module: str) -> None:
    relative = _module_relative(module)
    entry = (root / relative).resolve()
    if entry.is_symlink() or not entry.is_file() or not entry.is_relative_to(root.resolve()):
        raise ValueError(f"cannot load module {module} under {root}")


def _module_relative(module: str) -> Path:
    text = module.strip()
    relative = Path(text) if text.endswith(".py") else Path(*text.split(".")).with_suffix(".py")
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise ValueError(f"entry path is invalid: {module}")
    return relative


#: Every load gets new module names, like the Core loader's load serial: a package reinstalled at the
#: same path and version must not reuse the sibling modules cached in ``sys.modules`` by the earlier load.
_LOAD_SERIAL = itertools.count(1)
_LOAD_SERIAL_LOCK = threading.Lock()


def _next_load_serial() -> int:
    with _LOAD_SERIAL_LOCK:
        return next(_LOAD_SERIAL)


def _load_handlers(root: Path, modules: tuple[str, ...]) -> tuple[list[HandlerDeclaration], tuple[str, ...]]:
    """Execute every entry and return its declarations with the root module names it created."""

    registrar = GatewayRegistrar()
    serial = f"{hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:8]}-{_next_load_serial()}-"
    roots: list[str] = []
    try:
        for index, module in enumerate(modules):
            name = _module_name(root.name, module, f"{serial}{index}")
            roots.append(name)
            entry_name, _ = _load_entry(name, root, _module_relative(module))
            _call_register(entry_name, registrar)
    except Exception:
        # Earlier entries of the same package are released as well.
        _release_modules(roots)
        raise
    return list(registrar.declarations), tuple(roots)


def _module_name(version: str, module: str, serial: str) -> str:
    raw = f"{version}\n{module}\n{serial}".encode()
    return f"jiuwenswarm_gateway_ext_{hashlib.sha256(raw).hexdigest()[:24]}"


def _call_register(module_name: str, registrar: GatewayRegistrar) -> None:
    module = sys.modules[module_name]
    factory = getattr(module, "create_extension", None)
    if not callable(factory):
        raise ValueError(f"{module_name} has no create_extension()")
    extension = factory()
    register = getattr(extension, "register", None)
    if not callable(register):
        raise ValueError(f"{module_name} create_extension() result has no register method")
    register(registrar)


# ── module loading ────────────────────────────────────────────────────────
# A package entry is executed as a member of a fresh package rooted at the
# installed version directory, so ``from .handlers import x`` works without
# adding the install directory to ``sys.path``.


def _load_entry(name: str, package_root: Path, relative: Path) -> tuple[str, list[str]]:
    package_root = package_root.resolve()
    entry = (package_root / relative).resolve()
    if entry.is_symlink() or not entry.is_file() or not entry.is_relative_to(package_root):
        raise ValueError(f"cannot load module {entry}")
    parts = relative.with_suffix("").parts
    created: list[str] = []
    try:
        _ensure_package(name, package_root, package_root, created)
        parent_parts: list[str] = []
        for part in parts[:-1]:
            parent_parts.append(part)
            _ensure_package(
                f"{name}.{'.'.join(parent_parts)}",
                package_root.joinpath(*parent_parts),
                package_root,
                created,
            )
        entry_name = f"{name}.{'.'.join(parts)}"
        package_name = name if len(parts) == 1 else f"{name}.{'.'.join(parts[:-1])}"
        _load_module(entry_name, entry, package_name=package_name, search_locations=None, created=created)
    except Exception:
        _release_modules(created)
        raise
    return entry_name, created


def _ensure_package(name: str, path: Path, boundary: Path, created: list[str]) -> None:
    if name in created:
        return
    package_path = path.resolve()
    if not package_path.is_relative_to(boundary):
        raise ValueError(f"package path escapes package root: {package_path}")
    init = package_path / "__init__.py"
    if init.exists():
        _load_module(
            name,
            init.resolve(),
            package_name=name,
            search_locations=[str(package_path)],
            created=created,
        )
        return
    module = types.ModuleType(name)
    module.__path__ = [str(package_path)]
    module.__package__ = name
    sys.modules[name] = module
    _attach_to_parent(name, module)
    created.append(name)


def _load_module(
    name: str,
    module_path: Path,
    *,
    package_name: str,
    search_locations: list[str] | None,
    created: list[str],
) -> None:
    importlib.invalidate_caches()
    kwargs: dict[str, Any] = {}
    if search_locations is not None:
        kwargs["submodule_search_locations"] = search_locations
    spec = importlib.util.spec_from_file_location(name, module_path, **kwargs)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot create import spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = package_name
    sys.modules[name] = module
    _attach_to_parent(name, module)
    created.append(name)
    spec.loader.exec_module(module)


def _attach_to_parent(name: str, module: types.ModuleType) -> None:
    parent_name, dot, attribute = name.rpartition(".")
    if dot and parent_name in sys.modules:
        setattr(sys.modules[parent_name], attribute, module)


def _release_modules(names: list[str]) -> None:
    for name in names:
        sys.modules.pop(name, None)
        prefix = f"{name}."
        for loaded in [key for key in sys.modules if key.startswith(prefix)]:
            sys.modules.pop(loaded, None)


def _version_key(name: str) -> tuple[int, ...]:
    parts = name.split(".")
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return (0,)
