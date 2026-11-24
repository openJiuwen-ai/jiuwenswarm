# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Resolve preserved Windows npm CLIs without depending on installer-owned files."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def windows_npm_root() -> Path:
    """Share the user data path contract with the pre-install PowerShell helper."""
    from jiuwenswarm.common.utils import get_user_workspace_dir

    return get_user_workspace_dir() / "runtime" / "npm"


def _preserved_prefixes(*, sdk: bool = False) -> list[tuple[Path, Path]]:
    """Read completed records, confining working copies to our installed store."""
    root = windows_npm_root()
    store = root.parent / "external-cli-preserved" if sdk else root / "preserved"
    installed = root.parent / "external-cli" if sdk else root / "installed"
    if not store.is_dir():
        return []
    try:
        manifests = sorted(store.glob("*/mapping.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    prefixes: list[tuple[Path, Path]] = []
    for manifest in manifests:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict) or data.get("schema") != 1:
                continue
            source = Path(data["source"])
            target = Path(data["target"])
            target.resolve().relative_to(installed.resolve())
            if sdk:
                from jiuwenswarm.common.external_cli_runtime import get_external_cli_runtime_status

                if target.parent.parent.resolve() != installed.resolve() or target.name != "site-packages":
                    continue
                if get_external_cli_runtime_status(target.parent.name)["runtime_state"] != "current":
                    continue
            if source.is_absolute() and target.is_dir() and (source, target) not in prefixes:
                prefixes.append((source, target))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            continue
    return prefixes


def resolve_preserved_cli_path(value: str | None, *, directory: bool = False) -> str | None:
    """Translate only missing absolute paths covered by a verified preservation."""
    if os.name != "nt" or not value:
        return value
    quote = value[:1] if value[:1] in {"\"", "'"} and value[-1:] == value[:1] else ""
    original = Path(value[1:-1] if quote else value)
    if not original.is_absolute() or original.exists():
        return value
    for source, target in [*_preserved_prefixes(), *_preserved_prefixes(sdk=True)]:
        try:
            relative = original.relative_to(source)
            candidate = target / relative
            candidate.resolve().relative_to(target.resolve())
            matches = candidate.is_dir() if directory else candidate.is_file()
            if matches:
                return f"{quote}{candidate}{quote}"
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return value


def _configured_prefix() -> str | None:
    explicit = os.environ.get("NPM_CONFIG_PREFIX") or os.environ.get("npm_config_prefix")
    if explicit:
        return explicit
    configs = [Path(os.environ.get("NPM_CONFIG_USERCONFIG") or Path.home() / ".npmrc")]
    if os.environ.get("NPM_CONFIG_GLOBALCONFIG"):
        configs.append(Path(os.environ["NPM_CONFIG_GLOBALCONFIG"]))
    for config in configs:
        try:
            matches = re.findall(
                r"^\s*prefix\s*=\s*(.+?)\s*$",
                config.read_text(encoding="utf-8-sig"),
                re.MULTILINE | re.IGNORECASE,
            )
        except (OSError, UnicodeError):
            continue
        if matches:
            return os.path.expandvars(matches[-1].strip().strip("\"'"))
    return None


def _inside_installation(value: str) -> bool:
    executable_dir = Path(sys.executable).resolve().parent
    install_dir = executable_dir
    if executable_dir.name.lower() == "backend" and executable_dir.parent.name.lower() == "resources":
        install_dir = executable_dir.parent.parent
    try:
        Path(value).expanduser().resolve().relative_to(install_dir)
        return True
    except (OSError, ValueError):
        return False


def configure_windows_npm_environment() -> None:
    """Keep managed npm writes outside the install tree and preserve user overrides."""
    if os.name != "nt":
        return
    root = windows_npm_root()
    prefix = root / "global"
    paths = os.environ.get("PATH", "").split(os.pathsep)
    mappings = _preserved_prefixes()
    saved_paths: list[str] = []
    for _, saved in mappings:
        for directory in (saved, saved / "node_modules" / ".bin"):
            if directory.is_dir():
                saved_paths.append(str(directory))
    # Promote newer generations within our own entries, keeping unrelated PATH
    # order intact and older working copies available as command fallbacks.
    owned = {os.path.normcase(p) for p in saved_paths}
    insertion = next((i for i, p in enumerate(paths) if os.path.normcase(p) in owned), len(paths))
    paths = [p for p in paths if os.path.normcase(p) not in owned]
    paths[insertion:insertion] = saved_paths
    # Respect custom user prefixes outside the installer-owned tree. Replace
    # stale/unsafe selections inside it without rewriting the user's npmrc.
    selected = _configured_prefix()
    managed_selection = not selected or _inside_installation(selected)
    if selected and not managed_selection:
        for source, _ in mappings:
            try:
                Path(os.path.expandvars(selected)).expanduser().resolve().relative_to(source.resolve())
                managed_selection = True
                break
            except (OSError, ValueError):
                continue
    if selected and not managed_selection:
        try:
            managed_selection = Path(selected).expanduser().resolve() == prefix.resolve()
        except (OSError, ValueError):
            # A malformed user preference must not break application startup.
            managed_selection = False
    # This directory contains only npm/npx launchers, so bundled node keeps its
    # process PATH priority for browser tools. npm uses its own pinned interpreter.
    bin_dir = root / "bin"
    if managed_selection and (prefix / "node.exe").is_file():
        os.environ["NPM_CONFIG_PREFIX"] = str(prefix)
        identity = os.path.normcase(str(prefix))
        insertion = next(
            (i for i, p in enumerate(paths) if os.path.normcase(p) in owned or os.path.normcase(p) == identity),
            len(paths),
        )
        paths = [p for p in paths if os.path.normcase(p) != identity]
        paths.insert(insertion, str(prefix))
        if bin_dir.is_dir():
            paths = [str(bin_dir), *[p for p in paths if os.path.normcase(p) != os.path.normcase(str(bin_dir))]]
    os.environ["PATH"] = os.pathsep.join(paths)
