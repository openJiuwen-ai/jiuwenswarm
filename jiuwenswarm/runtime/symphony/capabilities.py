"""Swarm catalog adapters for shared Symphony capability fingerprints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams.experts.catalog import ExpertRef
from openjiuwen.core.foundation.tool import ToolExposure
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.harness.resources import AgentTemplateCard, read_agent_template_card
from openjiuwen.symphony import (
    CapabilityDescriptor, FingerprintArtifactStore, FingerprintService, SourceSnapshot, SymphonyConfig,
    capability_content_hash,
)
from openjiuwen.symphony.discovery import (
    CapabilityAction, CapabilityCandidate, SkillFS, read_fingerprints,
)

from jiuwenswarm.common.mcp_package_manifest import McpPackageManifest

_LOGGER = logging.getLogger(__name__)


def _describe(kind: str, identifier: str, name: str, description: str, **fields: Any) -> CapabilityDescriptor:
    descriptor = CapabilityDescriptor(
        capability_id=identifier, capability_type=kind, name=name or identifier,
        description=description, source=f"swarm-{kind}", **fields,
    )
    return descriptor.model_copy(update={"content_hash": capability_content_hash(descriptor)})


def _localized_texts(*values: Any) -> tuple[str, ...]:
    texts = []
    for value in values:
        if isinstance(value, dict):
            texts.extend(text.strip() for text in value.values() if isinstance(text, str) and text.strip())
        elif isinstance(value, str) and value.strip():
            texts.append(value.strip())
    return tuple(dict.fromkeys(texts))


def expert_descriptor(ref: ExpertRef, card: AgentTemplateCard) -> CapabilityDescriptor:
    names = _localized_texts(card.display_name, card.name)
    return _describe(
        "expert", ref.label(), names[0] if names else ref.name,
        "\n".join(_localized_texts(card.display_description, card.description)),
        classification=card.category, tags=_localized_texts(*card.tags),
        metadata={"aliases": [ref.name, *names], "version": card.version},
    )


async def _installed_expert_candidates(*, paths_only: bool = False) -> tuple[CapabilityCandidate, ...]:
    """Read the installed catalog, letting Core project explicit source manifests."""
    from openjiuwen.agent_teams.experts.agent_group import read_agent_group_card

    from jiuwenswarm.agents.harness.team.experts import JiuwenExpertCatalog

    catalog = JiuwenExpertCatalog()
    packages = await catalog.list_expert_packages()
    if paths_only:
        return tuple(
            CapabilityCandidate(
                descriptor=CapabilityDescriptor(
                    capability_type="expert", capability_id=location.name, name=location.name,
                ),
                actions=(CapabilityAction("expert", location.name),),
                manifest_path=location.path / "manifest.json",
            )
            for location in packages
        )

    entries = []
    for location in packages:
        manifest = location.path / "manifest.json"
        try:
            card = await read_agent_template_card(manifest)
        except (OSError, ValueError) as exc:
            _LOGGER.debug("Skipping an unreadable expert card (%s)", type(exc).__name__)
            continue
        entries.append(CapabilityCandidate(
            descriptor=expert_descriptor(ExpertRef(location.name), card),
            actions=(CapabilityAction("expert", location.name),),
            manifest_path=manifest,
        ))
    for location in await catalog.list_group_packages():
        try:
            group = await read_agent_group_card(location.path)
        except (OSError, ValueError) as exc:
            _LOGGER.debug("Skipping an unreadable expert group card (%s)", type(exc).__name__)
            continue
        entries.extend(
            CapabilityCandidate(
                descriptor=expert_descriptor(ExpertRef(member.name, location.name), member.card),
                actions=(CapabilityAction("expert", member.name, group=location.name),),
            )
            for member in group.members if not member.is_leader
        )
    return tuple(entries)


def _package_descriptor(package: McpPackageManifest, service_description: str) -> CapabilityDescriptor:
    names = _localized_texts(package.raw.get("display_name"), package.name, package.server_name)
    description = "\n".join(_localized_texts(
        package.raw.get("display_description"), package.description, service_description,
    ))
    category = package.raw.get("category")
    return _describe(
        "mcp", package.package_id, names[0] if names else package.package_id, description,
        classification=category.strip() if isinstance(category, str) else "",
        metadata={"aliases": list(names), "version": package.version},
    )


@dataclass(frozen=True)
class _ServiceCatalog:
    descriptors: dict[str, CapabilityDescriptor]
    packages_by_server: dict[str, list[str]]
    known_ids: frozenset[str]
    available_ids: frozenset[str]
    bundled_skill_roots: dict[str, str]

    def resolve_server(self, name: str) -> str | None:
        """Prefer explicit service IDs and resolve manifest aliases only to a unique available package."""
        if name in self.known_ids:
            return name if name in self.available_ids else None
        packages = self.packages_by_server.get(name)
        if packages is None:
            return name
        available = self.available_ids.intersection(packages)
        return next(iter(available)) if len(available) == 1 else None


def _service_catalog() -> _ServiceCatalog:
    from jiuwenswarm.common.config import get_config_yaml_mcp_servers
    from jiuwenswarm.server.runtime.mcp.registry import list_local_packages
    from jiuwenswarm.server.runtime.mcp.state_store import connected_mcp_skill_dirs, read_mcp_state

    packages = list_local_packages()
    raw_records = read_mcp_state().get("mcp", {})
    records = raw_records if isinstance(raw_records, dict) else {}
    descriptions = {
        name: str(record.get("description") or "").strip() for name, record in records.items()
        if isinstance(record, dict) and record.get("state") == "connected"
    }
    connected = set(descriptions)
    yaml_names = set()
    for entry in get_config_yaml_mcp_servers():
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        yaml_names.add(name)
        if entry.get("enabled", True):
            descriptions[name] = str(entry.get("description") or "").strip() or descriptions.get(name, "")

    descriptors = {
        package.package_id: _package_descriptor(package, descriptions.get(package.package_id, ""))
        for package in packages
    }
    for name, description in descriptions.items():
        if name not in descriptors:
            descriptors[name] = _describe("mcp", name, name, description)
    packages_by_server: dict[str, list[str]] = {}
    for package in packages:
        if package.server_name:
            packages_by_server.setdefault(package.server_name, []).append(package.package_id)
    return _ServiceCatalog(
        descriptors=descriptors,
        packages_by_server=packages_by_server,
        known_ids=frozenset(descriptors.keys() | records.keys() | yaml_names),
        available_ids=frozenset(descriptions),
        bundled_skill_roots={
            str(Path(entry["dir"]).resolve()): entry["name"]
            for entry in connected_mcp_skill_dirs() if entry["name"] in connected
        },
    )


class _CatalogInventory:
    """One immutable provider snapshot for an installed source catalog."""

    def __init__(self, kind: str, descriptors: tuple[CapabilityDescriptor, ...]) -> None:
        self._descriptors = descriptors
        identities = sorted((item.capability_id, item.content_hash) for item in descriptors)
        revision = hashlib.sha256(json.dumps(identities).encode()).hexdigest()
        self._snapshot = SourceSnapshot(
            snapshot_id=revision, source=f"swarm-{kind}", content_hash=revision, capability_count=len(descriptors),
        )

    async def capabilities(self) -> tuple[CapabilityDescriptor, ...]:
        return self._descriptors

    async def source_snapshot(self) -> SourceSnapshot:
        return self._snapshot

    async def inventory_snapshot(self) -> tuple[SourceSnapshot, tuple[CapabilityDescriptor, ...]]:
        return self._snapshot, self._descriptors


def _fingerprint_root(config: SymphonyConfig, kind: str) -> Path:
    return Path(config.storage.artifact_root) / "cache" / "fingerprints" / kind


async def build_catalog_fingerprints(config: SymphonyConfig, model: Any, *, force: bool) -> None:
    """Refresh the non-Skill source catalogs during an explicit retrieval build."""
    services = await asyncio.to_thread(_service_catalog)
    sources = {"mcp": tuple(services.descriptors.values())}
    if not any(source.type == "expert" for source in config.capability.sources):
        experts = await _installed_expert_candidates()
        sources["expert"] = tuple(candidate.descriptor for candidate in experts)
    for kind, descriptors in sources.items():
        root = _fingerprint_root(config, kind)
        if descriptors or FingerprintArtifactStore(root).exists():
            service = FingerprintService(
                _CatalogInventory(kind, descriptors), root, llm=model, config=config.capability.fingerprint,
                model_response_observer=getattr(model, "symphony_response_observer", None),
                allow_metadata_fallback=config.retrieval.index_build.task.allow_fallback_tree,
            )
            await service.build(force=force)


def service_candidates(
    mcp_tools: tuple[tuple[Any, Any], ...],
    environment: SkillFS,
) -> tuple[CapabilityCandidate, ...]:
    """Group session-visible tools and bundled Skills under their owning MCP service."""
    catalog = _service_catalog()
    actions: dict[str, list[CapabilityAction]] = {}
    for server, card in mcp_tools:
        identifier = catalog.resolve_server(server.server_name)
        if identifier is None:
            continue
        actions.setdefault(identifier, []).append(
            CapabilityAction("tool", card.name, card.description, server.server_id)
        )
    if catalog.bundled_skill_roots:
        for skill in environment.directory.record_by_id.values():
            identifier = catalog.bundled_skill_roots.get(skill.source_root)
            if identifier is not None:
                actions.setdefault(identifier, []).append(
                    CapabilityAction("skill", skill.worker_id, skill.description)
                )
    return tuple(
        CapabilityCandidate(
            descriptor=catalog.descriptors.get(identifier) or _describe("mcp", identifier, identifier, ""),
            actions=tuple(dict.fromkeys(references)),
        )
        for identifier, references in actions.items()
    )


async def mcp_candidates(
    config: SymphonyConfig, environment: SkillFS, manager: AbilityManager | None,
) -> tuple[CapabilityCandidate, ...]:
    """Supply services and actions visible to the current session."""
    if manager is None:
        return ()
    can_expand = manager.get("tool_search") is not None
    tools = tuple((server, card) for server, card in manager.registered_mcp_tool_cards()
                  if can_expand or card.exposure != ToolExposure.DEFERRED)

    def load():
        return _enrich(config, "mcp", service_candidates(tools, environment))

    return await asyncio.to_thread(load)


def _enrich(
    config: SymphonyConfig,
    kind: str,
    candidates: tuple[CapabilityCandidate, ...],
) -> tuple[CapabilityCandidate, ...]:
    fingerprints = read_fingerprints(FingerprintArtifactStore(_fingerprint_root(config, kind))) if candidates else {}
    return tuple(
        replace(candidate, fingerprint=fingerprints.get((kind, candidate.descriptor.capability_id)),
                aliases=tuple(candidate.descriptor.metadata.get("aliases", ())))
        for candidate in candidates
    )


async def expert_candidates(config: SymphonyConfig) -> tuple[CapabilityCandidate, ...]:
    """Supply the live installed expert catalog independently of session tools."""
    paths_only = any(source.type == "expert" for source in config.capability.sources)
    candidates = await _installed_expert_candidates(paths_only=paths_only)
    if paths_only:
        return candidates
    return await asyncio.to_thread(_enrich, config, "expert", candidates)
