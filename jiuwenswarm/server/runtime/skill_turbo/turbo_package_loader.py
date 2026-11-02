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

进程级注册表 ``_PACKAGE_REGISTRY``：包名 -> turbo_codes 目录绝对路径；
``_PACKAGE_SKILL_NAMES``：包名 -> 原始 skill_name（清洗碰撞检测用）。

单进程假设与多租户路线图：
    当前部署形态为 per-user sidecar（每用户独立进程），``_PACKAGE_REGISTRY``
    等注册表为进程级，不存在跨租户共享。``ensure_turbo_package`` 中"同名
    不同目录"的 rebind 路径在该假设下安全（同一技能目录变更后重注册）。

    多租户共享进程形态下，rebind 会导致先注册租户的 plan_code import 解析
    到后注册租户的代码。若未来支持多租户共享 sidecar，需将注册表改为
    per-tenant 隔离（如 ``dict[tenant_id, dict[pkg_name, ...]]``），或在
    rebind 分支拒绝并降级（``return None``）。
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
    "dynamic_package_prefix",
    "is_dynamic_package_prefix",
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
# 包名 -> 原始 skill_name（清洗前）。用于检测清洗碰撞：不同 skill_name
# 清洗后落到同一包名（如 "p.pt" 与 "p_pt" -> skill_turbo_codes_p_pt）时
# 拒绝后注册者，防止其重绑包导致先注册技能执行到错误代码。
_PACKAGE_SKILL_NAMES: dict[str, str] = {}
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


def dynamic_package_prefix(skill_name: str) -> str:
    """技能名 -> 动态包导入前缀（如 ``skill_turbo_codes_ppt.``）。

    白名单/校验器构造统一经本函数取前缀，禁止消费方手写
    ``skill_turbo_codes_`` 字面量（前缀定义单一来源，防漂移）。
    """
    return f"{_PACKAGE_PREFIX}{sanitize_skill_name(skill_name)}."


def is_dynamic_package_prefix(prefix: str) -> bool:
    """判断某 import 前缀是否指向动态注册包（用于过滤/识别）。"""
    return str(prefix).startswith(_PACKAGE_PREFIX)


def ensure_runtime_facade() -> None:
    """注册中立门面 skill_turbo_runtime（委托 runtime.ensure_runtime_facade）。"""
    # 别名导入：避免与同名模块级函数形成外层作用域重定义
    from jiuwenswarm.server.runtime.skill_turbo.runtime import (
        ensure_runtime_facade as _ensure_runtime_facade,
    )

    _ensure_runtime_facade()


def _cleanup_package_modules(pkg_name: str) -> None:
    """从 sys.modules 清理 pkg_name 及其所有子模块（目录变更时防陈旧残留）。"""
    prefix = pkg_name + "."
    stale = [n for n in sys.modules if n == pkg_name or n.startswith(prefix)]
    for n in stale:
        sys.modules.pop(n, None)


def ensure_turbo_package(skill_name: str, turbo_codes_dir: str | Path) -> str | None:
    """把 turbo_codes 目录注册为唯一动态包（幂等，含目录变更清理重注册）。

    Returns:
        包名（如 ``skill_turbo_codes_ppt``）；清洗碰撞（不同 skill_name
        清洗后同包名，如 ``"p.pt"`` 与 ``"p_pt"``）时返回 ``None``
        拒绝注册，调用方应跳过该 skill。
    """
    ensure_runtime_facade()

    dir_str = str(Path(turbo_codes_dir).resolve())
    pkg_name = f"{_PACKAGE_PREFIX}{sanitize_skill_name(skill_name)}"

    with _REGISTRY_LOCK:
        registered_dir = _PACKAGE_REGISTRY.get(pkg_name)
        registered_skill = _PACKAGE_SKILL_NAMES.get(pkg_name)
        # 清洗碰撞：后注册者不得重绑已由其他 skill_name 占用的包名，
        # 否则先注册技能的 plan_code import 会解析到错误技能的代码。
        if registered_skill is not None and registered_skill != skill_name:
            logger.error(
                "[TurboPackageLoader] package %s collision: skill %r conflicts "
                "with registered %r, reject registration",
                pkg_name,
                skill_name,
                registered_skill,
            )
            return None
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
        _PACKAGE_SKILL_NAMES[pkg_name] = skill_name
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
    from jiuwenswarm.server.runtime.skill_turbo.skill_meta import (
        find_skill_root_file,
        load_skill_meta,
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
            meta = load_skill_meta(skill_dir.name, turbo_dir)
            # enable_turbo 开关：true / 缺省 = 允许该技能走 turbo 加速（向后
            # 兼容，pptx-craft / xlsx-craft 等未声明字段的技能保持启用）；
            # 显式 false = 跳过该技能——既不出现在 skill_acceleration_exec 的
            # 描述清单（skill_turbo_tools._collect_skill_entries 源二），也不
            # 注册进 environment 的 skills 路由池（planner 无匹配 →
            # SkillTurboNotHandled → interface 层降级为标准流程）。
            # 开关翻转即刻生效（无需重启）：发现阶段每次重扫（meta.json 每次
            # 重读），技能从 discovered 消失/出现即改变 _external_scan_cache
            # 的 mtime 快照 key 集合（必 miss 走全量重扫）；仅工具描述清单
            # 受 _entries_cache 60s TTL 滞后。
            if meta.get("enable_turbo") is False:
                logger.info(
                    "[TurboPackageLoader] skip turbo-disabled skill "
                    "(enable_turbo=false): %s (%s)",
                    skill_dir.name,
                    turbo_dir,
                )
                continue
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

    遍历 plan_code 中全部 from-import，对每个以动态包前缀开头且未注册的
    顶层包，重新发现外部技能并注册；仍未命中则交由后续 import 自然抛
    PlanCodeLoadError（既有降级链路兜底）。
    """
    ensure_runtime_facade()
    # 遍历全部 from-import（防多行 plan_code 首个非动态包 import 时遗漏）
    seen: set[str] = set()
    unregistered: list[str] = []
    for m in _PLAN_CODE_IMPORT_RE.finditer(plan_code or ""):
        top = m.group(1).split(".")[0]
        if (
            top.startswith(_PACKAGE_PREFIX)
            and top not in seen
            and not is_package_registered(top)
        ):
            seen.add(top)
            unregistered.append(top)
    if not unregistered:
        return
    for top in unregistered:
        logger.info(
            "[TurboPackageLoader] plan_code package %s unregistered, rediscovering",
            top,
        )
    for item in discover_external_turbo_skills():
        ensure_turbo_package(item.skill_name, item.turbo_codes_dir)
        if all(is_package_registered(top) for top in unregistered):
            return
