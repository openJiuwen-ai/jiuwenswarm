"""Xiaoyi-private invocation, Device RPC, and model-trace adapters."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from jiuwenswarm.common.device_rpc.models import DeviceCommandContext
from jiuwenswarm.common.invocation_context import TRACE_HEADER_EXPORTER_METADATA_KEY
from jiuwenswarm.common.invocation_context.models import (
    TRACE_CONTEXT_VERSION,
    InvocationContext,
    TraceContext,
)
from jiuwenswarm.common.invocation_context.runtime import get_current_invocation_context
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.invocation_context.model_trace import register_trace_header_exporter


XIAOYI_INVOCATION_EXTENSION_KEY = "jiuwenswarm.xiaoyi_invocation"

# x-hag-trace-id 头值上限：celia sse-api 模型网关拒绝 >64 字符（回 data: {"error":{}}
# 空错误帧，对话空返回）。trace 核心段 = `${sessionId}&${interactionId 短码}`，
# 构造统一收口在 common.invocation_context.billing_trace.build_billing_core
# （上限 45，超长先截 session 段保 interaction 短码）；模型调用携带的
# x-hag-trace-id 与计费上报（task/status/update）完全同值（裸核心段，2026-09-02 起
# 无 xiaoyi-work-* 前缀；与桌面端 billing-service.ts coreTraceId 同口径）。
from jiuwenswarm.common.invocation_context.billing_trace import (
    MAX_CORE_LEN,
    MAX_TRACE_ID_LEN,
    build_billing_core,
)


def build_trace_id(session_id: str, interaction_id: str) -> str:
    """构造 x-hag-trace-id 核心段：sessionId&interactionId 短码（上限 45，
    超长先截 session 段保 interaction 短码；与桌面 coreTraceId 同口径）。"""
    return build_billing_core(session_id, interaction_id)


def request_billing_trace_id(request: AgentRequest) -> str:
    """本轮计费/模型 x-hag-trace-id 核心段；未注入 TraceContext 时返回空串。

    桌面带 ``metadata.interaction_id`` → ``session&短码``；xiaoyi 渠道 →
    ``task_id[:45]``；cron → ``cron_{run_id}``。调用方需要日志/出网回退时
    再用 ``request.request_id``。
    """
    trace = build_xiaoyi_trace_context(request)
    if trace is None:
        return ""
    return str(trace.trace_id or "").strip()


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _copy_mapping(value: Any) -> dict[str, Any] | None:
    return copy.deepcopy(dict(value)) if isinstance(value, dict) else None


@dataclass(frozen=True, slots=True)
class XiaoyiInvocationExtension:
    root_session_id: str | None = None
    params_session_id: str | None = None
    task_id: str | None = None
    message_id: str | None = None
    device_id: str | None = None
    scheduled_device: dict[str, Any] | None = None
    cron: dict[str, Any] | None = None
    app_id: str | None = None
    binding_id: str | None = None


def build_xiaoyi_invocation_extension(request: AgentRequest) -> dict[str, Any]:
    """Extract Xiaoyi routing data into an opaque InvocationContext extension."""
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    params = request.params if isinstance(request.params, dict) else {}
    cron = _copy_mapping(metadata.get("cron")) or _copy_mapping(params.get("cron"))
    extension = XiaoyiInvocationExtension(
        root_session_id=_first_text(
            metadata.get("xiaoyi_root_session_id"),
            metadata.get("xiaoyi_session_id"),
            request.chat_id,
        ),
        params_session_id=_first_text(
            metadata.get("xiaoyi_params_session_id"),
            params.get("xiaoyi_session_id"),
            params.get("session_id"),
        ),
        task_id=_first_text(metadata.get("xiaoyi_task_id"), params.get("task_id"), request.request_id),
        message_id=_first_text(metadata.get("xiaoyi_rpc_id"), metadata.get("xiaoyi_message_id")),
        device_id=_first_text(metadata.get("xiaoyi_device_id"), metadata.get("device_id")),
        scheduled_device=_copy_mapping(metadata.get("scheduled_device")),
        cron=cron,
        app_id=_first_text(metadata.get("app_id")),
        binding_id=_first_text(metadata.get("binding_id")),
    )
    has_xiaoyi_metadata = any(
        isinstance(key, str) and key.startswith("xiaoyi_") for key in metadata
    )
    if not (
        str(request.channel_id or "").strip().lower() in {"xiaoyi", "__cron__"}
        or has_xiaoyi_metadata
        or extension.scheduled_device is not None
        or extension.cron is not None
    ):
        return {}
    return {
        XIAOYI_INVOCATION_EXTENSION_KEY: {
            "root_session_id": extension.root_session_id,
            "params_session_id": extension.params_session_id,
            "task_id": extension.task_id,
            "message_id": extension.message_id,
            "device_id": extension.device_id,
            "scheduled_device": copy.deepcopy(extension.scheduled_device),
            "cron": copy.deepcopy(extension.cron),
            "app_id": extension.app_id,
            "binding_id": extension.binding_id,
        },
        TRACE_HEADER_EXPORTER_METADATA_KEY: "xiaoyi",
    }


def _xiaoyi_extension_from_metadata(metadata: dict[str, Any]) -> XiaoyiInvocationExtension | None:
    raw = metadata.get(XIAOYI_INVOCATION_EXTENSION_KEY)
    if not isinstance(raw, dict):
        return None
    return XiaoyiInvocationExtension(
        root_session_id=_first_text(raw.get("root_session_id")),
        params_session_id=_first_text(raw.get("params_session_id")),
        task_id=_first_text(raw.get("task_id")),
        message_id=_first_text(raw.get("message_id")),
        device_id=_first_text(raw.get("device_id")),
        scheduled_device=_copy_mapping(raw.get("scheduled_device")),
        cron=_copy_mapping(raw.get("cron")),
        app_id=_first_text(raw.get("app_id")),
        binding_id=_first_text(raw.get("binding_id")),
    )


def get_xiaoyi_invocation_extension(
    invocation: InvocationContext,
) -> XiaoyiInvocationExtension | None:
    """Read a validated Xiaoyi extension without exposing it to common code."""
    metadata = invocation.metadata if isinstance(invocation.metadata, dict) else {}
    return _xiaoyi_extension_from_metadata(metadata)


def get_xiaoyi_request_extension(request: AgentRequest) -> XiaoyiInvocationExtension | None:
    """Parse one inbound Xiaoyi request without leaking its schema to callers."""
    return _xiaoyi_extension_from_metadata(build_xiaoyi_invocation_extension(request))


def build_xiaoyi_trace_context(request: AgentRequest) -> TraceContext | None:
    """Convert Xiaoyi task/cron identity to the public trace contract."""
    # 桌面计费链路（优先于渠道分支）：客户端在 metadata.interaction_id 显式下发
    # 本轮交互 id 时，trace 核心段固定为 sessionId&interactionId 短码——与端侧计费
    # 状态上报（fulfillment task/status/update NEW→FINISH/FAILED）的
    # x-hag-trace-id 完全同值；HITL 续跑 request_id 变更但 interaction_id 不变，
    # 本轮所有模型调用的 x-hag-trace-id 全程稳定。
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    session_id = _first_text(request.session_id)
    explicit_interaction_id = _first_text(metadata.get("interaction_id"))
    if session_id and explicit_interaction_id:
        return TraceContext(
            version=TRACE_CONTEXT_VERSION,
            trace_id=build_trace_id(session_id, explicit_interaction_id),
            conversation_id=session_id,
            interaction_id=explicit_interaction_id,
        )
    extension = get_xiaoyi_request_extension(request)
    if extension is None:
        return None
    cron = extension.cron or {}
    job_id = _first_text(cron.get("job_id"))
    run_id = _first_text(cron.get("run_id"))
    if str(request.request_id or "").startswith("cron-") and job_id and run_id:
        encoded_job_id = quote(job_id, safe="")
        encoded_run_id = quote(run_id, safe="")
        return TraceContext(
            version=TRACE_CONTEXT_VERSION,
            trace_id=f"cron_{encoded_run_id}",
            conversation_id=encoded_job_id,
            interaction_id=encoded_run_id,
        )
    task_id = extension.task_id
    if not task_id:
        return None
    parts = task_id.split("&")
    return TraceContext(
        version=TRACE_CONTEXT_VERSION,
        # xiaoyi 渠道 task_id 本身即复合形态（session&task&…），保留原串；
        # 按计费核心段上限 45 截断
        trace_id=task_id[:MAX_CORE_LEN],
        conversation_id=parts[0].strip() or None,
        interaction_id=parts[1].strip() if len(parts) > 1 and parts[1].strip() else None,
    )


def build_xiaoyi_device_command_context(
    invocation: InvocationContext,
) -> DeviceCommandContext:
    """Adapt the private Xiaoyi extension to the unchanged Device RPC schema."""
    extension = get_xiaoyi_invocation_extension(invocation)
    metadata: dict[str, Any] = {"invocation_id": invocation.invocation_id}
    if extension is not None:
        for key, value in (("app_id", extension.app_id), ("binding_id", extension.binding_id)):
            if value:
                metadata[key] = value
        if extension.scheduled_device is not None:
            metadata["scheduled_device"] = copy.deepcopy(extension.scheduled_device)
        if extension.cron is not None:
            metadata["cron"] = copy.deepcopy(extension.cron)
    return DeviceCommandContext(
        source_request_id=invocation.request_id,
        channel_id=invocation.channel_id,
        jiuwen_session_id=invocation.session_id,
        xiaoyi_root_session_id=extension.root_session_id if extension else None,
        xiaoyi_params_session_id=extension.params_session_id if extension else None,
        xiaoyi_task_id=extension.task_id if extension else None,
        xiaoyi_rpc_id=extension.message_id if extension else None,
        metadata=metadata,
    )


class XiaoyiTraceHeaderExporter:
    """Export the Xiaoyi model header allow-list from a public TraceContext."""

    def supports(self, invocation: InvocationContext) -> bool:
        return get_xiaoyi_invocation_extension(invocation) is not None

    def export(self, trace: TraceContext) -> dict[str, str]:
        headers = {"x-hag-trace-id": trace.trace_id}
        if trace.conversation_id:
            headers["x-session-id"] = trace.conversation_id
        if trace.interaction_id:
            headers["x-interaction-id"] = trace.interaction_id
        return headers


class DesktopTraceHeaderExporter:
    """桌面/通用渠道兜底导出：无 Xiaoyi 扩展但 invocation 带 trace（计费链路）。

    与 XiaoyiTraceHeaderExporter 互斥（supports 条件互补），导出同形态头：
    x-hag-trace-id = sessionId&interactionId 短码，与端侧计费上报同核心段。
    """

    def supports(self, invocation: InvocationContext) -> bool:
        return (
            invocation.trace is not None
            and get_xiaoyi_invocation_extension(invocation) is None
        )

    def export(self, trace: TraceContext) -> dict[str, str]:
        headers = {"x-hag-trace-id": trace.trace_id}
        if trace.conversation_id:
            headers["x-session-id"] = trace.conversation_id
        if trace.interaction_id:
            headers["x-interaction-id"] = trace.interaction_id
        return headers


_XIAOYI_TRACE_HEADER_EXPORTER = XiaoyiTraceHeaderExporter()
_DESKTOP_TRACE_HEADER_EXPORTER = DesktopTraceHeaderExporter()
register_trace_header_exporter("xiaoyi", _XIAOYI_TRACE_HEADER_EXPORTER)
register_trace_header_exporter("desktop", _DESKTOP_TRACE_HEADER_EXPORTER)


def get_xiaoyi_trace_header_exporters() -> tuple[XiaoyiTraceHeaderExporter | DesktopTraceHeaderExporter, ...]:
    # 顺序安全：两个 exporter 的 supports 互斥（有/无 Xiaoyi 扩展）
    return (_XIAOYI_TRACE_HEADER_EXPORTER, _DESKTOP_TRACE_HEADER_EXPORTER)


def export_xiaoyi_trace_headers(trace: TraceContext | None) -> dict[str, str]:
    return _XIAOYI_TRACE_HEADER_EXPORTER.export(trace) if trace is not None else {}


def export_current_xiaoyi_trace_headers() -> dict[str, str]:
    invocation = get_current_invocation_context()
    if invocation is None or invocation.trace is None:
        return {}
    return (
        _XIAOYI_TRACE_HEADER_EXPORTER.export(invocation.trace)
        if _XIAOYI_TRACE_HEADER_EXPORTER.supports(invocation)
        else {}
    )


__all__ = [
    "XIAOYI_INVOCATION_EXTENSION_KEY",
    "DesktopTraceHeaderExporter",
    "XiaoyiInvocationExtension",
    "XiaoyiTraceHeaderExporter",
    "build_xiaoyi_device_command_context",
    "build_xiaoyi_invocation_extension",
    "build_xiaoyi_trace_context",
    "request_billing_trace_id",
    "export_current_xiaoyi_trace_headers",
    "export_xiaoyi_trace_headers",
    "get_xiaoyi_invocation_extension",
    "get_xiaoyi_request_extension",
    "get_xiaoyi_trace_header_exporters",
]
