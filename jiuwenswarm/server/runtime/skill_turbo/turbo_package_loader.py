# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""外部 turbo_codes 动态包装载器。

职责（借鉴 enterprise_dev_skill_online_v6 executor_single/sandbox 的已验证机制）：
1. 发现：扫描已注册技能目录（含 office-claw-skills 等）下
   ``{skill}/turbo/turbo_codes/{skill_name}/``；
2. 动态包绑定：把每个技能的 ``turbo_codes/`` 目录以唯一包名
   ``skill_turbo_codes_{sanitized(skill_name)}`` 注册进 ``sys.modules``
   （``types.ModuleType`` + ``__path__``，不进 sys.path，多技能不撞名）；
3. 失效清理：目录变更时清理旧子模块后重注册，防陈旧残留；
4. plan_code 自愈：从持久化 plan_code 解析顶层包名，注册表未命中时
   重新发现外部技能并注册（HITL resume 重放保障）。

进程级注册表 ``_PACKAGE_REGISTRY``：包名 -> turbo_codes 目录绝对路径。
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import types
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "ExternalTurboSkill",
    "sanitize_skill_name",
    "ensure_runtime_facade",
    "ensure_turbo_package",
    "is_package_registered",
    "registered_packages",
    "discover_external_turbo_skills",
    "ensure_packages_for_plan_code",
]

_PACKAGE_PREFIX = "skill_turbo_codes_"
# 包名 -> turbo_codes 目录绝对路径（进程级）
_PACKAGE_REGISTRY: dict[str, str] = {}
_REGISTRY_LOCK = threading.Lock()

# plan_code 形如 "from {pkg}.{skill}.{stem} import root"
_PLAN_CODE_IMPORT_RE = re.compile(
    r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+", re.MULTILINE
)


@dataclass(frozen=True)
class ExternalTurboSkill:
    """外部 turbo 技能发现结果。"""

    skill_name: str            # turbo_codes 下的子目录名（如 "ppt"）
    external_name: str         # 技能目录名（如 "pptx-craft"）
    turbo_codes_dir: str       # turbo_codes 目录绝对路径
    meta: dict = field(default_factory=dict)  # turbo/meta.json 内容


def sanitize_skill_name(skill_name: str) -> str:
    """把 skill_name 规范为合法 Python 标识符片段（非字母数字 -> 下划线）。"""
    out = re.sub(r"\W", "_", str(skill_name or "").strip())
    if not out or out[0].isdigit():
        out = f"_{out}"
    return out


def ensure_runtime_facade() -> None:
    """注册中立门面 skill_turbo_runtime（委托 runtime.ensure_runtime_facade）。"""
    from jiuwenswarm.server.runtime.skill_turbo.runtime import ensure_runtime_facade

    ensure_runtime_facade()


def _cleanup_package_modules(pkg_name: str) -> None:
    """从 sys.modules 清理 pkg_name 及其所有子模块（目录变更时防陈旧残留）。"""
    prefix = pkg_name + "."
    stale = [n for n in sys.modules if n == pkg_name or n.startswith(prefix)]
    for n in stale:
        sys.modules.pop(n, None)


def ensure_turbo_package(skill_name: str, turbo_codes_dir: str | Path) -> str:
    """把 turbo_codes 目录注册为唯一动态包（幂等，含目录变更清理重注册）。

    Returns:
        包名（如 ``skill_turbo_codes_ppt``）。
    """
    ensure_runtime_facade()

    dir_str = str(Path(turbo_codes_dir).resolve())
    pkg_name = f"{_PACKAGE_PREFIX}{sanitize_skill_name(skill_name)}"

    with _REGISTRY_LOCK:
        registered_dir = _PACKAGE_REGISTRY.get(pkg_name)
        existing = sys.modules.get(pkg_name)
        if (
            registered_dir == dir_str
            and existing is not None
            and dir_str in list(getattr(existing, "__path__", []) or [])
        ):
            return pkg_name  # 未变化，复用

        # 首次注册 / 目录变更 / 冲突（同名不同目录）：清理重注册
        if registered_dir and registered_dir != dir_str:
            logger.warning(
                "[TurboPackageLoader] package %s rebind: %s -> %s",
                pkg_name,
                registered_dir,
                dir_str,
            )
        _cleanup_package_modules(pkg_name)
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [dir_str]
        pkg.__package__ = pkg_name
        sys.modules[pkg_name] = pkg
        _PACKAGE_REGISTRY[pkg_name] = dir_str
        logger.info("[TurboPackageLoader] bound %s -> %s", pkg_name, dir_str)
        return pkg_name


