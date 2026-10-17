# coding: utf-8
from __future__ import annotations

from typing import Any, Optional


OPENROUTER_ATTRIBUTION_HEADERS: dict[str, str] = {
    "HTTP-Referer": "https://openjiuwen.com/",
    "X-OpenRouter-Title": "JiuwenSwarm",
    "X-OpenRouter-Categories": (
        "cli-agent,cloud-agent,programming-app,"
        "creative-writing,writing-assistant,general-chat,personal-agent"
    ),
}


def is_openrouter_provider(provider: Optional[str]) -> bool:
    """旧式判别：仅认 client_provider == 'OpenRouter'。

    新声明下 OpenRouter 的 client_provider 是 OpenAI，方言由 endpoint_profile=openrouter
    表达。优先用 :func:`is_openrouter_client_config` 判整个 mcc。
    """
    if not provider:
        return False
    return str(provider).strip().lower() == "openrouter"


def is_openrouter_client_config(mcc: dict[str, Any]) -> bool:
    """新式判别：认 endpoint_profile=openrouter，兼容旧 client_provider=OpenRouter。

    经 Anthropic 协议打 OpenRouter 时(client_provider=Anthropic)没有
    endpoint_profile=openrouter；若仍需归因头，按 api_base 是否指向 OpenRouter 另行判断。
    """
    profile = str(mcc.get("endpoint_profile") or "").strip().lower()
    if profile == "openrouter":
        return True
    # 兼容旧配置：client_provider 仍是 OpenRouter 别名(core 内部归一)。
    return is_openrouter_provider(mcc.get("client_provider"))


def inject_attribution_headers(mcc: dict[str, Any]) -> dict[str, Any]:
    """Inject OpenRouter attribution headers into model_client_config dict.

    新声明下认 endpoint_profile=openrouter(兼容旧 client_provider=OpenRouter)。
    Returns the same dict (mutated in place) for chaining convenience.
    """
    if not is_openrouter_client_config(mcc):
        return mcc

    custom_headers = mcc.get("custom_headers")
    if not isinstance(custom_headers, dict):
        custom_headers = {}
    else:
        custom_headers = dict(custom_headers)

    for key, value in OPENROUTER_ATTRIBUTION_HEADERS.items():
        custom_headers.setdefault(key, value)

    mcc["custom_headers"] = custom_headers
    return mcc


def inject_attribution_to_config(config: dict[str, Any]) -> None:
    """Inject OpenRouter attribution headers into all model_client_config entries in-place.

    Covers models.defaults (list), models.default (single), and react sections.
    """
    models = config.get("models", {})
    if isinstance(models, dict):
        defaults = models.get("defaults")
        if isinstance(defaults, list):
            for entry in defaults:
                mcc = entry.get("model_client_config") if isinstance(entry, dict) else None
                if isinstance(mcc, dict):
                    inject_attribution_headers(mcc)
        default_single = models.get("default")
        if isinstance(default_single, dict):
            mcc = default_single.get("model_client_config")
            if isinstance(mcc, dict):
                inject_attribution_headers(mcc)

    react = config.get("react")
    if isinstance(react, dict):
        mcc = react.get("model_client_config")
        if isinstance(mcc, dict):
            inject_attribution_headers(mcc)
