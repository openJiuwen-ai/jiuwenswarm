# Gateway 侧副本，源文件 jiuwenswarm/common/windows_cli_paths.py；改动请同步两侧。
# 本副本仅保留 gateway 实际引用的符号；同步母仓时请勿整体覆盖。
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Resolve preserved Windows npm CLIs without depending on installer-owned files."""

from __future__ import annotations

import json
import os
from pathlib import Path


def windows_npm_root() -> Path:
    """Share the user data path contract with the pre-install PowerShell helper."""
    from gateway.common.paths import get_user_workspace_dir

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
                from gateway.common.external_cli_runtime import (
                    get_external_cli_runtime_status,
                )

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
