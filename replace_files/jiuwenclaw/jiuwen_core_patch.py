# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from jiuwenclaw.utils import build_default_headers
import os
import asyncio
import logging
from typing import Any, Optional, AsyncIterator, Callable
from contextvars import ContextVar
from typing import FrozenSet
from dataclasses import dataclass

from pydantic import Field
import httpx
from openjiuwen.core.common.logging import llm_logger, LogEventType
from openjiuwen.core.common.security.ssl_utils import SslUtils
from openjiuwen.core.common.security.url_utils import UrlUtils
from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig
from openjiuwen.core.foundation.llm.model_clients.openai_model_client import \
    AssistantMessageChunk, OpenAIModelClient, ToolCall, UsageMetadata
from openjiuwen.core.foundation.llm import BaseOutputParser, AssistantMessage
from openjiuwen.core.foundation.llm.model_clients.siliconflow_model_client import (
    SiliconFlowModelClient,
)
from openjiuwen.core.session.stream import OutputSchema
from jiuwenclaw.parser_patch import assemble_openai_response

llm_logger = logging.getLogger("jiuwenclaw.agentserver")
# Session context for retry notifications.
# Set by react_agent._call_llm_stream before calling llm.stream/invoke.
_retry_session: ContextVar[Optional[Any]] = ContextVar("retry_session", default=None)
_stream_tool_header_seen: ContextVar[FrozenSet[int]] = ContextVar(
    "stream_tool_header_seen", default=frozenset()
)

_ORIGINAL_BUILD_REQUEST_PARAMS = None


def _format_messages_by_role(messages: Any) -> str:
    """把 LLM 输入 messages 按 role 逐条格式化

    每条一行: [idx] role (len=N[, name=...][, tool_calls=M]): content
    兼容 List[BaseMessage] / List[dict] / str 三种入参形态
    """
    if isinstance(messages, str):
        return f"[raw str] (len={len(messages)}): {messages}"
    if not isinstance(messages, (list, tuple)):
        return str(messages)

    lines = [f"total={len(messages)}"]
    for idx, msg in enumerate(messages):
        if isinstance(msg, dict):
            role = msg.get("role", "?")
            content = msg.get("content")
            name = msg.get("name")
            tool_calls = msg.get("tool_calls")
        else:
            role = getattr(msg, "role", "?")
            content = getattr(msg, "content", None)
            name = getattr(msg, "name", None)
            tool_calls = getattr(msg, "tool_calls", None)

        if content is None:
            content_str, content_len = "", 0
        elif isinstance(content, str):
            content_str, content_len = content, len(content)
        else:
            # 多模态 list 等非字符串 content
            content_str = str(content)
            content_len = len(content_str)

        meta = f"len={content_len}"
        if name:
            meta += f", name={name}"
        if tool_calls:
            meta += f", tool_calls={len(tool_calls)}"
        lines.append(f"  [{idx}] {role} ({meta}): {content_str}")
    return "\n".join(lines)


# ============================================================
# LLM Retry Mechanism
# ============================================================
_parse_response = OpenAIModelClient._parse_response
_orig_invoke = OpenAIModelClient.invoke
_orig_stream = OpenAIModelClient.stream
# SiliconFlow 原始方法引用
_sf_orig_invoke = SiliconFlowModelClient.invoke
_sf_orig_stream = SiliconFlowModelClient.stream


@dataclass
class LlmRetryConfig:
    """LLM 调用重试配置。"""
    enabled: bool = False
    max_attempts: int = 3
    initial_backoff: float = 10.0
    max_backoff: float = 60.0
    backoff_factor: float = 2.0
    retry_on_rate_limit: bool = True


