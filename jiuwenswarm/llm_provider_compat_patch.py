"""Runtime compatibility fixes for provider-specific OpenAI/Anthropic dialects."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse
from weakref import WeakSet


logger = logging.getLogger("jiuwenswarm.llm_provider_compat_patch")

_MODELARTS_TOOLS_NONE_MODELS = frozenset({"qwen3-30b-a3b", "qwen3-32b"})
_MODELARTS_PANGU_MODELS = frozenset({"openpangu-2.0-pro", "openpangu-2.0-flash"})
_ANTHROPIC_PATCHED_CLASSES: WeakSet[type] = WeakSet()
_OPENAI_PATCHED_CLASSES: WeakSet[type] = WeakSet()
_PROVIDER_PATCHES_APPLIED = False


def _host_from_client(client: Any) -> str:
    config = getattr(client, "model_client_config", None)
    # Keep compatibility with the current core schema and older local builds.
    api_url = (
        getattr(config, "api_base", None)
        or getattr(config, "base_url", None)
        or ""
    )
    return (urlparse(str(api_url)).hostname or "").lower()


def _is_modelarts(client: Any) -> bool:
    return _host_from_client(client) == "api.modelarts-maas.com"


def _flatten_text_blocks(value: Any) -> Any:
    """Use the scalar text shape accepted by ModelArts' Anthropic façade."""
    if not isinstance(value, list) or not value:
        return value
    texts: list[str] = []
    for block in value:
        if not isinstance(block, Mapping) or block.get("type") != "text":
            return value
        texts.append(str(block.get("text") or ""))
    return "\n".join(texts)


def _patch_anthropic_modelarts(client_class: type) -> None:
    if client_class in _ANTHROPIC_PATCHED_CLASSES:
        return
    # AnthropicModelClient builds its final payload through
    # ``_build_anthropic_params``. Patching the inherited OpenAI-shaped
    # ``_build_request_params`` does not affect real Anthropic calls because
    # the implementation deliberately invokes ``super()._build_request_params``.
    original = client_class._build_anthropic_params  # pylint: disable=protected-access

    def _build_anthropic_params(self, *args, **kwargs):
        params = original(self, *args, **kwargs)
        if not _is_modelarts(self):
            return params
        for message in params.get("messages") or []:
            if isinstance(message, dict):
                message["content"] = _flatten_text_blocks(message.get("content"))
        if "system" in params:
            params["system"] = _flatten_text_blocks(params.get("system"))

        model_name = str(params.get("model") or "").strip().lower()
        if model_name in _MODELARTS_PANGU_MODELS and "thinking" not in params:
            # ModelArts enables thinking by default for openPangu. In that
            # mode every replayed assistant turn must contain the exact
            # provider-issued thinking block and signature. JiuwenSwarm's
            # ordinary persisted chat history contains the visible answer,
            # not that provider-private block, so a later turn otherwise
            # fails with ModelArts.81001 (missing 'thinking' field). Make the
            # ordinary/no-explicit-reasoning path deterministic. An explicit
            # reasoning configuration already produces ``thinking`` and is
            # intentionally preserved.
            params["thinking"] = {"type": "disabled"}
        return params

    client_class._build_anthropic_params = _build_anthropic_params  # type: ignore[method-assign]  # pylint: disable=protected-access
    _ANTHROPIC_PATCHED_CLASSES.add(client_class)


def _patch_openai_modelarts_tool_choice(client_class: type) -> None:
    if client_class in _OPENAI_PATCHED_CLASSES:
        return
    original = client_class._build_request_params  # pylint: disable=protected-access

    def _build_request_params(self, *args, **kwargs):
        params = original(self, *args, **kwargs)
        model_name = str(params.get("model") or "").strip().lower()
        is_affected_model = model_name in _MODELARTS_TOOLS_NONE_MODELS
        if not _is_modelarts(self) or not is_affected_model:
            return params
        if params.get("tools") and params.get("tool_choice", "auto") == "auto":
            # This hosted model accepts tool schemas but rejects auto selection
            # unless the serving deployment enables a tool-call parser.
            params["tool_choice"] = "none"
        return params

    client_class._build_request_params = _build_request_params  # type: ignore[method-assign]  # pylint: disable=protected-access
    _OPENAI_PATCHED_CLASSES.add(client_class)


