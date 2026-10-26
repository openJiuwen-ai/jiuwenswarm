# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""行为审计（UA/EVT）——gateway / agentserver 统一打点入口。

字段语义映射、session 注册表、OTLP emit 全部在 foundation 审计 SDK
（以别名 ``audit`` 引用，目录改名只改一处 import）；
本模块只做 jiuwenswarm 接线：

- :func:`audit_claw_log` —— 唯一打点入口（SUBMDL 区分来源；永不抛异常）
- :func:`bind_audit_routing` —— 请求入口路由绑定（interface_deep 调用）
- :func:`install_audit_middleware` —— M1 gateway web 全局 HTTP 审计中间件

个人版：打点入口 no-op（``is_enterprise`` 门控）；异常只记 warning，不影响业务。
无 audit_log_config 库配置时：写出关闭（``is_audit_config_enabled``），直至落盘并 apply。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from openjiuwen_runtime.foundation import audit

from jiuwenswarm.common.audit_gate import is_audit_config_enabled
from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.extensions.identity_provider import IdentityStore
from jiuwenswarm.telemetry import metrics as telemetry_metrics

logger = logging.getLogger(__name__)

# 规范 SUBMDL 取值（挂点表）——与 foundation audit 保持一致
SUBMDL_GATEWAY = "gateway"
SUBMDL_AGENT = "agent"
SUBMDL_API_CLIENT = "api_client"
SUBMDL_FILE = "file"
SUBMDL_ALERT = "alert"
SUBMDL_SANDBOX = "sandbox"

__all__ = [
    "audit_claw_log",
    "bind_audit_routing",
    "install_audit_middleware",
]


