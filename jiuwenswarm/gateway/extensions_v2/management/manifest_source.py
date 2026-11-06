# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read installed package facts without importing or executing package code."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from jiuwenswarm.gateway.extensions_v2.manifest import (
    DOMAIN_AGENT_SERVER,
    has_manifest,
    package_domain,
    read_manifest_document,
)


@dataclass(frozen=True, slots=True)
class InstalledPackage:
    """The three host-facing fields of one installed version directory."""

    package_id: str
    version: str
    domain: str
    root: Path


def read_domain(root: Path) -> str:
    """Return the manifest ``domain``; a missing or invalid value means agent."""

    try:
        return package_domain(read_manifest_document(Path(root)))
    except (OSError, UnicodeError, ValueError):
        return DOMAIN_AGENT_SERVER


def list_installed(install_dir: Path) -> list[InstalledPackage]:
    """Return every ``<id>/<version>`` directory that has a manifest."""

    store = Path(install_dir).expanduser()
    if not store.is_dir():
        return []
    found: list[InstalledPackage] = []
    for package_dir in sorted(store.iterdir()):
        if not package_dir.is_dir() or package_dir.name.startswith("."):
            continue
        for version_dir in sorted(package_dir.iterdir()):
            if not version_dir.is_dir() or version_dir.name.startswith("."):
                continue
            if not has_manifest(version_dir):
                continue
            try:
                document = read_manifest_document(version_dir)
            except (OSError, UnicodeError, ValueError):
                document = {}
            package = document.get("package")
            package = package if isinstance(package, Mapping) else {}
            found.append(
                InstalledPackage(
                    package_id=str(package.get("id") or package_dir.name),
                    version=str(package.get("version") or version_dir.name),
                    domain=package_domain(document),
                    root=version_dir.resolve(),
                )
            )
    return found
