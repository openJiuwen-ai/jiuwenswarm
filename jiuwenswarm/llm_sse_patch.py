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
   ``api_base`` 识别为 MaaS 端点时触发，非 MaaS 调用零开销透传；调用方或
   ``before_model_http`` 扩展已提供该头时保留其值。新核心的 ``maas``
   endpoint profile 会无条件覆盖该头，这里一并接管其 ``inject_x_span_id``，
   保证最终请求头里至多一个 ``x-span-id`` 且优先使用调用方/扩展的值。

5. before_model_http 扩展埋点：模型 HTTP 出口的公共事件点。按清单包装各模型
   客户端的 ``invoke`` / ``stream`` / ``generate_image``，在真正发出 HTTP 之前
   触发 ``agent_server:before_model_http``，扩展往 context 的 ``headers`` /
   ``extra_body`` 口袋写值，核心在 trigger返回后按保留键白名单合并进本次请求。
   无人登记或扩展框架未初始化时零副作用，请求与现状完全一致。
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit

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
_MAAS_PROFILE_SPAN_PATCH_APPLIED = False
_MODEL_HTTP_HOOK_PATCH_APPLIED = False
_HUAWEI_MAAS_PLACEHOLDER_API_KEY = "huawei-maas-session"

# before_model_http 埋点：扩展口袋的保留键，防止扩展弄坏鉴权与 thinking。
# 请求头按大小写不敏感匹配（HTTP header 名本身大小写不敏感）。
# x-span-id 不在此列：扩展可以注入自己的链路 id，MaaS 自动注入仅在扩展与
# 调用方都没提供时兜底（见 _maybe_inject_maas_span_id）。
_RESERVED_HOOK_HEADERS = frozenset({"authorization"})
_RESERVED_HOOK_EXTRA_BODY_KEYS = frozenset(
    {"thinking", "enable_thinking", "reasoning_effort"}
)

# before_model_http 出口包装清单：方法集合按各客户端实际支持的 custom_headers
# 通道确定（方法内 pop custom_headers 并经 merge_request_headers 合并进
# extra_headers）。Anthropic / OpenAIAccount 的 generate_image 等多媒体出口
# 直接抛 NotImplementedError、无 HTTP 出口，不包；OpenAIAccount 为独立实现
# 但 invoke/stream 通道完整，已列入。
# 无法通过追加清单覆盖的客户端（2026-10 逐文件核实）：SiliconFlow /
# IntelliRouter / AscendAffinity / InferenceAffinity 的出口方法不支持
# custom_headers 通道（口袋合并后进不了请求头）；DashScope 重写的
# generate_image 走自有 Wanx HTTP、DeepSeek 的 generate_image/speech/video
# 均为 pass 桩——需上游先支持通道后才可追加。子类重写的同名方法会遮蔽父类
# 包装，要覆盖需将子类追加进本清单（包装器只包类自身定义的方法，继承方法
# 由父类包装覆盖，不会双包）。
_MODEL_HTTP_HOOK_CLIENT_SPECS: tuple = (
    (
        "openjiuwen.core.foundation.llm.model_clients."
        "openai_model_client.OpenAIModelClient",
        ("invoke", "stream", "generate_image"),
    ),
    (
        "openjiuwen.core.foundation.llm.model_clients."
        "anthropic_model_client.AnthropicModelClient",
        ("invoke", "stream"),
    ),
    (
        "openjiuwen.core.foundation.llm.model_clients."
        "openai_account_model_client.OpenAIAccountModelClient",
        ("invoke", "stream"),
    ),
)


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
    client_cls._response_assembly_original_parse_response = (  # pylint: disable=protected-access
        _orig_parse_response
    )

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


def remove_openai_response_assembly_patch() -> bool:
    """移除按渠道启用的 SSE 响应组装补丁，并恢复原始解析方法。

    运行时配置可能删除或禁用 ``xiaoyi_claw``。此时必须撤销进程级补丁，避免
    后续其他渠道的普通字符串响应继续经过 SSE 组装逻辑。
    """
    global _RESPONSE_ASSEMBLY_PATCH_APPLIED

    client_cls = _import_openai_model_client()
    if client_cls is None:
        return False

    original = getattr(
        client_cls,
        "_response_assembly_original_parse_response",
        None,
    )
    if original is None:
        _RESPONSE_ASSEMBLY_PATCH_APPLIED = False
        client_cls._response_assembly_patch_applied = False  # pylint: disable=protected-access
        return False

    client_cls._parse_response = original  # pylint: disable=protected-access
    delattr(client_cls, "_response_assembly_original_parse_response")
    client_cls._response_assembly_patch_applied = False  # pylint: disable=protected-access
    _RESPONSE_ASSEMBLY_PATCH_APPLIED = False
    logger.info("[llm_sse_patch] OpenAIModelClient SSE 响应组装补丁已移除")
    return True


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


