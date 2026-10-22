"""Assemble one colleague's saved Skill, connector, plugin and knowledge selection into its published Spec.

Installation, selection and credential storage stay with the existing Swarm catalogs.
Publishing copies only the selected skill directories and selected personal-context pages
into that colleague's office and freezes connector configuration, including resolved secrets,
into the Spec. It does not change process environment variables, the shared skill library,
the personal-context store, or another colleague's copy.
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
_CONNECTOR_SKILL_DIR = "connector-skills"
_KNOWLEDGE_DIR = "knowledge"
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _package_file_spec(spec: Any, package: Path) -> dict[str, Any] | None:
    dumped = spec.model_dump(mode="json")
    params = dumped.get("params")
    raw = params.get("file_path") if isinstance(params, dict) else None
    path = Path(raw) if isinstance(raw, str) and raw else None
    if path is None or path.is_symlink() or not path.is_file() or not _inside(path, package):
        return None
    return dumped


_KNOWLEDGE_NAME = re.compile(r"^[^\\\x00-\x1f]{1,200}$")
_PLACEHOLDER = re.compile(r"\$\{(\w+)\}")


@dataclass(frozen=True, kw_only=True)
class SkillSource:
    """One installed skill directory. ``name`` is the directory placed in the colleague copy."""

    name: str
    directory: Path


@dataclass(frozen=True, kw_only=True)
class KnowledgeSource:
    """One published personal-context page. ``name`` is its path relative to the context root."""

    name: str
    path: Path


@dataclass(frozen=True, kw_only=True)
class PluginSource:
    """One installed plugin. Skills are copied; tools and rails stay files inside the package."""

    name: str
    skills: tuple[SkillSource, ...] = ()
    tools: tuple[dict[str, Any], ...] = ()
    rails: tuple[dict[str, Any], ...] = ()


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

    def plugin(self, name: str) -> PluginSource | None:
        """Return the installed plugin, or None when that name is not installed."""

    def plugin_skills(self, name: str) -> tuple[SkillSource, ...] | None:
        """Return the plugin's skill directories, or None when the plugin is not installed."""

    def connector(self, name: str) -> ConnectorSource | None:
        """Return the installed connector, or None when that name is not installed."""

    def knowledge(self, name: str) -> KnowledgeSource | None:
        """Return one published personal-context page, or None when that page is not available."""


@dataclass(frozen=True, kw_only=True)
class AssembledResources:
    """Spec fragments and the extra system-prompt section for one publication."""

    spec_fields: dict[str, Any]
    prompt: str
    skill_root: Path | None = None
    knowledge_root: str | None = None


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
    plugins = _selected_plugins(capabilities, catalog)
    skills = _selected_skills(capabilities, catalog, plugins)
    connectors = _selected_connectors(capabilities, catalog)
    manuals = _connector_manuals(connectors)
    pages = _selected_knowledge(capabilities, catalog)
    configs = _connector_configs(agent_id, connectors)
    plugin_tools = [tool for plugin in plugins for tool in plugin.tools]
    plugin_rails = [rail for plugin in plugins for rail in plugin.rails]
    if not skills and not configs and not pages and not manuals and not plugin_tools and not plugin_rails:
        return AssembledResources(spec_fields={}, prompt="")

    fields: dict[str, Any] = {}
    prompt = ""
    skill_root = None
    knowledge_root_relative = None
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
            },
            *plugin_rails,
        ]
        prompt += (
            "\n\n## 已装配技能\n\n"
            f"所选技能和插件的副本在办公目录的 `{relative}/` 下，用相对路径读取。"
            "这些副本只属于这个同事，修改它们不会改动技能库原件。"
            "需要运行脚本或 CLI 时遵守命令使用约定，不使用 code 工具。"
        )
    elif plugin_rails:
        fields["rails"] = list(plugin_rails)
    if plugin_tools:
        fields["tools"] = list(plugin_tools)
    if plugin_tools or plugin_rails:
        prompt += (
            "\n\n## 已装配插件\n\n"
            "所选插件的工具和护栏已经写入这个同事的当前发布版本，文件仍在已安装的插件包内。"
        )
    if configs:
        fields["mcps"] = configs
        prompt += (
            "\n\n## 已装配连接器\n\n"
            "所选连接器的配置和凭据只属于这个同事的当前发布版本，不写入进程环境变量，也不替换其他同事的连接。"
        )
    if manuals:
        manual_root = _materialize_skills(office, manuals, directory_name=_CONNECTOR_SKILL_DIR)
        relative = manual_root.relative_to(office).as_posix()
        prompt += (
            "\n\n## 连接器附带说明\n\n"
            f"所选连接器附带的操作说明在办公目录的 `{relative}/` 下。"
            "这些说明只在执行对应连接器的任务时按相对路径读取。"
            "用户询问这个员工有哪些技能时，不要列举或清点这些说明，也不要把个人技能库里没有分配给这个员工的技能说成这个员工的技能。"
            "只回答已装配技能；没有已装配技能时，直接说明当前没有分配技能。"
        )
    if pages:
        knowledge_root = _materialize_knowledge(office, pages)
        relative = knowledge_root.relative_to(office).as_posix()
        knowledge_root_relative = relative
        names = "、".join(f"`{item.name}`" for item in pages)
        prompt += (
            "\n\n## 已装配知识\n\n"
            f"所选个人上下文页面的副本在办公目录的 `{relative}/` 下，页面为 {names}。"
            "用相对路径读取这些副本。未选择的个人上下文页面不在这个目录中，不要到个人上下文目录里继续读取。"
            "飞书私聊仅可读取本目录中的已选知识副本，不能读取办公目录其他内容；飞书群聊没有文件读取权限。"
            "这些副本只属于这个同事，修改它们不会改动个人上下文原件。"
        )
    return AssembledResources(
        spec_fields=fields,
        prompt=prompt,
        skill_root=skill_root,
        knowledge_root=knowledge_root_relative,
    )


