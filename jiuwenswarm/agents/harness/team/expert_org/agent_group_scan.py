# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure AgentGroup directory scan (no workspace / utils imports)."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from typing import Any


def _reject_name(name: Any) -> str:
    raw = str(name or "").strip()
    if not raw or raw in {".", ".."}:
        raise ValueError(f"invalid agent_group name: {name!r}")
    if "/" in raw or "\\" in raw:
        raise ValueError(f"invalid agent_group name (path separator): {raw}")
    if Path(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
        raise ValueError(f"invalid agent_group name (absolute): {raw}")
    return raw


def _read_manifest(pkg_dir: Path) -> dict[str, Any] | None:
    path = pkg_dir / "manifest.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def scan_agent_group_dirs(
    roots: list[tuple[str, Path | None]],
) -> list[tuple[str, Path]]:
    """Return unique ``(name, path)`` packages across roots.

    Multi-source name conflicts are skipped. Invalid / non-agent_group
    manifests are skipped.
    """
    by_name: dict[str, list[tuple[str, Path]]] = {}
    for source, root in roots:
        if root is None or not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        root_resolved = root.resolve()
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            try:
                safe_name = _reject_name(child.name)
            except ValueError:
                continue
            try:
                candidate = (root / safe_name).resolve()
            except OSError:
                continue
            if not candidate.is_relative_to(root_resolved) or not candidate.is_dir():
                continue
            manifest = _read_manifest(candidate)
            if manifest is None:
                continue
            if manifest.get("package_type") != "agent_group":
                continue
            if manifest.get("name") != safe_name:
                continue
            by_name.setdefault(safe_name, []).append((source, candidate))

    results: list[tuple[str, Path]] = []
    for name in sorted(by_name):
        matches = by_name[name]
        if len(matches) != 1:
            continue
        results.append((name, matches[0][1]))
    return results


__all__ = ["scan_agent_group_dirs"]