# ── MaaS x-span-id 策略 ─────────────────────────────────────────────
# 三个入口覆盖不同配置路径，共用下面这组函数保证语义一致：
#   1. before_model_http 合并层：对所有头按大小写不敏感去重（调用方非空优先）；
#   2. invoke/stream 包装：client_provider=OpenAI + MaaS api_base 时兜底注入；
#   3. endpoint profile transform：endpoint_profile=maas 时兜底注入。
# 优先级：请求级（调用方 > 扩展，均需非空） > 配置级 custom_headers > 自动 uuid4。
# 兜底注入统一走 _write_span_id，保证最终请求头里只有一个 x-span-id
# （HTTP 头名大小写不敏感，多个变体会被 httpx 合并成 "a, b"）。
_SPAN_HEADER = "x-span-id"


def _span_id_value(headers: Any) -> str | None:
    """返回第一个非空 ``x-span-id`` 值（键名大小写不敏感）；空/纯空白视为未提供。"""
    for key, value in (headers or {}).items():
        if str(key).lower() != _SPAN_HEADER:
            continue
        text = "" if value is None else str(value)
        if text.strip():
            return text
    return None


def _write_span_id(headers: dict[str, str], value: str) -> dict[str, str]:
    """清掉所有同名（任意大小写）键后写入唯一 ``x-span-id``。"""
    for stale in [item for item in headers if str(item).lower() == _SPAN_HEADER]:
        del headers[stale]
    headers[_SPAN_HEADER] = value
    return headers


def _ensure_span_id(headers: dict[str, str]) -> dict[str, str]:
    """已有非空值时原样保留，否则兜底生成 uuid4（并清掉同名空键）。"""
    if _span_id_value(headers) is not None:
        return headers
    return _write_span_id(headers, uuid.uuid4().hex)


def _patch_maas_profile_span_id() -> bool:
    """兼容 openjiuwen ``maas`` endpoint profile 的 ``inject_x_span_id``。

    新核心把华为 MaaS 的建链能力从 ``HuaweiMaasModelClient`` 迁到
    ``endpoint_profiles``：``maas`` profile 的 ``inject_x_span_id`` 会在
    ``_build_request_headers`` 末尾无条件覆盖 ``x-span-id``（源码注释写明
    "replaces any caller-provided value"）。若不接管，扩展/调用方注入的链路
    id 在 MaaS 主路径上会被静默丢弃。

    旧核心没有该模块时静默跳过（debug 日志），兼容由 ``invoke``/``stream``
    包装的注入兜底完成。幂等：使用独立的
    ``_MAAS_PROFILE_SPAN_PATCH_APPLIED`` 模块标志。
    """
    global _MAAS_PROFILE_SPAN_PATCH_APPLIED
    if _MAAS_PROFILE_SPAN_PATCH_APPLIED:
        return True

    try:
        from openjiuwen.core.foundation.llm.utils import endpoint_profiles
    except ImportError:
        logger.debug(
            "[llm_sse_patch] endpoint_profiles 不可用，跳过 MaaS profile x-span-id 兼容"
        )
        return False

    transforms = getattr(endpoint_profiles, "REQUEST_HEADER_TRANSFORMS", None)
    if not isinstance(transforms, dict) or "inject_x_span_id" not in transforms:
        logger.debug(
            "[llm_sse_patch] 未找到 inject_x_span_id transform，跳过 x-span-id 兼容"
        )
        return False

    transforms["inject_x_span_id"] = _ensure_span_id
    _MAAS_PROFILE_SPAN_PATCH_APPLIED = True
    return True


