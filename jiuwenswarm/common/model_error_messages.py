# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""模型调用底层异常 → 用户能直接照做的提示。

配置页「测试连接」此前把 ``str(exc)`` 原样回传前端，账户欠费时用户看到的是

    [181001] model call failed, reason: openAI API async invoke error:
    APIStatusError: Error code: 402 - {'error': {'message':
    'Insufficient Balance (request_id: ...)' ...}}

这类内部包装文本：既读不懂，也不知道该做什么。本模块在**错误出口**做一次归一化：

* ``humanize_model_error`` 只翻译**能确定的**服务商错误（欠费、鉴权、限流、
  模型不存在、网络不通、服务端故障），无法识别时返回 ``None``，让调用方保留
  自己更精准的原有逻辑；
* ``user_facing_model_error`` 是配置页等直接面向用户的出口：已知错误给提示，
  未知但明显过长的内部报错收敛成一句可读提示，短错误保持原样；
* ``is_non_retryable_model_error`` 供探测函数判断「不必再试一次」的永久错误。

完整技术细节由调用方继续写日志，本模块只负责「给用户看的那句话」。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

__all__ = [
    "BALANCE_ERROR_CODE",
    "humanize_model_error",
    "is_non_retryable_model_error",
    "user_facing_model_error",
]

BALANCE_ERROR_CODE = "INSUFFICIENT_BALANCE"

# 集中声明阈值与外部入口，避免判定逻辑里散落魔法数字和硬编码链接。
_MAX_ERROR_TEXT_CHARS = 6000
_MAX_CAUSE_DEPTH = 4
_MAX_RAW_ERROR_CHARS = 240
_DEEPSEEK_TOPUP_URL = "https://platform.deepseek.com/top_up"
_OPENAI_BILLING_URL = "https://platform.openai.com/settings/billing"

_BALANCE_MARKERS = (
    "insufficient balance",
    "insufficient_balance",
    "insufficient quota",
    "insufficient_quota",
    "account balance is not enough",
    "payment required",
    "billing hard limit",
    "余额不足",
    "余额已用完",
    "欠费",
    "请先充值",
    "需要充值",
)
_AUTH_MARKERS = (
    "authentication fails",
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "unauthorized",
    "access denied",
    "authentication error",
    "api key 无效",
    "密钥无效",
    "认证失败",
    "鉴权失败",
)
_MODEL_MARKERS = (
    "model not found",
    "model_not_found",
    "unknown model",
    "invalid model",
    "no such model",
    "model does not exist",
    "model does not support",
    "模型不存在",
    "模型名称错误",
)
_RATE_MARKERS = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "freeusagelimiterror",
    "quota exceeded",
    "concurrency limit",
    "请求过于频繁",
    "额度已用完",
)
_NETWORK_MARKERS = (
    "connection refused",
    "connection reset",
    "connection error",
    "connecterror",
    "could not connect",
    "name or service not known",
    "nodename nor servname",
    "network is unreachable",
    # 用具体的超时说法，避免把 "process CLI execution timed out" 这类
    # 与模型无关的错误也归成网络故障。
    "connection timed out",
    "request timed out",
    "read timed out",
    "apitimeouterror",
    "read timeout",
    "connect timeout",
    "ssl:",
    "certificate verify failed",
    "无法连接",
    "连接失败",
)
_SERVER_MARKERS = (
    "server error",
    "service unavailable",
    "overloaded",
    "bad gateway",
    "服务器繁忙",
    "服务暂时不可用",
)

# 分类规则表：(类别, 关键字, 状态码, 状态码命中时需同时出现的关键字)。
# 顺序即优先级，越具体的类别越靠前。
_CLASSIFY_RULES = (
    ("balance", _BALANCE_MARKERS, (402,), None),
    ("auth", _AUTH_MARKERS, (401, 403), None),
    ("model", _MODEL_MARKERS, (404,), "model"),
    ("rate", _RATE_MARKERS, (429,), None),
    ("network", _NETWORK_MARKERS, (), None),
    ("server", _SERVER_MARKERS, (500, 502, 503, 504), None),
)

_BALANCE_MESSAGE = (
    "模型账户余额不足，服务商已拒绝本次调用（HTTP 402）。"
    "请先到模型服务商平台充值；充值完成后无需修改当前配置，直接重新测试即可。"
)
_AUTH_MESSAGE = (
    "API Key 无效或已被撤销，也可能是该账户无权访问此模型。"
    "请检查密钥是否填写正确、是否过期或已被删除。"
)
_MODEL_MESSAGE = (
    "模型名称不存在，或当前账户无权使用该模型。"
    "请核对模型名（注意大小写与版本号），必要时先在服务商控制台开通该模型。"
)
_RATE_MESSAGE = (
    "模型服务请求过于频繁，或已达并发/额度上限，请稍后再试。"
    "若使用的是免费额度，请等待额度刷新。"
)
_NETWORK_MESSAGE = (
    "无法连接到模型服务，请检查 API 地址、网络连通性与代理设置；"
    "若使用自建服务，请确认服务已启动且端口可访问。"
)
_SERVER_MESSAGE = (
    "模型服务商暂时不可用，请稍后重试；若持续失败，请查看服务商的状态页。"
)
_INTERNAL_MESSAGE = (
    "模型调用失败，请检查 API 地址、API Key 与模型名称是否正确；详细原因已写入日志。"
)


