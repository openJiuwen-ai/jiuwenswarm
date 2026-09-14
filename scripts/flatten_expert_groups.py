# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Flatten legacy agent-group members into standalone Xiaoyi Work experts.

The command intentionally lives outside product runtime code.  It prepares a
reviewable expert pool from existing packages without teaching the graph or the
materializer about any demo-specific expert IDs.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


_AGENT_TEMPLATE = "agent_template"
_AGENT_GROUP = "agent_group"
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DENIED_TEAMS = frozenset({"academic-journal-selector"})
_SENSITIVE_BASENAMES = frozenset(
    {
        ".env",
        "settings.json",
        "credential.json",
        "credentials.json",
        "secret.json",
        "secrets.json",
        "apikey.json",
        "api-key.json",
        "token.json",
        "password.json",
    }
)
_SENSITIVE_NAME = re.compile(
    r"(?:^|[._-])(?:api[_-]?key|access[_-]?token|private[_-]?key|"
    r"credentials?|secrets?|password)(?:[._-]|$)",
    re.IGNORECASE,
)
_SKILL_NAME = re.compile(r"^name:\s*['\"]?([^'\"\r\n]+)", re.MULTILINE)


class FlattenError(ValueError):
    """Raised when a source package or catalogue violates the flatten contract."""


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FlattenError(f"{label} is not valid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FlattenError(f"{label} must contain a JSON object: {path}")
    return value


def _localized(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("zh", "zh-CN", "en"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return ""


def _safe_segment(value: str, *, label: str) -> str:
    value = value.strip()
    if not _SAFE_SEGMENT.fullmatch(value) or value in {".", ".."}:
        raise FlattenError(f"unsafe {label}: {value!r}")
    return value


def _is_sensitive(relative: Path) -> bool:
    for part in relative.parts:
        lowered = part.lower()
        if lowered in _SENSITIVE_BASENAMES or lowered.startswith(".env."):
            return True
        if _SENSITIVE_NAME.search(lowered):
            return True
    return False


def _relative_directory(root: Path, raw: str, *, label: str) -> Path:
    relative = Path(raw.strip())
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise FlattenError(f"unsafe {label}: {raw!r}")
    root_resolved = root.resolve(strict=True)
    candidate = (root_resolved / relative).resolve(strict=True)
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise FlattenError(f"{label} escapes package root: {raw!r}")
    return candidate


def _copy_filtered_tree(source: Path, destination: Path) -> list[str]:
    """Copy regular files, omitting credentials and all symbolic links."""

    filtered: list[str] = []
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        if item.is_symlink():
            filtered.append(f"{relative} (symlink)")
            continue
        if item.is_dir():
            continue
        if _is_sensitive(relative):
            filtered.append(str(relative))
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
    return filtered


def _skill_name(skill_dir: Path) -> str:
    skill_file = skill_dir / "SKILL.md"
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FlattenError(f"cannot read Skill metadata: {skill_file}: {exc}") from exc
    match = _SKILL_NAME.search(text[:8192])
    if not match:
        raise FlattenError(f"Skill frontmatter name is missing: {skill_file}")
    return _safe_segment(match.group(1).strip(), label="Skill name")


def _declared_team_skill_dirs(
    group_dir: Path, manifest: Mapping[str, Any]
) -> list[Path]:
    entries = manifest.get("skills") or []
    if not isinstance(entries, list):
        raise FlattenError(f"team skills must be a list: {group_dir.name}")
    result: list[Path] = []
    for entry in entries:
        raw = entry.get("dir") if isinstance(entry, Mapping) else entry
        if not isinstance(raw, str) or not raw.strip():
            raise FlattenError(
                f"invalid team Skill entry in {group_dir.name}: {entry!r}"
            )
        relative = Path(raw.strip())
        if len(relative.parts) == 1:
            relative = Path("skills") / relative
        source = _relative_directory(
            group_dir, str(relative), label="team Skill directory"
        )
        if not source.is_dir() or not (source / "SKILL.md").is_file():
            raise FlattenError(f"declared team Skill is missing: {source}")
        result.append(source)
    return result


def _index_skills(
    group_dir: Path,
    manifest: Mapping[str, Any],
    shared_skill_roots: Sequence[Path],
) -> tuple[dict[str, Path], list[str]]:
    """Index by the immutable SKILL.md frontmatter name.

    Team-local Skills win.  For shared roots, a directory already named after
    the frontmatter name wins over a legacy storage alias.
    """

    candidates: dict[str, list[tuple[int, int, str, Path]]] = {}
    for source in _declared_team_skill_dirs(group_dir, manifest):
        name = _skill_name(source)
        candidates.setdefault(name, []).append((0, 0, source.name, source))
    for root_index, root in enumerate(shared_skill_roots, start=1):
        resolved = root.expanduser().resolve(strict=True)
        for source in sorted(resolved.iterdir()):
            if source.is_symlink() or not source.is_dir():
                continue
            if not (source / "SKILL.md").is_file():
                continue
            try:
                name = _skill_name(source)
            except FlattenError:
                continue
            alias_penalty = 0 if source.name == name else 1
            candidates.setdefault(name, []).append(
                (root_index, alias_penalty, source.name, source)
            )

    index: dict[str, Path] = {}
    warnings: list[str] = []
    for name, choices in sorted(candidates.items()):
        # Team-local first; then prefer canonical directory names inside each root.
        choices.sort(key=lambda item: (item[0], item[1], item[2]))
        index[name] = choices[0][3]
        if len(choices) > 1:
            warnings.append(f"duplicate Skill name {name!r}; selected {choices[0][3]}")
    return index, warnings


def _merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_catalog(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"defaults": {}, "teams": {}}
    catalog = _read_json(path, label="catalog")
    defaults = catalog.get("defaults") or {}
    teams = catalog.get("teams") or {}
    if not isinstance(defaults, Mapping) or not isinstance(teams, Mapping):
        raise FlattenError("catalog defaults and teams must be objects")
    return {"defaults": dict(defaults), "teams": dict(teams)}


def _team_packages(source_roots: Sequence[Path]) -> tuple[dict[str, Path], list[str]]:
    packages: dict[str, Path] = {}
    warnings: list[str] = []
    for root in source_roots:
        resolved = root.expanduser().resolve(strict=True)
        for child in sorted(resolved.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            manifest_file = child / "manifest.json"
            if not manifest_file.is_file():
                continue
            try:
                manifest = _read_json(manifest_file, label="team manifest")
            except FlattenError as exc:
                warnings.append(str(exc))
                continue
            if manifest.get("package_type") != _AGENT_GROUP:
                continue
            team_id = str(manifest.get("name") or child.name).strip()
            if team_id in packages:
                warnings.append(
                    f"duplicate source team {team_id!r}; kept {packages[team_id]}"
                )
                continue
            packages[team_id] = child
    return packages, warnings


def _member_manifest(group_dir: Path, member_id: str) -> tuple[Path, dict[str, Any]]:
    member_dir = group_dir / "agents" / member_id
    manifest = _read_json(member_dir / "manifest.json", label="member manifest")
    if manifest.get("packageType") != _AGENT_TEMPLATE:
        raise FlattenError(
            f"member {group_dir.name}/{member_id} is not an agent_template"
        )
    persona = manifest.get("persona")
    if not isinstance(persona, Mapping) or not isinstance(persona.get("dir"), str):
        raise FlattenError(
            f"member persona.dir is missing: {group_dir.name}/{member_id}"
        )
    persona_dir = _relative_directory(
        member_dir, str(persona["dir"]), label="member persona directory"
    )
    if not persona_dir.is_dir() or not list(persona_dir.rglob("*.md")):
        raise FlattenError(f"member persona is empty: {group_dir.name}/{member_id}")
    return persona_dir, manifest


def _default_prompt(display_name: str, capability: str) -> str:
    focus = capability.rstrip("。") if capability else "这项任务"
    return f"请让{display_name}帮我完成：{focus}。"


def _context_persona(
    *, team_id: str, member_id: str, display_name: str, capability: str
) -> str:
    return (
        "# 独立专家运行说明\n\n"
        f"你是从专家团 `{team_id}` 中独立出来的 `{display_name}`"
        f"（成员 ID：`{member_id}`）。\n\n"
        f"核心能力：{capability or '依据原角色说明完成专业任务。'}\n\n"
        "用户通常只会用一句自然语言描述需求。你需要自行理解上下文、选择已挂载的 Skills、"
        "完成必要步骤并直接交付可用结果，不要求用户复述内部流程。信息不足但不影响方向时可"
        "做保守假设并明确标注；涉及事实、数据和外部服务时不得伪造成功结果。\n"
    )


def _light_validate(package_dir: Path) -> None:
    manifest = _read_json(package_dir / "manifest.json", label="flattened manifest")
    card = manifest.get("agentCard")
    if manifest.get("packageType") != _AGENT_TEMPLATE or not isinstance(card, Mapping):
        raise FlattenError(f"invalid flattened package: {package_dir}")
    if card.get("id") != package_dir.name:
        raise FlattenError(
            f"flattened expert id does not match directory: {package_dir}"
        )
    persona_dir = package_dir / str((manifest.get("persona") or {}).get("dir") or "")
    if not persona_dir.is_dir() or not list(persona_dir.rglob("*.md")):
        raise FlattenError(f"flattened persona is empty: {package_dir}")
    for item in manifest.get("skills") or []:
        skill_dir = package_dir / str(item.get("dir") or "")
        if _skill_name(skill_dir) != skill_dir.name:
            raise FlattenError(f"flattened Skill name/path mismatch: {skill_dir}")


def flatten_expert_groups(
    *,
    source_roots: Sequence[Path],
    destination_root: Path,
    catalog_path: Path | None = None,
    shared_skill_roots: Sequence[Path] = (),
) -> dict[str, Any]:
    """Flatten all non-leader members, applying optional reviewed overrides."""

    catalog = _read_catalog(catalog_path)
    defaults = catalog["defaults"]
    catalog_teams = catalog["teams"]
    packages, warnings = _team_packages(source_roots)
    destination_root = destination_root.expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    seen_catalog_members: set[tuple[str, str]] = set()
    for team_id, group_dir in sorted(packages.items()):
        if team_id in _DENIED_TEAMS:
            skipped.append({"team": team_id, "member": "*", "reason": "denied-team"})
            continue
        try:
            manifest = _read_json(group_dir / "manifest.json", label="team manifest")
            if manifest.get("name") != group_dir.name:
                raise FlattenError(
                    f"team manifest name does not match directory: {group_dir}"
                )
            members = manifest.get("agents")
            if not isinstance(members, list):
                raise FlattenError(f"team agents must be a list: {team_id}")
            skill_index, skill_warnings = _index_skills(
                group_dir, manifest, shared_skill_roots
            )
            warnings.extend(f"{team_id}: {item}" for item in skill_warnings)
        except FlattenError as exc:
            skipped.append({"team": team_id, "member": "*", "reason": str(exc)})
            continue

        team_metadata = manifest.get("metadata") or {}
        team_config = catalog_teams.get(team_id) or {}
        if not isinstance(team_config, Mapping):
            warnings.append(f"ignored invalid catalog team entry: {team_id}")
            team_config = {}
        member_configs = team_config.get("members") or {}
        if not isinstance(member_configs, Mapping):
            warnings.append(f"ignored invalid member catalog for team: {team_id}")
            member_configs = {}

        for raw_member_id in members:
            if raw_member_id == "leader":
                continue
            try:
                member_id = _safe_segment(str(raw_member_id), label="member id")
                raw_config = member_configs.get(member_id) or {}
                if not isinstance(raw_config, Mapping):
                    raise FlattenError(
                        f"member catalog entry must be an object: {team_id}/{member_id}"
                    )
                member_config = _merge(defaults.get("member") or {}, raw_config)
                seen_catalog_members.add((team_id, member_id))
                if member_config.get("enabled") is False:
                    skipped.append(
                        {"team": team_id, "member": member_id, "reason": "disabled"}
                    )
                    continue
                persona_source, source_manifest = _member_manifest(group_dir, member_id)
                expert_id = _safe_segment(
                    str(member_config.get("id") or f"{team_id}-{member_id}"),
                    label="expert id",
                )
                destination = destination_root / expert_id
                if destination.exists():
                    skipped.append(
                        {
                            "team": team_id,
                            "member": member_id,
                            "id": expert_id,
                            "reason": "destination-exists",
                        }
                    )
                    continue

                source_card = source_manifest.get("agentCard") or {}
                display_name = (
                    _localized(member_config.get("displayName"))
                    or _localized(source_card.get("name"))
                    or _localized(source_manifest.get("name"))
                    or member_id
                )
                capability = (
                    _localized(member_config.get("capability"))
                    or _localized(member_config.get("description"))
                    or _localized(source_card.get("description"))
                    or _localized(source_manifest.get("description"))
                )
                description = (
                    _localized(member_config.get("description"))
                    or capability
                    or f"{display_name}提供的专业能力。"
                )
                quick_prompt = _localized(
                    member_config.get("quickPrompt")
                ) or _default_prompt(display_name, capability)

                explicit_skills = "skills" in member_config
                configured_skills = member_config.get("skills")
                if configured_skills is None:
                    configured_skills = [
                        _skill_name(path)
                        for path in _declared_team_skill_dirs(group_dir, manifest)
                    ]
                if not isinstance(configured_skills, list) or not all(
                    isinstance(item, str) for item in configured_skills
                ):
                    raise FlattenError(
                        f"member skills must be a list of original Skill names: {team_id}/{member_id}"
                    )
                skill_names = list(
                    dict.fromkeys(
                        item.strip() for item in configured_skills if item.strip()
                    )
                )
                missing_skills = [
                    name for name in skill_names if name not in skill_index
                ]
                if missing_skills and explicit_skills:
                    raise FlattenError(
                        f"configured Skills not found: {', '.join(missing_skills)}"
                    )
                skill_names = [name for name in skill_names if name in skill_index]
                if not skill_names and member_config.get("allowNoSkills") is not True:
                    raise FlattenError("no-available-skills")

                collaboration = _merge(
                    {
                        "contractVersion": "xiaoyi.expert-collaboration.v1",
                        "reusable": True,
                    },
                    member_config.get("collaboration") or {},
                )
                tags = list(
                    dict.fromkeys(
                        [
                            str(item)
                            for item in (
                                list(team_metadata.get("tags") or [])
                                + list(team_config.get("tags") or [])
                                + list(member_config.get("tags") or [])
                            )
                            if str(item).strip()
                        ]
                    )
                )
                category_id = str(
                    member_config.get("categoryId")
                    or team_config.get("categoryId")
                    or team_metadata.get("categoryId")
                    or "Other"
                )

                staging_parent = Path(
                    tempfile.mkdtemp(prefix=f".{expert_id}.", dir=destination_root)
                )
                staging = staging_parent / expert_id
                try:
                    persona_destination = staging / "persona"
                    filtered = _copy_filtered_tree(persona_source, persona_destination)
                    if not list(persona_destination.rglob("*.md")):
                        raise FlattenError(
                            f"all persona files were filtered: {team_id}/{member_id}"
                        )
                    (persona_destination / "99-flattened-team-context.md").write_text(
                        _context_persona(
                            team_id=team_id,
                            member_id=member_id,
                            display_name=display_name,
                            capability=capability,
                        ),
                        encoding="utf-8",
                    )
                    manifest_skills: list[dict[str, str]] = []
                    for name in skill_names:
                        source = skill_index[name]
                        target = staging / "skills" / name
                        filtered.extend(
                            f"skills/{name}/{item}"
                            for item in _copy_filtered_tree(source, target)
                        )
                        if not (target / "SKILL.md").is_file():
                            raise FlattenError(
                                f"Skill was filtered or incomplete: {name}"
                            )
                        if _skill_name(target) != name:
                            raise FlattenError(
                                f"Skill name changed while copying: {name}"
                            )
                        manifest_skills.append({"dir": f"skills/{name}", "mode": "all"})

                    flattened_manifest = {
                        "packageType": _AGENT_TEMPLATE,
                        "agentCard": {
                            "id": expert_id,
                            "name": display_name,
                            "description": description,
                        },
                        "persona": {"dir": "persona"},
                        "skills": manifest_skills,
                        "metadata": {
                            "version": "1.0.0-flattened",
                            "source": "local-flattened-agent-group",
                            "sourceTeam": team_id,
                            "sourceMember": member_id,
                            "generatedBy": "expert-pool-flattener",
                            "profession": _localized(member_config.get("profession"))
                            or display_name,
                            "categoryId": category_id,
                            "tags": tags,
                            "quickPrompts": [quick_prompt],
                            "audiences": list(member_config.get("audiences") or []),
                            "deliverables": list(
                                member_config.get("deliverables") or []
                            ),
                            "capability": capability,
                            "collaboration": collaboration,
                        },
                    }
                    (staging / "manifest.json").write_text(
                        json.dumps(flattened_manifest, ensure_ascii=False, indent=2)
                        + "\n",
                        encoding="utf-8",
                    )
                    _light_validate(staging)
                    staging.rename(destination)
                finally:
                    shutil.rmtree(staging_parent, ignore_errors=True)

                created.append(
                    {
                        "id": expert_id,
                        "path": str(destination),
                        "sourceTeam": team_id,
                        "sourceMember": member_id,
                        "skillNames": skill_names,
                        "quickPrompt": quick_prompt,
                        "filteredFiles": filtered,
                    }
                )
            except FlattenError as exc:
                skipped.append(
                    {"team": team_id, "member": str(raw_member_id), "reason": str(exc)}
                )

    for team_id, value in catalog_teams.items():
        if not isinstance(value, Mapping):
            continue
        for member_id in value.get("members") or {}:
            if (str(team_id), str(member_id)) not in seen_catalog_members:
                warnings.append(
                    f"catalog member was not found in sources: {team_id}/{member_id}"
                )

    return {
        "success": True,
        "created": created,
        "skipped": skipped,
        "warnings": warnings,
        "summary": {
            "created": len(created),
            "skipped": len(skipped),
            "sourceTeams": len(packages),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Flatten agent_group members into standalone agent_template packages"
    )
    parser.add_argument(
        "--source-root",
        action="append",
        required=True,
        type=Path,
        help="Directory whose immediate children are agent_group packages",
    )
    parser.add_argument(
        "--skill-root",
        action="append",
        default=[],
        type=Path,
        help="Optional shared Skill root; indexed by immutable SKILL.md name",
    )
    parser.add_argument("--destination-root", required=True, type=Path)
    parser.add_argument("--catalog", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = flatten_expert_groups(
        source_roots=args.source_root,
        destination_root=args.destination_root,
        catalog_path=args.catalog,
        shared_skill_roots=args.skill_root,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