def apply_huawei_maas_span_id_patch() -> None:
    """Patch 4: 华为 MaaS x-span-id 注入。

    调用华为云 ModelArts MaaS 时，在请求头注入 ``x-span-id``（uuid4 hex）便于
    MaaS 侧链路追踪。通过包装 ``invoke`` / ``stream`` 出口，复用原 custom_headers
    合并链路（``_build_request_headers``），非 MaaS 端点零开销透传。
    每次 ``invoke`` / ``stream`` 调用（含重试触发的重新调用）都会生成独立 span_id，
    除非调用方或 ``before_model_http`` 扩展已提供 ``x-span-id``——此时保留其值，
    不覆盖（扩展注入的链路 id 优先）。

    同时接管 openjiuwen ``maas`` endpoint profile 的 ``inject_x_span_id``：新核心
    在 ``_build_request_headers`` 末尾会无条件覆盖 ``x-span-id``，这里替换为
    「已有非空值则保留」的实现，保证扩展/调用方注入的链路 id 在 MaaS 主路径
    上不被覆盖（见 ``_patch_maas_profile_span_id``）。旧核心无该模块时静默跳过。

    生产 OfficeClaw + Huawei MaaS 链路依赖该补丁，与渠道类型无关，必须无条件应用。

    幂等：使用独立的 ``_MAAS_SPAN_ID_PATCH_APPLIED`` 标志。
    """
    global _MAAS_SPAN_ID_PATCH_APPLIED

    # endpoint profile 头兼容与 invoke/stream 包装相互独立，先于早退安装
    _patch_maas_profile_span_id()

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
        headers = dict(kwargs.get("custom_headers") or {})
        if _span_id_value(headers) is not None:
            return
        kwargs["custom_headers"] = _write_span_id(headers, uuid.uuid4().hex)

    async def _invoke_with_maas_span(self: Any, *args, **kwargs):
        _maybe_inject_maas_span_id(self, kwargs)
        return await _orig_invoke(self, *args, **kwargs)

    async def _stream_with_maas_span(self: Any, *args, **kwargs):
        _maybe_inject_maas_span_id(self, kwargs)
        _mcc = getattr(self, "model_client_config", None)
        _mc = getattr(self, "model_config", None)
        _span = (
            _span_id_value(kwargs.get("custom_headers"))
            or _span_id_value(getattr(_mcc, "custom_headers", None))
            or "-"
        )
        logger.info(
            "[MODEL_HTTP] begin span=%s model=%s host=%s idle_timeout=%s",
            _span,
            kwargs.get("model") or getattr(_mc, "model_name", "") or "-",
            urlsplit(str(getattr(_mcc, "api_base", "") or "")).netloc or "-",
            getattr(_mcc, "stream_idle_timeout", None),
        )
        # [PERF] 打点:LLM 流调用 TTFT 与总时长
        import time as _perf_time
        _t0 = _perf_time.perf_counter()
        _ttft_ms = None
        _producer_s = 0.0
        _n_chunks = 0
        _status = "ok"
        _exc_name = "-"
        _it = _orig_stream(self, *args, **kwargs)
        try:
            while True:
                _t_next = _perf_time.perf_counter()
                try:
                    chunk = await _it.__anext__()
                except StopAsyncIteration:
                    break
                _producer_s += _perf_time.perf_counter() - _t_next
                _n_chunks += 1
                if _ttft_ms is None:
                    _ttft_ms = (_perf_time.perf_counter() - _t0) * 1000
                    logger.info("[PERF] llm_stream ttft_ms=%.1f", _ttft_ms)
                yield chunk
        except BaseException as exc:
            _status = "error"
            _exc_name = type(exc).__name__
            raise
        finally:
            # 正常结束、超时、取消都打：失败时也要留下本次尝试的汇总
            logger.info(
                "[MODEL_HTTP] end span=%s status=%s chunks=%d ttft_ms=%s producer_ms=%.1f "
                "total_ms=%.1f exc=%s",
                _span,
                _status,
                _n_chunks,
                f"{_ttft_ms:.1f}" if _ttft_ms is not None else "-",
                _producer_s * 1000,
                (_perf_time.perf_counter() - _t0) * 1000,
                _exc_name,
            )

    client_cls.invoke = _invoke_with_maas_span
    client_cls.stream = _stream_with_maas_span

    client_cls._maas_span_id_patch_applied = True  # pylint: disable=protected-access
    _MAAS_SPAN_ID_PATCH_APPLIED = True
    logger.info("[llm_sse_patch] OpenAIModelClient 华为 MaaS x-span-id 注入补丁已应用")