class RetryMixin:
    """LLM 重试逻辑混入基类，供所有 Patch ModelClient 复用。"""

    _RETRYABLE_KEYWORDS = (
        "connection error", "connecttimeout", "connect error",
        "network is unreachable", "connection reset", "broken pipe",
        "connection refused", "readtimeout", "timeout",
        "operation timed out", "500", "502", "503", "504",
        "too many requests", "响应超时", "timed out", "timeout",
        "connection failed", "connection closed unexpectedly", "WebSocket connection closed"
    )

    _NON_RETRYABLE_KEYWORDS = (
        "400", "401", "403", "404", "422",
        "invalid_api_key", "unauthorized", "forbidden", "authentication",
        "model cannot be none", "invalid request",
        "ssl", "certificate verify",
        "model provider is invalid",
        "model service config error",
        "model config error",
        "model invoke parameter error",
        "model client_config is invalid",
    )

    @classmethod
    def _classify_error(cls, exc: Exception, cfg: LlmRetryConfig) -> str:
        """分类错误原因，返回可读描述。"""
        error_msg = str(exc).lower()

        if any(kw in error_msg for kw in ("429", "rate_limit", "rate limit")):
            return "HTTP 429 限流"

        if any(kw in error_msg for kw in ("500",)):
            return "HTTP 500 服务端错误"
        if any(kw in error_msg for kw in ("502",)):
            return "HTTP 502 网关错误"
        if any(kw in error_msg for kw in ("503",)):
            return "HTTP 503 服务不可用"
        if any(kw in error_msg for kw in ("504",)):
            return "HTTP 504 网关超时"

        if any(kw in error_msg for kw in ("readtimeout", "timeout", "operation timed out")):
            return "请求超时"
        # pylint: disable=complicate-comprehension
        if any(kw in error_msg for kw in ("connection error", "connecttimeout", "connect error",
                                          "network is unreachable", "connection reset", "broken pipe",
                                          "connection refused")):  # pylint: disable=complicate-comprehension
            return "连接错误"

        return "未知错误"

    @staticmethod
    def _extract_error_details(exc: Exception) -> str:
        """提取异常中的详细信息：HTTP 状态码、响应体、请求头等。"""
        parts = []

        # 提取 HTTP 状态码
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            resp = getattr(exc, "response", None)
            if resp is not None:
                status_code = getattr(resp, "status_code", None)
        if status_code is not None:
            parts.append(f"status_code={status_code}")

        # 提取异常类型名
        parts.append(f"type={type(exc).__name__}")

        # 提取响应体/message 字段
        for attr in ("message", "body", "response_text"):
            val = getattr(exc, attr, None)
            if val is not None:
                parts.append(f"{attr}={val}")
                break

        # 提取 request.id
        request_id = getattr(getattr(exc, "request", None), "id", None)
        if request_id:
            parts.append(f"request_id={request_id}")

        # 如果有 response 对象，尝试提取更多字段
        resp = getattr(exc, "response", None)
        if resp is not None:
            url = getattr(resp, "url", None)
            if url:
                parts.append(f"url={url}")
            headers = getattr(resp, "headers", None)
            if headers:
                x_request_id = headers.get("x-request-id") or headers.get("cf-ray")
                if x_request_id:
                    parts.append(f"trace_id={x_request_id}")

        return ", ".join(parts)

    @staticmethod
    def _get_env_value(env_var: str, default: Any, type_func: Callable[[str], Any]) -> Any:
        """从环境变量读取并转换值，失败时记录警告并返回默认值。

        Args:
            env_var: 环境变量名
            default: 默认值
            type_func: 类型转换函数 (int/float等)

        Returns:
            转换后的环境变量值或默认值
        """
        env_val = os.getenv(env_var)
        if env_val is None:
            return default
        try:
            return type_func(env_val)
        except (ValueError, TypeError) as e:
            llm_logger.warning(f"Invalid {env_var} env value '{env_val}': {e}")
            return default

    @classmethod
    def _get_retry_config(cls) -> LlmRetryConfig:
        """从环境变量或 config.yaml 读取重试配置，未配置则使用代码默认值。

        优先级：环境变量 > config.yaml > 代码默认值
        """
        # 从环境变量读取
        env_max_attempts = RetryMixin._get_env_value("LLM_RETRY_MAX_ATTEMPTS", None, int)
        env_max_backoff = RetryMixin._get_env_value("LLM_RETRY_MAX_BACKOFF", None, float)

        # 构建默认配置（环境变量优先）
        defaults = LlmRetryConfig(
            max_attempts=env_max_attempts if env_max_attempts is not None else 4,
            max_backoff=env_max_backoff if env_max_backoff is not None else 60.0,
        )

        # 从 config.yaml 读取并合并配置
        try:
            from jiuwenclaw.config import get_config
            cfg = get_config()
            retry_cfg = ((cfg or {}).get("react", {}) or {}).get("llm_retry", {})
            if not retry_cfg:
                return defaults

            def get_val(key: str, env_val: Any, code_default: Any) -> Any:
                """获取配置值，优先级：环境变量 > config.yaml > 代码默认值"""
                return env_val if env_val is not None else retry_cfg.get(key, code_default)

            return LlmRetryConfig(
                enabled=retry_cfg.get("enabled", True),
                max_attempts=get_val("max_attempts", env_max_attempts, 3),
                initial_backoff=retry_cfg.get("initial_backoff", 10.0),
                max_backoff=get_val("max_backoff", env_max_backoff, 60.0),
                backoff_factor=retry_cfg.get("backoff_factor", 2.0),
                retry_on_rate_limit=retry_cfg.get("retry_on_rate_limit", True),
            )
        except Exception as e:
            llm_logger.warning("Failed to get retry config from config.yaml, use default values. %s", e)
            return defaults

    def _is_retryable_error(self, exc: Exception, cfg: LlmRetryConfig) -> bool:
        """判断错误是否可重试。

        两步策略：
        1. 命中不可重试关键词 → 立即返回 False
        2. 命中可重试关键词 → 返回 True（429 受 retry_on_rate_limit 控制）
        3. 兜底返回 False（未知错误不重试）
        """
        error_msg = str(exc).lower()

        if any(kw in error_msg for kw in self._NON_RETRYABLE_KEYWORDS):
            return False

        if any(kw in error_msg for kw in ("429", "rate_limit", "rate limit")):
            return cfg.retry_on_rate_limit

        if any(kw in error_msg for kw in self._RETRYABLE_KEYWORDS):
            return True

        return False

    def _calculate_backoff(self, attempt: int, exc: Exception,
                           cfg: LlmRetryConfig) -> float:
        """指数退避 + Retry-After 头（取 max，避免等待过短）。"""
        backoff = min(cfg.initial_backoff * (cfg.backoff_factor ** attempt), cfg.max_backoff)
        retry_after = self._extract_retry_after(exc)
        if retry_after is not None:
            backoff = max(backoff, retry_after)
        return backoff

    async def _notify_retry_start(self, reason: str, attempt: int, max_attempts: int, backoff: float) -> None:
        """向前端发送重试开始通知。"""
        session = _retry_session.get()
        if session is None:
            llm_logger.debug("[notify] _notify_retry_start: session is None, skip notification")
            return
        try:
            await session.write_stream(
                OutputSchema(
                    type="retry_notification",
                    index=999,
                    payload={
                        "output": {
                            "output": f"\n\n⚠️ 模型调用异常 [{reason}], "
                                      f"将在 {backoff: .1f} 秒后进行第 {attempt}/{max_attempts} 次重试...",
                            "result_type": "text",
                        },
                    },
                )
            )
            llm_logger.info(f"retry notification sent successfully, ({attempt}/{max_attempts}), "
                            f"session_id={getattr(session, 'get_session_id', lambda: 'N/A')()}")
        except Exception as e:
            llm_logger.error(f"[notify] _notify_retry_start 发送失败: {type(e).__name__}: {e}")

    async def _notify_retry_end(self) -> None:
        """清除前端重试提示。"""
        session = _retry_session.get()
        if session is None:
            return
        try:
            await session.write_stream(
                OutputSchema(
                    type="processing_complete",
                    index=999,
                    payload={},
                )
            )
        except Exception as e:
            llm_logger.error(f"[notify] _notify_retry_end 发送失败: {type(e).__name__}: {e}")

    @staticmethod
    def _extract_retry_after(exc: Exception) -> float | None:
        """从 429 响应的 Retry-After 头读取等待时间。"""
        if hasattr(exc, "response") and exc.response is not None:
            retry_after = exc.response.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    return float(retry_after)
                except (ValueError, TypeError):
                    pass
        return None

    async def _invoke_with_retry(self, invoke_func, *args, **kwargs):
        """带重试的 invoke 包装器。
        max_attempts 表示纯重试次数，不包含首次正常调用。
        """
        cfg = self._get_retry_config()
        if not cfg.enabled:
            llm_logger.info("LLM invoke 未启用重试，直接返回结果")
            return await invoke_func(*args, **kwargs)

        last_error = None
        for attempt in range(cfg.max_attempts + 1):
            try:
                return await invoke_func(*args, **kwargs)
            except Exception as e:
                last_error = e
                if not self._is_retryable_error(e, cfg):
                    reason = self._classify_error(e, cfg)
                    details = self._extract_error_details(e)
                    llm_logger.info(f"LLM invoke 不可重试 [{reason}] [{details}], details: {e}")
                    raise
                reason = self._classify_error(e, cfg)
                details = self._extract_error_details(e)
                backoff = self._calculate_backoff(attempt, e, cfg)
                if attempt >= cfg.max_attempts:
                    break
                llm_logger.warning(
                    f"LLM invoke 失败 [{reason}] [{details}]，将在 {backoff: .1f}s 后重试, "
                    f"(第 {attempt + 1} 次重试 / 共 {cfg.max_attempts} 次): {e}"
                )
                await self._notify_retry_start(reason, attempt + 1, cfg.max_attempts, backoff)
                await asyncio.sleep(backoff)

        reason = self._classify_error(last_error, cfg)
        details = self._extract_error_details(last_error)
        llm_logger.error(
            f"LLM invoke 重试次数耗尽 [{reason}] [{details}]，已执行 {cfg.max_attempts} 次重试）: {last_error}"
        )
        await self._notify_retry_end()
        raise last_error

    async def _stream_with_retry(self, stream_func, *args, **kwargs):
        """带重试的 stream 包装器。
        max_attempts 表示纯重试次数，不包含首次正常调用。。
        """
        cfg = self._get_retry_config()
        if not cfg.enabled:
            llm_logger.info("LLM stream 未启用重试机制")
            async for chunk in stream_func(*args, **kwargs):
                yield chunk
            return

        last_error = None
        for attempt in range(cfg.max_attempts + 1):
            try:
                async for chunk in stream_func(*args, **kwargs):
                    yield chunk
                return  # 流式成功完成
            except Exception as e:
                last_error = e
                llm_logger.info(f"LLM stream 开始重试, details: {e}")
                reason = self._classify_error(e, cfg)
                details = self._extract_error_details(e)
                backoff = self._calculate_backoff(attempt, e, cfg)
                if attempt >= cfg.max_attempts:
                    break
                llm_logger.warning(
                    f"LLM stream 失败 [{reason}] [{details}]，将在 {backoff: .1f}s 后重试 "
                    f"(第 {attempt + 1} 次重试 / 共 {cfg.max_attempts} 次): {e}"
                )
                await self._notify_retry_start(reason, attempt + 1, cfg.max_attempts, backoff)
                await asyncio.sleep(backoff)

        reason = self._classify_error(last_error, cfg)
        details = self._extract_error_details(last_error)
        llm_logger.error(
            f"LLM stream 重试次数耗尽 [{reason}] [{details}]，已尝试{cfg.max_attempts} 次重试）: {last_error}")
        await self._notify_retry_end()
        raise last_error


