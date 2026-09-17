"""工作模式（work_mode）共享基础设施。

位于 ``common/`` 层供 ``server.runtime.session.work_mode`` 与
``gateway.cron.models`` 共同 import，避免 gateway 反向依赖 server runtime。

work_mode 与 Agent 执行模式（``mode`` 字段）正交：
- ``code``：代码工程，绑定项目目录，展示 Git 状态/分支/diff。
- ``work``：普通办公协作，不默认暴露 Git 能力。
"""
from __future__ import annotations

from typing import Any

DEFAULT_WEB_WORK_MODE: str = "work"
# 同时作为"code 模式字面量"语义,resolve_default_project_id 据此判定 code 模式
DEFAULT_TUI_WORK_MODE: str = "code"

SUPPORTED_WORK_MODES: frozenset[str] = frozenset({"code", "work"})

DEFAULT_PROJECT_ID_WORK: str = "default"
DEFAULT_PROJECT_ID_CODE: str = "default_code"
DEFAULT_PROJECT_IDS: frozenset[str] = frozenset({DEFAULT_PROJECT_ID_WORK, DEFAULT_PROJECT_ID_CODE})


def normalize_work_mode(raw: Any, *, default: str = DEFAULT_WEB_WORK_MODE) -> str:
    """宽松规范化 ``work_mode``,非法值回落到 ``default``。

    仅用于旧数据迁移 / ``from_dict`` 兜底 / 内部防御性读取;
    公开请求入口必须用 ``resolve_request_work_mode`` 严格校验。
    """
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in SUPPORTED_WORK_MODES:
            return value
    if isinstance(default, str) and default.strip().lower() in SUPPORTED_WORK_MODES:
        return default.strip().lower()
    return DEFAULT_WEB_WORK_MODE


def is_default_project_id(project_id: str | None) -> bool:
    """是否为虚拟默认项目 ID(含空串/``None``,归默认项目)。"""
    if not project_id or not isinstance(project_id, str):
        return True
    return project_id in DEFAULT_PROJECT_IDS


def resolve_default_project_id(work_mode: str) -> str:
    """按 ``work_mode`` 返回默认项目 ID:``work``→``default``,``code``→``default_code``。"""
    if isinstance(work_mode, str) and work_mode.strip().lower() == DEFAULT_TUI_WORK_MODE:
        return DEFAULT_PROJECT_ID_CODE
    return DEFAULT_PROJECT_ID_WORK
