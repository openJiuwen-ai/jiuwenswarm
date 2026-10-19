"""Shared validation helpers for model configuration values."""

from __future__ import annotations

from urllib.parse import urlparse

PLACEHOLDER_API_BASES = frozenset({"https://example.com/compatible-mode/v1"})
EXAMPLE_DOMAINS = frozenset({"example.com", "example.org", "example.net"})


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