def _patched_build_request_params(self, *, stream: bool, **kwargs) -> dict:
    """Patched version: ensure streaming requests carry ``stream_options={"include_usage": true}``.

    Required for OpenAI-compatible streaming APIs to return the usage payload in
    the final chunk; without it ``chunk.usage`` is always ``None`` and the
    ``gen_ai.client.token.usage`` metric never gets recorded.

    Respects a user-provided ``stream_options`` override.
    """
    params = _ORIGINAL_BUILD_REQUEST_PARAMS(self, stream=stream, **kwargs)
    if stream:
        existing = params.get("stream_options")
        if existing is None:
            params["stream_options"] = {"include_usage": True}
        elif isinstance(existing, dict) and "include_usage" not in existing:
            existing["include_usage"] = True
    # Fallback max_tokens to environment variable if not configured
    if params.get("max_tokens") is None:
        env_max_tokens = os.environ.get("LLM_MAX_TOKENS")
        if env_max_tokens:
            try:
                params["max_tokens"] = int(env_max_tokens)
            except ValueError:
                llm_logger.warning(
                    f"Invalid LLM_MAX_TOKENS env value: '{env_max_tokens}' (not an integer), ignoring"
                )
    return params


class PatchOpenAIModelClient(RetryMixin, OpenAIModelClient):
    _MIN_SEGMENT_SIZE = 500

    def _create_async_openai_client(self, timeout: Optional[float] = None) -> "openai.AsyncOpenAI":
        """
        Create an OpenAI Async client with configured SSL/proxy/http client settings.

        Args:
            timeout: Optional timeout override for this specific request
        """
        from openai import AsyncOpenAI

        api_base = self.model_client_config.api_base or ""
        if "example.com" in api_base:
            raise ValueError(
                f"api_base 指向占位域名 ({api_base})，"
                "请将 API_BASE 环境变量或配置修改为真实的 API 地址。"
                "参考 .env.template 中的说明。"
            )

        ssl_verify, ssl_cert = self.model_client_config.verify_ssl, self.model_client_config.ssl_cert
        verify = SslUtils.create_strict_ssl_context(ssl_cert) if ssl_verify else ssl_verify

        http_client = httpx.AsyncClient(
            proxy=UrlUtils.get_global_proxy_url(self.model_client_config.api_base),
            verify=verify
        )

        # Use method-level timeout if provided, otherwise use config timeout
        final_timeout = timeout if timeout is not None else self.model_client_config.timeout
        llm_logger.info(
            "Before create openai client, model client config params ready. "
            "event_type=%s, timeout=%s, max_retries=%s",
            LogEventType.LLM_CALL_START,
            final_timeout,
            self.model_client_config.max_retries
        )
        session_value = self.model_client_config.model_extra.get("session", {})
        if isinstance(session_value, dict):
            session_id = session_value.get("sessionId") or "default"
        elif isinstance(session_value, str):
            session_id = session_value
        else:
            session_id = "default"
        default_headers = build_default_headers(session_id)
        return AsyncOpenAI(
            api_key=self.model_client_config.api_key,
            base_url=self.model_client_config.api_base,
            http_client=http_client,
            timeout=final_timeout,
            max_retries=self.model_client_config.max_retries,
            default_headers=default_headers
        )

    def _parse_stream_chunk(self, chunk: Any) -> Optional[AssistantMessageChunk]:
        """Parse OpenAI streaming response chunk

        Args:
            chunk: OpenAI streaming response chunk

        Returns:
            AssistantMessageChunk or None
        """

        # Detect Huawei MaaS by api_base (workaround for malformed tool_calls delta)
        # Only apply workaround for glm-5.1 model on MaaS endpoint
        _is_huawei_maas = False
        _has_mcc = hasattr(self, "model_client_config")
        _mcc_is_none = self.model_client_config is None if _has_mcc else True
        _has_mc = hasattr(self, "model_config")
        _mc_is_none = self.model_config is None if _has_mc else True
        _api_base = ""
        _model_name = ""
        if _has_mcc and not _mcc_is_none:
            _api_base = getattr(self.model_client_config, "api_base", "") or ""
            _model_name = getattr(self.model_client_config, "model_name", "") or ""
        if _has_mc and not _mc_is_none and not _model_name:
            _model_name = getattr(self.model_config, "model_name", "") or ""
        # Check if it's Huawei MaaS AND model is glm-5.1 (the affected model)
        _is_maas_endpoint = (
                "modelarts-maas.com" in _api_base
                or "modelarts" in _api_base.lower()
                or "huaweiapaas.com" in _api_base
                or "agentarts" in _api_base.lower()
        )
        _is_glm51 = _model_name.lower() in ("glm-5.1", "glm5.1")
        _is_huawei_maas = _is_maas_endpoint and _is_glm51

        # Check for usage-only chunk (empty choices with usage data - final chunk with stream_options)
        _has_usage = hasattr(chunk, 'usage') and chunk.usage
        _has_choices = bool(chunk.choices) if hasattr(chunk, 'choices') else False
        if _has_usage and not _has_choices:
            # This is the final usage chunk - parse it even if choices is empty
            usage_metadata = UsageMetadata(
                model_name=self.model_config.model_name,
                input_tokens=getattr(chunk.usage, 'prompt_tokens', 0) or 0,
                output_tokens=getattr(chunk.usage, 'completion_tokens', 0) or 0,
                total_tokens=getattr(chunk.usage, 'total_tokens', 0) or 0,
            )
            # Return a chunk with only usage metadata (no content)
            return AssistantMessageChunk(
                content="",
                reasoning_content=None,
                tool_calls=None,
                usage_metadata=usage_metadata,
                finish_reason="stop",  # Usage chunk always indicates completion
            )

        # When stream_options={"include_usage": true}, OpenAI sends a final usage-only
        # chunk with ``choices []`` and ``usage`` populated. Emit a
        # usage-only AssistantMessageChunk so downstream aggregation picks it up.
        if not chunk.choices:
            chunk_usage = getattr(chunk, 'usage', None)
            if chunk_usage:
                input_cost, output_cost, total_cost = self._extract_cost_info(chunk_usage)
                usage_metadata = UsageMetadata(
                    model_name=self.model_config.model_name,
                    input_tokens=getattr(chunk_usage, 'prompt_tokens', 0) or 0,
                    output_tokens=getattr(chunk_usage, 'completion_tokens', 0) or 0,
                    total_tokens=getattr(chunk_usage, 'total_tokens', 0) or 0,
                    input_cost=input_cost,
                    output_cost=output_cost,
                    total_cost=total_cost,
                )
                return AssistantMessageChunk(
                    content="",
                    reasoning_content=None,
                    tool_calls=None,
                    usage_metadata=usage_metadata,
                    finish_reason="null",
                )
            return None

        choice = chunk.choices[0]
        delta = choice.delta

        # Extract content
        content = getattr(delta, 'content', None) or ""
        reasoning_content = getattr(delta, 'reasoning_content', None)

        # Parse tool_calls delta
        tool_calls = []
        if hasattr(delta, 'tool_calls') and delta.tool_calls:
            seen_tool_header_indexes = set(_stream_tool_header_seen.get())
            for tc_delta in delta.tool_calls:
                if hasattr(tc_delta, 'function') and tc_delta.function:
                    index = getattr(tc_delta, 'index', None)
                    normalized_index = index if index is not None else 0
                    function_name = getattr(tc_delta.function, 'name', None) or ""
                    function_arguments = getattr(tc_delta.function, 'arguments', None) or ""
                    tool_call_id = getattr(tc_delta, 'id', '') or ""
                    has_header = bool(tool_call_id or function_name)

                    # Some providers may expose malformed ``delta.tool_calls`` entries
                    # while actually streaming plain text. Do not start a tool-call
                    # sequence from an arguments-only fragment unless a valid tool
                    # header was already seen for the same tool-call index.
                    if not has_header and normalized_index not in seen_tool_header_indexes:
                        continue
                    if has_header:
                        seen_tool_header_indexes.add(normalized_index)

                    tool_call = ToolCall(
                        id=tool_call_id,
                        type="function",
                        name=function_name,
                        arguments=function_arguments,
                        index=normalized_index,
                    )
                    tool_calls.append(tool_call)
            _stream_tool_header_seen.set(frozenset(seen_tool_header_indexes))

        # Huawei MaaS workaround: merge tool_calls with same index
        # MaaS sometimes returns multiple tool_calls deltas with identical index,
        # where the second one only contains arguments increment.
        if _is_huawei_maas and len(tool_calls) > 1:
            merged_by_index: dict[int, ToolCall] = {}
            for tc in tool_calls:
                idx = tc.index if tc.index is not None else 0
                if idx not in merged_by_index:
                    merged_by_index[idx] = tc
                else:
                    existing = merged_by_index[idx]
                    new_id = tc.id or existing.id
                    new_name = tc.name or existing.name
                    new_args = existing.arguments + tc.arguments
                    merged_by_index[idx] = ToolCall(
                        id=new_id,
                        type="function",
                        name=new_name,
                        arguments=new_args,
                        index=idx,
                    )
            tool_calls = list(merged_by_index.values())

        # Build usage_metadata (usually only in the last chunk)
        usage_metadata = None
        if hasattr(chunk, 'usage') and chunk.usage:
            # Extract cost information if available
            input_cost, output_cost, total_cost = self._extract_cost_info(chunk.usage)

            usage_metadata = UsageMetadata(
                model_name=self.model_config.model_name,
                input_tokens=getattr(chunk.usage, 'prompt_tokens', 0) or 0,
                output_tokens=getattr(chunk.usage, 'completion_tokens', 0) or 0,
                total_tokens=getattr(chunk.usage, 'total_tokens', 0) or 0,
                input_cost=input_cost,
                output_cost=output_cost,
                total_cost=total_cost,
            )

        return AssistantMessageChunk(
            content=content,
            reasoning_content=reasoning_content,
            tool_calls=tool_calls if tool_calls else None,
            usage_metadata=usage_metadata,
            finish_reason=choice.finish_reason or "null"
        )

    async def _parse_response(
            self,
            response: Any,
            parser: Optional[BaseOutputParser] = None
    ) -> AssistantMessage:
        # 非标准OpenAi格式
        if isinstance(response, str):
            response = assemble_openai_response(response)
        return await _parse_response(self, response, parser)

    async def invoke(
            self,
            messages,
            *,
            tools=None,
            temperature=None,
            top_p=None,
            model=None,
            max_tokens=None,
            stop=None,
            output_parser=None,
            timeout=None,
            **kwargs,
    ):
        session_id = self.model_client_config.model_extra.get("session", "default")

        if llm_logger.isEnabledFor(logging.DEBUG):
            llm_logger.debug(
                "[session=%s] [LLM] Input messages:\n%s",
                session_id,
                _format_messages_by_role(messages),
            )
        min_segment_size = self._MIN_SEGMENT_SIZE
        resp = await self._invoke_with_retry(
            _orig_invoke,
            self,
            messages,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            model=model,
            max_tokens=max_tokens,
            stop=stop,
            output_parser=output_parser,
            timeout=timeout,
            **kwargs,
        )

        # 分段打印 invoke 的输出（思考 / 正文分别分段）
        _content = getattr(resp, "content", None) or ""
        _reasoning = getattr(resp, "reasoning_content", None) or ""

        def _print_segments(text: str, kind: str) -> int:
            if not text:
                return 0
            text_len = len(text)
            segment_num = 0
            for i in range(0, text_len, min_segment_size):
                segment_num += 1
                segment = text[i:i + min_segment_size]
                is_last = (i + min_segment_size) >= text_len
                llm_logger.info(
                    "[session=%s] [LLM] %s segment #%d%s: %s",
                    session_id,
                    kind,
                    segment_num,
                    " (final)" if is_last else "",
                    segment,
                )
            return segment_num

        reasoning_segments = _print_segments(_reasoning, "Reasoning")
        content_segments = _print_segments(_content, "Output")

        llm_logger.info(
            "[session=%s] [LLM] Generation completed. reasoning segments: %d, output segments: %d",
            session_id,
            reasoning_segments,
            content_segments,
        )
        return resp

    async def stream(
            self,
            messages,
            *,
            tools=None,
            temperature=None,
            top_p=None,
            model=None,
            max_tokens=None,
            stop=None,
            output_parser=None,
            timeout=None,
            **kwargs,
    ):
        session_id = self.model_client_config.model_extra.get("session", "default")

        if llm_logger.isEnabledFor(logging.DEBUG):
            llm_logger.debug(
                "[session=%s] [LLM] Input messages:\n%s",
                session_id,
                _format_messages_by_role(messages),
            )
        chunk_counter = 0
        token = _stream_tool_header_seen.set(frozenset())

        # 思考 / 正文两条独立分段缓冲
        reasoning_parts: list = []  # 思考分段打印缓冲区
        content_parts: list = []  # 正文分段打印缓冲区
        reasoning_seg_counter = 0  # 思考已打印段数
        content_seg_counter = 0  # 正文已打印段数
        reasoning_accum = 0  # 思考累积字符数
        content_accum = 0  # 正文累积字符数
        min_segment_size = self._MIN_SEGMENT_SIZE

        def _flush_segment(parts: list, seg_no: int, kind: str, tail: bool = False) -> None:
            full_segment = "".join(parts)
            llm_logger.info(
                "[session=%s] [LLM] %s segment #%d (%s, len=%d): %s",
                session_id,
                kind,
                seg_no,
                "final" if tail else "live",
                len(full_segment),
                full_segment,
            )

        try:
            async for chunk in self._stream_with_retry(
                    _orig_stream,
                    self,
                    messages,
                    tools=tools,
                    temperature=temperature,
                    top_p=top_p,
                    model=model,
                    max_tokens=max_tokens,
                    stop=stop,
                    output_parser=output_parser,
                    timeout=timeout,
                    **kwargs,
            ):
                chunk_counter += 1
                _reasoning = getattr(chunk, "reasoning_content", None)
                _content = getattr(chunk, "content", None)

                if _reasoning:
                    reasoning_parts.append(_reasoning)
                    reasoning_accum += len(_reasoning)

                    if reasoning_accum >= min_segment_size:
                        reasoning_seg_counter += 1
                        _flush_segment(reasoning_parts, reasoning_seg_counter, "Reasoning")
                        reasoning_parts = []
                        reasoning_accum = 0

                if _content:
                    # 思考 → 正文切换：先把思考缓冲尾巴冲刷掉，保证分段不跨界
                    if reasoning_parts:
                        reasoning_seg_counter += 1
                        _flush_segment(reasoning_parts, reasoning_seg_counter, "Reasoning", tail=True)
                        reasoning_parts = []
                        reasoning_accum = 0
                    content_parts.append(_content)
                    content_accum += len(_content)

                    if content_accum >= min_segment_size:
                        content_seg_counter += 1
                        _flush_segment(content_parts, content_seg_counter, "Output")
                        content_parts = []
                        content_accum = 0
                yield chunk
        finally:
            _stream_tool_header_seen.reset(token)

        # 处理最后剩余的缓冲区内容（两条缓冲分别冲刷）
        if reasoning_parts:
            reasoning_seg_counter += 1
            _flush_segment(reasoning_parts, reasoning_seg_counter, "Reasoning", tail=True)
        if content_parts:
            content_seg_counter += 1
            _flush_segment(content_parts, content_seg_counter, "Output", tail=True)

        llm_logger.info(
            "[session=%s] [LLM] Generation completed. Total chunks: %d, "
            "reasoning segments: %d, output segments: %d",
            session_id,
            chunk_counter,
            reasoning_seg_counter,
            content_seg_counter,
        )