_FEISHU_PROMPT = re.compile(r"\n<!-- jm-feishu-cli -->.*?<!-- /jm-feishu-cli -->", re.DOTALL)


def apply_feishu_skills(fields: dict[str, Any], office: Path, directories: tuple[Path, ...]) -> dict[str, Any]:
    """Replace only auto-mounted CLI assets, keeping user-selected resources intact."""
    import copy

    if not directories and _FEISHU_PROMPT.search(fields.get("system_prompt", "")) is None:
        return fields
    result = copy.deepcopy(fields)
    prompt = _FEISHU_PROMPT.sub("", result.get("system_prompt", ""))
    rails = result.get("rails") or []
    result["rails"] = rails
    auto_parent = office / "resources" / "feishu-skills"
    old_names = set()
    skill_rail = next((rail for rail in rails if rail.get("type") == _SKILL_RAIL), None)
    roots = []
    if skill_rail is not None:
        raw = skill_rail["params"]["skills_dir"]
        for value in ([raw] if isinstance(raw, str) else raw):
            path = Path(value)
            if path.parent == auto_parent:
                old_names.update(item.parent.name for item in path.rglob("SKILL.md"))
            else:
                roots.append(value)
    user_names = {item.parent.name for root in roots for item in Path(root).rglob("SKILL.md")}
    names = [name for name in (result.get("skills") or []) if name not in old_names or name in user_names]
    chosen: dict[str, SkillSource] = {}
    for directory in directories:
        for file in sorted(directory.rglob("SKILL.md")):
            _accept(chosen, _checked(SkillSource(name=file.parent.name, directory=file.parent)))
    if chosen:
        root = _materialize_skills(office, tuple(chosen.values()), directory_name="feishu-skills")
        roots.append(str(root))
        names = list(dict.fromkeys([*names, *chosen]))
        relative = root.relative_to(office).as_posix()
        prompt += (
            "\n<!-- jm-feishu-cli -->\n## 飞书 CLI\n"
            f"当前同事已绑定飞书应用，可使用 lark-cli。配套技能位于 `{relative}/`。"
            "先读取 `lark-shared` 和相关业务 Skill 的 SKILL.md。飞书应用级操作默认使用 bot；"
            "读取用户个人相关信息时，按 Skill 指引在当前同事独立的 CLI 配置中完成一次 `auth login`，并显式使用 `--as user`。"
            "需要用户完成授权时，使用 Skill 指定的非阻塞登录方式，仅把验证链接发给用户并结束本轮；用户回来后再检查登录状态。"
            "该用户登录态由此 Agent 的私有 CLI 配置保存，供该 Agent 的私聊和群聊共同使用；群内结果直接回复当前群，群成员均可见。"
            "不要读取、输出或复制 Token、Secret 或认证文件；创建回执与文档访问权限分别核实。\n<!-- /jm-feishu-cli -->"
        )
    if roots:
        if skill_rail is None:
            skill_rail = {"type": _SKILL_RAIL, "params": {"include_tools": False, "skill_mode": "all"}}
            rails.insert(0, skill_rail)
        skill_rail["params"]["skills_dir"] = roots[0] if len(roots) == 1 else roots
    elif skill_rail is not None:
        rails.remove(skill_rail)
    result["skills"], result["system_prompt"] = names, prompt
    return result


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


