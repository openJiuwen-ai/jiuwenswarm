# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Strict loader for Team AgentGroup packages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from openjiuwen.harness.resources import load_agent_template_package
from openjiuwen.harness.schema.extension_spec import (
    AgentTemplateSpec,
    PromptSectionSpec,
    SkillSpec,
)


@dataclass(frozen=True)
class LoadedAgentGroupPackage:
    """One validated AgentGroup load, reusable within a single call chain."""

    package_dir: Path
    manifest: dict[str, Any]
    templates: dict[str, AgentTemplateSpec]

    @property
    def instruction(self) -> str:
        value = self.manifest.get("instruction", "")
        return value.strip() if isinstance(value, str) else ""

    @property
    def capabilities(self) -> tuple[str, ...]:
        raw = self.manifest.get("capabilities", [])
        if not isinstance(raw, list):
            return ()
        names: list[str] = []
        seen: set[str] = set()
        for item in raw:
            name = str(item or "").strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return tuple(names)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous manifests instead of silently keeping the last key."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _read_mapping(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
        )
    except FileNotFoundError as exc:
        raise ValueError(f"{label} missing: {path}") from exc
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object: {path}")
    return payload


def _safe_component(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}: expected a string")
    name = value.strip()
    if not name or name in {".", ".."} or any(ord(char) < 32 for char in name):
        raise ValueError(f"invalid {label}: {value!r}")
    if "/" in name or "\\" in name:
        raise ValueError(f"invalid {label} (path separator): {name}")
    if Path(name).is_absolute() or PureWindowsPath(name).drive:
        raise ValueError(f"invalid {label} (absolute path): {name}")
    return name


def _relative_package_path(value: str, *, label: str) -> Path:
    """Interpret portable package paths without accepting host paths.

    Check Windows drives even on POSIX: ``C:persona`` is drive-relative on
    Windows, while POSIX treats it as an ordinary filename. Relative paths
    retain their native spelling for the underlying AgentTemplate loader.
    """
    windows_path = PureWindowsPath(value)
    if (
        not value.strip()
        or "\x00" in value
        or windows_path.drive
        or windows_path.root
        or Path(value).expanduser().is_absolute()
    ):
        raise ValueError(f"{label} must be package-relative: {value!r}")
    return Path(value)


def _resolve_inside(path: Path, *, root: Path, label: str) -> Path:
    """Resolve symlinks and translate filesystem failures to loader errors."""
    try:
        resolved = path.resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as exc:
        # RuntimeError is the symlink-loop exception on Python 3.11/3.12.
        raise ValueError(f"{label} missing or invalid: {path}") from exc
    if not resolved.is_relative_to(root):
        raise ValueError(f"{label} escapes AgentGroup package: {path}")
    return resolved


def _child_dir(root: Path, name: str, *, label: str) -> Path:
    candidate = _resolve_inside(root / name, root=root, label=label)
    if not candidate.is_dir():
        raise ValueError(f"{label} is not a directory: {candidate}")
    return candidate


def _package_file(path: Path, *, root: Path, label: str) -> Path:
    resolved = _resolve_inside(path, root=root, label=label)
    if not resolved.is_file():
        raise ValueError(f"{label} must be a file inside {root}: {path}")
    return resolved


def _persona_dir(
    agent_dir: Path,
    manifest: dict[str, Any],
    *,
    agent_name: str,
) -> Path:
    persona = manifest.get("persona")
    if not isinstance(persona, dict) or not isinstance(persona.get("dir"), str):
        raise ValueError(
            f"AgentTemplate {agent_name!r} must declare persona.dir"
        )
    label = f"AgentTemplate {agent_name!r} persona.dir"
    relative_dir = _relative_package_path(persona["dir"], label=label)
    resolved = _resolve_inside(agent_dir / relative_dir, root=agent_dir, label=label)
    if not resolved.is_dir():
        raise ValueError(
            f"AgentTemplate {agent_name!r} persona.dir escapes its package"
        )
    markdown_files = [path for path in resolved.rglob("*.md") if path.is_file()]
    if not markdown_files:
        raise ValueError(
            f"AgentTemplate {agent_name!r} persona.dir has no Markdown files"
        )
    for markdown_file in markdown_files:
        _package_file(
            markdown_file,
            root=agent_dir,
            label=f"AgentTemplate {agent_name!r} persona Markdown",
        )
    return resolved