class PatchSiliconFlowModelClient(RetryMixin, SiliconFlowModelClient):
    """带重试的 SiliconFlowModelClient。结构同 OpenAI，包装原始方法即可。"""

    async def invoke(
            self,
            messages,
            *,
            tools=None,
            temperature=None,
            top_p=None,
            model=None,
            max_tokens=None,
            stop=None,
            output_parser=None,
            timeout=None,
            **kwargs,
    ):
        return await self._invoke_with_retry(
            _sf_orig_invoke,
            self,
            messages,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            model=model,
            max_tokens=max_tokens,
            stop=stop,
            output_parser=output_parser,
            timeout=timeout,
            **kwargs,
        )

    async def stream(
            self,
            messages,
            *,
            tools=None,
            temperature=None,
            top_p=None,
            model=None,
            max_tokens=None,
            stop=None,
            output_parser=None,
            timeout=None,
            **kwargs,
    ):
        async for chunk in self._stream_with_retry(
                _sf_orig_stream,
                self,
                messages,
                tools=tools,
                temperature=temperature,
                top_p=top_p,
                model=model,
                max_tokens=max_tokens,
                stop=stop,
                output_parser=output_parser,
                timeout=timeout,
                **kwargs,
        ):
            yield chunk


def _patch_railed_model_call_session() -> None:
    """Monkey-patch ReActAgent._railed_model_call to set _retry_session ContextVar
    around llm.invoke/stream calls so RetryMixin._notify_retry_start can reach the frontend."""
    from openjiuwen.core.single_agent.agents.react_agent import ReActAgent

    if not hasattr(ReActAgent, "_railed_model_call"):
        return

    _orig_railed_model_call = ReActAgent._railed_model_call  # pylint: disable=protected-access

    async def _patched_railed_model_call(self, ctx):
        session = ctx.session
        token = _retry_session.set(session)
        try:
            return await _orig_railed_model_call(self, ctx)
        finally:
            _retry_session.reset(token)

    ReActAgent._railed_model_call = _patched_railed_model_call  # pylint: disable=protected-access


