# coding: utf-8
"""错误文本归一化：后端异常类型/消息丢失时仍给客户端非空错误。

Python 的 ``TimeoutError()``、无参自定义异常、部分 ``asyncio.TimeoutError``
的 ``str(exc)`` 是空串。直接 ``"error": str(exc)`` 会让前端把“失败但无详情”
误判成“没有实时错误”，进而回退历史记录或把回合收成已完成。
本模块只做无副作用的文本兜底，供 chat.error / terminal status 共用。
"""

from __future__ import annotations

from typing import Any

# 后端未给出任何异常消息时的技术兜底（前端有用户向映射，不直接展示英文细节）
DEFAULT_ERROR_FALLBACK = "internal error (no exception message)"
DEFAULT_LEADER_ERROR_FALLBACK = "Leader model call failed (no error detail)"


def non_empty_text(*values: Any, fallback: str = DEFAULT_ERROR_FALLBACK) -> str:
    """返回第一个 trim 后非空的字符串；全部为空时返回 fallback。"""
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if text:
            return text
    return fallback


def leader_error_text(error: Any) -> str:
    """leader 未恢复错误史：空串必须落成非空兜底（终态 error 字段才不会被省略）。"""
    return non_empty_text(error, fallback=DEFAULT_LEADER_ERROR_FALLBACK)


def exception_text(exc: BaseException, *, fallback: str = DEFAULT_ERROR_FALLBACK) -> str:
    """异常 → 非空错误文本。

    - 有消息：原样返回（保留开发者级原文，前端负责用户向映射）；
    - 无消息：返回 ``<异常类名>: <fallback>``，至少保留异常类型，便于日志/前端
      识别为超时、数据库锁等而不是“未知空错误”。
    """
    text = str(exc).strip()
    if text:
        return text
    return f"{type(exc).__name__}: {fallback}"


def chat_error_payload(
    exc: BaseException,
    *,
    phase: str = "",
    fallback: str = DEFAULT_ERROR_FALLBACK,
) -> dict[str, Any]:
    """构造带异常类型/阶段的 chat.error payload（保留 error 兼容旧前端）。"""
    payload: dict[str, Any] = {
        "event_type": "chat.error",
        "error": exception_text(exc, fallback=fallback),
        "error_type": type(exc).__name__,
    }
    if phase:
        payload["phase"] = phase
    return payload