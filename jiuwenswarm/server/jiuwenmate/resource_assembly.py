"""Assemble one colleague's saved Skill, connector and plugin selection into its published Spec.

Installation, selection and credential storage stay with the existing Swarm catalogs.
Publishing copies only the selected skill directories into that colleague's office and
freezes connector configuration, including resolved secrets, into the Spec. It does not
change process environment variables, the shared skill library, or another colleague's copy.
The skill rail is mounted without bash or code tools; those still require confirmation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from jiuwenswarm.server.jiuwenmate.models import CapabilitySelection, PersistentAgentError
from jiuwenswarm.server.jiuwenmate.sqlite_store import private_dir

_SKILL_RAIL = "core.skill_use"
_RESOURCE_DIR = "resources"
_SKILL_DIR = "skills"
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_PLACEHOLDER = re.compile(r"\$\{(\w+)\}")


@dataclass(frozen=True, kw_only=True)
class SkillSource:
    """One installed skill directory. ``name`` is the directory placed in the colleague copy."""

    name: str
    directory: Path


@dataclass(frozen=True, kw_only=True)
class ConnectorSource:
    """One installed connector. ``entry`` is an MCP server record; skill-only connectors omit it."""

    name: str
    entry: dict[str, Any] | None = None
    skill_directories: tuple[SkillSource, ...] = ()
    secrets: Mapping[str, str] = field(default_factory=dict)


class ResourceCatalog(Protocol):
    """Read-only view of skills, plugins and connectors already installed by Swarm."""

    def skill(self, name: str) -> SkillSource | None:
        """Return the installed skill, or None when that name is not installed."""

    def plugin_skills(self, name: str) -> tuple[SkillSource, ...] | None:
        """Return the plugin's skill directories, or None when the plugin is not installed."""

    def connector(self, name: str) -> ConnectorSource | None:
        """Return the installed connector, or None when that name is not installed."""


@dataclass(frozen=True, kw_only=True)
class AssembledResources:
    """Spec fragments and the extra system-prompt section for one publication."""

    spec_fields: dict[str, Any]
    prompt: str
    skill_root: Path | None = None


def assemble_resources(
    agent_id: str,
    capabilities: CapabilitySelection,
    office: Path,
    catalog: ResourceCatalog,
) -> AssembledResources:
    """Resolve the saved selection and freeze it for this colleague.

    An empty selection adds nothing, so a colleague that has not chosen resources keeps
    the previous Spec shape. A name that is not installed refuses publication.
    """
    skills = _selected_skills(capabilities, catalog)
    connectors = _selected_connectors(capabilities, catalog)
    configs = _connector_configs(agent_id, connectors)
    if not skills and not configs:
        return AssembledResources(spec_fields={}, prompt="")

    fields: dict[str, Any] = {}
    prompt = ""
    skill_root = None
    if skills:
        skill_root = _materialize_skills(office, skills)
        relative = skill_root.relative_to(office).as_posix()
        fields["skills"] = [item.name for item in skills]
        fields["rails"] = [
            {
                "type": _SKILL_RAIL,
                "params": {
                    "skills_dir": str(skill_root),
                    "include_tools": False,
                    "skill_mode": "all",
                },
            }
        ]
        prompt += (
            "\n\n## 已装配技能\n\n"
            f"所选技能和插件的副本在办公目录的 `{relative}/` 下，用相对路径读取。"
            "这些副本只属于这个同事，修改它们不会改动技能库原件。"
            "不使用 bash、code 或其他需要人工确认的命令。"
        )
    if configs:
        fields["mcps"] = configs
        prompt += (
            "\n\n## 已装配连接器\n\n"
            "所选连接器的配置和凭据只属于这个同事的当前发布版本，不写入进程环境变量，也不替换其他同事的连接。"
        )
    return AssembledResources(spec_fields=fields, prompt=prompt, skill_root=skill_root)


