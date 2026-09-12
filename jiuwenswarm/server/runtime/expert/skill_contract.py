# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Inspect beta3 Skill display names and runtime call identifiers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

import yaml

from jiuwenswarm.server.runtime.expert.metadata_safety import (
    is_safe_prompt_identifier,
)


@dataclass(frozen=True, slots=True)
class SkillContractInspection:
    """Original names safe to display plus beta3 runtime-callability errors."""

    names: tuple[str, ...]
    errors: tuple[str, ...]


def inspect_skill_contracts(
    package_root: Path,
    manifest: Mapping[str, Any],
    *,
    is_team: bool = False,
) -> SkillContractInspection:
    """Inspect declared Skills without leaking files outside ``package_root``.

    beta3 addresses a Skill by its directory basename.  The product displays the
    original frontmatter ``name``.  A member is team-callable only when those two
    identifiers are identical, so discovery and materialization must share this
    check.
    """

    package_root = Path(package_root).resolve()
    names: list[str] = []
    errors: list[str] = []
    for item in manifest.get("skills") or []:
        if isinstance(item, str):
            relative = f"skills/{item}" if is_team else item
        elif isinstance(item, Mapping):
            relative = str(item.get("dir") or item.get("path") or "").strip()
        else:
            errors.append("Skill declaration must be a string or mapping")
            continue
        if not relative:
            errors.append("Skill declaration has no directory")
            continue
        raw_path = Path(relative)
        windows_path = PureWindowsPath(relative)
        if (
            raw_path.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or ".." in raw_path.parts
            or ".." in windows_path.parts
        ):
            errors.append(f"Skill directory is unsafe: {relative!r}")
            continue
        try:
            skill_dir = (package_root / raw_path).resolve(strict=True)
            skill_md = (skill_dir / "SKILL.md").resolve(strict=True)
        except OSError:
            errors.append(f"Skill metadata is missing: {relative!r}")
            continue
        if (
            not skill_dir.is_relative_to(package_root)
            or not skill_dir.is_dir()
            or not skill_md.is_relative_to(skill_dir)
            or not skill_md.is_file()
        ):
            errors.append(f"Skill directory escapes its package: {relative!r}")
            continue

        runtime_name = skill_dir.name
        display_name = runtime_name
        if not is_safe_prompt_identifier(runtime_name):
            errors.append(f"Skill runtime id is not prompt-safe: {runtime_name!r}")
        try:
            text = skill_md.read_text(encoding="utf-8")
            if not text.startswith("---"):
                raise ValueError("missing frontmatter")
            closing = text.find("\n---", 3)
            if closing < 0:
                raise ValueError("unterminated frontmatter")
            metadata = yaml.safe_load(text[3:closing])
            declared = metadata.get("name") if isinstance(metadata, Mapping) else None
            if not isinstance(declared, str) or not declared.strip():
                raise ValueError("missing frontmatter name")
            display_name = declared.strip()
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
            errors.append(f"Skill {runtime_name!r} has invalid frontmatter name: {exc}")

        if display_name and display_name not in names:
            names.append(display_name)
        if display_name != runtime_name:
            errors.append(
                "Skill frontmatter name does not match its beta3 runtime id: "
                f"{display_name!r} != {runtime_name!r}"
            )
        elif not is_safe_prompt_identifier(display_name):
            errors.append(
                f"Skill frontmatter name is not prompt-safe: {display_name!r}"
            )

    return SkillContractInspection(tuple(names), tuple(errors))


__all__ = ["SkillContractInspection", "inspect_skill_contracts"]
