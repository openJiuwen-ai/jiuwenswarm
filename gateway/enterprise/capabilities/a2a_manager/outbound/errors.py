"""Stable errors exposed by the A2A outbound domain."""

from __future__ import annotations

from enum import Enum


class A2AOutboundErrorCode(str, Enum):
    DISCOVERY_URL_INVALID = "A2A_DISCOVERY_URL_INVALID"
    DISCOVERY_BLOCKED = "A2A_DISCOVERY_BLOCKED"
    CARD_FETCH_FAILED = "A2A_CARD_FETCH_FAILED"
    CARD_INVALID = "A2A_CARD_INVALID"
    DISCOVERY_NOT_FOUND = "A2A_DISCOVERY_NOT_FOUND"
    DISCOVERY_EXPIRED = "A2A_DISCOVERY_EXPIRED"
    AGENT_ALREADY_REGISTERED = "A2A_AGENT_ALREADY_REGISTERED"
    AGENT_NOT_REGISTERED = "A2A_AGENT_NOT_REGISTERED"
    AGENT_DISABLED = "A2A_AGENT_DISABLED"
    AGENT_UNAVAILABLE = "A2A_AGENT_UNAVAILABLE"
    AGENT_NOT_AUTHORIZED = "A2A_AGENT_NOT_AUTHORIZED"
    USER_IDENTITY_REQUIRED = "A2A_USER_IDENTITY_REQUIRED"
    AGENT_USER_DISABLED = "A2A_AGENT_USER_DISABLED"
    AGENT_MANAGER_DISABLED = "A2A_AGENT_MANAGER_DISABLED"
    AGENT_REVIEW_REQUIRED = "A2A_AGENT_REVIEW_REQUIRED"
    AUTH_REQUIRED = "A2A_AUTH_REQUIRED"
    AUTH_SCHEME_MISSING = "A2A_AUTH_SCHEME_MISSING"
    DISPATCH_NOT_FOUND = "A2A_DISPATCH_NOT_FOUND"
    DISPATCH_REJECTED = "A2A_DISPATCH_REJECTED"
    DISPATCH_TIMEOUT = "A2A_DISPATCH_TIMEOUT"
    DISPATCH_CONFLICT = "A2A_DISPATCH_CONFLICT"
    REMOTE_STATUS_UNKNOWN = "A2A_REMOTE_STATUS_UNKNOWN"
    OUTBOUND_BUSY = "A2A_OUTBOUND_BUSY"
    TASK_INVALID = "A2A_OUTBOUND_TASK_INVALID"
    MODE_INVALID = "A2A_OUTBOUND_MODE_INVALID"
    MANAGER_UNAVAILABLE = "A2A_OUTBOUND_MANAGER_UNAVAILABLE"
    STORE_INVALID = "A2A_OUTBOUND_STORE_INVALID"


_SAFE_SUMMARIES: dict[A2AOutboundErrorCode, str] = {
    A2AOutboundErrorCode.USER_IDENTITY_REQUIRED: "无法确认当前用户身份。",
    A2AOutboundErrorCode.DISCOVERY_URL_INVALID: "发现地址无效。",
    A2AOutboundErrorCode.DISCOVERY_BLOCKED: "发现地址被安全策略拦截。",
    A2AOutboundErrorCode.CARD_FETCH_FAILED: "无法获取第三方 Agent Card。",
    A2AOutboundErrorCode.CARD_INVALID: "第三方 Agent Card 无效或不兼容。",
    A2AOutboundErrorCode.DISCOVERY_NOT_FOUND: "未找到指定的发现候选。",
    A2AOutboundErrorCode.DISCOVERY_EXPIRED: "发现候选已过期，请重新发现。",
    A2AOutboundErrorCode.AGENT_ALREADY_REGISTERED: "该第三方 Agent 已注册。",
    A2AOutboundErrorCode.AGENT_NOT_REGISTERED: "指定的第三方 Agent 尚未注册。",
    A2AOutboundErrorCode.AGENT_DISABLED: "指定的第三方 Agent 已停用。",
    A2AOutboundErrorCode.AGENT_UNAVAILABLE: "指定的第三方 Agent 当前不可用。",
    A2AOutboundErrorCode.AGENT_NOT_AUTHORIZED: "当前 Agent 无权调用该第三方 Agent。",
    A2AOutboundErrorCode.AGENT_USER_DISABLED: "该第三方 Agent 已被用户停用。",
    A2AOutboundErrorCode.AGENT_MANAGER_DISABLED: "该第三方 Agent 已被管理员停用。",
    A2AOutboundErrorCode.AGENT_REVIEW_REQUIRED: "第三方 Agent 配置变化需要确认。",
    A2AOutboundErrorCode.AUTH_REQUIRED: "第三方 Agent 需要有效凭据。",
    A2AOutboundErrorCode.AUTH_SCHEME_MISSING: "已配置凭据，但 Agent Card 未声明认证方式。请让对方补充认证声明后刷新 Card，或清除不需要的凭据。",
    A2AOutboundErrorCode.DISPATCH_NOT_FOUND: "未找到指定的出站请求。",
    A2AOutboundErrorCode.DISPATCH_REJECTED: "第三方 Agent 拒绝了本次请求。",
    A2AOutboundErrorCode.DISPATCH_TIMEOUT: "等待第三方 Agent 回复超时。",
    A2AOutboundErrorCode.DISPATCH_CONFLICT: "出站请求状态已发生变化。",
    A2AOutboundErrorCode.REMOTE_STATUS_UNKNOWN: "暂时无法确认第三方请求状态。",
    A2AOutboundErrorCode.OUTBOUND_BUSY: "A2A 出站服务当前繁忙。",
    A2AOutboundErrorCode.TASK_INVALID: "A2A 出站任务文本无效。",
    A2AOutboundErrorCode.MODE_INVALID: "A2A 出站派发模式无效。",
    A2AOutboundErrorCode.MANAGER_UNAVAILABLE: "A2A 出站管理服务当前不可用。",
    A2AOutboundErrorCode.STORE_INVALID: "A2A 出站数据无效。",
}


def safe_error_summary(code: A2AOutboundErrorCode | str) -> str:
    """Return a fixed displayable summary without exposing exception details."""
    try:
        normalized = (
            code
            if isinstance(code, A2AOutboundErrorCode)
            else A2AOutboundErrorCode(str(code))
        )
    except ValueError:
        return "A2A 出站请求处理失败。"
    return _SAFE_SUMMARIES[normalized]


class A2AOutboundError(RuntimeError):
    """Domain error whose public text is selected from a stable code."""

    def __init__(self, code: A2AOutboundErrorCode | str) -> None:
        try:
            self.code = (
                code
                if isinstance(code, A2AOutboundErrorCode)
                else A2AOutboundErrorCode(str(code))
            )
        except ValueError:
            self.code = A2AOutboundErrorCode.STORE_INVALID
        super().__init__(safe_error_summary(self.code))

    @property
    def summary(self) -> str:
        return safe_error_summary(self.code)


__all__ = [
    "A2AOutboundError",
    "A2AOutboundErrorCode",
    "safe_error_summary",
]