def is_package_registered(pkg_name: str) -> bool:
    return pkg_name in _PACKAGE_REGISTRY and pkg_name in sys.modules


def registered_packages() -> dict[str, str]:
    return dict(_PACKAGE_REGISTRY)


def _iter_skill_dir_candidates(skill_root: Path) -> list[Path]:
    """枚举 skill_root 下的技能目录候选（root 本身 + 一层子目录）。"""
    candidates: list[Path] = []
    if (skill_root / "turbo").is_dir():
        candidates.append(skill_root)
    try:
        for child in sorted(skill_root.iterdir()):
            if child.is_dir() and (child / "turbo").is_dir():
                candidates.append(child)
    except OSError:
        pass
    return candidates


def discover_external_turbo_skills(
    skill_roots: list[Path] | None = None,
) -> list[ExternalTurboSkill]:
    """扫描技能目录，发现全部 turbo 技能（不注册包、不校验，纯发现）。

    同一 skill_name 多源时按 roots 顺序取第一个
    （请求级绑定目录 > 共享目录 > 工作区，与 resolve_agent_registered_skill_dirs
    返回顺序一致）。
    """
    from jiuwenswarm.server.runtime.skill_turbo.environment import (
        _load_skill_meta,
        find_skill_root_file,
    )

    if skill_roots is None:
        try:
            from jiuwenswarm.common.utils import resolve_agent_registered_skill_dirs

            skill_roots = list(resolve_agent_registered_skill_dirs())
        except Exception as exc:
            logger.warning(
                "[TurboPackageLoader] resolve_agent_registered_skill_dirs failed: %s",
                exc,
            )
            skill_roots = []

    found: list[ExternalTurboSkill] = []
    seen_skill_names: set[str] = set()
    for root in skill_roots:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for skill_dir in _iter_skill_dir_candidates(root_path):
            turbo_dir = skill_dir / "turbo"
            codes_root = turbo_dir / "turbo_codes"
            if not codes_root.is_dir():
                continue
            meta = _load_skill_meta(skill_dir.name, turbo_dir)
            for name_dir in sorted(codes_root.iterdir()):
                if not name_dir.is_dir():
                    continue
                skill_name = name_dir.name
                if skill_name.startswith(("_", ".")):
                    continue
                if skill_name in seen_skill_names:
                    continue
                if find_skill_root_file(name_dir) is None:
                    continue
                seen_skill_names.add(skill_name)
                found.append(
                    ExternalTurboSkill(
                        skill_name=skill_name,
                        external_name=str(
                            meta.get("external_name") or skill_dir.name
                        ),
                        turbo_codes_dir=str(codes_root.resolve()),
                        meta=meta,
                    )
                )
    return found


def ensure_packages_for_plan_code(plan_code: str) -> None:
    """按持久化 plan_code 保障动态包已注册（HITL resume 自愈）。

    从 plan_code 提取顶层包名；若为动态包前缀且未注册，重新发现外部技能并
    注册对应包；仍未命中则交由后续 import 自然抛 PlanCodeLoadError
    （既有降级链路兜底）。
    """
    ensure_runtime_facade()
    match = _PLAN_CODE_IMPORT_RE.search(plan_code or "")
    if not match:
        return
    top = match.group(1).split(".")[0]
    if not top.startswith(_PACKAGE_PREFIX) or is_package_registered(top):
        return
    logger.info(
        "[TurboPackageLoader] plan_code package %s unregistered, rediscovering",
        top,
    )
    for item in discover_external_turbo_skills():
        ensure_turbo_package(item.skill_name, item.turbo_codes_dir)
        if is_package_registered(top):
            return
