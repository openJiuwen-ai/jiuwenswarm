# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deterministic offline snapshots for Flash prompt sections.

This module is intentionally independent of openjiuwen and Flash Rails. It turns
the final ``PromptSection`` objects into a language-specific snapshot so a
content-only refactor can prove that rendered prompt text did not change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..sections import FLASH_SECTION_BANDS, FLASH_SECTION_REGISTRY, resolve_carrier

FLASH_PROMPT_SNAPSHOT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PromptSectionSnapshot:
    """One rendered prompt section in stable render order."""

    priority: int
    name: str
    carrier: str
    band: str
    registered: bool
    chars: int
    sha256: str
    text: str

    def as_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "priority": self.priority,
            "name": self.name,
            "carrier": self.carrier,
            "band": self.band,
            "registered": self.registered,
            "chars": self.chars,
            "sha256": self.sha256,
        }
        if include_text:
            payload["text"] = self.text
        return payload


@dataclass(frozen=True)
class PromptBandSnapshot:
    """Hash and size for one numbered Flash prompt band."""

    name: str
    priorities: tuple[int, ...]
    section_names: tuple[str, ...]
    chars: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "priorities": list(self.priorities),
            "section_names": list(self.section_names),
            "chars": self.chars,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class PromptSnapshot:
    """Complete deterministic snapshot for one rendered language."""

    language: str
    sections: tuple[PromptSectionSnapshot, ...]
    bands: tuple[PromptBandSnapshot, ...]
    full_hash: str
    static_hash: str
    prefix_hash: str
    prefix_chars: int
    schema_version: int = FLASH_PROMPT_SNAPSHOT_SCHEMA_VERSION

    @property
    def total_chars(self) -> int:
        return sum(section.chars for section in self.sections)

    @property
    def section_hashes(self) -> tuple[tuple[int, str, str], ...]:
        return tuple(
            (section.priority, section.name, section.sha256)
            for section in self.sections
        )

    @property
    def band_hashes(self) -> dict[str, str]:
        return {band.name: band.sha256 for band in self.bands}

    def as_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "language": self.language,
            "section_count": len(self.sections),
            "total_chars": self.total_chars,
            "full_hash": self.full_hash,
            "static_hash": self.static_hash,
            "prefix_hash": self.prefix_hash,
            "prefix_chars": self.prefix_chars,
            "sections": [
                section.as_dict(include_text=include_text)
                for section in self.sections
            ],
            "bands": [band.as_dict() for band in self.bands],
        }


def build_prompt_snapshot(
    sections: Iterable[Any],
    *,
    language: str = "cn",
) -> PromptSnapshot:
    """Build a deterministic, language-specific snapshot from prompt sections.

    Sections are ordered by ``(priority, input order, name)``. ``priority`` is
    the primary render key; input order preserves the builder's stable ordering
    if two sections collide, while ``name`` is the final deterministic fallback.
    """

    normalized_language = _normalize_language(language)
    indexed_sections = list(enumerate(sections))
    indexed_sections.sort(
        key=lambda item: (
            int(getattr(item[1], "priority", 0) or 0),
            item[0],
            str(getattr(item[1], "name", "") or ""),
        )
    )

    section_snapshots = tuple(
        _snapshot_section(section, normalized_language)
        for _index, section in indexed_sections
    )
    static_sections = tuple(
        section for section in section_snapshots if section.carrier == "system"
    )

    prefix_text = "\n\n".join(
        section.text for section in static_sections if section.text.strip()
    )
    return PromptSnapshot(
        language=normalized_language,
        sections=section_snapshots,
        bands=_build_band_snapshots(section_snapshots),
        full_hash=_hash_sections(section_snapshots),
        static_hash=_hash_sections(static_sections),
        prefix_hash=_hash_payload(prefix_text),
        prefix_chars=len(prefix_text),
    )


