# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime patches for OpenAIModelClient.

1. SSE-only 网关兼容：部分网关（如 celia-claw sse-api）即使在非流式调用下也只返回
   ``text/event-stream`` 文本，此时 openai SDK 交回给框架的 ``response`` 会是 ``str``
   而非 ``ChatCompletion``，导致 ``response.choices`` 抛出异常。补丁在收到 ``str``
   响应时，先把 SSE 文本组装成标准的 ``ChatCompletion`` 再交回原解析逻辑。

2. GLM XML 标签清洗：流式场景下 GLM 调用原生工具的 XML 标签（如 ``<arg_value>``、
   ``<arg_key>``、``<tool_call>``）未剥离干净，会污染后续 todo_list、LLM 上下文及
   前端。补丁在解析流式 chunk 和非流式响应时，对 tool_call.arguments 做标签清洗。

3. 保留 ``Authorization``：openjiuwen ``sanitize_headers`` 会剥离 protected
   ``authorization``。OfficeClaw / Huawei MaaS 经 tip ``default_headers`` 下发的是
   ``Authorization: Basic ...``（``api_key`` 仅为占位 ``huawei-maas-session``）；
   若不保留该头，网关会报 APIG.0303（Authorization header is missing）。

4. 华为 MaaS x-span-id 注入：调用华为云 ModelArts MaaS 时，在 HTTP 请求头注入
   ``x-span-id``（uuid4 hex），便于 MaaS 侧做链路追踪与本地日志对齐。仅当
   ``api_base`` 识别为 MaaS 端点时触发，非 MaaS 调用零开销透传。
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger("jiuwenswarm.llm_sse_patch")

# ── 华为云 ModelArts MaaS 端点识别 ───────────────────────────────────────
# 命中以下任一标记，或同时包含 modelarts + maas，即视为 MaaS 端点。
_HUAWEI_MAAS_API_MARKERS: tuple[str, ...] = (
    "modelarts-maas.com",
    "modelarts-maas.cn",
    "huaweiapaas.com",
    "agentarts",
)


def _is_huawei_maas_api_base(api_base: str) -> bool:
    """检测 ``api_base`` 是否指向华为云 ModelArts MaaS 服务。

    与旧架构 ``jiuwen_core_patch._is_huawei_maas_api_base`` 保持一致，确保
    MaaS 端点的识别规则向前兼容。
    """
    base = (api_base or "").lower()
    if any(marker in base for marker in _HUAWEI_MAAS_API_MARKERS):
        return True
    return "modelarts" in base and "maas" in base

_GLM_TOOL_XML_CLOSED_RE = re.compile(
    r"<(arg_value|arg_key|tool_call)[^>]*?>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_GLM_TOOL_XML_TRUNCATED_OPEN_RE = re.compile(
    r"^.*?<(?:arg_value|arg_key|tool_call)[^>]*?>",
    re.IGNORECASE | re.DOTALL,
)


def _sanitize_glm_tool_xml_tags(raw: str) -> str:
    """Strip GLM native XML tags and their inner content.

    Complete closed tags: remove entire segment including inner content.
    The closing tag name must match the opening tag name, so nested tags
    of different names are handled correctly without leaving stray
    closing tags behind.
    Truncated open tags: delete preceding text + the tag itself,
    preserve content after the tag.
      e.g. 'prefix<tool_call...>suffix' -> 'suffix'
    The ^-anchored regex is applied repeatedly until no truncated
    open tag remains, so multiple truncated tags are all removed.

    The early-exit guard checks for '<arg_' and '<tool_call' in
    lowercased input so that uppercase variants (e.g. <TOOL_CALL>)
    are also caught by the subsequent regex substitutions that use
    re.IGNORECASE.
    """
    if not raw:
        return raw
    lowered = raw.lower()
    if "<arg_" not in lowered and "<tool_call" not in lowered:
        return raw
    result = _GLM_TOOL_XML_CLOSED_RE.sub("", raw)
    prev = None
    while prev != result:
        prev = result
        result = _GLM_TOOL_XML_TRUNCATED_OPEN_RE.sub("", result)
    return result

_PATCH_APPLIED = False
# 每个补丁拥有独立的幂等标志：补丁之间互不短路，可以按渠道分别开启
# （例如仅 SSE 响应组装补丁需要按渠道门控）。
_AUTH_HEADER_PATCH_APPLIED = False
_RESPONSE_ASSEMBLY_PATCH_APPLIED = False
_GLM_XML_SANITIZE_PATCH_APPLIED = False
_MAAS_SPAN_ID_PATCH_APPLIED = False
_HUAWEI_MAAS_PLACEHOLDER_API_KEY = "huawei-maas-session"


