# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Install bundled or programmatic fixed experts without overwriting user data."""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_BUNDLED_ROOT = _PACKAGE_ROOT / "bundled"


class HarnessExpertConflict(Exception):
    """A fixed expert id already exists with a different Harness provider."""


@dataclass(frozen=True)
class _HarnessExpert:
    """One fixed expert created by this package."""

    template_id: str
    name: str
    provider_name: str
    description: str
    persona: str


HARNESS_EXPERTS: tuple[_HarnessExpert, ...] = (
    _HarnessExpert(
        template_id="harness-claude-expert",
        name="Claude",
        provider_name="claudecode",
        description="Harness Workbench 固定专家，使用 Claude Code。",
        persona="# Claude Code\n\n使用 Claude Code Harness。\n",
    ),
    _HarnessExpert(
        template_id="harness-codex-expert",
        name="Codex",
        provider_name="codex",
        description="Harness Workbench 固定专家，使用 Codex。",
        persona="# Codex\n\n使用 Codex Harness。\n",
    ),
)


class _AgentTemplateOps(Protocol):
    """Subset of the AgentServer expert catalog used by this package."""

    async def list_agent_templates(self, params: dict | None = None) -> list[dict]:
        """Return expert cards, including ones that are not installed."""

    def create_agent_template(self, params: dict) -> None:
        """Create one local expert package."""

    def install_agent_template(self, params: dict) -> None:
        """Install one existing expert package."""

    def import_agent_template(self, params: dict) -> dict:
        """Import one expert package from a directory or archive path."""


async def ensure_harness_experts(
    manager: _AgentTemplateOps | None = None,
    *,
    bundled_root: Path | None = _DEFAULT_BUNDLED_ROOT,
) -> None:
    """Create and install the two fixed experts without overwriting user data.

    Missing experts are imported from bundled content when present, otherwise
    created programmatically, then installed. An existing expert with the
    expected provider is installed when needed, or left as-is when already
    installed. An id owned by a different Harness provider raises
    ``HarnessExpertConflict`` before any write.
    """
    catalog = _default_catalog() if manager is None else manager
    grouped = _group_templates(await catalog.list_agent_templates())
    for expert in HARNESS_EXPERTS:
        _reject_provider_conflict(expert, grouped.get(expert.template_id, ()))
    for expert in HARNESS_EXPERTS:
        _ensure_one(catalog, expert, grouped.get(expert.template_id, ()), bundled_root)
        _sync_bundled_avatar(expert, bundled_root)


def _default_catalog() -> _AgentTemplateOps:
    from jiuwenswarm.server.runtime import extension_package_manager

    return extension_package_manager


def _group_templates(cards: object) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    if not isinstance(cards, list):
        return grouped
    for card in cards:
        if not isinstance(card, dict):
            continue
        template_id = card.get("id")
        if not isinstance(template_id, str) or not template_id.strip():
            continue
        grouped.setdefault(template_id, []).append(card)
    return grouped


def _provider_name(card: dict) -> str:
    runtime = card.get("runtime")
    if not isinstance(runtime, dict):
        return ""
    provider_name = runtime.get("provider_name")
    if not isinstance(provider_name, str):
        return ""
    return provider_name.strip()


def _reject_provider_conflict(expert: _HarnessExpert, cards: tuple[dict, ...] | list[dict]) -> None:
    if not cards:
        return
    providers = {_provider_name(card) for card in cards}
    if providers == {expert.provider_name}:
        return
    actual = ", ".join(sorted(provider or "missing" for provider in providers))
    message = (
        f"{expert.template_id} already exists with Harness provider {actual}, "
        f"expected {expert.provider_name}"
    )
    logger.error("[harness-workbench] %s", message)
    raise HarnessExpertConflict(message)


def _bundled_source(bundled_root: Path | None, template_id: str) -> Path | None:
    """Return a bundled directory or zip path for one expert, if present."""
    if bundled_root is None:
        return None
    for candidate in (bundled_root / template_id, bundled_root / f"{template_id}.zip"):
        if candidate.exists():
            return candidate
    return None