def apply_siliconflow_model_client_patch() -> None:
    """Inject retry into SiliconFlowModelClient."""
    _impl = PatchSiliconFlowModelClient.__dict__
    SiliconFlowModelClient.invoke = _impl["invoke"]
    SiliconFlowModelClient.stream = _impl["stream"]

    _instance_attrs = (
        '_stream_with_retry', '_invoke_with_retry',
        '_classify_error', '_is_retryable_error', '_calculate_backoff',
        '_notify_retry_start', '_notify_retry_end', '_get_retry_config',
    )
    _class_attrs = ('_NON_RETRYABLE_KEYWORDS', '_RETRYABLE_KEYWORDS')
    _static_attrs = ('_extract_error_details', '_extract_retry_after')

    for _attr in _static_attrs:
        if hasattr(RetryMixin, _attr):
            setattr(SiliconFlowModelClient, _attr, staticmethod(getattr(RetryMixin, _attr)))

    for _attr in _instance_attrs + _class_attrs:
        if hasattr(RetryMixin, _attr):
            setattr(SiliconFlowModelClient, _attr, getattr(RetryMixin, _attr))


# ============================================================
# active_skill_bodies patch: avoid inserting UserMessage between ToolMessages
# ============================================================
_ACTIVE_SKILL_PATCH_APPLIED = False