def _selected_plugins(capabilities: CapabilitySelection, catalog: ResourceCatalog) -> tuple[PluginSource, ...]:
    found = []
    for name in capabilities.plugins:
        source = catalog.plugin(name)
        if source is None:
            raise PersistentAgentError(f"plugin is not installed: {name}", code="RESOURCE_NOT_INSTALLED")
        if not source.skills and not source.tools and not source.rails:
            raise PersistentAgentError(f"plugin has no capability: {name}", code="RESOURCE_NOT_INSTALLED")
        found.append(source)
    return tuple(found)


def _selected_skills(
    capabilities: CapabilitySelection,
    catalog: ResourceCatalog,
    plugins: tuple[PluginSource, ...],
) -> tuple[SkillSource, ...]:
    chosen: dict[str, SkillSource] = {}
    for name in capabilities.skills:
        source = catalog.skill(name)
        if source is None:
            raise PersistentAgentError(f"skill is not installed: {name}", code="RESOURCE_NOT_INSTALLED")
        _accept(chosen, _checked(source))
    for plugin in plugins:
        for source in plugin.skills:
            _accept(chosen, _checked(source))
    return tuple(chosen[name] for name in sorted(chosen))


def _connector_manuals(connectors: tuple[ConnectorSource, ...]) -> tuple[SkillSource, ...]:
    """Skill documents shipped with a connector. They are not the employee's selected skills."""
    chosen: dict[str, SkillSource] = {}
    for source in connectors:
        for skill in source.skill_directories:
            _accept(chosen, _checked(skill))
    return tuple(chosen[name] for name in sorted(chosen))


def _selected_knowledge(capabilities: CapabilitySelection, catalog: ResourceCatalog) -> tuple[KnowledgeSource, ...]:
    found = []
    for name in capabilities.knowledge:
        source = catalog.knowledge(name)
        if source is None:
            raise PersistentAgentError(f"knowledge page is not published: {name}", code="RESOURCE_NOT_INSTALLED")
        found.append(_checked_knowledge(source))
    return tuple(found)


def _checked_knowledge(source: KnowledgeSource) -> KnowledgeSource:
    relative = _knowledge_relative(source.name)
    path = Path(source.path)
    if relative is None or path.is_symlink() or not path.is_file():
        raise PersistentAgentError(f"invalid knowledge page: {source.name}", code="INVALID_RESOURCE")
    return KnowledgeSource(name=relative.as_posix(), path=path.resolve())


def _knowledge_relative(name: str):
    if not isinstance(name, str) or _KNOWLEDGE_NAME.fullmatch(name) is None:
        return None
    relative = Path(name)
    if relative.is_absolute() or relative.suffix != ".md" or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    return relative


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


def _materialize_skills(
    office: Path,
    skills: tuple[SkillSource, ...],
    *,
    directory_name: str = _SKILL_DIR,
) -> Path:
    parent = office / _RESOURCE_DIR / directory_name
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