def _extract_authorization_header(
    headers: dict[str, Any] | None,
) -> tuple[str, str] | None:
    """Return ``(original_key, value)`` for Authorization when present."""
    if not headers:
        return None
    for key, value in headers.items():
        if key is None or value is None:
            continue
        key_str = str(key).strip()
        if key_str.lower() != "authorization":
            continue
        value_str = str(value).strip()
        if not value_str:
            return None
        return key_str, value_str
    return None


def _restore_authorization_header(
    headers: dict[str, str],
    auth: tuple[str, str] | None,
) -> dict[str, str]:
    """Re-apply Authorization after sanitize (case-insensitive replace)."""
    if not auth:
        return headers
    key, value = auth
    drop = [existing for existing in headers if existing.lower() == "authorization"]
    for existing in drop:
        del headers[existing]
    headers[key] = value
    return headers


def _resolve_model_client_authorization(
    model_client_config: Any,
) -> tuple[str, str] | None:
    """Resolve explicit auth, or the scoped Huawei MaaS compatibility fallback."""
    auth = _extract_authorization_header(
        getattr(model_client_config, "custom_headers", None)
    )
    if auth is not None:
        return auth

    api_key = str(getattr(model_client_config, "api_key", "") or "").strip()
    if api_key != _HUAWEI_MAAS_PLACEHOLDER_API_KEY:
        return None

    try:
        from jiuwenswarm.common.local_env_config import (
            is_task_env_overlay_bound,
            read_default_headers,
        )

        if not is_task_env_overlay_bound():
            return None
        return _extract_authorization_header(read_default_headers())
    except Exception:
        logger.warning(
            "[llm_sse_patch] failed to resolve request-scoped Huawei MaaS Authorization",
            exc_info=True,
        )
        return None


def apply_openai_auth_header_patch() -> None:
    """Keep intentional ``Authorization`` on LLM client headers (Huawei MaaS Basic).

    openjiuwen clients do ``from headers_helper import build_base_headers``, so
    patching only ``headers_helper.build_base_headers`` is a no-op for them —
    we must also rebind the names already imported into each client module.

    Idempotent. Safe to call before or after :func:`apply_openai_sse_invoke_patch`.
    """
    global _AUTH_HEADER_PATCH_APPLIED
    if _AUTH_HEADER_PATCH_APPLIED:
        return

    try:
        from openjiuwen.core.foundation.llm import headers_helper
    except Exception as exc:  # pragma: no cover
        logger.warning("[llm_sse_patch] 未能导入 headers_helper，跳过 Authorization 补丁: %s", exc)
        return

    if getattr(headers_helper, "_auth_header_patch_applied", False):
        _AUTH_HEADER_PATCH_APPLIED = True
        return

    _orig_build_base_headers = headers_helper.build_base_headers
    _orig_merge_request_headers = headers_helper.merge_request_headers

    def _build_base_headers_keep_auth(
        *,
        custom_headers: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        auth = _extract_authorization_header(custom_headers)
        result = _orig_build_base_headers(custom_headers=custom_headers)
        return _restore_authorization_header(result, auth)

    def _merge_request_headers_keep_auth(
        base_headers: dict[str, Any] | None,
        request_custom_headers: dict[str, Any] | None,
    ) -> dict[str, str]:
        # Request-level Authorization wins over base when both are set.
        auth = _extract_authorization_header(request_custom_headers) or _extract_authorization_header(
            dict(base_headers) if base_headers else None
        )
        result = _orig_merge_request_headers(base_headers, request_custom_headers)
        return _restore_authorization_header(result, auth)

    headers_helper.build_base_headers = _build_base_headers_keep_auth
    headers_helper.merge_request_headers = _merge_request_headers_keep_auth
    headers_helper._auth_header_patch_applied = True  # pylint: disable=protected-access

    # Rebind already-imported names on model clients (from-import binding).
    client_modules = (
        "openjiuwen.core.foundation.llm.model_clients.openai_model_client",
        "openjiuwen.core.foundation.llm.model_clients.openai_account_model_client",
        "openjiuwen.core.foundation.llm.model_clients.anthropic_model_client",
    )
    rebound = 0
    for module_name in client_modules:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # pragma: no cover
            logger.debug("[llm_sse_patch] skip rebind %s: %s", module_name, exc)
            continue
        if hasattr(module, "build_base_headers"):
            module.build_base_headers = _build_base_headers_keep_auth
            rebound += 1
        if hasattr(module, "merge_request_headers"):
            module.merge_request_headers = _merge_request_headers_keep_auth

    # Belt-and-suspenders: inject Authorization into AsyncOpenAI(default_headers=...)
    # so auth does not depend solely on extra_headers surviving sanitize.
    try:
        from openjiuwen.core.foundation.llm.model_clients.openai_model_client import (
            OpenAIModelClient,
        )

        create_attr = "_create_async_openai_client"
        applied_attr = "_auth_default_headers_patch_applied"
        if not getattr(OpenAIModelClient, applied_attr, False):
            _orig_create = getattr(OpenAIModelClient, create_attr)

            def _create_async_openai_client_with_auth(self: Any, timeout: Any = None):
                client = _orig_create(self, timeout=timeout)
                auth = _resolve_model_client_authorization(self.model_client_config)
                if auth is None:
                    return client
                _key, value = auth
                try:
                    # OpenAI default_headers merges _custom_headers over api_key Bearer.
                    existing = dict(getattr(client, "_custom_headers", {}) or {})
                    existing["Authorization"] = value
                    setattr(client, "_custom_headers", existing)
                except Exception:
                    logger.warning(
                        "[llm_sse_patch] failed to inject Authorization into AsyncOpenAI",
                        exc_info=True,
                    )
                return client

            setattr(OpenAIModelClient, create_attr, _create_async_openai_client_with_auth)
            setattr(OpenAIModelClient, applied_attr, True)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "[llm_sse_patch] AsyncOpenAI Authorization 注入补丁跳过: %s", exc
        )

    _AUTH_HEADER_PATCH_APPLIED = True
    logger.info(
        "[llm_sse_patch] LLM Authorization header 保留补丁已应用 (rebound_client_modules=%s)",
        rebound,
    )