def _agent_names(payload: dict[str, Any]) -> list[str]:
    raw_agents = payload.get("agents")
    if not isinstance(raw_agents, list) or not raw_agents:
        raise ValueError("agent_group manifest agents must be a non-empty list")
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_agents:
        name = _safe_component(raw_name, label="agent name")
        if name in seen:
            raise ValueError(f"duplicate agent name in agent_group manifest: {name}")
        seen.add(name)
        names.append(name)
    if "leader" not in seen:
        raise ValueError("agent_group manifest agents must contain 'leader'")
    return names


def _shared_skills(package_dir: Path, payload: dict[str, Any]) -> list[SkillSpec]:
    raw_skills = payload.get("skills", [])
    if not isinstance(raw_skills, list):
        raise ValueError("agent_group manifest skills must be a list")

    skills_path = package_dir / "skills"
    specs: list[SkillSpec] = []
    seen_paths: set[str] = set()

    def _append_skill(raw: Any) -> None:
        if isinstance(raw, str):
            name = _safe_component(raw, label="skill name")
            values: dict[str, Any] = {"dir": str(skills_path / name), "mode": "all"}
        elif isinstance(raw, dict) and isinstance(raw.get("dir"), str):
            relative_dir = _relative_package_path(raw["dir"], label="agent_group skill dir")
            values = {**raw, "dir": str(package_dir / relative_dir)}
        else:
            raise ValueError("agent_group manifest skills entries must be names or dir mappings")
        skill_dir = _resolve_inside(
            Path(values["dir"]), root=package_dir, label=f"shared skill {raw!r}",
        )
        if not skill_dir.is_dir():
            raise ValueError(f"shared skill escapes AgentGroup package: {raw!r}")
        _package_file(
            skill_dir / "SKILL.md",
            root=skill_dir,
            label=f"shared skill {skill_dir.name!r} SKILL.md",
        )
        key = str(skill_dir)
        if key in seen_paths:
            raise ValueError(f"duplicate shared skill in agent_group manifest: {skill_dir.name}")
        seen_paths.add(key)
        specs.append(SkillSpec.model_validate({**values, "dir": key}))

    for raw_skill in raw_skills:
        _append_skill(raw_skill)

    if skills_path.exists() or skills_path.is_symlink():
        skills_root = _child_dir(package_dir, "skills", label="skills directory")
        for child in sorted(skills_root.iterdir(), key=lambda item: item.name):
            if not child.is_dir() or not (child / "SKILL.md").is_file():
                continue
            if str(child.resolve()) not in seen_paths:
                _append_skill({"dir": str(child.relative_to(package_dir)), "mode": "all"})
    return specs