def _connector_configs(agent_id: str, connectors: tuple[ConnectorSource, ...]) -> list[dict[str, Any]]:
    from jiuwenswarm.common.mcp_config import build_mcp_server_config

    configs = []
    for source in connectors:
        if source.entry is None:
            if not source.skill_directories:
                raise PersistentAgentError(
                    f"connector cannot be assembled: {source.name}", code="RESOURCE_NOT_INSTALLED"
                )
            continue
        config = build_mcp_server_config(
            source.entry,
            server_id_scope=f"jiuwenmate:{agent_id}",
            credential_resolver=_secret_resolver(source.secrets),
        )
        if config is None:
            raise PersistentAgentError(
                f"connector cannot be assembled: {source.name}", code="RESOURCE_NOT_INSTALLED"
            )
        dumped = config.model_dump(mode="json")
        unresolved = sorted(set(_PLACEHOLDER.findall(json.dumps(dumped, ensure_ascii=False))))
        if unresolved:
            raise PersistentAgentError(
                f"connector is missing credentials: {source.name} ({', '.join(unresolved)})",
                code="CREDENTIAL_MISSING",
            )
        configs.append(dumped)
    return configs


def _selected_skills(capabilities: CapabilitySelection, catalog: ResourceCatalog) -> tuple[SkillSource, ...]:
    chosen: dict[str, SkillSource] = {}
    for name in capabilities.skills:
        source = catalog.skill(name)
        if source is None:
            raise PersistentAgentError(f"skill is not installed: {name}", code="RESOURCE_NOT_INSTALLED")
        _accept(chosen, _checked(source))
    for name in capabilities.plugins:
        sources = catalog.plugin_skills(name)
        if sources is None:
            raise PersistentAgentError(f"plugin is not installed: {name}", code="RESOURCE_NOT_INSTALLED")
        if not sources:
            raise PersistentAgentError(f"plugin has no skill directory: {name}", code="RESOURCE_NOT_INSTALLED")
        for source in sources:
            _accept(chosen, _checked(source))
    for name in capabilities.connectors:
        source = catalog.connector(name)
        if source is None:
            raise PersistentAgentError(f"connector is not installed: {name}", code="RESOURCE_NOT_INSTALLED")
        for skill in source.skill_directories:
            _accept(chosen, _checked(skill))
    return tuple(chosen[name] for name in sorted(chosen))


def _selected_connectors(capabilities: CapabilitySelection, catalog: ResourceCatalog) -> tuple[ConnectorSource, ...]:
    found = []
    for name in capabilities.connectors:
        source = catalog.connector(name)
        if source is None:
            raise PersistentAgentError(f"connector is not installed: {name}", code="RESOURCE_NOT_INSTALLED")
        found.append(source)
    return tuple(found)


def _checked(source: SkillSource) -> SkillSource:
    if not isinstance(source.name, str) or _NAME.fullmatch(source.name) is None or source.name in {".", ".."}:
        raise PersistentAgentError(f"invalid skill name: {source.name}", code="INVALID_RESOURCE")
    directory = Path(source.directory)
    if not (directory / "SKILL.md").is_file():
        raise PersistentAgentError(f"skill directory is incomplete: {source.name}", code="RESOURCE_NOT_INSTALLED")
    return SkillSource(name=source.name, directory=directory.resolve())


def _accept(chosen: dict[str, SkillSource], source: SkillSource) -> None:
    current = chosen.get(source.name)
    if current is not None and current.directory != source.directory:
        raise PersistentAgentError(f"skill is selected from two directories: {source.name}", code="INVALID_RESOURCE")
    chosen[source.name] = source


def _secret_resolver(secrets: Mapping[str, str]):
    """Resolve ``${VAR}`` only from this connector's stored secrets, never from the process environment."""

    stored = {str(key): str(value) for key, value in secrets.items()}

    def resolve(key: str) -> str | None:
        return stored.get(key)

    return resolve