def _parse_chunk(chunk_str: str) -> dict | None:
    """解析单个数据块 JSON。"""
    if not chunk_str or not chunk_str.startswith("data:"):
        return None
    try:
        return json.loads(chunk_str[5:].strip())
    except json.JSONDecodeError as e:
        logger.info("[ParserPatch] JSON 解析错误: %s", e)
        return None


def _extract_message_content(chunk: dict) -> tuple[str, str]:
    """从 chunk 中提取思考内容和输出内容。"""
    if not chunk or not chunk.get("choices"):
        return "", ""
    msg = chunk["choices"][0]["message"]
    return msg.get("reasoning_token_text", ""), msg.get("token_text", "")


def _build_tool_calls(msg: dict) -> list | None:
    """从消息中构建工具调用对象列表。"""
    if not msg.get("tool_calls"):
        return None
    from openai.types.chat import ChatCompletionMessageFunctionToolCall
    from openai.types.chat.chat_completion_message_function_tool_call import Function

    return [
        ChatCompletionMessageFunctionToolCall(
            id=tc["id"],
            type="function",
            function=Function(
                name=tc["function"]["name"],
                arguments=_sanitize_glm_tool_xml_tags(tc["function"]["arguments"]),
            ),
        )
        for tc in msg["tool_calls"]
    ]


def assemble_openai_response(response: str) -> Any:
    """将分块 SSE 数据组装成标准的 OpenAI ``ChatCompletion``。"""
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice
    from openai.types.completion_usage import CompletionUsage

    content = think_content = ""
    last_chunk = None
    cache_chunk = ""

    for line in response.split("\n"):
        if not line.strip():
            continue
        if line.startswith("id:"):
            if cache_chunk:
                chunk = _parse_chunk(cache_chunk)
                cache_chunk = ""
                if chunk:
                    think, out = _extract_message_content(chunk)
                    think_content += think
                    content += out
                    last_chunk = chunk
        elif line.startswith("data:"):
            cache_chunk = line

    # 处理最后一个 chunk
    if cache_chunk:
        chunk = _parse_chunk(cache_chunk)
        if chunk:
            think, out = _extract_message_content(chunk)
            think_content += think
            content += out
            last_chunk = chunk

    # 提取并构建工具调用对象
    formatted_tool_calls = None
    if last_chunk and last_chunk.get("choices"):
        msg = last_chunk["choices"][0]["message"]
        formatted_tool_calls = _build_tool_calls(msg)

    # 构建 message（扩展 reasoning_content 字段存储思考内容）
    message = ChatCompletionMessage(
        role="assistant",
        content=content or None,
        tool_calls=formatted_tool_calls,
    )
    if think_content:
        message.reasoning_content = think_content

    # 构建 usage
    usage = None
    if last_chunk and last_chunk.get("usage"):
        u = last_chunk["usage"]
        usage = CompletionUsage(
            prompt_tokens=u.get("prompt_tokens", 0),
            completion_tokens=u.get("completion_tokens", 0),
            total_tokens=u.get("total_tokens", 0),
        )

    # 构建 finish_reason：有工具调用时优先使用 "tool_calls"
    finish_reason = "stop"
    if formatted_tool_calls:
        finish_reason = "tool_calls"
    elif last_chunk and last_chunk.get("choices"):
        finish_reason = last_chunk["choices"][0].get("finish_reason", "stop")

    return ChatCompletion(
        id=last_chunk.get("id", "chatcmpl-default") if last_chunk else "chatcmpl-default",
        choices=[
            Choice(
                index=0,
                message=message,
                finish_reason=finish_reason,
            )
        ],
        created=int(time.time()),
        model="unknown",
        object="chat.completion",
        usage=usage,
    )


