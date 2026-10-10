# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""根据 Skill frontmatter / 资源识别 skill_type.

优先级固定：skillpack > swarm_skill > multimodal_skill > skill。
swarm 判定依据 frontmatter ``kind: swarm-skill`` 或兼容别名 ``kind: team-skill``。
扫描多媒体时排除根级 ``.archive/``。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from jiuwenswarm.server.runtime.skill.archive_store import ARCHIVE_DIRNAME
from jiuwenswarm.server.runtime.skill.skillpack import (
    SKILLPACK_SKILL_TYPE,
    is_skillpack,
)

SKILL_TYPE_SKILL = "skill"
SKILL_TYPE_SKILLPACK = SKILLPACK_SKILL_TYPE
SKILL_TYPE_SWARM = "swarm_skill"
SKILL_TYPE_MULTIMODAL = "multimodal_skill"

# ``team-skill`` 为 Swarm Skill 的兼容别名（文档 / 存量包沿用）。
_SWARM_KINDS = frozenset({"swarm-skill", "team-skill"})

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


def detect_skill_type(skill_dir: Path | None) -> str:
    """识别 Skill 类型；目录无效时返回普通 ``skill``."""
    if skill_dir is None or not skill_dir.is_dir():
        return SKILL_TYPE_SKILL

    if is_skillpack(skill_dir):
        return SKILL_TYPE_SKILLPACK

    if _frontmatter_kind_is_swarm(skill_dir):
        return SKILL_TYPE_SWARM

    if _has_multimedia_asset(skill_dir):
        return SKILL_TYPE_MULTIMODAL
    return SKILL_TYPE_SKILL


def _frontmatter_kind_is_swarm(skill_dir: Path) -> bool:
    """SKILL.md frontmatter 的 kind 是否为 ``swarm-skill`` / ``team-skill``."""
    return _read_frontmatter_kind(skill_dir) in _SWARM_KINDS


def _read_frontmatter_kind(skill_dir: Path) -> str:
    skill_md = None
    for name in ("SKILL.md", "skill.md"):
        candidate = skill_dir / name
        if candidate.is_file():
            skill_md = candidate
            break
    if skill_md is None:
        return ""

    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return ""

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

    # 回退：逐行解析 kind
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
    archive = skill_dir / ARCHIVE_DIRNAME
    for path in skill_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            path.relative_to(archive)
            continue  # 落在 .archive 下，跳过
        except ValueError:
            pass
        if path.suffix.lower() in _MULTIMEDIA_SUFFIXES:
            return True
    return False