def _materialize_knowledge(office: Path, pages: tuple[KnowledgeSource, ...]) -> Path:
    parent = office / _RESOURCE_DIR / _KNOWLEDGE_DIR
    private_dir(parent)
    staging = parent / f".staging-{uuid.uuid4().hex}"
    try:
        private_dir(staging)
        for source in pages:
            relative = Path(source.name)
            destination = staging / relative
            private_dir(destination.parent)
            shutil.copyfile(source.path, destination)
            os.chmod(destination, 0o600)
        digest = _digest(staging)
        published = parent / digest
        if published.exists():
            shutil.rmtree(staging)
        else:
            staging.rename(published)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return published


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
        knowledge_root: Path | None = None,
    ) -> None:
        self._skills_dir = skills_dir
        self._builtin_dir = builtin_dir
        self._state_file = state_file
        self._knowledge_root = knowledge_root

    def knowledge(self, name: str) -> KnowledgeSource | None:
        relative = _knowledge_relative(name)
        root = self._context_root()
        if relative is None or not root.is_dir():
            return None
        candidate = root / relative
        if candidate.is_symlink() or not candidate.is_file():
            return None
        try:
            candidate.resolve().relative_to(root.resolve())
        except ValueError:
            return None
        return KnowledgeSource(name=relative.as_posix(), path=candidate)

    def list_knowledge(self) -> tuple[KnowledgeSource, ...]:
        """Published markdown pages under the personal-context root, in path order."""
        root = self._context_root()
        if not root.is_dir():
            return ()
        found = []
        for path in sorted(root.rglob("*.md")):
            relative = path.relative_to(root).as_posix()
            source = self.knowledge(relative)
            if source is not None:
                found.append(source)
        return tuple(found)

    def _context_root(self) -> Path:
        if self._knowledge_root is not None:
            return self._knowledge_root
        return Path.home() / ".jiuwenswarm" / ".personal_context" / "workspace" / "context"

    def skill(self, name: str) -> SkillSource | None:
        if _NAME.fullmatch(name) is None:
            return None
        for root in self._skill_roots():
            candidate = root / name
            if (candidate / "SKILL.md").is_file() and not candidate.is_symlink():
                return SkillSource(name=name, directory=candidate)
        return None

    def plugin(self, name: str) -> PluginSource | None:
        if _NAME.fullmatch(name) is None:
            return None
        recorded = self._recorded_plugin_skills(name)
        if recorded is not None:
            return PluginSource(name=name, skills=recorded)
        return self._package_plugin(name)

    def plugin_skills(self, name: str) -> tuple[SkillSource, ...] | None:
        source = self.plugin(name)
        return None if source is None else source.skills

    def _recorded_plugin_skills(self, name: str) -> tuple[SkillSource, ...] | None:
        match = next((item for item in self._plugin_records() if item.get("name") == name), None)
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

    def _package_plugin(self, name: str) -> PluginSource | None:
        package = self._plugin_package_dir(name)
        if package is None:
            return None
        try:
            from openjiuwen.harness.resources.extension_loader import load_plugin_package

            loaded = load_plugin_package(package / "manifest.json")
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        skills = []
        for item in loaded.skills:
            directory = Path(item.dir)
            skill_id = directory.name
            if (
                _NAME.fullmatch(skill_id) is None
                or directory.is_symlink()
                or not (directory / "SKILL.md").is_file()
                or not _inside(directory, package)
            ):
                return None
            skills.append(SkillSource(name=skill_id, directory=directory.resolve()))
        tools = []
        rails = []
        for spec in loaded.tools:
            dumped = _package_file_spec(spec, package)
            if dumped is None:
                return None
            tools.append(dumped)
        for spec in loaded.rails:
            dumped = _package_file_spec(spec, package)
            if dumped is None:
                return None
            rails.append(dumped)
        return PluginSource(name=name, skills=tuple(skills), tools=tuple(tools), rails=tuple(rails))

    def _plugin_package_dir(self, name: str) -> Path | None:
        root = self._plugin_packages_root()
        found = self._named_package_dir(root, name)
        if found is not None:
            return found
        package_id = self._installed_hub_package_id(root, name)
        if package_id is None:
            return None
        return self._named_package_dir(root, package_id)

    def _plugin_packages_root(self) -> Path:
        if self._skills_dir is not None:
            workspace = self._skills_dir.parent
        else:
            from jiuwenswarm.common.utils import get_agent_workspace_dir

            workspace = get_agent_workspace_dir()
        return workspace / "plugins" / "plugin_packages"

    @staticmethod
    def _named_package_dir(root: Path, name: str) -> Path | None:
        for kind in ("local", "built_in"):
            candidate = root / kind / name
            if (candidate / "manifest.json").is_file() and not candidate.is_symlink():
                return candidate
        return None

    @staticmethod
    def _installed_hub_package_id(root: Path, asset_id: str) -> str | None:
        from jiuwenswarm.server.runtime.marketplace.hub_install_state import HubInstallStateStore

        record = HubInstallStateStore(root).get(asset_id)
        if record is None or record.kind != "plugin":
            return None
        package_id = record.package_id.strip()
        if package_id == asset_id or _NAME.fullmatch(package_id) is None:
            return None
        return package_id

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