def _import_openai_model_client() -> Any | None:
    """导入 ``OpenAIModelClient``；openjiuwen 不可用时返回 ``None``。"""
    try:
        from openjiuwen.core.foundation.llm.model_clients.openai_model_client import (
            OpenAIModelClient,
        )
    except Exception as exc:  # pragma: no cover - openjiuwen 不可用时静默跳过
        logger.warning("[llm_sse_patch] 未能导入 OpenAIModelClient，跳过补丁: %s", exc)
        return None
    return OpenAIModelClient


def apply_openai_response_assembly_patch() -> None:
    """Patch 1: SSE-only 网关兼容 —— 非流式调用返回 ``str`` 时组装成标准 ``ChatCompletion``。

    该补丁只对「以 ``text/event-stream`` 返回非流式响应」的网关有意义，因此是
    唯一需要按渠道门控的补丁（见 ``app_agentserver._should_apply_sse_invoke_patch``）；
    其余补丁（GLM 清洗 / Authorization / MaaS x-span-id）对所有渠道无条件生效。

    幂等：使用独立的 ``_RESPONSE_ASSEMBLY_PATCH_APPLIED`` 标志，不受其他补丁影响。
    """
    global _RESPONSE_ASSEMBLY_PATCH_APPLIED
    if _RESPONSE_ASSEMBLY_PATCH_APPLIED:
        return

    client_cls = _import_openai_model_client()
    if client_cls is None:
        return

    if getattr(client_cls, "_response_assembly_patch_applied", False):
        _RESPONSE_ASSEMBLY_PATCH_APPLIED = True
        return

    _orig_parse_response = client_cls._parse_response  # pylint: disable=protected-access

    async def _parse_response_with_sse_guard(
        self: Any,
        response: Any,
        parser: Optional[Any] = None,
    ):
        if isinstance(response, str):
            response = assemble_openai_response(response)
        return await _orig_parse_response(self, response, parser)

    client_cls._parse_response = _parse_response_with_sse_guard  # pylint: disable=protected-access
    client_cls._response_assembly_patch_applied = True  # pylint: disable=protected-access
    _RESPONSE_ASSEMBLY_PATCH_APPLIED = True
    logger.info("[llm_sse_patch] OpenAIModelClient SSE 响应组装补丁已应用")


def apply_glm_tool_xml_sanitize_patch() -> None:
    """Patch 2: GLM XML 标签清洗 —— 剥离流式 chunk 中 tool_call.arguments 的原生 XML 标签。

    生产使用 GLM 模型（含 glm-5.2）时必需，与渠道类型无关，必须无条件应用。

    幂等：使用独立的 ``_GLM_XML_SANITIZE_PATCH_APPLIED`` 标志。
    """
    global _GLM_XML_SANITIZE_PATCH_APPLIED
    if _GLM_XML_SANITIZE_PATCH_APPLIED:
        return

    client_cls = _import_openai_model_client()
    if client_cls is None:
        return

    if getattr(client_cls, "_glm_xml_sanitize_patch_applied", False):
        _GLM_XML_SANITIZE_PATCH_APPLIED = True
        return

    _orig_parse_stream_chunk = client_cls._parse_stream_chunk  # pylint: disable=protected-access

    def _parse_stream_chunk_with_sanitize(self: Any, chunk: Any):
        result = _orig_parse_stream_chunk(self, chunk)
        if result is not None and getattr(result, "tool_calls", None):
            for tc in result.tool_calls:
                orig_args = getattr(tc, "arguments", None)
                if isinstance(orig_args, str):
                    sanitized = _sanitize_glm_tool_xml_tags(orig_args)
                    if sanitized != orig_args:
                        try:
                            tc.arguments = sanitized
                        except (AttributeError, TypeError):
                            pass
        return result

    client_cls._parse_stream_chunk = _parse_stream_chunk_with_sanitize  # pylint: disable=protected-access
    client_cls._glm_xml_sanitize_patch_applied = True  # pylint: disable=protected-access
    _GLM_XML_SANITIZE_PATCH_APPLIED = True
    logger.info("[llm_sse_patch] OpenAIModelClient GLM XML 标签清洗补丁已应用")


