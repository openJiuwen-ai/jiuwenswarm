# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Skill 状态工具函数 — 纯函数，无 SkillManager 依赖.

提供 skills_state.json 的读写与查询能力，可被 skill_manager 内部使用，
也可被 agents/harness/team 等模块直接引用，避免对 SkillManager 的循环依赖。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_agent_skills_dir

logger = logging.getLogger(__name__)


def get_state_file() -> Path:
    return get_agent_skills_dir() / "skills_state.json"


def normalize_skill_configs(raw_configs: Any) -> dict[str, dict[str, bool]]:
    """Normalize per-skill config records."""
    if not isinstance(raw_configs, dict):
        return {}

    normalized: dict[str, dict[str, bool]] = {}
    for raw_name, raw_cfg in raw_configs.items():
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip()
        if not name:
            continue
        config = raw_cfg if isinstance(raw_cfg, dict) else {}
        normalized[name] = {"enabled": bool(config.get("enabled", True))}
    return normalized


def get_registered_skill_names(state: dict[str, Any]) -> set[str]:
    """Return all skill names recorded in installed/local state lists."""
    names: set[str] = set()
    for key in ("installed_plugins", "local_skills"):
        items = state.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if name:
                names.add(name)
    return names


def normalize_local_skills(
    raw_local_skills: Any,
    existing_local_skill_names: set[str],
    existing_clawhub_origins: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Keep only local skill records that still exist under the local skills dir.

    重名技能下 name 会撞（两个 ClawHub slug 的 SKILL.md name 相同），按 name
    筛无法区分谁被删、谁还在。因此对带 ``clawhub:`` 前缀 origin 的记录改用
    origin 精确判存活：从 origin 抽 slug（去掉 owner 前缀）→ 构造 ``clawhub:{slug}``
    判断是否在 existing_clawhub_origins 中（ClawHub 目录名 = slug，可由磁盘反推）。
    其余来源（SkillNet URL、teamskillshub:asset_id）的目录名 = skill_name，磁盘
    反推不出 origin，仍按 name 兜底。无 origin 的历史本地记录同样按 name。
    """
    if not isinstance(raw_local_skills, list):
        return []

    clawhub_origins = existing_clawhub_origins or set()
    normalized: list[dict[str, Any]] = []
    for item in raw_local_skills:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        origin = str(item.get("origin", "") or "").strip()
        # ClawHub 记录：从 origin 抽 slug，按 slug 判存活
        # origin 可能为 ``clawhub:owner/slug`` 或 ``clawhub:slug``，
        # 都可以抽出 slug → ``clawhub:{slug}`` 与磁盘反推集合比较。
        if origin.startswith("clawhub:"):
            raw_slug = origin.split(":", 1)[1].strip()
            slug = raw_slug.rsplit("/", 1)[-1]
            if f"clawhub:{slug}" in clawhub_origins:
                normalized.append(item)
            continue
        # 其余来源 / 无 origin 历史记录：目录名 = skill_name，按 name 兜底判存活
        if not name or name not in existing_local_skill_names:
            continue
        normalized.append(item)
    return normalized


def get_skill_enabled(state: dict[str, Any], skill_name: str) -> bool:
    """Read a skill enabled flag with backward-compatible default true."""
    if not skill_name:
        return True

    configs = state.get("skill_configs", {})
    if not isinstance(configs, dict):
        return True

    config = configs.get(skill_name)
    if not isinstance(config, dict):
        return True
    return bool(config.get("enabled", True))


def set_skill_enabled(
    state: dict[str, Any],
    skill_name: str,
    enabled: bool,
) -> None:
    """Persist a skill enabled flag into state."""
    configs = state.setdefault("skill_configs", {})
    if not isinstance(configs, dict):
        configs = {}
        state["skill_configs"] = configs
    configs[skill_name] = {"enabled": bool(enabled)}


def remove_skill_config(state: dict[str, Any], skill_name: str) -> bool:
    """Drop a skill's per-skill config record. Returns True if anything was removed.

    skill_configs only holds the enabled flag, so on uninstall the whole record
    can be dropped — otherwise a stale ``{"enabled": false}`` would be re-applied
    when a skill of the same name is reinstalled later.
    """
    if not skill_name:
        return False
    configs = state.get("skill_configs")
    if not isinstance(configs, dict) or skill_name not in configs:
        return False
    del configs[skill_name]
    return True


def list_disabled_skills(state: dict[str, Any]) -> list[str]:
    """Return sorted disabled skill names from canonical config."""
    configs = state.get("skill_configs", {})
    if not isinstance(configs, dict):
        return []

    disabled = []
    for name, config in configs.items():
        if not isinstance(name, str) or not isinstance(config, dict):
            continue
        if config.get("enabled") is False:
            disabled.append(name)
    return sorted(disabled)


def list_execution_disabled_skills(state: dict[str, Any]) -> list[str]:
    """Return disabled skill names that are currently installed."""
    registered = get_registered_skill_names(state)
    if not registered:
        return []
    return [
        name for name in list_disabled_skills(state)
        if name in registered
    ]


def load_execution_disabled_skills() -> list[str]:
    """Read skills_state.json and return disabled skill names that are installed."""
    try:
        state_file = get_state_file()
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
            return list_execution_disabled_skills(state)
    except Exception as exc:
        logger.warning("[SkillState] Failed to load disabled skills: %s", exc)
    return []


def filter_visible_skill_names(names: list[str]) -> list[str]:
    """Return only the skill names that are not disabled."""
    disabled = set(load_execution_disabled_skills())
    if not disabled:
        return names
    return [n for n in names if n not in disabled]