def _try_import_bundled(
    catalog: _AgentTemplateOps,
    expert: _HarnessExpert,
    bundled_root: Path | None,
) -> bool:
    """Import one bundled expert package when the catalog supports it."""
    source = _bundled_source(bundled_root, expert.template_id)
    if source is None:
        return False
    importer = getattr(catalog, "import_agent_template", None)
    if importer is None:
        return False
    try:
        importer({"path": str(source)})
        catalog.install_agent_template({"id": expert.template_id})
    except Exception:
        logger.warning(
            "[harness-workbench] bundled import failed for %s",
            expert.template_id,
            exc_info=True,
        )
        return False
    logger.info("[harness-workbench] imported bundled and installed %s", expert.template_id)
    return True


def _ensure_one(
    catalog: _AgentTemplateOps,
    expert: _HarnessExpert,
    cards: tuple[dict, ...] | list[dict],
    bundled_root: Path | None,
) -> None:
    if not cards:
        if _try_import_bundled(catalog, expert, bundled_root):
            return
        catalog.create_agent_template(
            {
                "id": expert.template_id,
                "name": expert.name,
                "description": expert.description,
                "persona": expert.persona,
                "skills": [],
                "tags": [],
                "runtime": {"provider_name": expert.provider_name, "config": {}},
            }
        )
        catalog.install_agent_template({"id": expert.template_id})
        logger.info("[harness-workbench] created and installed %s", expert.template_id)
        return
    if any(card.get("installed") is True for card in cards):
        logger.info("[harness-workbench] reusing installed %s", expert.template_id)
        return
    catalog.install_agent_template({"id": expert.template_id})
    logger.info("[harness-workbench] installed existing %s", expert.template_id)


def _local_definition_dir(template_id: str) -> Path | None:
    """Return the on-disk expert package directory when one is already present."""
    from jiuwenswarm.server.runtime import extension_package_manager as catalog

    resolved = catalog._resolve_agent_template_definition_dir(template_id)
    if resolved is None:
        return None
    package_dir, _source = resolved
    return package_dir if package_dir.is_dir() else None


def _needs_avatar_sync(package_dir: Path) -> bool:
    """Return whether the local expert is missing a usable avatar asset."""
    manifest_path = package_dir / "manifest.json"
    if not manifest_path.is_file():
        return True
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    raw = manifest.get("avatar")
    if not isinstance(raw, str) or not raw.strip():
        return True
    rel = raw.strip().replace("\\", "/")
    if rel.startswith("/") or ".." in Path(rel).parts:
        return True
    return not (package_dir / rel).is_file()


def _sync_bundled_avatar(expert: _HarnessExpert, bundled_root: Path | None) -> None:
    """Copy avatar assets from bundled content onto legacy programmatic experts."""
    bundled = _bundled_source(bundled_root, expert.template_id)
    if bundled is None or not bundled.is_dir():
        return
    package_dir = _local_definition_dir(expert.template_id)
    if package_dir is None or not _needs_avatar_sync(package_dir):
        return
    manifest_path = bundled / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        bundled_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("[harness-workbench] bundled manifest unreadable for %s", expert.template_id)
        return
    avatar_rel = bundled_manifest.get("avatar")
    if not isinstance(avatar_rel, str) or not avatar_rel.strip():
        return
    normalized = avatar_rel.strip().replace("\\", "/")
    source_file = bundled / normalized
    if source_file.is_file():
        target = package_dir / normalized
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target)
    source_dir = bundled / "avatars"
    if source_dir.is_dir():
        target_dir = package_dir / "avatars"
        shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)
    local_manifest_path = package_dir / "manifest.json"
    try:
        local_manifest = json.loads(local_manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        local_manifest = {}
    if not isinstance(local_manifest, dict):
        local_manifest = {}
    local_manifest["avatar"] = normalized
    try:
        local_manifest_path.write_text(
            json.dumps(local_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        logger.warning("[harness-workbench] failed to write avatar manifest for %s", expert.template_id, exc_info=True)
        return
    logger.info("[harness-workbench] synced bundled avatar for %s", expert.template_id)