def apply_huawei_maas_span_id_patch() -> None:
    """Patch 4: 华为 MaaS x-span-id 注入。

    调用华为云 ModelArts MaaS 时，在请求头注入 ``x-span-id``（uuid4 hex）便于
    MaaS 侧链路追踪。通过包装 ``invoke`` / ``stream`` 出口，复用原 custom_headers
    合并链路（``_build_request_headers``），非 MaaS 端点零开销透传。
    每次 ``invoke`` / ``stream`` 调用（含重试触发的重新调用）都会生成独立 span_id。

    生产 OfficeClaw + Huawei MaaS 链路依赖该补丁，与渠道类型无关，必须无条件应用。

    幂等：使用独立的 ``_MAAS_SPAN_ID_PATCH_APPLIED`` 标志。
    """
    global _MAAS_SPAN_ID_PATCH_APPLIED
    if _MAAS_SPAN_ID_PATCH_APPLIED:
        return

    client_cls = _import_openai_model_client()
    if client_cls is None:
        return

    if getattr(client_cls, "_maas_span_id_patch_applied", False):
        _MAAS_SPAN_ID_PATCH_APPLIED = True
        return

    _orig_invoke = client_cls.invoke
    _orig_stream = client_cls.stream

    def _maybe_inject_maas_span_id(self: Any, kwargs: dict) -> None:
        """当 self 指向华为 MaaS 端点时，向 kwargs 注入 x-span-id 到 custom_headers。"""
        mcc = getattr(self, "model_client_config", None)
        api_base = getattr(mcc, "api_base", "") if mcc is not None else ""
        if not _is_huawei_maas_api_base(api_base):
            return
        existing = kwargs.get("custom_headers")
        headers = dict(existing or {})
        headers["x-span-id"] = uuid.uuid4().hex
        kwargs["custom_headers"] = headers

    async def _invoke_with_maas_span(self: Any, *args, **kwargs):
        _maybe_inject_maas_span_id(self, kwargs)
        return await _orig_invoke(self, *args, **kwargs)

    async def _stream_with_maas_span(self: Any, *args, **kwargs):
        _maybe_inject_maas_span_id(self, kwargs)
        async for chunk in _orig_stream(self, *args, **kwargs):
            yield chunk

    client_cls.invoke = _invoke_with_maas_span
    client_cls.stream = _stream_with_maas_span

    client_cls._maas_span_id_patch_applied = True  # pylint: disable=protected-access
    _MAAS_SPAN_ID_PATCH_APPLIED = True
    logger.info("[llm_sse_patch] OpenAIModelClient 华为 MaaS x-span-id 注入补丁已应用")


def apply_openai_sse_invoke_patch() -> None:
    """聚合入口：一次性应用全部 4 个补丁（保留以兼容既有调用方）。

    ⚠️ 服务启动路径**不要**直接调用本函数：它会把「仅对 SSE-only 网关有意义」的
    :func:`apply_openai_response_assembly_patch` 一并打开。启动时应分别调用
    :func:`apply_openai_auth_header_patch` / :func:`apply_glm_tool_xml_sanitize_patch`
    / :func:`apply_huawei_maas_span_id_patch`（无条件），再按渠道门控决定是否调用
    :func:`apply_openai_response_assembly_patch`。

    幂等：4 个子补丁各自幂等，重复调用只生效一次。
    """
    global _PATCH_APPLIED
    apply_openai_auth_header_patch()
    apply_glm_tool_xml_sanitize_patch()
    apply_huawei_maas_span_id_patch()
    apply_openai_response_assembly_patch()
    _PATCH_APPLIED = True