def apply_active_skill_bodies_patch() -> None:
    """Monkey-patch append_active_skill_pins_to_window to move misplaced pins.

    The original function inserts skill-body UserMessage pins right after the
    skill stub ToolMessage (stub_idx + 1).  When the AIMessage had multiple
    tool_calls, this places the UserMessage *between* sibling ToolMessages,
    breaking the OpenAI API requirement that all tool responses must be
    contiguous after the AIMessage.

    Fix: after the original function runs, scan context_messages and move any
    UserMessage that sits between ToolMessages to after the last ToolMessage
    in that group.
    """
    global _ACTIVE_SKILL_PATCH_APPLIED
    if _ACTIVE_SKILL_PATCH_APPLIED:
        return

    try:
        import openjiuwen.core.context_engine.active_skill_bodies as _asb
        from openjiuwen.core.foundation.llm import ToolMessage, UserMessage
    except ImportError:
        return

    if getattr(_asb, "_jiuwen_toolmsg_pin_patch", False):
        return

    _orig = _asb.append_active_skill_pins_to_window

    def _patched_append(context, window, *, max_active_skill_bodies=1, pin_target="system"):
        result = _orig(context, window, max_active_skill_bodies=max_active_skill_bodies, pin_target=pin_target)
        ctx_msgs = getattr(window, "context_messages", None)
        if not ctx_msgs:
            return result
        # Move any UserMessage that sits between ToolMessages to after the group
        i = 0
        while i < len(ctx_msgs):
            msg = ctx_msgs[i]
            if isinstance(msg, UserMessage) and i > 0 and isinstance(ctx_msgs[i - 1], ToolMessage):
                # Find the end of the contiguous ToolMessage group after this UserMessage
                j = i + 1
                while j < len(ctx_msgs) and isinstance(ctx_msgs[j], ToolMessage):
                    j += 1
                if j > i + 1:
                    # ToolMessages exist after the UserMessage → move it after the group
                    pin = ctx_msgs.pop(i)
                    ctx_msgs.insert(j - 1, pin)
                    # Don't increment i — re-check the moved position
                    continue
            i += 1
        return result

    _asb.append_active_skill_pins_to_window = _patched_append
    _asb._jiuwen_toolmsg_pin_patch = True
    _ACTIVE_SKILL_PATCH_APPLIED = True
    llm_logger.info("[active_skill_bodies patch] misplaced-pin fix applied")

    # context.py uses `from ... import append_active_skill_pins_to_window`,
    # creating a local binding that bypasses the module attribute patch above.
    # Patch the binding in context.py too.
    try:
        import openjiuwen.core.context_engine.context.context as _ctx_mod
        _ctx_mod.append_active_skill_pins_to_window = _patched_append
        llm_logger.info("[active_skill_bodies patch] context.py binding patched")
    except ImportError:
        pass  # context.py not yet imported — will pick up patched attr via from-import