def _load_member_template(
    package_dir: Path,
    agent_name: str,
) -> AgentTemplateSpec:
    agents_root = _child_dir(package_dir, "agents", label="agents directory")
    agent_dir = _child_dir(agents_root, agent_name, label=f"agent directory {agent_name!r}")
    manifest_path = _package_file(
        agent_dir / "manifest.json",
        root=agent_dir,
        label=f"AgentTemplate manifest for {agent_name!r}",
    )
    manifest = _read_mapping(
        manifest_path,
        label=f"AgentTemplate manifest for {agent_name!r}",
    )
    if manifest.get("package_type") != "agent_template":
        raise ValueError(
            f"AgentTemplate {agent_name!r} must declare "
            "package_type='agent_template'"
        )
    persona_dir = _persona_dir(agent_dir, manifest, agent_name=agent_name)

    agent_md = agent_dir / "AGENT.md"
    if agent_name == "leader":
        agent_md = _package_file(
            agent_md,
            root=agent_dir,
            label="AgentGroup leader AGENT.md",
        )
    elif agent_md.exists() or agent_md.is_symlink():
        raise ValueError(
            f"AgentGroup member {agent_name!r} must not contain AGENT.md; "
            "put its responsibilities in persona instead"
        )

    template = load_agent_template_package(manifest_path)
    # Team member identity is defined by the AgentGroup roster/directory;
    # the template's top-level name remains its user-facing display name.
    if template.agent_card.id != agent_name:
        template = template.model_copy(
            update={
                "agent_card": template.agent_card.model_copy(
                    update={"id": agent_name}
                )
            }
        )

    # A leader manifest normally uses persona.dir="." so the standard loader
    # reads AGENT.md together with persona/*.md.  Preserve the contract for a
    # narrower persona directory by mounting AGENT.md explicitly once.
    if agent_name == "leader" and not agent_md.resolve().is_relative_to(persona_dir):
        leader_rules = PromptSectionSpec(
            name="agent_group_leader_rules",
            content={
                "cn": agent_md.read_text(encoding="utf-8"),
                "en": agent_md.read_text(encoding="utf-8"),
            },
            priority=15,
        )
        template = template.model_copy(
            update={"prompt_sections": [*template.prompt_sections, leader_rules]}
        )
    return template


def load_agent_group_package_bundle(path: Path) -> LoadedAgentGroupPackage:
    """Load and validate one AgentGroup package once."""
    try:
        package_dir = Path(path).expanduser().resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError(f"agent_group package directory not found: {path}") from exc
    if not package_dir.is_dir():
        raise ValueError(f"agent_group package path is not a directory: {path}")

    manifest_path = _package_file(
        package_dir / "manifest.json",
        root=package_dir,
        label="agent_group manifest",
    )
    payload = _read_mapping(manifest_path, label="agent_group manifest")
    if payload.get("package_type") != "agent_group":
        raise ValueError("agent_group manifest package_type must be 'agent_group'")
    if payload.get("name") != package_dir.name:
        raise ValueError(
            f"agent_group manifest name must match directory: "
            f"{payload.get('name')!r} != {package_dir.name!r}"
        )

    instruction = payload.get("instruction", "")
    if not isinstance(instruction, str):
        raise ValueError("agent_group manifest instruction must be a string")
    instruction = instruction.strip()
    shared_skills = _shared_skills(package_dir, payload)

    templates: dict[str, AgentTemplateSpec] = {}
    for agent_name in _agent_names(payload):
        template = _load_member_template(package_dir, agent_name)
        if agent_name == "leader" and template.runtime is not None:
            raise ValueError("AgentGroup leader does not support external runtime")
        prompt_sections = list(template.prompt_sections)
        if instruction:
            if any(
                section.name == "agent_group_instruction"
                for section in prompt_sections
            ):
                raise ValueError(
                    f"AgentTemplate {agent_name!r} reserves prompt section name "
                    "'agent_group_instruction'"
                )
            prompt_sections.append(
                PromptSectionSpec(
                    name="agent_group_instruction",
                    content={"cn": instruction, "en": instruction},
                    priority=20,
                )
            )

        skills = list(template.skills)
        skill_dirs = {str(Path(skill.dir).resolve()) for skill in skills}
        for skill in shared_skills:
            if str(Path(skill.dir).resolve()) not in skill_dirs:
                skills.append(skill)
                skill_dirs.add(str(Path(skill.dir).resolve()))
        templates[agent_name] = template.model_copy(
            update={"prompt_sections": prompt_sections, "skills": skills}
        )
    return LoadedAgentGroupPackage(
        package_dir=package_dir,
        manifest=payload,
        templates=templates,
    )


def load_agent_group_package(path: Path) -> dict[str, AgentTemplateSpec]:
    """Load one validated AgentGroup into ordered per-member template specs."""
    return load_agent_group_package_bundle(path).templates


__all__ = [
    "LoadedAgentGroupPackage",
    "load_agent_group_package",
    "load_agent_group_package_bundle",
]
