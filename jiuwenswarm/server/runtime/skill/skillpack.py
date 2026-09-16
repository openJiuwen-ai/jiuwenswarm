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
    if (
        not value
        or value in {".", ".."}
        or len(value) > _MAX_NAME_LENGTH
        or "/" in value
        or "\\" in value
        or path_value.is_absolute()
        or PureWindowsPath(value).is_absolute()
        or any(char in value for char in _INVALID_NAME_CHARS)
        or value.startswith(".")
        or value.endswith(".")
        or set(value) <= {"."}
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or value.split(".", 1)[0].upper() in _RESERVED_NAME_STEMS
    ):
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
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source not in nodes
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
    "SKILLPACK_KIND",
    "SKILLPACK_SKILL_TYPE",
    "SkillPackDefinition",
    "SkillPackStatus",
    "SkillPackValidationError",
    "compute_skillpack_status",
    "is_skillpack",
    "load_skillpack",
    "project_skillpack",
    "read_skill_kind",
    "referencing_skillpacks",
    "unavailable_skillpacks",
]
