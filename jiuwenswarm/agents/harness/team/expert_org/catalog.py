# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host ExpertGroupCatalog: scan AgentGroup packages and return descriptors."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExpertGroupDescriptor:
    """AgentGroup template metadata for OrganizationRuntime Catalog injection.

    Shape matches openjiuwen ``ExpertGroupDescriptor`` (duck-typed Protocol).
    """

    agent_group_name: str
    display_name: str = ""
    description: str = ""
    capabilities: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["capabilities"] = list(self.capabilities)
        return data


def _iter_agent_group_dirs() -> list[tuple[str, Path]]:
    """Enumerate installed packages through the same resolver used for launch."""
    from jiuwenswarm.server.runtime.extension_package_manager import (
        read_agent_group_marketplace_entries,
        resolve_agent_group_dir,
    )

    results: list[tuple[str, Path]] = []
    for entry in read_agent_group_marketplace_entries():
        if not entry.get("installed", False):
            continue
        name = entry["id"]
        try:
            results.append((name, resolve_agent_group_dir(name)))
        except (ValueError, OSError) as exc:
            logger.warning("[ExpertGroupCatalog] skip unavailable agent_group %s: %s", name, exc)
    return sorted(results)


def _resolve_agent_group_dir(name: str) -> Path:
    from jiuwenswarm.server.runtime.extension_package_manager import (
        resolve_agent_group_dir,
    )

    return resolve_agent_group_dir(name)


def _load_agent_group_package_bundle(path: Path) -> Any:
    """Lazy import so unit tests can monkeypatch without loading swarm assembly."""
    from jiuwenswarm.agents.swarm.agent_group import (
        load_agent_group_package_bundle,
    )

    return load_agent_group_package_bundle(path)


def descriptor_from_agent_group_dir(
    agent_group_name: str, package_dir: Path
) -> ExpertGroupDescriptor:
    """Validate one package and map its single load result to a descriptor."""
    package = _load_agent_group_package_bundle(package_dir)
    leader = package.templates.get("leader")
    leader_card = getattr(leader, "agent_card", None) if leader is not None else None
    display_name = package.manifest.get("display_name")
    if isinstance(display_name, dict):
        display_name = display_name.get("zh") or display_name.get("en")
    display_name = str(display_name or getattr(leader_card, "name", "") or "").strip() or agent_group_name
    description = package.instruction or str(
        getattr(leader_card, "description", "") or ""
    ).strip()
    return ExpertGroupDescriptor(
        agent_group_name=agent_group_name,
        display_name=display_name,
        description=description,
        capabilities=package.capabilities,
    )


class JiuwenExpertGroupCatalog:
    """Scan AgentGroup roots, validate packages, return ExpertGroupDescriptor list."""

    @staticmethod
    def list(*, capabilities: set[str] | None = None) -> list[ExpertGroupDescriptor]:
        required = set(capabilities or ())
        results: list[ExpertGroupDescriptor] = []
        for name, package_dir in _iter_agent_group_dirs():
            try:
                descriptor = descriptor_from_agent_group_dir(name, package_dir)
            except (ValueError, OSError) as exc:
                logger.warning(
                    "[ExpertGroupCatalog] skip invalid agent_group %s: %s",
                    name,
                    exc,
                )
                continue
            if required and not required.issubset(set(descriptor.capabilities)):
                continue
            results.append(descriptor)
        return results

    @staticmethod
    def get(name: str) -> ExpertGroupDescriptor:
        package_dir = _resolve_agent_group_dir(str(name).strip())
        return descriptor_from_agent_group_dir(str(name).strip(), package_dir)


__all__ = [
    "ExpertGroupDescriptor",
    "JiuwenExpertGroupCatalog",
    "descriptor_from_agent_group_dir",
]
