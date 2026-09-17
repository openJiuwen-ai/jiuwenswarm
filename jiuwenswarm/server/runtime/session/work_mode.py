"""工作模式（work_mode）高层 helper。

提供 ``common.work_mode`` 之上需要 ``channel_id`` / 请求 ``params`` 上下文的 helper:
通道默认推断、严格请求解析、旧项目兜底推断、``session.create`` 参数归一化。

设计要点:本模块只做参数解析与归一化,**不反查 ProjectStore**;
Project 存在性与 ``work_mode`` 一致性校验由调用方通过
``ProjectStore.resolve_session_project_binding`` 完成。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jiuwenswarm.common.work_mode import (
    DEFAULT_PROJECT_ID_CODE,
    DEFAULT_PROJECT_ID_WORK,
    DEFAULT_PROJECT_IDS,
    DEFAULT_TUI_WORK_MODE,
    DEFAULT_WEB_WORK_MODE,
    SUPPORTED_WORK_MODES,
    is_default_project_id,
    normalize_work_mode,
    resolve_default_project_id,
)

# 重导出底层常量与函数,供外部调用方保持单一 import 来源
__all__ = [
    "DEFAULT_WEB_WORK_MODE",
    "DEFAULT_TUI_WORK_MODE",
    "DEFAULT_PROJECT_ID_WORK",
    "DEFAULT_PROJECT_ID_CODE",
    "DEFAULT_PROJECT_IDS",
    "SUPPORTED_WORK_MODES",
    "normalize_work_mode",
    "is_default_project_id",
    "resolve_default_project_id",
    "default_work_mode_for_channel",
    "resolve_request_work_mode",
    "infer_legacy_project_work_mode",
    "SessionWorkModeParams",
    "resolve_session_work_mode_params",
]


def default_work_mode_for_channel(channel_id: str | None) -> str:
    """按通道推断默认 ``work_mode``:``tui``→``code``,其他→``work``。"""
    if isinstance(channel_id, str) and channel_id.strip().lower() == "tui":
        return DEFAULT_TUI_WORK_MODE
    return DEFAULT_WEB_WORK_MODE


def infer_legacy_project_work_mode(raw_project: dict[str, Any]) -> str:
    """旧项目记录推断 ``work_mode``:有合法字段用之,否则回退 ``"work"``。"""
    if not isinstance(raw_project, dict):
        return DEFAULT_WEB_WORK_MODE
    return normalize_work_mode(raw_project.get("work_mode"), default=DEFAULT_WEB_WORK_MODE)


def resolve_request_work_mode(
    params: dict[str, Any],
    channel_id: str | None,
) -> tuple[str | None, str | None]:
    """从请求参数解析 ``work_mode``,严格校验。

    未传 / 显式 ``None`` / 空串 → 按通道默认推断;
    非法值 → ``(None, "BAD_REQUEST")``,公开接口不得静默回落。
    """
    if not isinstance(params, dict):
        return default_work_mode_for_channel(channel_id), None

    if "work_mode" not in params:
        return default_work_mode_for_channel(channel_id), None

    raw = params.get("work_mode")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default_work_mode_for_channel(channel_id), None

    if isinstance(raw, str) and raw.strip().lower() in SUPPORTED_WORK_MODES:
        return raw.strip().lower(), None

    return None, "BAD_REQUEST"


@dataclass
class SessionWorkModeParams:
    """``session.create`` 参数归一化结果(纯归一化,非最终归属绑定)。

    真实 ``project_id`` 仅透传请求/通道推断的 ``work_mode``,最终值以
    ProjectStore 查到的 Project 为准;默认项目按 channel_id 推断并映射到
    ``default`` / ``default_code``。失败时调用方必须优先检查 ``error``/``code``。

    ``has_explicit_work_mode`` 标识请求是否显式传入 ``work_mode``:True 时
    调用方需在命中真实 Project 后做一致性校验(不一致 → BAD_REQUEST)。
    """

    project_id: str
    project_dir: str
    work_mode: str
    error: str | None = None
    code: str | None = None
    has_explicit_work_mode: bool = False


def resolve_session_work_mode_params(
    params: dict[str, Any],
    *,
    channel_id: str | None,
) -> SessionWorkModeParams:
    """为 ``session.create`` 归一化 ``project_id`` / ``project_dir`` / ``work_mode``。

    纯参数归一化,不反查 ProjectStore:
    - 真实 ``project_id``:透传三元组,由调用方做存在性与一致性校验。
    - 默认项目:按 channel_id 推断 ``work_mode`` 并映射到 ``default``/``default_code``。

    始终以服务端传入的 channel_id 为准,不再信任 ``params.channel_id``
    (防 Web 客户端伪装 TUI 通道获得 code 模式会话,属功能面越权)。
    """
    raw_wm_for_explicit = params.get("work_mode") if isinstance(params, dict) else None
    has_explicit_work_mode = (
        isinstance(raw_wm_for_explicit, str) and raw_wm_for_explicit.strip() != ""
    )

    work_mode, mode_error = resolve_request_work_mode(params, channel_id)
    if mode_error is not None:
        return SessionWorkModeParams(
            project_id="",
            project_dir="",
            work_mode="",
            error=f"invalid work_mode: {params.get('work_mode')!r}",
            code=mode_error,
        )

    raw_project_id = params.get("project_id") if isinstance(params, dict) else None
    raw_project_dir = params.get("project_dir") if isinstance(params, dict) else None

    project_id = raw_project_id.strip() if isinstance(raw_project_id, str) else ""
    project_dir = raw_project_dir.strip() if isinstance(raw_project_dir, str) else ""

    if not project_id or project_id in DEFAULT_PROJECT_IDS:
        normalized_default_id = resolve_default_project_id(work_mode)
        if project_id == DEFAULT_PROJECT_ID_WORK:
            # project_id="default" 明确指向 work 模式;显式 work_mode 与之矛盾 → BAD_REQUEST
            if has_explicit_work_mode and work_mode != DEFAULT_WEB_WORK_MODE:
                return SessionWorkModeParams(
                    project_id="",
                    project_dir="",
                    work_mode="",
                    error=f"work_mode={work_mode!r} conflicts with project_id={project_id!r} (expected 'work')",
                    code="BAD_REQUEST",
                )
            work_mode = DEFAULT_WEB_WORK_MODE
            normalized_default_id = DEFAULT_PROJECT_ID_WORK
        elif project_id == DEFAULT_PROJECT_ID_CODE:
            if has_explicit_work_mode and work_mode != DEFAULT_TUI_WORK_MODE:
                return SessionWorkModeParams(
                    project_id="",
                    project_dir="",
                    work_mode="",
                    error=f"work_mode={work_mode!r} conflicts with project_id={project_id!r} (expected 'code')",
                    code="BAD_REQUEST",
                )
            work_mode = DEFAULT_TUI_WORK_MODE
            normalized_default_id = DEFAULT_PROJECT_ID_CODE
        # 保留 project_dir:默认项目无 project_dir,但调用方传入的 project_dir 需透传给
        # resolve_session_project_binding,由其拒绝"仅传 project_dir 无真实 project_id"的请求
        return SessionWorkModeParams(
            project_id=normalized_default_id,
            project_dir=project_dir,
            work_mode=work_mode,
            has_explicit_work_mode=has_explicit_work_mode,
        )

    return SessionWorkModeParams(
        project_id=project_id,
        project_dir=project_dir,
        work_mode=work_mode,
        has_explicit_work_mode=has_explicit_work_mode,
    )
