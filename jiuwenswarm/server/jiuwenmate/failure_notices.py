"""Safe host-owned status messages, never raw exception bodies or model outputs."""

from __future__ import annotations

import re

_EXCEPTION_TYPE = re.compile(
    r"\b(UnicodeDecodeError|TimeoutError|ReadTimeout|ConnectTimeout|APIConnectionError|"
    r"RateLimitError|AuthenticationError)\b"
)


def failure_notice_text(request_id: str, *, reason: str, detail: str | None) -> str:
    """Classify diagnostics without broadcasting credentials, paths or provider bodies."""
    group_reasons = {
        "group_access_lost": "等待或恢复期间，员工的群访问权限已失效。",
        "group_access_unavailable": "当前无法确认员工的群访问权限。",
        "group_authorization_changed": "处理期间，员工的群访问权限或可读取的历史范围发生变化。",
        "group_trigger_out_of_scope": "原消息早于员工当前可读取的群历史范围，本次请求未继续执行。",
    }
    detail = detail or ""
    # Inspect known nested causes rather than stopping at a FrameworkError wrapper.
    error_types = set(_EXCEPTION_TYPE.findall(detail))
    normalized = detail.lower()
    if reason in group_reasons:
        summary = group_reasons[reason]
    elif reason == "budget_denied":
        summary = "执行额度不足或预算检查未通过。"
    elif reason == "context_unfit":
        summary = "上下文无法安全整理到允许的窗口大小。"
    elif reason == "missing_terminal":
        summary = "执行流意外结束，没有收到终态结果。"
    elif "model hit its output limit" in normalized:
        summary = "模型输出达到上限，未能生成完整回答。"
    elif "context_length_exceeded" in normalized or "maximum context length" in normalized:
        summary = "对话内容超过模型可处理的上下文范围。"
    elif "AuthenticationError" in error_types:
        summary = "模型服务认证失败。"
    elif "insufficient_balance" in normalized:
        summary = "模型服务余额不足。"
    elif "insufficient_quota" in normalized:
        summary = "模型服务可用额度不足。"
    elif "RateLimitError" in error_types:
        summary = "模型服务请求受限。"
    elif "ReadTimeout" in error_types:
        summary = "模型服务响应超时。"
    elif "ConnectTimeout" in error_types:
        summary = "模型服务请求超时。"
    elif "TimeoutError" in error_types:
        summary = "服务请求超时。"
    elif "APIConnectionError" in error_types:
        summary = "模型服务连接失败。"
    elif "UnicodeDecodeError" in error_types:
        summary = "模型响应解码失败。"
    else:
        summary = "执行发生内部异常。"
    return f"原因：{summary}\n请求编号：{request_id}"
