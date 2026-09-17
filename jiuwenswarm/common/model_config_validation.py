"""Shared validation helpers for model configuration values."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

PLACEHOLDER_API_BASES = frozenset({"https://example.com/compatible-mode/v1"})
EXAMPLE_DOMAINS = frozenset({"example.com", "example.org", "example.net"})
# 首次启动时从 .env.template 复制来的占位凭据（见 resources/.env.template）。
PLACEHOLDER_MODEL_NAMES = frozenset({"your-model-name"})
PLACEHOLDER_API_KEYS = frozenset({"sk-xxxxxxxxx"})


def is_placeholder_api_base(api_base: str) -> bool:
    """Return True when api_base is a documentation placeholder URL."""
    value = str(api_base or "").strip()
    if not value:
        return False
    if value in PLACEHOLDER_API_BASES:
        return True
    try:
        host = urlparse(value).hostname or ""
    except Exception:
        return False
    if not host:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in EXAMPLE_DOMAINS)


def is_placeholder_model_entry(mcc: dict | None) -> bool:
    """Return True when mcc still carries first-run placeholder credentials.

    只识别明确的占位值（模板 URL/占位 model_name/占位 api_key），不会误伤
    "api_base 为真实地址、空 key" 的本地 vLLM 等合法无鉴权部署。
    """
    mcc = mcc or {}
    if is_placeholder_api_base(str(mcc.get("api_base", "") or "").strip()):
        return True
    if str(mcc.get("model_name", "") or "").strip() in PLACEHOLDER_MODEL_NAMES:
        return True
    if str(mcc.get("api_key", "") or "").strip() in PLACEHOLDER_API_KEYS:
        return True
    return False


def model_client_config_view(mcc: Any) -> dict[str, Any]:
    """Normalize openjiuwen ``ModelClientConfig`` (or a plain dict) for the checks above.

    ``ModelClientConfig`` is a pydantic/dataclass-like object built by
    ``build_model_from_entry``; reduce it to the 3 fields these helpers read.
    """
    if isinstance(mcc, dict):
        return {
            "api_base": mcc.get("api_base", "") or "",
            "api_key": mcc.get("api_key", "") or "",
            "model_name": mcc.get("model_name", "") or "",
        }
    return {
        "api_base": getattr(mcc, "api_base", None) or "",
        "api_key": getattr(mcc, "api_key", None) or "",
        "model_name": getattr(mcc, "model_name", None) or "",
    }
