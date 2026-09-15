"""Runtime compatibility fixes for provider-specific OpenAI/Anthropic dialects."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse
from weakref import WeakSet


logger = logging.getLogger("jiuwenswarm.llm_provider_compat_patch")

_MODELARTS_TOOLS_NONE_MODELS = frozenset({"qwen3-30b-a3b", "qwen3-32b"})
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
    original = client_class._build_request_params  # pylint: disable=protected-access

    def _build_request_params(self, *args, **kwargs):
        params = original(self, *args, **kwargs)
        if not _is_modelarts(self):
            return params
        for message in params.get("messages") or []:
            if isinstance(message, dict):
                message["content"] = _flatten_text_blocks(message.get("content"))
        if "system" in params:
            params["system"] = _flatten_text_blocks(params.get("system"))
        return params

    client_class._build_request_params = _build_request_params  # type: ignore[method-assign]  # pylint: disable=protected-access
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


def apply_provider_compat_patches() -> None:
    """Install narrowly-scoped request-shape patches once per process."""
    global _PROVIDER_PATCHES_APPLIED  # pylint: disable=global-statement
    if _PROVIDER_PATCHES_APPLIED:
        return
    try:
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
    _PROVIDER_PATCHES_APPLIED = True
    logger.info("Provider compatibility patches applied")


__all__ = ["apply_provider_compat_patches"]