def _materialize_skills(office: Path, skills: tuple[SkillSource, ...]) -> Path:
    parent = office / _RESOURCE_DIR / _SKILL_DIR
    private_dir(parent)
    staging = parent / f".staging-{uuid.uuid4().hex}"
    try:
        private_dir(staging)
        for source in skills:
            _copy_skill(source.directory, staging / source.name)
        digest = _digest(staging)
        destination = parent / digest
        if destination.exists():
            shutil.rmtree(staging)
        else:
            staging.rename(destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


def _copy_skill(source: Path, destination: Path) -> None:
    """Copy regular files only. A symlink is not followed, so the copy cannot escape the skill directory."""
    private_dir(destination)
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(dirpath)
        relative = current.relative_to(source)
        kept = []
        for name in dirnames:
            if (current / name).is_symlink():
                continue
            kept.append(name)
        dirnames[:] = kept
        private_dir(destination / relative)
        for name in filenames:
            child = current / name
            if child.is_symlink() or not child.is_file():
                continue
            target = destination / relative / name
            shutil.copyfile(child, target)
            os.chmod(target, 0o600)


def _digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


class InstalledResourceCatalog:
    """Reads the skill library, installed-plugin record and MCP state without modifying them."""

    def __init__(
        self,
        *,
        skills_dir: Path | None = None,
        builtin_dir: Path | None = None,
        state_file: Path | None = None,
    ) -> None:
        self._skills_dir = skills_dir
        self._builtin_dir = builtin_dir
        self._state_file = state_file

    def skill(self, name: str) -> SkillSource | None:
        if _NAME.fullmatch(name) is None:
            return None
        for root in self._skill_roots():
            candidate = root / name
            if (candidate / "SKILL.md").is_file() and not candidate.is_symlink():
                return SkillSource(name=name, directory=candidate)
        return None

    def plugin_skills(self, name: str) -> tuple[SkillSource, ...] | None:
        plugins = self._plugin_records()
        match = next((item for item in plugins if item.get("name") == name), None)
        if match is None:
            return None
        raw_names = match.get("skills")
        names = [str(item) for item in raw_names] if isinstance(raw_names, list) and raw_names else [name]
        found = []
        for skill_name in names:
            source = self.skill(skill_name)
            if source is None:
                return None
            found.append(source)
        return tuple(found)

    def connector(self, name: str) -> ConnectorSource | None:
        from jiuwenswarm.server.runtime.mcp.credential import CredentialStore
        from jiuwenswarm.server.runtime.mcp.state_store import get_mcp_record, record_to_mcp_entry

        try:
            record = get_mcp_record(name)
        except Exception:  # noqa: BLE001 — a missing state file means the connector is not installed
            record = None
        if not isinstance(record, dict):
            return None
        entry = record_to_mcp_entry(name, record)
        skills = tuple(self._connector_skills(name))
        if entry is None and not skills:
            return None
        try:
            secrets = CredentialStore().get_all(name)
        except Exception:  # noqa: BLE001 — no stored secret leaves placeholders unresolved
            secrets = {}
        return ConnectorSource(name=name, entry=entry, skill_directories=skills, secrets=secrets)

    def _skill_roots(self) -> tuple[Path, ...]:
        if self._skills_dir is not None or self._builtin_dir is not None:
            return tuple(path for path in (self._skills_dir, self._builtin_dir) if path is not None)
        from jiuwenswarm.common.utils import get_agent_skills_dir, get_builtin_skills_dir

        return get_agent_skills_dir(), get_builtin_skills_dir()

    def _plugin_records(self) -> list[dict[str, Any]]:
        path = self._state_file
        if path is None:
            from jiuwenswarm.common.utils import get_agent_skills_dir

            path = get_agent_skills_dir() / "skills_state.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        records = payload.get("installed_plugins") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            return []
        return [item for item in records if isinstance(item, dict)]

    def _connector_skills(self, name: str) -> list[SkillSource]:
        from jiuwenswarm.server.runtime.mcp.state_store import connected_mcp_skill_dirs

        found: list[SkillSource] = []
        try:
            entries = connected_mcp_skill_dirs()
        except Exception:  # noqa: BLE001
            return found
        for entry in entries:
            if str(entry.get("name") or "") != name:
                continue
            root = Path(str(entry.get("dir") or ""))
            if (root / "SKILL.md").is_file():
                found.append(SkillSource(name=root.name, directory=root))
                continue
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir(), key=lambda item: item.name):
                if child.is_dir() and not child.is_symlink() and (child / "SKILL.md").is_file():
                    found.append(SkillSource(name=child.name, directory=child))
        return found
