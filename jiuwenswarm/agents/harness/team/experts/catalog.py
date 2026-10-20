# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``ExpertCatalog`` for openjiuwen: which expert packages are installed, and where.

Installed means: the kind's ``marketplace.json`` lists the package with
``installed: true`` and its directory exists under ``local/`` or
``built_in/`` — the same rule Team assembly uses to resolve an AgentGroup.
The catalog never opens a manifest; openjiuwen parses packages itself
(cards for browsing, the full loader for spawning). All file access goes
through ``aiofiles`` so the leader's tools never block the event loop.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import aiofiles
import aiofiles.os

from jiuwenswarm.server.runtime.extension_package_manager import (
    _reject_package_name,
    expert_package_roots,
)

if TYPE_CHECKING:
    from openjiuwen.agent_teams.experts import PackageLocation

logger = logging.getLogger(__name__)

# aiofiles has no realpath; ``wrap`` is its documented way to add one.
_realpath = aiofiles.os.wrap(os.path.realpath)

PackageType = Literal["agent_template", "agent_group"]
_SOURCES = frozenset({"builtin", "local", "hub"})


class JiuwenExpertCatalog:
    """Locates installed ``agent_template`` / ``agent_group`` packages."""

    async def list_expert_packages(self) -> list["PackageLocation"]:
        return await _list_installed("agent_template")

    async def locate_expert(self, name: str) -> "PackageLocation":
        return await _locate_installed("agent_template", name)

    async def list_group_packages(self) -> list["PackageLocation"]:
        return await _list_installed("agent_group")

    async def locate_group(self, name: str) -> "PackageLocation":
        return await _locate_installed("agent_group", name)


async def _marketplace_entries(path: Path) -> list[dict]:
    """``plugins`` entries of a marketplace file; missing or corrupt reads as empty."""
    try:
        async with aiofiles.open(path, encoding="utf-8") as handle:
            data = json.loads(await handle.read())
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning("[expert-catalog] unreadable marketplace %s", path, exc_info=True)
        return []
    plugins = data.get("plugins") if isinstance(data, dict) else None
    return [entry for entry in plugins or [] if isinstance(entry, dict)]


async def _package_dir(root: Path, name: str) -> Path | None:
    """The resolved package directory, as ``resolve_agent_group_dir`` / ``resolve_agent_template_dir`` give it."""
    candidate = root / name
    if not await aiofiles.os.path.isdir(candidate):
        return None
    return Path(await _realpath(candidate))


async def _location(package_type: PackageType, entry: dict) -> "PackageLocation":
    """Resolve one installed marketplace entry; ``ValueError`` when it has no usable directory."""
    from openjiuwen.agent_teams.experts import PackageLocation

    name = _reject_package_name(entry.get("id"), package_type)
    _, local_root, built_in_root = expert_package_roots(package_type)
    local_dir = await _package_dir(local_root, name)
    built_in_dir = await _package_dir(built_in_root, name)
    if local_dir is not None and built_in_dir is not None:
        raise ValueError(f"{package_type} package conflict: {name} exists in both local and built_in")
    path = local_dir or built_in_dir
    if path is None:
        raise ValueError(f"{package_type} package directory missing: {name}")
    source = entry.get("source")
    if source not in _SOURCES:
        source = "local" if path == local_dir else "builtin"
    return PackageLocation(name=name, path=path, source=source)


async def _list_installed(package_type: PackageType) -> list["PackageLocation"]:
    marketplace, _, _ = expert_package_roots(package_type)
    locations = []
    seen: set[object] = set()
    for entry in await _marketplace_entries(marketplace):
        if not entry.get("installed") or entry.get("id") in seen:
            continue
        seen.add(entry.get("id"))
        try:
            locations.append(await _location(package_type, entry))
        except ValueError as exc:
            logger.warning("[expert-catalog] skip %s %r: %s", package_type, entry.get("id"), exc)
    return locations


async def _locate_installed(package_type: PackageType, name: str) -> "PackageLocation":
    marketplace, _, _ = expert_package_roots(package_type)
    for entry in await _marketplace_entries(marketplace):
        if entry.get("id") == name and entry.get("installed"):
            return await _location(package_type, entry)
    raise ValueError(f"{package_type} package not installed: {name}")


def install_expert_catalog() -> None:
    """Inject the catalog into openjiuwen's Team runtime (leaders get the expert tools).

    Skipped silently when the installed openjiuwen predates the expert catalog.
    """
    from openjiuwen.agent_teams.runtime import TeamRuntimeManager

    set_catalog = getattr(TeamRuntimeManager, "set_expert_catalog", None)
    if callable(set_catalog):
        set_catalog(JiuwenExpertCatalog())


__all__ = ["JiuwenExpertCatalog", "install_expert_catalog"]
