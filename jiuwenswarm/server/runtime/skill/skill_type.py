# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Normalize Skill ``skill_type`` for list / picker dual-read with frontmatter ``kind``.

Priority (dev-stable slim port of develop ``skill_type``):
``swarm_skill`` > ``multimodal_skill`` > ``skill``.

``skillpack`` is omitted here — that module is develop-only and not on this line.
Swarm detection: frontmatter ``kind: swarm-skill`` or alias ``kind: team-skill``.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

import yaml

SKILL_TYPE_SKILL = "skill"
SKILL_TYPE_SWARM = "swarm_skill"
SKILL_TYPE_MULTIMODAL = "multimodal_skill"

# ``team-skill`` is the Hub / jiuwen-swarmskill alias for Swarm Skill packages.
SWARM_KINDS = frozenset({"swarm-skill", "team-skill"})

_MULTIMEDIA_SUFFIXES = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".mp3",
        ".wav",
        ".mp4",
        ".mov",
    }
)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_ARCHIVE_DIRNAME = ".archive"

# Bounded LRU caches: chat hot paths classify many skills per message; avoid
# unbounded growth across skill version churn in long-lived processes.
_CACHE_MAX = 512
_frontmatter_kind_cache: OrderedDict[tuple[str, int], str] = OrderedDict()
# Key includes skill-dir mtime so adding media without editing SKILL.md invalidates.
_multimedia_cache: OrderedDict[tuple[str, int, int], bool] = OrderedDict()


def _cache_put(cache: OrderedDict, key: tuple, value: Any) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _CACHE_MAX:
        cache.popitem(last=False)


def is_swarm_kind(kind: Any) -> bool:
    return str(kind or "").strip() in SWARM_KINDS


def is_swarm_skill_type(skill_type: Any) -> bool:
    return str(skill_type or "").strip() == SKILL_TYPE_SWARM


def detect_skill_type(
    skill_dir: Path | None,
    *,
    kind: str | None = None,
) -> str:
    """Detect skill_type for a package directory; invalid dirs → ``skill``.

    Pass ``kind`` when the caller already read frontmatter to skip a second
    ``SKILL.md`` parse on the Swarm check path.
    """
    if skill_dir is None or not skill_dir.is_dir():
        return SKILL_TYPE_SKILL

    resolved_kind = kind if kind is not None else read_frontmatter_kind(skill_dir)
    if is_swarm_kind(resolved_kind):
        return SKILL_TYPE_SWARM

    if _has_multimedia_asset(skill_dir):
        return SKILL_TYPE_MULTIMODAL
    return SKILL_TYPE_SKILL


def skill_type_from_meta(meta: dict[str, Any] | None, skill_dir: Path | None = None) -> str:
    """Prefer already-parsed ``kind`` / ``skill_type``, else scan ``skill_dir``."""
    record = meta if isinstance(meta, dict) else {}
    existing = str(record.get("skill_type") or "").strip()
    if existing == SKILL_TYPE_SWARM or is_swarm_kind(record.get("kind")):
        return SKILL_TYPE_SWARM
    if existing in {SKILL_TYPE_SKILL, SKILL_TYPE_MULTIMODAL}:
        return existing
    if existing:
        # Unknown but present — keep for forward-compat (e.g. develop skillpack).
        return existing
    if "kind" in record:
        return detect_skill_type(
            skill_dir,
            kind=str(record.get("kind") or "").strip(),
        )
    return detect_skill_type(skill_dir)


def apply_skill_type_fields(meta: dict[str, Any], skill_dir: Path | None = None) -> dict[str, Any]:
    """Mutate ``meta`` to set normalized ``skill_type`` while keeping ``kind``."""
    if not isinstance(meta, dict):
        return meta
    meta["skill_type"] = skill_type_from_meta(meta, skill_dir)
    return meta


def read_frontmatter_kind(skill_dir: Path | None) -> str:
    """Read SKILL.md frontmatter ``kind`` (list DTO + Swarm session policy).

    Looks for ``SKILL.md`` / ``skill.md`` / ``Skill.md``. Empty when absent.
    Results are cached per ``(skill_dir, mtime_ns)`` for chat hot paths.
    """
    if skill_dir is None or not skill_dir.is_dir():
        return ""

    skill_md = None
    for name in ("SKILL.md", "skill.md", "Skill.md"):
        candidate = skill_dir / name
        if candidate.is_file():
            skill_md = candidate
            break
    if skill_md is None:
        return ""

    cache_key = _skill_md_cache_key(skill_dir, skill_md)
    if cache_key is not None and cache_key in _frontmatter_kind_cache:
        _frontmatter_kind_cache.move_to_end(cache_key)
        return _frontmatter_kind_cache[cache_key]

    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return ""

    kind = _parse_frontmatter_kind(text)
    if cache_key is not None:
        _cache_put(_frontmatter_kind_cache, cache_key, kind)
    return kind


def _parse_frontmatter_kind(text: str) -> str:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return ""

    fm_text = match.group(1)
    try:
        loaded = yaml.safe_load(fm_text)
    except Exception:
        loaded = None

    if isinstance(loaded, dict):
        return str(loaded.get("kind") or "").strip()

    for line in fm_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        if key.strip() == "kind":
            return value.strip().strip("\"'")
    return ""


def _has_multimedia_asset(skill_dir: Path) -> bool:
    skill_md = None
    for name in ("SKILL.md", "skill.md", "Skill.md"):
        candidate = skill_dir / name
        if candidate.is_file():
            skill_md = candidate
            break
    cache_key = _multimedia_cache_key(skill_dir, skill_md)
    if cache_key is not None and cache_key in _multimedia_cache:
        _multimedia_cache.move_to_end(cache_key)
        return _multimedia_cache[cache_key]

    found = False
    for path in skill_dir.rglob("*"):
        if not path.is_file():
            continue
        # Skip version archive trees without depending on develop archive_store.
        if _ARCHIVE_DIRNAME in path.parts:
            continue
        if path.suffix.lower() in _MULTIMEDIA_SUFFIXES:
            found = True
            break
    if cache_key is not None:
        _cache_put(_multimedia_cache, cache_key, found)
    return found


def _skill_md_cache_key(skill_dir: Path, skill_md: Path) -> tuple[str, int] | None:
    try:
        return (str(skill_dir.resolve()), int(skill_md.stat().st_mtime_ns))
    except OSError:
        return None


def _multimedia_cache_key(
    skill_dir: Path, skill_md: Path | None
) -> tuple[str, int, int] | None:
    """Include directory mtime so media added without SKILL.md edits invalidate."""
    try:
        resolved = str(skill_dir.resolve())
        dir_mtime = int(skill_dir.stat().st_mtime_ns)
        md_mtime = int(skill_md.stat().st_mtime_ns) if skill_md is not None else 0
        return (resolved, md_mtime, dir_mtime)
    except OSError:
        return None


def clear_skill_type_caches() -> None:
    """Test helper: drop process-level frontmatter / multimedia caches."""
    _frontmatter_kind_cache.clear()
    _multimedia_cache.clear()
