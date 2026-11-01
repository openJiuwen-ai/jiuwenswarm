# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read installed package facts without importing or executing package code."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

MANIFEST_NAME = "manifest.yaml"
DOMAIN_AGENT = "agent"
DOMAIN_GATEWAY = "gateway"


@dataclass(frozen=True, slots=True)
class InstalledPackage:
    """The three host-facing fields of one installed version directory."""

    package_id: str
    version: str
    domain: str
    root: Path


def read_domain(root: Path) -> str:
    """Return the manifest ``domain``; a missing or invalid value means agent."""

    raw = _read(root)
    domain = raw.get("domain")
    if isinstance(domain, str) and domain.strip().lower() == DOMAIN_GATEWAY:
        return DOMAIN_GATEWAY
    return DOMAIN_AGENT


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
            if not (version_dir / MANIFEST_NAME).is_file():
                continue
            raw = _read(version_dir)
            found.append(
                InstalledPackage(
                    package_id=str(raw.get("package_id") or package_dir.name),
                    version=str(raw.get("version") or version_dir.name),
                    domain=read_domain(version_dir),
                    root=version_dir.resolve(),
                )
            )
    return found


def _read(root: Path) -> Mapping[str, object]:
    try:
        raw = yaml.safe_load((Path(root) / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    package = raw.get("package")
    if not isinstance(package, Mapping):
        return {}
    return {
        "package_id": package.get("id"),
        "version": package.get("version"),
        "domain": raw.get("domain"),
    }
