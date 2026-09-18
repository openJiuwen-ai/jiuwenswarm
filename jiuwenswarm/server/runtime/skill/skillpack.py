# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SDD-0010 SkillPack parsing, validation, and availability projection."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

import yaml

SKILLPACK_KIND = "skillpack"
SKILLPACK_SKILL_TYPE = "skillpack"
MEMBER_BACKUP_DIRNAME = "_member_backup"

_FRONTMATTER_RE = re.compile(r"^(?:\s*\n)*---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)
_WORKFLOW_GRAPH_RE = re.compile(
    r"^## Workflow Graph\s*$\n(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)
_JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
_REQUIRED_SECTIONS = (
    "When to use",
    "Do not use",
    "Required inputs",
    "Side effects and confirmation",
    "Included Skills",
    "Execution Process",
    "Failure handling",
    "Final output",
)
_INVALID_NAME_CHARS = frozenset('<>:"|?*')
_RESERVED_NAME_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_MAX_NAME_LENGTH = 128


class SkillPackValidationError(ValueError):
    """The root document does not satisfy the SDD-0010 contract."""


class SkillPackOperationUnsupportedError(ValueError):
    """An operation is not supported for SkillPacks."""


@dataclass(frozen=True)
class SkillPackDefinition:
    """Validated root metadata and optional display-only workflow graph."""

    name: str
    description: str
    members: tuple[str, ...]
    workflow_graph: dict[str, Any] | None


@dataclass(frozen=True)
class SkillPackStatus:
    """Computed package intent and member-dependent availability."""

    requested_enabled: bool
    enabled: bool
    members: tuple[dict[str, Any], ...]
    blocked_members: tuple[dict[str, str], ...]


class SkillPackService:
    """Coordinate SkillPack status, response projection, and operation checks.

    Directory resolution and enabled-state reads are supplied by the owner;
    this service does not cache mutable state or depend on RPC error types.
    """

    def __init__(
        self,
        skills_dir: Path,
        *,
        enabled_for: Callable[[str], bool],
        resolve_skill_dir: Callable[[str], Path | None],
    ) -> None:
        self._skills_dir = skills_dir
        self._enabled_for = enabled_for
        self._resolve_skill_dir = resolve_skill_dir

    def definition_status(
        self,
        skill_dir: Path,
        *,
        expected_name: str | None = None,
    ) -> tuple[SkillPackDefinition, SkillPackStatus]:
        """Load a package definition and evaluate its current member status."""
        definition = load_skillpack(skill_dir, expected_name=expected_name)
        status = compute_skillpack_status(
            definition,
            skills_dir=self._skills_dir,
            enabled_for=self._enabled_for,
        )
        return definition, status

    def apply_projection(
        self,
        payload: dict[str, Any],
        skill_dir: Path,
        *,
        include_members: bool,
        expected_name: str | None = None,
    ) -> None:
        """Add package availability and optional member details to a response."""
        definition, status = self.definition_status(
            skill_dir,
            expected_name=expected_name,
        )
        payload.update(
            project_skillpack(definition, status, include_members=include_members)
        )

    def ensure_operation_supported(self, skill_name: str, operation: str) -> None:
        """Reject operations that are currently unavailable for SkillPacks."""
        if is_skillpack(self._resolve_skill_dir(skill_name)):
            raise SkillPackOperationUnsupportedError(
                f"SkillPack 暂不支持 {operation}: {skill_name}"
            )

    def impacts(self, skillpack_names: list[str]) -> list[dict[str, Any]]:
        """Recompute blockers for packages affected by a member change."""
        impacts: list[dict[str, Any]] = []
        for skillpack_name in skillpack_names:
            skillpack_dir = self._resolve_skill_dir(skillpack_name)
            if skillpack_dir is None:
                continue
            try:
                _, status = self.definition_status(
                    skillpack_dir,
                    expected_name=skillpack_name,
                )
            except SkillPackValidationError:
                blocked_members = [{"name": skillpack_name, "reason": "invalid"}]
            else:
                blocked_members = [dict(item) for item in status.blocked_members]
            impacts.append({"name": skillpack_name, "blocked_members": blocked_members})
        return impacts


def read_skill_kind(skill_dir: Path | None) -> str:
    """Read a root ``kind`` without accepting non-root Markdown fallbacks."""

    if skill_dir is None or not skill_dir.is_dir():
        return ""
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return ""
    try:
        frontmatter = yaml.safe_load(match.group(1))
    except (yaml.YAMLError, RecursionError):
        frontmatter = None
    if isinstance(frontmatter, dict):
        return str(frontmatter.get("kind") or "").strip().casefold()
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "kind":
            return value.strip().strip("\"'").casefold()
    return ""


def is_skillpack(skill_dir: Path | None) -> bool:
    """Return whether the exact root document declares ``kind: skillpack``."""

    return read_skill_kind(skill_dir) == SKILLPACK_KIND


def load_skillpack(
    skill_dir: Path,
    *,
    expected_name: str | None = None,
) -> SkillPackDefinition:
    """Parse and validate one installed or staged SkillPack root."""

    frontmatter, body = _read_skill_document(skill_dir / "SKILL.md")
    kind = str(frontmatter.get("kind") or "").strip().casefold()
    if kind != SKILLPACK_KIND:
        raise SkillPackValidationError("SKILL.md kind 必须为 skillpack")

    raw_name = frontmatter.get("name")
    raw_description = frontmatter.get("description")
    if not isinstance(raw_name, str) or not isinstance(raw_description, str):
        raise SkillPackValidationError("SkillPack name 和 description 必须是字符串")
    name = raw_name.strip()
    description = raw_description.strip()
    _validate_skill_id(name, "name")
    if expected_name is not None and name != expected_name:
        raise SkillPackValidationError("SkillPack name 必须与目录名一致")
    if not description:
        raise SkillPackValidationError("SKILL.md frontmatter 缺少 description")

    raw_members = frontmatter.get("skills")
    if not isinstance(raw_members, list):
        raise SkillPackValidationError("SkillPack skills 必须是成员 ID 列表")
    members: list[str] = []
    for raw_member in raw_members:
        if not isinstance(raw_member, str):
            raise SkillPackValidationError("SkillPack 成员 ID 必须是字符串")
        member = raw_member.strip()
        _validate_skill_id(member, "member")
        members.append(member)
    if len(members) < 2:
        raise SkillPackValidationError("SkillPack 至少需要两个成员 Skill")
    if len(set(members)) != len(members):
        raise SkillPackValidationError("SkillPack 成员 ID 不得重复")
    if name in members:
        raise SkillPackValidationError("SkillPack 不得引用自身")

    _validate_required_sections(body)
    workflow_graph = _parse_workflow_graph(body, set(members))
    return SkillPackDefinition(
        name=name,
        description=description,
        members=tuple(members),
        workflow_graph=workflow_graph,
    )


def compute_skillpack_status(
    definition: SkillPackDefinition,
    *,
    skills_dir: Path,
    enabled_for: Callable[[str], bool],
) -> SkillPackStatus:
    """Resolve ordered member summaries and the aggregate enabled state."""

    summaries: list[dict[str, Any]] = []
    blocked: list[dict[str, str]] = []
    for member in definition.members:
        summary = _inspect_member(skills_dir, member, enabled_for)
        if summary.get("blocking_reason") == "missing":
            summary["restorable"] = (
                skills_dir / definition.name / MEMBER_BACKUP_DIRNAME / member
            ).is_dir()
        summaries.append(summary)
        reason = summary.get("blocking_reason")
        if isinstance(reason, str) and reason:
            blocked.append({"name": member, "reason": reason})

    requested_enabled = enabled_for(definition.name)
    return SkillPackStatus(
        requested_enabled=requested_enabled,
        enabled=requested_enabled and not blocked,
        members=tuple(summaries),
        blocked_members=tuple(blocked),
    )


def project_skillpack(
    definition: SkillPackDefinition,
    status: SkillPackStatus,
    *,
    include_members: bool,
) -> dict[str, Any]:
    """Build the additive ``skills.list`` or ``skills.get`` response fields."""

    projection: dict[str, Any] = {
        "skill_type": SKILLPACK_SKILL_TYPE,
        "member_count": len(definition.members),
        "requested_enabled": status.requested_enabled,
        "enabled": status.enabled,
        "config": {"enabled": status.requested_enabled},
        "blocked_members": [dict(item) for item in status.blocked_members],
    }
    if include_members:
        projection["skillpack"] = {
            "workflow_graph": definition.workflow_graph,
            "members": [dict(item) for item in status.members],
        }
    return projection


def unavailable_skillpacks(
    skills_dir: Path,
    *,
    enabled_for: Callable[[str], bool],
) -> list[str]:
    """Return installed SkillPack directory IDs that must not execute."""

    if not skills_dir.is_dir():
        return []
    unavailable: list[str] = []
    for child in skills_dir.iterdir():
        if child.name.startswith("_") or not child.is_dir() or not is_skillpack(child):
            continue
        try:
            definition = load_skillpack(child, expected_name=child.name)
            status = compute_skillpack_status(
                definition,
                skills_dir=skills_dir,
                enabled_for=enabled_for,
            )
        except SkillPackValidationError:
            unavailable.append(child.name)
            continue
        if not status.enabled:
            unavailable.append(child.name)
    return sorted(unavailable)


def referencing_skillpacks(skills_dir: Path, member_name: str) -> list[str]:
    """Return valid installed SkillPacks that declare ``member_name``."""

    if not skills_dir.is_dir() or not member_name:
        return []
    references: list[str] = []
    for child in skills_dir.iterdir():
        if child.name.startswith("_") or not child.is_dir() or not is_skillpack(child):
            continue
        try:
            definition = load_skillpack(child, expected_name=child.name)
        except SkillPackValidationError:
            continue
        if member_name in definition.members:
            references.append(child.name)
    return sorted(references)


# ---------------------------------------------------------------------------
# Container SkillPack（zip 解压形态）
#
# 市场技能包 zip 内部根是 ``skills/`` 目录，原样解压后形成
# ``<workspace>/skills/<容器名>/skills/<成员技能>/SKILL.md`` 的嵌套结构。
# 容器根下没有 SKILL.md，不满足 SDD-0010 标准包契约，但应当整体作为
# 一个技能包条目展示，成员即其 ``skills/`` 子目录内的技能。
# ---------------------------------------------------------------------------

CONTAINER_SKILLS_DIRNAME = "skills"


def _dir_has_root_skill_md(directory: Path) -> bool:
    """Return whether the directory carries a root SKILL.md document."""

    return (directory / "SKILL.md").is_file() or (directory / "skill.md").is_file()


def container_members_root(container_dir: Path) -> Path | None:
    """Return the directory whose direct children are container members.

    支持两种 zip 解压形态：
    - ``<容器>/skills/<成员>/SKILL.md``（解压工具先建了一层容器目录）
    - ``<容器>/<成员>/SKILL.md``（zip 的 ``skills/`` 根直接落在 skills 目录下）
    """

    inner = container_dir / CONTAINER_SKILLS_DIRNAME
    if inner.is_dir() and any(
        sub.is_dir() and _dir_has_root_skill_md(sub) for sub in inner.iterdir()
    ):
        return inner
    if any(
        sub.is_dir() and _dir_has_root_skill_md(sub) for sub in container_dir.iterdir()
    ):
        return container_dir
    return None


# 兼容别名：模块内部既有调用沿用私有名
_container_members_root = container_members_root


def is_container_skillpack(skill_dir: Path | None) -> bool:
    """Return whether the directory is an unzipped SkillPack container.

    识别条件：根下没有 SKILL.md，但直接子目录（或其 ``skills/``
    子目录的直接子目录）中存在携带 SKILL.md 的成员技能目录。
    """

    if skill_dir is None or not skill_dir.is_dir():
        return False
    if _dir_has_root_skill_md(skill_dir):
        return False
    return _container_members_root(skill_dir) is not None


def container_member_backup_dir(container_dir: Path) -> Path:
    """Return the backup root holding member copies captured at uninstall.

    根目录以 ``_`` 开头，会被 _container_members_root / skills.list 等
    目录扫描跳过，因此备份不会被当成成员或独立技能列出。
    """

    return container_dir / CONTAINER_SKILLS_DIRNAME / MEMBER_BACKUP_DIRNAME


def member_backup_dir(pack_dir: Path) -> Path:
    """Return the backup root for either SkillPack shape.

    标准包备份在包根 ``_member_backup/``，容器包备份在成员根
    ``skills/_member_backup/``；两者都以 ``_`` 开头从而被成员扫描跳过。
    """

    if is_container_skillpack(pack_dir):
        return container_member_backup_dir(pack_dir)
    return pack_dir / MEMBER_BACKUP_DIRNAME


def summarize_member_dir(member_dir: Path, *, enabled: bool) -> dict[str, Any]:
    """Build one member summary dict from a member skill directory."""

    try:
        frontmatter, _ = _read_skill_document(member_dir / "SKILL.md")
        raw_name = frontmatter.get("name")
        raw_description = frontmatter.get("description")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        description = (
            raw_description.strip() if isinstance(raw_description, str) else ""
        )
        kind = str(frontmatter.get("kind") or "").strip().casefold()
        if not name or not description or kind not in {"", "skill", "skillpack"}:
            raise SkillPackValidationError("member is not an ordinary Skill")
        _validate_skill_id(name, "member name")
    except SkillPackValidationError:
        return {
            "name": member_dir.name,
            "display_name": member_dir.name,
            "description": "",
            "enabled": False,
            "available": False,
            "blocking_reason": "invalid",
        }

    return {
        "name": name,
        "display_name": str(frontmatter.get("display_name") or name).strip() or name,
        "description": description,
        "enabled": enabled,
        "available": True,
        "blocking_reason": None if enabled else "disabled",
    }


def find_container_member_dir(container_dir: Path, member_name: str) -> Path | None:
    """Locate a member skill directory inside a container by name.

    成员名匹配目录名或 SKILL.md frontmatter 的 ``name``；供 skills.get /
    skills.toggle 对容器成员的寻址使用。找不到返回 None。
    """

    if not member_name:
        return None
    root = _container_members_root(container_dir)
    if root is None:
        return None
    for sub in sorted(root.iterdir(), key=lambda p: p.name):
        if not sub.is_dir() or not _dir_has_root_skill_md(sub):
            continue
        if sub.name == member_name:
            return sub
        try:
            frontmatter, _ = _read_skill_document(sub / "SKILL.md")
        except SkillPackValidationError:
            continue
        fm_name = frontmatter.get("name")
        if isinstance(fm_name, str) and fm_name.strip() == member_name:
            return sub
    return None


def _backup_member_summary(backup_dir: Path, *, name: str) -> dict[str, Any]:
    """Build a member summary from a backup copy captured at uninstall.

    卸载成员的目录已从成员根移除，改读包内备份的 SKILL.md 还原
    展示信息；成员不可用且可通过单个「安装」操作从备份恢复。
    """

    summary: dict[str, Any] = {
        "name": name,
        "display_name": name,
        "description": "",
        "enabled": False,
        "available": False,
        "blocking_reason": "uninstalled",
        "restorable": True,
    }
    try:
        frontmatter, _ = _read_skill_document(backup_dir / "SKILL.md")
    except SkillPackValidationError:
        return summary
    raw_name = frontmatter.get("name")
    raw_description = frontmatter.get("description")
    raw_display = frontmatter.get("display_name")
    if isinstance(raw_description, str) and raw_description.strip():
        summary["description"] = raw_description.strip()
    if isinstance(raw_name, str) and raw_name.strip():
        summary["name"] = raw_name.strip()
    if isinstance(raw_display, str) and raw_display.strip():
        summary["display_name"] = raw_display.strip()
    return summary


def _uninstalled_backup_members(
    backup_root: Path,
    *,
    known_names: set[str],
) -> list[dict[str, Any]]:
    """Summarize backup copies whose member dir is absent from the pack.

    仅收集成员根中已不存在的备份（仍安装的成员由常规扫描列出），
    按 name 排序保证输出稳定。
    """

    members: list[dict[str, Any]] = []
    if not backup_root.is_dir():
        return members
    for sub in sorted(backup_root.iterdir(), key=lambda p: p.name):
        if sub.name.startswith("_") or not sub.is_dir() or not _dir_has_root_skill_md(sub):
            continue
        if sub.name in known_names:
            continue
        members.append(_backup_member_summary(sub, name=sub.name))
    return members


def container_pack_members(
    container_dir: Path,
    *,
    enabled_for: Callable[[str], bool],
) -> list[dict[str, Any]]:
    """Collect member summaries from a container's member directories.

    卸载过的成员（其备份留在 ``skills/_member_backup/``）也会以
    ``available: False / blocking_reason: "uninstalled"`` 条目列出，
    供前端展示「安装」按钮从备份恢复。
    """

    root = _container_members_root(container_dir)
    members: list[dict[str, Any]] = []
    known: set[str] = set()
    if root is None:
        return members
    for sub in sorted(root.iterdir(), key=lambda p: p.name):
        if sub.name.startswith("_") or not sub.is_dir() or not _dir_has_root_skill_md(sub):
            continue
        summary = summarize_member_dir(sub, enabled=enabled_for(sub.name))
        members.append(summary)
        # 同时记录目录名与 frontmatter name，避免备份去重时漏判
        known.add(sub.name)
        known.add(str(summary.get("name") or ""))
    known.discard("")
    members.extend(
        _uninstalled_backup_members(
            container_member_backup_dir(container_dir),
            known_names=known,
        )
    )
    return members


def _iter_container_pack_roots(container_dir: Path):
    """Yield member dirs declaring ``kind: skillpack``, sorted by name."""

    root = _container_members_root(container_dir)
    if root is None:
        return
    for sub in sorted(root.iterdir(), key=lambda p: p.name):
        if not sub.is_dir() or not _dir_has_root_skill_md(sub):
            continue
        try:
            frontmatter, _ = _read_skill_document(sub / "SKILL.md")
        except SkillPackValidationError:
            continue
        if str(frontmatter.get("kind") or "").strip().casefold() == SKILLPACK_KIND:
            yield frontmatter, sub


def container_pack_description(container_dir: Path) -> str:
    """Return the container description, preferring an inner SkillPack root."""

    for frontmatter, _sub in _iter_container_pack_roots(container_dir):
        raw = frontmatter.get("description")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def read_container_pack_body(container_dir: Path) -> str:
    """Return the markdown body of the inner SkillPack root, if any.

    容器详情的内容详情页签展示包本体的说明文档；容器内没有
    ``kind: skillpack`` 成员时返回空串。
    """

    for _frontmatter, sub in _iter_container_pack_roots(container_dir):
        try:
            _frontmatter, body = _read_skill_document(sub / "SKILL.md")
        except SkillPackValidationError:
            continue
        return body
    return ""


def _read_skill_document(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        raise SkillPackValidationError("缺少根 SKILL.md")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SkillPackValidationError("无法读取 SKILL.md") from exc
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillPackValidationError("SKILL.md 缺少合法 YAML frontmatter")
    try:
        frontmatter = yaml.safe_load(match.group(1))
    except (yaml.YAMLError, RecursionError) as exc:
        raise SkillPackValidationError("SKILL.md frontmatter YAML 无效") from exc
    if not isinstance(frontmatter, dict):
        raise SkillPackValidationError("SKILL.md frontmatter 必须是 YAML 对象")
    return {str(key): value for key, value in frontmatter.items()}, match.group(2)


def _validate_skill_id(value: str, label: str) -> None:
    path_value = Path(value)
    if not value or value in {".", ".."} or len(value) > _MAX_NAME_LENGTH:
        raise SkillPackValidationError(f"invalid SkillPack {label}: {value}")
    if "/" in value or "\\" in value or path_value.is_absolute():
        raise SkillPackValidationError(f"invalid SkillPack {label}: {value}")
    if (
        PureWindowsPath(value).is_absolute()
        or any(char in value for char in _INVALID_NAME_CHARS)
        or value.startswith(".")
    ):
        raise SkillPackValidationError(f"invalid SkillPack {label}: {value}")
    if (
        value.endswith(".")
        or set(value) <= {"."}
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise SkillPackValidationError(f"invalid SkillPack {label}: {value}")
    if value.split(".", 1)[0].upper() in _RESERVED_NAME_STEMS:
        raise SkillPackValidationError(f"invalid SkillPack {label}: {value}")


def _validate_required_sections(body: str) -> None:
    positions = []
    for section in _REQUIRED_SECTIONS:
        heading = re.search(
            rf"^##[ \t]+{re.escape(section)}[ \t]*$",
            body,
            re.MULTILINE,
        )
        positions.append(-1 if heading is None else heading.start())
    if any(position < 0 for position in positions):
        missing = [
            section
            for section, position in zip(_REQUIRED_SECTIONS, positions)
            if position < 0
        ]
        raise SkillPackValidationError(f"SkillPack 正文缺少章节: {', '.join(missing)}")
    if positions != sorted(positions):
        raise SkillPackValidationError("SkillPack 正文章节顺序无效")
    workflow = _WORKFLOW_GRAPH_RE.search(body)
    if workflow is not None and workflow.start() < positions[-1]:
        raise SkillPackValidationError("Workflow Graph 章节顺序无效")


def _parse_workflow_graph(
    body: str,
    members: set[str],
) -> dict[str, Any] | None:
    section = _WORKFLOW_GRAPH_RE.search(body)
    if section is None:
        return None
    fenced = _JSON_FENCE_RE.search(section.group(1))
    if fenced is None:
        raise SkillPackValidationError("Workflow Graph 缺少 JSON 代码块")
    try:
        payload = json.loads(fenced.group(1))
    except json.JSONDecodeError as exc:
        raise SkillPackValidationError("Workflow Graph JSON 无效") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("graph"), dict):
        raise SkillPackValidationError("Workflow Graph 必须包含 graph 对象")
    graph = payload["graph"]
    if graph.get("type") != "skillpack_workflow" or graph.get("directed") is not True:
        raise SkillPackValidationError("Workflow Graph 类型或 directed 无效")
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, dict) or not isinstance(edges, list):
        raise SkillPackValidationError("Workflow Graph nodes 或 edges 无效")
    for node_id, node in nodes.items():
        if not isinstance(node_id, str) or not isinstance(node, dict):
            raise SkillPackValidationError("Workflow Graph node 无效")
        metadata = node.get("metadata")
        skill = metadata.get("skill") if isinstance(metadata, dict) else None
        if not isinstance(skill, str) or skill not in members:
            raise SkillPackValidationError("Workflow Graph node 引用了未声明成员")
    for edge in edges:
        if not isinstance(edge, dict):
            raise SkillPackValidationError("Workflow Graph edge 无效")
        source = edge.get("source")
        target = edge.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            raise SkillPackValidationError("Workflow Graph edge 引用或关系无效")
        if (
            source not in nodes
            or target not in nodes
            or edge.get("relation") != "can_feed"
        ):
            raise SkillPackValidationError("Workflow Graph edge 引用或关系无效")
    return payload


def _inspect_member(
    skills_dir: Path,
    member: str,
    enabled_for: Callable[[str], bool],
) -> dict[str, Any]:
    member_dir = skills_dir / member
    if not member_dir.is_dir():
        return {
            "name": member,
            "display_name": member,
            "description": "",
            "enabled": False,
            "available": False,
            "blocking_reason": "missing",
        }
    try:
        frontmatter, _ = _read_skill_document(member_dir / "SKILL.md")
        raw_name = frontmatter.get("name")
        raw_description = frontmatter.get("description")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        description = (
            raw_description.strip() if isinstance(raw_description, str) else ""
        )
        kind = str(frontmatter.get("kind") or "").strip().casefold()
        if not name or not description or kind not in {"", "skill"}:
            raise SkillPackValidationError("member is not an ordinary Skill")
        _validate_skill_id(name, "member name")
    except SkillPackValidationError:
        return {
            "name": member,
            "display_name": member,
            "description": "",
            "enabled": False,
            "available": False,
            "blocking_reason": "invalid",
        }

    enabled = enabled_for(member)
    return {
        "name": member,
        "display_name": str(frontmatter.get("display_name") or member).strip()
        or member,
        "description": description,
        "enabled": enabled,
        "available": True,
        "blocking_reason": None if enabled else "disabled",
    }


__all__ = [
    "CONTAINER_SKILLS_DIRNAME",
    "SKILLPACK_KIND",
    "SKILLPACK_SKILL_TYPE",
    "SkillPackDefinition",
    "SkillPackOperationUnsupportedError",
    "SkillPackService",
    "SkillPackStatus",
    "SkillPackValidationError",
    "compute_skillpack_status",
    "container_member_backup_dir",
    "member_backup_dir",
    "container_members_root",
    "container_pack_description",
    "container_pack_members",
    "find_container_member_dir",
    "is_container_skillpack",
    "is_skillpack",
    "load_skillpack",
    "project_skillpack",
    "read_container_pack_body",
    "read_skill_kind",
    "referencing_skillpacks",
    "summarize_member_dir",
    "unavailable_skillpacks",
]
