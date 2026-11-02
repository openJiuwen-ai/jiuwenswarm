# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""技能元数据加载与入口文件发现（loader/environment 共用，消除循环依赖）。

从 environment.py 提取，使 turbo_package_loader 可直接依赖本模块而非
environment 的私有 API。本模块无引擎内部依赖，可独立测试。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# meta.json 的路由元数据键：含任一键的有效 JSON 即视为 meta（其余字段 setdefault 默认值）
META_KNOWN_KEYS = ("external_name", "description", "match_keywords")


def load_skill_meta(skill_name: str, skill_dir: Path) -> dict[str, Any]:
    """从 skill_codes/{name}/meta.json 读取路由元数据。

    meta.json 格式：
        {
            "external_name": "pptx-craft",
            "description": "PPT / 演示文稿制作...",
            "match_keywords": ["ppt", "pptx", "演示文稿", ...]
        }

    含 external_name / description / match_keywords 任一键的有效 JSON 即被
    接受（仅含 external_name 也可路由，其余字段走 setdefault 默认值）；
    无 meta.json、解析失败或不含任一已知键时返回空 dict，
    调用方走默认 description/keywords（不含任一已知键时打 warning——
    仅含扩展键的最小配置静默失效是事故关停的可观测性黑洞）。
    """
    meta_file = skill_dir / "meta.json"
    if meta_file.is_file():
        try:
            import json

            data = json.loads(meta_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and any(key in data for key in META_KNOWN_KEYS):
                data.setdefault("external_name", skill_name.replace("_", "-"))
                data.setdefault("description", f"{skill_name} 任务流")
                data.setdefault("match_keywords", [skill_name])
                logger.info(
                    "[SkillMeta] load_skill_meta from meta.json skill=%s",
                    skill_name,
                )
                return data
            if isinstance(data, dict):
                # 有效 JSON dict 但不含任何已知键：整体忽略（返回 {}），
                # enable_turbo 等扩展键不生效。静默忽略会让"仅含扩展键"
                # 的最小配置（如 {"enable_turbo": false}）零信号失效——
                # 事故关停场景致命；与解析失败分支同级告警（含文件路径
                # 与实际键列表，定位到具体 meta.json）。
                logger.warning(
                    "[SkillMeta] meta.json has no known keys (need any of "
                    "%s); ignored entirely, extension keys (e.g. "
                    "enable_turbo) have no effect: %s keys=%s",
                    ",".join(META_KNOWN_KEYS),
                    meta_file,
                    sorted(data.keys()),
                )
        except (OSError, ValueError) as e:
            logger.warning(
                "[SkillMeta] load_skill_meta meta.json parse failed skill=%s: %s",
                skill_name, e,
            )
    return {}


def find_skill_root_file(skill_dir: Path) -> Path | None:
    """查找 skill 目录的入口文件（全仓唯一判定，三处消费方共用）。

    消费方：environment._scan_skills_dir（skill 注册）、
    SkillTurboPlanner._find_skill_root_file（plan_code 组装）、
    skill_turbo_tools._build_tool_description（工具描述扫描）。
    此前三处各自维护一份候选列表，规则漂移会导致「已注册但描述缺失」
    或反向不一致；收口到本函数后规则变更只改一处。

    优先级：
        1. {skill_name}_gen_root.py
        2. {skill_name}_root.py
        3. 任意 *_gen_root.py（按名称排序）
        4. 任意 *_root.py（按名称排序）
        5. plan_code.py（兜底入口）
    """
    skill_name = skill_dir.name
    candidates = [
        skill_dir / f"{skill_name}_gen_root.py",
        skill_dir / f"{skill_name}_root.py",
        *sorted(skill_dir.glob("*_gen_root.py")),
        *sorted(skill_dir.glob("*_root.py")),
        skill_dir / "plan_code.py",
    ]

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            return candidate
    return None