def write_prompt_snapshot(
    snapshot: PromptSnapshot,
    output_dir: str | Path,
    *,
    stem: str = "flash_prompt",
    include_text: bool = True,
) -> tuple[Path, Path]:
    """Write deterministic JSON and plain-text representations to disk."""

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    safe_stem = _safe_stem(stem)
    json_path = output_path / f"{safe_stem}.json"
    text_path = output_path / f"{safe_stem}.txt"

    json_path.write_text(
        json.dumps(
            snapshot.as_dict(include_text=include_text),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    text_path.write_text(_render_text(snapshot), encoding="utf-8")
    return json_path, text_path


def _snapshot_section(section: Any, language: str) -> PromptSectionSnapshot:
    priority = int(getattr(section, "priority", 0) or 0)
    name = str(getattr(section, "name", "") or "")
    spec = FLASH_SECTION_REGISTRY.get(priority)
    carrier = resolve_carrier(priority, name)
    text = _render_section_text(getattr(section, "content", None), language)
    return PromptSectionSnapshot(
        priority=priority,
        name=name,
        carrier=carrier,
        band=_resolve_band(priority),
        registered=spec is not None,
        chars=len(text),
        sha256=_hash_payload(
            f"{priority}\0{name}\0{carrier}\0{text}",
        ),
        text=text,
    )


def _render_section_text(content: Any, language: str) -> str:
    if isinstance(content, dict):
        value = content.get(language)
        if not value:
            value = next((item for item in content.values() if item), "")
        return str(value or "")
    return str(content or "")


def _build_band_snapshots(
    sections: tuple[PromptSectionSnapshot, ...],
) -> tuple[PromptBandSnapshot, ...]:
    grouped: dict[str, list[PromptSectionSnapshot]] = {}
    for section in sections:
        grouped.setdefault(section.band, []).append(section)

    ordered_names = [*FLASH_SECTION_BANDS, "unbanded"]
    snapshots: list[PromptBandSnapshot] = []
    for band_name in ordered_names:
        members = grouped.get(band_name, [])
        if not members:
            continue
        ordered = sorted(members, key=lambda item: (item.priority, item.name))
        snapshots.append(
            PromptBandSnapshot(
                name=band_name,
                priorities=tuple(section.priority for section in ordered),
                section_names=tuple(section.name for section in ordered),
                chars=sum(section.chars for section in ordered),
                sha256=_hash_sections(ordered),
            )
        )
    return tuple(snapshots)


def _hash_sections(sections: tuple[PromptSectionSnapshot, ...]) -> str:
    payload = "\n".join(
        f"{section.priority}\0{section.name}\0{section.sha256}"
        for section in sections
    )
    return _hash_payload(payload)


def _hash_payload(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _resolve_band(priority: int) -> str:
    for name, priorities in FLASH_SECTION_BANDS.items():
        if priority in priorities:
            return name
    return "unbanded"


def _normalize_language(language: str) -> str:
    return "cn" if str(language or "").lower() in {"cn", "zh"} else "en"


def _safe_stem(stem: str) -> str:
    value = str(stem or "").strip()
    if not value:
        return "flash_prompt"
    if any(part in value for part in ("/", "\\", "\0")):
        raise ValueError("snapshot stem must not contain path separators or NUL")
    return value


def _render_text(snapshot: PromptSnapshot) -> str:
    lines = [
        f"Flash prompt snapshot schema={snapshot.schema_version}",
        f"language={snapshot.language}",
        f"full_hash={snapshot.full_hash}",
        f"static_hash={snapshot.static_hash}",
        f"prefix_hash={snapshot.prefix_hash}",
        f"prefix_chars={snapshot.prefix_chars}",
        f"section_count={len(snapshot.sections)}",
        "",
    ]
    for section in snapshot.sections:
        lines.extend(
            [
                "=" * 80,
                (
                    f"[{section.priority}] {section.name} "
                    f"carrier={section.carrier} band={section.band} "
                    f"chars={section.chars} sha256={section.sha256}"
                ),
                "=" * 80,
                section.text,
                "",
            ]
        )
    return "\n".join(lines)


__all__ = [
    "FLASH_PROMPT_SNAPSHOT_SCHEMA_VERSION",
    "PromptBandSnapshot",
    "PromptSectionSnapshot",
    "PromptSnapshot",
    "build_prompt_snapshot",
    "write_prompt_snapshot",
]