def _flatten_error(error: Any) -> str:
    """收集异常自身、cause 链与常见详情字段的文本，统一小写便于匹配。"""
    parts: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if value is None:
            return
        text = value if isinstance(value, str) else str(value)
        if text and text not in seen:
            seen.add(text)
            parts.append(text)

    add(error)
    current = error
    for _ in range(_MAX_CAUSE_DEPTH):
        cause = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        if cause is None:
            break
        add(cause)
        current = cause
    if isinstance(error, BaseException):
        for attr in ("body", "message", "detail", "response"):
            try:
                add(getattr(error, attr, None))
            except (AttributeError, TypeError, ValueError, RuntimeError):
                # SDK 详情字段可能由懒加载触发异常；跳过该字段即可，
                # 异常文本与其余字段仍会参与分类，不吞掉调用方的真实故障。
                continue
    return " \n".join(parts)[:_MAX_ERROR_TEXT_CHARS].lower()


def _has_status(text: str, code: int) -> bool:
    """只在带明确状态语义的前缀后认状态码，避免误命中 request_id 里的数字。"""
    return bool(
        re.search(
            rf"(?:error\s*code|status[_\s]*code|http\s*status|http)\s*[:=]?\s*{code}\b",
            text,
        )
    )


def _status_matches(text: str, codes: tuple[int, ...], requires: str | None) -> bool:
    """状态码命中判定；``requires`` 非空时还需文本中出现该关键字。"""
    if requires and requires not in text:
        return False
    return any(_has_status(text, code) for code in codes)


def _classify(text: str) -> str | None:
    """按规则表顺序返回错误类别；无法识别时返回 ``None``。"""
    for kind, markers, codes, requires in _CLASSIFY_RULES:
        if any(marker in text for marker in markers):
            return kind
        if _status_matches(text, codes, requires):
            return kind
    return None


def _balance_provider_hint(api_base: str) -> str:
    """针对已知服务商给出充值入口，未知服务商回显其域名便于定位。"""
    try:
        host = (urlparse(str(api_base or "")).hostname or "").lower()
    except ValueError:
        # 畸形 URL（如非法 IPv6 字面量）解析不出域名，回退到空域名即可。
        host = ""
    if "deepseek" in host:
        return f" DeepSeek 充值页：{_DEEPSEEK_TOPUP_URL}"
    if "openai.com" in host:
        return f" OpenAI 账单页：{_OPENAI_BILLING_URL}"
    if host:
        return f" 当前配置的服务商：{host}。"
    return ""


def humanize_model_error(error: Any, *, api_base: str = "") -> str | None:
    """把已识别的模型调用失败翻译成用户可读提示；无法识别时返回 ``None``。"""
    text = _flatten_error(error)
    if not text:
        return None
    kind = _classify(text)
    messages = {
        "balance": _BALANCE_MESSAGE + _balance_provider_hint(api_base),
        "auth": _AUTH_MESSAGE,
        "model": _MODEL_MESSAGE,
        "rate": _RATE_MESSAGE,
        "network": _NETWORK_MESSAGE,
        "server": _SERVER_MESSAGE,
    }
    # 类别由规则表生成，仍用 get() 取值：日后新增类别却漏配提示时返回 None，
    # 让调用方保留原始错误处理，而不是抛出 KeyError 打断用户提示。
    return messages.get(kind) if kind else None


def user_facing_model_error(
    error: Any,
    *,
    api_base: str = "",
    fallback: str = "LLM request failed",
) -> str:
    """错误出口统一入口：已知错误给友好提示，未知长报错收敛，短错误保持原样。"""
    friendly = humanize_model_error(error, api_base=api_base)
    if friendly:
        return friendly
    raw = "" if error is None else str(error).strip()
    if not raw:
        return fallback
    if len(raw) > _MAX_RAW_ERROR_CHARS or "\n" in raw:
        return _INTERNAL_MESSAGE
    return raw


def is_non_retryable_model_error(error: Any) -> bool:
    """永久性错误（欠费/鉴权/模型不存在）不值得换 max_tokens 再试一次。"""
    text = _flatten_error(error)
    if not text:
        return False
    return _classify(text) in {"balance", "auth", "model"}