def bind_audit_routing(request: Any) -> None:
    """请求入口绑定路由三元组（interface_deep 等入口调用；个人版 no-op）。"""
    if not is_enterprise():
        return
    try:
        # 函数内 import：gateway.__init__ → message_handler → 本模块（模块级
        # import 会在半初始化状态下形成循环，详见挂点接线说明）。
        from jiuwenswarm.gateway.cron.enterprise_gate import extract_routing_triple

        group_id, bot_id, user_id = extract_routing_triple(request)
        metadata = getattr(request, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        audit.bind_routing(
            session_id=str(getattr(request, "session_id", None) or ""),
            user_id=user_id or "",
            request_id=str(getattr(request, "request_id", None) or ""),
            group_id=group_id or "",
            bot_id=bot_id or "",
            channel_id=str(
                getattr(request, "channel_id", None)
                or metadata.get("channel_id")
                or ""
            ),
        )
    except Exception as exc:
        logger.debug("[audit] bind_audit_routing skipped: %s", exc)


def _request_context() -> dict[str, str]:
    """请求上下文：SDK ContextVar 快照 + session 注册表回退。"""
    ctx: dict[str, str] = dict(audit.audit_context_snapshot())
    try:
        for key in ("session_id", "channel_id"):
            if not ctx.get(key):
                value = getattr(telemetry_metrics, f"metrics_{key}", None)
                ctx[key] = str(value.get() or "") if value else ""
    except Exception as exc:
        logger.warning("[audit] metrics session/channel fallback failed: %s: %s", type(exc).__name__, exc)
    if not ctx.get("user_id"):
        try:
            identity = IdentityStore.get_identity()
            ctx["user_id"] = (
                str(getattr(identity, "user_id", "") or "") if identity else ""
            )
        except Exception as exc:
            logger.warning("[audit] IdentityStore user_id fallback failed: %s: %s", type(exc).__name__, exc)
    routing = audit.lookup_routing(ctx.get("session_id", ""))
    for key in ("request_id", "bot_id", "group_id", "user_id", "channel_id"):
        ctx.setdefault(key, routing.get(key, ""))
    return ctx


def audit_claw_log(
    *,
    submdl: str,
    proc: str,
    success: bool = True,
    uid: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
    rspcd: str | None = None,
    desc: str | None = None,
    error: str = "",
    message: str = "",
    level: str | None = None,
    extra: dict[str, Any] | None = None,
    caller: str | None = None,
    **details: Any,
) -> None:
    """打一条 UA/EVT 审计事件（语义映射由 SDK ``log_event`` 完成）。

    Args:
        submdl: 子模块（gateway / agent / sandbox / api_client / file / alert）。
        proc: 过程名（挂点表，如 ws_resolve_identity / write_file / create_sandbox）。
        success: True → UA（成功正常），False → EVT（失败/违规/异常/告警）。
        uid / session_id / request_id 缺省走请求上下文回退；映射进 UID / trace_id / txn_seq。
        rspcd 缺省 UA→0000。
        desc: 与关键字同名的文案；error/message: 失败原因与补充信息。
        extra / **details: 附加属性（如 origin/path/sandbox_id）；勿放 session_id/request_id。

    跨组件：打点传入 ``DSTIP`` 时，自动补 ``SRCIP``=本端缓存（显式 SRCIP 优先）。
    """
    try:
        if not is_enterprise():
            return
        if not is_audit_config_enabled():
            return
        ctx = _request_context()
        merged_extra: dict[str, Any] = {
            "agent_pod": os.getenv("HOSTNAME", ""),
            **(extra or {}),
            **details,
        }
        # 桥接键只作映射入参，禁止经 extra 原名写出。
        for bridge in (
            "session_id",
            "request_id",
            "user_id",
            "bot_id",
            "group_id",
            "channel_id",
        ):
            merged_extra.pop(bridge, None)
        try:
            from jiuwenswarm.common.audit_net import enrich_extra_with_hop_ips

            enrich_extra_with_hop_ips(merged_extra, ctx)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[audit] hop ip enrich skipped: %s", exc)
        # 走 log_audit 而不是 log_event：把 caller 作为显式字段传入，覆盖 SDK
        # 内部栈回溯（否则 caller 恒为 audit.audit_claw_log）。
        event_type = "UA" if success else "EVT"
        level_norm = (level or ("INFO" if success else "WARN")).upper()
        text = desc or message or f"{submdl}.{proc} {'成功' if success else '失败'}"
        resolved_uid = (str(uid).strip() if uid else "") or ctx.get("user_id", "")
        resolved_session = (
            str(session_id).strip() if session_id else ""
        ) or ctx.get("session_id", "")
        resolved_request = (
            str(request_id).strip() if request_id else ""
        ) or ctx.get("request_id", "")
        from jiuwenswarm.common.audit_emit import capture_audit_caller

        audit.log_audit(
            event_type,
            level=level_norm,
            SUBMDL=submdl,
            PROC=proc,
            RESULT="success" if success else "fail",
            MSG=error or message or "",
            UID=resolved_uid,
            RSPCD=rspcd if rspcd is not None else ("0000" if success else ""),
            session_id=resolved_session,
            request_id=resolved_request,
            user_id=resolved_uid,
            bot_id=ctx.get("bot_id", ""),
            group_id=ctx.get("group_id", ""),
            caller=caller or capture_audit_caller(),
            extra=merged_extra,
            **{event_type: text},
        )
    except Exception as exc:  # noqa: BLE001 — 审计不得影响业务路径
        logger.warning("[audit] audit_claw_log failed: %s: %s", type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# gateway web 全局 HTTP 审计中间件（web_http_app 一行注册）
# ---------------------------------------------------------------------------


def install_audit_middleware(app: Any) -> None:
    """给 gateway web HTTP 应用挂行为审计中间件（个人版不安装）。"""
    if not is_enterprise():
        return
    # 函数内 import：web_http_dispatch → invoke → 本模块（同上循环原因）。
    from jiuwenswarm.gateway.channel_manager.web.web_http_dispatch import (
        _trust_client_tenant_headers,
    )

    def _uid_extractor(scope: dict, headers: dict[str, str]) -> str | None:
        client = scope.get("client") or (None, None)
        if not _trust_client_tenant_headers(client[0]):
            return None
        return headers.get("x-user-id") or None

    def _ids_extractor(scope: dict, headers: dict[str, str]) -> dict[str, str]:
        return {
            "session_id": headers.get("x-session-id", ""),
            "request_id": headers.get("x-request-id", ""),
        }

    from jiuwenswarm.telemetry.http_audit import install_http_audit_middleware

    install_http_audit_middleware(
        app,
        emit=audit_claw_log,
        submdl="gateway",
        include_prefix=("/gateway-api/v1", "/file-api", "/share-api", "/api/v1", "/api/sessions"),
        exclude_prefixes=(
            "/api/v1/health",
            "/api/v1/connection/status",
            "/api/v1/cron/jobs",     # 前端高频轮询（nginx 已剥 /gateway-api 前缀）
            "/api/v1/projects",      # 页面加载批量拉取
        ),
        proc_map={
            "chat/completions": "chat_send",
            "file-api/upload": "file_upload",
            "file-api/download": "file_download",
        },
        default_proc="http_request",  # 未匹配 PROC 的请求 → http_request UA
        error_proc="web_http_internal_error",  # 5xx 边界 EVT（B #17）
        uid_extractor=_uid_extractor,
        ids_extractor=_ids_extractor,
    )
