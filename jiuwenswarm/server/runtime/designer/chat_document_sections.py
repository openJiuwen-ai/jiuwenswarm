# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Preserve coverage of existing Markdown sections that describe every shot."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TypedDict

from jiuwenswarm.common.schema.designer_graph import DesignerGraphValidationError

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SHOT = re.compile(r"(?:shot|分镜|镜头)\s*#?\s*([1-9]\d*)(?:\s*[:：—-]\s*.+)?", re.I)
_FIELD = re.compile(r"^\s*[-*+]\s+(?:\*\*)?([^:：*]+?)(?:\*\*)?\s*[:：]\s*(?:\*\*)?\s*(.+)$")


class ShotSectionRequirement(TypedDict):
    heading_path: list[str]
    required_shot_indices: list[int]
    required_fields: list[str]


@dataclass
class ShotEntry:
    index: int
    lines: list[str] = field(default_factory=list)

    @property
    def fields(self) -> set[str]:
        return {match[1].strip() for line in self.lines if (match := _FIELD.match(line)) and match[2].strip()}


def shot_sections(text: str) -> dict[tuple[str, ...], list[ShotEntry]]:
    """Group standalone shot headings under their Markdown parent headings."""
    sections: dict[tuple[str, ...], list[ShotEntry]] = {}
    parents: list[tuple[int, str]] = []
    entry: ShotEntry | None = None
    fence = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not fence:
                fence = marker
            elif marker == fence:
                fence = ""
            continue
        if fence:
            continue
        heading = _HEADING.fullmatch(stripped)
        if heading:
            level, title = len(heading[1]), heading[2]
            while parents and parents[-1][0] >= level:
                parents.pop()
        elif stripped.startswith("**") and stripped.endswith("**"):
            title = stripped[2:-2]
        else:
            if entry is not None:
                entry.lines.append(line)
            continue
        shot = _SHOT.fullmatch(title.removeprefix("**").removesuffix("**"))
        if shot:
            entry = ShotEntry(int(shot[1]))
            sections.setdefault(tuple(title for _, title in parents), []).append(entry)
        elif heading:
            # The document title may change its totals; section headings keep their identity.
            if level > 1:
                parents.append((level, title))
            entry = None
        elif entry is not None:
            entry.lines.append(line)
    return sections


def section_requirements(text: str, original_indices: set[int], current_indices: set[int]) -> list[ShotSectionRequirement]:
    """Only expand sections whose original entries covered the complete workflow."""
    requirements: list[ShotSectionRequirement] = []
    for path, entries in shot_sections(text).items():
        indices = [entry.index for entry in entries]
        if not original_indices or set(indices) != original_indices or len(indices) != len(original_indices):
            continue
        requirements.append({
            "heading_path": list(path),
            "required_shot_indices": sorted(current_indices),
            "required_fields": sorted(set.intersection(*(entry.fields for entry in entries))),
        })
    return requirements


def validate_shot_sections(text: str, requirements: list[ShotSectionRequirement], node_id: str) -> None:
    """Require one complete entry per current shot in each established full-shot section."""
    sections = shot_sections(text)
    for requirement in requirements:
        path = tuple(requirement["heading_path"])
        entries = sections.get(path, [])
        label = " / ".join(path) or "document"
        if [entry.index for entry in entries] != requirement["required_shot_indices"]:
            raise DesignerGraphValidationError(
                f"Document {node_id} section {label} must cover shots {requirement['required_shot_indices']} in order"
            )
        for entry in entries:
            missing = set(requirement["required_fields"]) - entry.fields
            if missing:
                raise DesignerGraphValidationError(
                    f"Document {node_id} section {label}, shot {entry.index} is missing fields: {sorted(missing)}"
                )