def _merge_model_http_hook_ctx(ctx: Any, kwargs: dict) -> None:
    """把扩展写入的 ``headers`` / ``extra_body`` 口袋合并进本次调用 kwargs。

    - ``headers`` 逐键合并进 ``kwargs["custom_headers"]``，经
      ``_build_request_headers`` 进入最终请求头。头名按大小写不敏感去重
      （HTTP 头名不区分大小写，``X-Foo`` 与 ``x-foo`` 并存会被 httpx 合并成
      ``"a, b"``）：同名已存在非空值时保留调用方值；调用方值为空/纯空白时
      由扩展值覆盖并沿用扩展键名；扩展值为空/纯空白时跳过；
    - ``extra_body`` 逐键 setdefault 进 ``kwargs["extra_body"]``，经
      ``_build_request_params`` 进入 ``params["extra_body"]``，最终由 SDK
      摊开进请求 JSON 正文；
    - 保留键（``Authorization`` / thinking 族）扩展写了也丢弃。``x-span-id``
      不在保留键内，额外约定它优先于 MaaS 自动注入的 span id（见
      ``apply_huawei_maas_span_id_patch``）；
    - 含 CR/LF 的头值、非 latin-1 头值（httpx 发送时仅接受 latin-1，中文等
      需扩展侧先行 URL 编码）、无法 JSON 序列化的正文字段一律跳过，防扩展
      写出非法值弄坏本次请求；
    - 未写的口袋保持原样；``extra_body`` 为空则正文零变化。
    """
    headers = getattr(ctx, "headers", None)
    if headers:
        merged = dict(kwargs.get("custom_headers") or {})
        # HTTP 头名大小写不敏感，按小写建立索引做通用去重，避免 X-Foo 与 x-foo
        # 并存后被 httpx 合并成 "a, b"。规则对所有头一致：调用方非空值优先；
        # 扩展空值忽略；调用方空值时允许扩展值覆盖（沿用扩展的键名）。
        existing_keys = {str(key).lower(): key for key in merged}
        for key, value in headers.items():
            name = str(key or "").strip()
            if not name or name.lower() in _RESERVED_HOOK_HEADERS:
                continue
            if value is None:
                continue
            text = str(value)
            if "\r" in text or "\n" in text:
                continue
            try:
                # httpx 头值仅接受 latin-1 可编码字符；中文等需扩展侧先编码
                text.encode("latin-1")
            except UnicodeEncodeError:
                continue
            if not text.strip():
                continue
            lowered = name.lower()
            current_key = existing_keys.get(lowered)
            if current_key is not None:
                if str(merged[current_key]).strip():
                    continue
                del merged[current_key]
            merged[name] = text
            existing_keys[lowered] = name
        kwargs["custom_headers"] = merged
    extra_body = getattr(ctx, "extra_body", None)
    if extra_body:
        merged_body = dict(kwargs.get("extra_body") or {})
        for key, value in extra_body.items():
            name = str(key or "")
            if not name or name in _RESERVED_HOOK_EXTRA_BODY_KEYS:
                continue
            if value is None:
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                continue
            merged_body.setdefault(name, value)
        kwargs["extra_body"] = merged_body


async def _trigger_before_model_http(client: Any, kwargs: dict) -> None:
    """触发 ``before_model_http`` 埋点并把扩展口袋合并进本次调用。

    - 扩展框架不可导入或 registry 未初始化（``trigger_if_initialized``
      返回 False）：零副作用跳过；
    - handler 异常只记日志，本次请求不带扩展字段照常发出——积分参数失败
      不能变成用户可见的对话失败；
    - 无人登记该事件时 callback 框架空转，请求与现状完全一致。
    """
    # 惰性导入：llm_sse_patch 在进程启动极早期被加载，此时扩展框架尚未
    # 初始化；且 extensions.registry 会拉起 gateway 一大片依赖，避免循环导入。
    try:
        from jiuwenswarm.extensions.hook_event import AgentServerHookEvents
        from jiuwenswarm.extensions.hooks_context import ModelHttpHookContext
        from jiuwenswarm.extensions.registry import ExtensionRegistry
    except Exception:  # pragma: no cover - jiuwenswarm 扩展框架不可用
        logger.debug("[before_model_http] extension framework unavailable, skip")
        return

    mcc = getattr(client, "model_client_config", None)
    mc = getattr(client, "model_config", None)
    ctx = ModelHttpHookContext(
        # api_base 来自 ModelClientConfig（与 x-span-id 补丁同源）；
        # model_name 来自 ModelRequestConfig（base 客户端组参同源）
        api_base=str(getattr(mcc, "api_base", "") or ""),
        model_name=str(getattr(mc, "model_name", "") or ""),
    )
    try:
        triggered = await ExtensionRegistry.trigger_if_initialized(
            AgentServerHookEvents.BEFORE_MODEL_HTTP, ctx
        )
    except Exception:
        logger.warning(
            "[before_model_http] handler failed; send request without hook attrs",
            exc_info=True,
        )
        return
    if not triggered:
        return
    try:
        _merge_model_http_hook_ctx(ctx, kwargs)
    except Exception:
        logger.warning(
            "[before_model_http] merge hook attrs failed; send without them",
            exc_info=True,
        )


def _import_model_client_class(dotted: str) -> Any:
    """按 ``module.Class`` 点名导入模型客户端类；失败返回 None。"""
    module_name, _, class_name = dotted.rpartition(".")
    try:
        module = importlib.import_module(module_name)
        return getattr(module, class_name, None)
    except Exception:
        return None


