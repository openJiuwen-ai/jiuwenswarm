"""model_routing.types — data types + context helpers."""
from __future__ import annotations
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from jiuwenswarm.common.utils import logger


def _new_trace_id() -> str:
    """OTel trace_id：32 hex。"""
    return secrets.token_hex(16)


def _new_span_id() -> str:
    """OTel span_id：16 hex。"""
    return secrets.token_hex(8)


@dataclass
class PriorModelCall:
    """一次前置模型调用，序列化为完整 OTel span。"""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    iteration: int = 0
    trace_id: str = ""
    span_id: str = ""
    start_time: str = ""
    end_time: str = ""

    def __post_init__(self) -> None:
        if not self.trace_id:
            self.trace_id = _new_trace_id()
        if not self.span_id:
            self.span_id = _new_span_id()

    def to_otel_span(self) -> dict:
        """以 OpenTelemetry span 格式表示（含 context / 时间戳 / gen_ai.* 属性）。"""
        return {
            "name": "model_routing.prior_call",
            "context": {
                "trace_id": self.trace_id,
                "span_id": self.span_id,
                "trace_state": "",
            },
            "start_time": self.start_time,
            "end_time": self.end_time,
            "attributes": {
                "gen_ai.request.model": self.model,
                "gen_ai.usage.input_tokens": self.input_tokens,
                "gen_ai.usage.output_tokens": self.output_tokens,
                "gen_ai.system": "jiuwenswarm",
                "model_routing.iteration": self.iteration,
            },
        }


@dataclass
class TaskAnalysis:
    category: str
    difficulty: str
    target_score: int
    predicted_input_tokens: int
    agent_info: dict[str, Any]


@dataclass
class RoutingDecision:
    recommended_model_id: Optional[str]
    analysis: TaskAnalysis
    reasoning: str
    prior_calls_otel: list[dict] = field(default_factory=list)
    model_usage_stats: dict[str, Any] = field(default_factory=dict)



def _extract_prompt_text(messages: list[Any]) -> str:
    """取最后一条 user 消息文本作为分类输入。

    TUI 等通道会把用户输入包成 ``你收到一条消息：\\n{json envelope}``（真实文本在
    envelope 的 ``content`` 字段）。先解包 envelope 再返回，否则信封中的时间戳/数字
    会干扰分类器，且 1/2 确认回复会被当成整段 JSON 而永远匹配不上。
    """
    for msg in reversed(messages or []):
        role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
        if role == "user":
            return _unwrap_user_message(_message_text(msg))
    return _unwrap_user_message("\n".join(_message_text(m) for m in (messages or [])))


def _unwrap_user_message(text: str) -> str:
    """解开 TUI 的 ``你收到一条消息：\\n{...}`` 信封，取 ``content`` 字段。

    非 envelope 文本原样返回（对 web/其它通道零影响）；JSON 解析失败也原样返回，
    保守不抛。
    """
    if not text:
        return text
    s = text.lstrip()
    # 只认 "你收到一条消息" 前缀的壳，避免误吞普通 JSON 消息
    if not s.startswith("你收到一条消息"):
        return text
    lo = s.find("{")
    hi = s.rfind("}")
    if lo < 0 or hi <= lo:
        return text
    try:
        obj = json.loads(s[lo:hi + 1])
    except Exception:
        return text
    if not isinstance(obj, dict):
        return text
    content = obj.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text", "") or ""))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return text


def _message_text(msg: Any) -> str:
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content", "")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text", "")))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content or "")


def _agent_model_name(ctx: AgentCallbackContext) -> str:
    agent = getattr(ctx, "agent", None)
    if agent is None:
        return ""
    model_name = getattr(agent, "model_name", "")
    if model_name:
        return str(model_name)
    config = getattr(agent, "_config", None) or getattr(agent, "config", None)
    return str(getattr(config, "model_name", "") or "")


def _extract_agent_info(ctx: AgentCallbackContext) -> dict[str, Any]:
    """提取 agent 信息（model_name / provider / 可用模型 id 列表占位）。"""
    agent = getattr(ctx, "agent", None)
    model_name = _agent_model_name(ctx)
    provider = ""
    config = (
        getattr(agent, "_config", None) or getattr(agent, "config", None)
        if agent is not None
        else None
    )
    if config is not None:
        provider = str(getattr(config, "model_provider", "") or "")
    if not provider and agent is not None:
        mcc = getattr(agent, "model_client_config", None)
        if mcc is None and config is not None:
            mcc = getattr(config, "model_client_config", None)
        if isinstance(mcc, dict):
            provider = str(mcc.get("client_provider", "") or "")
        elif mcc is not None:
            provider = str(getattr(mcc, "client_provider", "") or "")
    return {
        "model_name": model_name,
        "provider": provider or "unknown",
        "available_model_ids": [],  # 占位：能力表完善后填充
    }


def _get_session_id(ctx: AgentCallbackContext) -> str | None:
    """从 ctx.session 取 session_id（兼容 get_session_id()/session_id 属性）。"""
    session = getattr(ctx, "session", None)
    if session is None:
        return None
    for _name in ("get_session_id", "session_id"):
        attr = getattr(session, _name, None)
        if attr is None:
            continue
        try:
            value = attr() if callable(attr) else attr
        except Exception as exc:
            logger.debug("[ModelRouting] session_id access failed: %s", exc)
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