def apply_openai_model_client_patch() -> None:
    """Monkey-patch upstream OpenAIModelClient with JiuwenClaw SSL/headers/stream behavior."""
    # Apply active_skill_bodies patch (fix misplaced UserMessage between ToolMessages)
    apply_active_skill_bodies_patch()

    global _ORIGINAL_BUILD_REQUEST_PARAMS
    if _ORIGINAL_BUILD_REQUEST_PARAMS is None:
        _ORIGINAL_BUILD_REQUEST_PARAMS = OpenAIModelClient._build_request_params

    _impl = PatchOpenAIModelClient.__dict__
    setattr(OpenAIModelClient, "_create_async_openai_client", _impl["_create_async_openai_client"])
    setattr(OpenAIModelClient, "_parse_stream_chunk", _impl["_parse_stream_chunk"])
    setattr(OpenAIModelClient, "_parse_response", _impl["_parse_response"])
    setattr(OpenAIModelClient, "_build_request_params", _patched_build_request_params)
    setattr(OpenAIModelClient, "_MIN_SEGMENT_SIZE", _impl["_MIN_SEGMENT_SIZE"])

    OpenAIModelClient.invoke = PatchOpenAIModelClient.invoke
    OpenAIModelClient.stream = PatchOpenAIModelClient.stream
    _patch_railed_model_call_session()
    _static_attrs = ('_extract_error_details', '_extract_retry_after', '_raise_mock_error')
    _instance_attrs = (
        '_stream_with_retry', '_invoke_with_retry',
        '_classify_error', '_is_retryable_error', '_calculate_backoff',
        '_notify_retry_start', '_notify_retry_end', '_get_retry_config',
    )
    _class_attrs = ('_NON_RETRYABLE_KEYWORDS', '_RETRYABLE_KEYWORDS')

    for _attr in _static_attrs:
        if hasattr(RetryMixin, _attr):
            setattr(OpenAIModelClient, _attr, staticmethod(getattr(RetryMixin, _attr)))

    for _attr in _instance_attrs + _class_attrs:
        if hasattr(RetryMixin, _attr):
            setattr(OpenAIModelClient, _attr, getattr(RetryMixin, _attr))
