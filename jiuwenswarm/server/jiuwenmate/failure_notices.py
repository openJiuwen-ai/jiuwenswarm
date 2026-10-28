"""Safe host-owned status messages, never raw exception bodies or model outputs."""

from __future__ import annotations

import re

_EXCEPTION_TYPE = re.compile(
    r"\b(UnicodeDecodeError|TimeoutError|ReadTimeout|ConnectTimeout|APIConnectionError|"
    r"RateLimitError|AuthenticationError|PermissionError|FileNotFoundError|RuntimeError|"
    r"ValueError|TypeError|ValidationError|FrameworkError|MemoryError|OSError)\b"
)


def failure_notice_text(request_id: str, *, reason: str, detail: str | None) -> str:
    """Classify diagnostics without broadcasting credentials, paths or provider bodies."""
    detail = detail or ""
    match = _EXCEPTION_TYPE.search(detail)
    error_type = match.group(1) if match else None
    if reason == "budget_denied":
        summary = "执行额度不足或预算检查未通过。"
    elif reason == "context_unfit":
        summary = "上下文无法安全整理到允许的窗口大小。"
    elif reason == "missing_terminal":
        summary = "执行流意外结束，没有收到终态结果。"
    elif "UnicodeDecodeError" in detail:
        summary = "模型响应解码失败（UnicodeDecodeError），返回数据无法按预期的文本编码读取。"
    elif error_type in {"TimeoutError", "ReadTimeout", "ConnectTimeout"}:
        summary = f"服务请求超时（{error_type}）。"
    elif error_type == "APIConnectionError":
        summary = "模型服务连接失败（APIConnectionError）。"
    elif error_type == "RateLimitError":
        summary = "模型服务限流或额度不足（RateLimitError）。"
    elif error_type == "AuthenticationError":
        summary = "模型服务认证失败（AuthenticationError）。"
    elif error_type:
        summary = f"执行发生内部异常（{error_type}）。"
    else:
        summary = "执行发生异常，详细原因请按请求编号查看服务日志。"
    status = "任务已暂停，未能正常完成。" if reason in {"budget_denied", "context_unfit"} else "任务因异常中断，未能正常完成。"
    return (
        f"{status}\n"
        f"原因：{summary}\n"
        "已执行的操作不代表已回滚，请核实后再决定是否继续。\n"
        f"请求编号：{request_id}"
    )