def _wrap_model_http_hook_on_class(
    client_cls: Any, method_names: tuple
) -> list:
    """对单个模型客户端类安装 before_model_http 出口包装（幂等）。

    返回实际包装的方法名列表。只包装类自身定义的方法（``vars`` 直查）：
    从父类继承的方法已由父类上的包装覆盖，再包一层会导致每次调用双触发
    埋点。注意继承遮蔽：子类若重写了清单中的同名方法（如 DashScope 重写
    generate_image），父类上的包装对子类实例无效，需把子类追加进
    ``_MODEL_HTTP_HOOK_CLIENT_SPECS`` 才能覆盖。
    """
    if getattr(client_cls, "_model_http_hook_patch_applied", False):
        return []

    wrapped: list = []
    for name in method_names:
        if name not in vars(client_cls):
            continue
        orig = getattr(client_cls, name, None)
        if orig is None:
            continue
        if name == "stream":

            async def _stream_with_model_http_hook(
                self: Any, *args, _orig=orig, **kwargs
            ):
                # 与 invoke 同口：先触发埋点（原地改 kwargs），再转发原生成器。
                await _trigger_before_model_http(self, kwargs)
                async for chunk in _orig(self, *args, **kwargs):
                    yield chunk

            setattr(client_cls, name, _stream_with_model_http_hook)
        else:

            async def _call_with_model_http_hook(
                self: Any, *args, _orig=orig, **kwargs
            ):
                await _trigger_before_model_http(self, kwargs)
                return await _orig(self, *args, **kwargs)

            setattr(client_cls, name, _call_with_model_http_hook)
        wrapped.append(name)

    if wrapped:
        setattr(client_cls, "_model_http_hook_patch_applied", True)
    return wrapped


def apply_before_model_http_hook_patch() -> None:
    """Patch 5: ``before_model_http`` 扩展埋点 —— 模型 HTTP 出口的公共事件点。

    按 ``_MODEL_HTTP_HOOK_CLIENT_SPECS`` 包装各模型客户端的 ``invoke`` /
    ``stream`` / ``generate_image``，在真正发出 HTTP 之前触发
    ``agent_server:before_model_http``：扩展往 context 的 ``headers`` /
    ``extra_body`` 口袋写值，核心在 trigger
    返回后按保留键白名单合并进本次请求。工具循环、重试、换模型、图像生成都经过该出口。

    客户端覆盖范围（见清单）：OpenAI 兼容族（未重写出口方法的子类含
    HuaweiMaas / OpenRouter 经继承自动覆盖）、Anthropic 与 OpenAIAccount。
    SiliconFlow / IntelliRouter / Ascend & InferenceAffinity 及 DashScope /
    DeepSeek 重写的多媒体出口因上游不支持 custom_headers 通道而无法经本
    清单覆盖（包装了口袋也进不了请求头）；generate_speech / generate_video
    出口 openjiuwen 尚不支持 custom_headers 通道，均不在本埋点覆盖内。

    与渠道无关，无条件应用；扩展框架未初始化或无人登记时零副作用，请求与
    现状完全一致。幂等：使用独立的 ``_MODEL_HTTP_HOOK_PATCH_APPLIED`` 标志，
    不受其他补丁影响；仅当至少包装了一个客户端时才置位——全部客户端导入
    失败时保持未应用，下次调用可重试（与 Patch 1/2 的可重试语义一致）。
    """
    global _MODEL_HTTP_HOOK_PATCH_APPLIED
    if _MODEL_HTTP_HOOK_PATCH_APPLIED:
        return

    wrapped_classes: list = []
    for dotted, method_names in _MODEL_HTTP_HOOK_CLIENT_SPECS:
        client_cls = _import_model_client_class(dotted)
        if client_cls is None:
            logger.warning(
                "[llm_sse_patch] before_model_http: 客户端 %s 不可用，跳过", dotted
            )
            continue
        methods = _wrap_model_http_hook_on_class(client_cls, method_names)
        if methods:
            wrapped_classes.append(f"{client_cls.__name__}({','.join(methods)})")

    if wrapped_classes:
        _MODEL_HTTP_HOOK_PATCH_APPLIED = True
        logger.info(
            "[llm_sse_patch] before_model_http 扩展埋点补丁已应用: %s",
            ", ".join(wrapped_classes),
        )
    else:
        logger.warning(
            "[llm_sse_patch] before_model_http 扩展埋点补丁未包装任何客户端，"
            "保持未应用状态以便后续重试"
        )


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