def _is_continuation_of(prev: Any, fragment: Any) -> bool:
    same_index = prev.index is None or fragment.index is None or prev.index == fragment.index
    return bool(prev.id) and not fragment.id and not fragment.name and prev.type == fragment.type and same_index


def _coalesce_tool_call_fragments(chunk: Any) -> Any:
    tool_calls = getattr(chunk, "tool_calls", None)
    if not tool_calls or len(tool_calls) < 2:
        return chunk
    merged: list[Any] = []
    for tool_call in tool_calls:
        if merged and _is_continuation_of(merged[-1], tool_call):
            prev = merged[-1]
            merged[-1] = prev.model_copy(update={
                "arguments": (prev.arguments or "") + (tool_call.arguments or ""),
                "response_item_id": prev.response_item_id or tool_call.response_item_id,
            })
        else:
            merged.append(tool_call)
    if len(merged) == len(tool_calls):
        return chunk
    return chunk.model_copy(update={"tool_calls": merged})


def _drop_orphan_tool_calls(messages: Any) -> Any:
    if not isinstance(messages, list):
        return messages
    repaired_messages = messages
    for position, message in enumerate(messages):
        tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not isinstance(tool_calls, list):
            continue
        kept = [
            tool_call for tool_call in tool_calls
            if not isinstance(tool_call, Mapping)
            or tool_call.get("id")
            or (tool_call.get("function") or {}).get("name")
        ]
        if len(kept) == len(tool_calls):
            continue
        if repaired_messages is messages:
            repaired_messages = list(messages)
        repaired = {**message, "tool_calls": kept}
        if not kept:
            repaired.pop("tool_calls")
        repaired_messages[position] = repaired
        logger.warning("Dropped %d tool call(s) without id and name from history message %d",
                       len(tool_calls) - len(kept), position)
    return repaired_messages


def _patch_openai_tool_call_fragments(client_class: type, client_module: Any) -> None:
    if getattr(client_class, "_tool_call_fragment_patch_applied", False):
        return
    original_parse_chunk = client_class._parse_stream_chunk
    original_parse_line = client_module._parse_gateway_stream_line
    original_build = client_class._build_request_params

    def _parse_stream_chunk(self, chunk):
        return _coalesce_tool_call_fragments(original_parse_chunk(self, chunk))

    def _parse_gateway_stream_line(line):
        return _coalesce_tool_call_fragments(original_parse_line(line))

    def _build_request_params(self, *args, **kwargs):
        params = original_build(self, *args, **kwargs)
        if isinstance(params, dict) and "messages" in params:
            params["messages"] = _drop_orphan_tool_calls(params["messages"])
        return params

    client_class._parse_stream_chunk = _parse_stream_chunk
    client_module._parse_gateway_stream_line = _parse_gateway_stream_line
    client_class._build_request_params = _build_request_params
    client_class._tool_call_fragment_patch_applied = True


def apply_provider_compat_patches() -> None:
    """Install narrowly-scoped request-shape patches once per process."""
    global _PROVIDER_PATCHES_APPLIED  # pylint: disable=global-statement
    if _PROVIDER_PATCHES_APPLIED:
        return
    try:
        from openjiuwen.core.foundation.llm.model_clients import openai_model_client
        from openjiuwen.core.foundation.llm.model_clients.anthropic_model_client import (
            AnthropicModelClient,
        )
        from openjiuwen.core.foundation.llm.model_clients.openai_model_client import (
            OpenAIModelClient,
        )
    except ImportError as exc:
        logger.warning("Unable to import model clients; skipping provider patches: %s", exc)
        return

    _patch_anthropic_modelarts(AnthropicModelClient)
    _patch_openai_modelarts_tool_choice(OpenAIModelClient)
    _patch_openai_tool_call_fragments(OpenAIModelClient, openai_model_client)
    _PROVIDER_PATCHES_APPLIED = True
    logger.info("Provider compatibility patches applied")


__all__ = ["apply_provider_compat_patches"]
