# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared network scope helpers for permission grants and read-only fetches."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl

SECRET_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "client_secret",
        "credential",
        "key",
        "password",
        "private_key",
        "secret",
        "session",
        "signature",
        "token",
    }
)
SECRET_QUERY_COMPACT_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "authkey",
        "authtoken",
        "authorization",
        "bearer",
        "bearertoken",
        "clientsecret",
        "credential",
        "credentials",
        "idtoken",
        "jwt",
        "oauth",
        "oauth2",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "session",
        "sessionid",
        "sessionkey",
        "sessiontoken",
        "signature",
        "token",
    }
)
SECRET_QUERY_WORDS = frozenset(
    {
        "auth",
        "authorization",
        "bearer",
        "credential",
        "credentials",
        "jwt",
        "passwd",
        "password",
        "pwd",
        "secret",
        "session",
        "token",
    }
)
SECRET_QUERY_KEY_QUALIFIERS = frozenset(
    {
        "access",
        "api",
        "auth",
        "authorization",
        "client",
        "private",
        "secret",
        "session",
    }
)

PUBLIC_SUFFIX_ONLY_HOSTS = frozenset(
    {
        "app",
        "co.uk",
        "com",
        "dev",
        "github.io",
        "gov",
        "io",
        "net",
        "org",
        "test",
    }
)


def normalize_network_host(host: str | None) -> str:
    """Return a canonical lowercase host for network permission matching."""
    raw = str(host or "").strip().strip("[]").rstrip(".").lower()
    if not raw:
        return ""
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        pass
    try:
        return raw.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return ""


def network_host_rejection_reason(host: str | None) -> str | None:
    """Return a rejection reason for unsafe grant hosts, or None when allowed."""
    normalized = normalize_network_host(host)
    if not normalized:
        return "network_host_missing"
    try:
        ip_address = ipaddress.ip_address(normalized)
    except ValueError:
        labels = normalized.split(".")
        if len(labels) < 2:
            return "network_single_label_host"
        if normalized in PUBLIC_SUFFIX_ONLY_HOSTS:
            return "network_public_suffix_host"
        return None
    if not ip_address.is_global:
        return "network_host_not_public"
    return None


def host_matches_allowed_domain(host: str | None, allowed_domain: str | None) -> bool:
    """Return whether host is the allowed domain or one of its subdomains."""
    normalized_host = normalize_network_host(host)
    normalized_allowed = normalize_network_host(allowed_domain)
    if not normalized_host or not normalized_allowed:
        return False
    if network_host_rejection_reason(normalized_host) is not None:
        return False
    if network_host_rejection_reason(normalized_allowed) is not None:
        return False
    if normalized_host == normalized_allowed:
        return True
    return normalized_host.endswith(f".{normalized_allowed}")


def has_secret_query(query: str) -> bool:
    """Return whether a URL query contains secret-like key names."""
    for key, _value in parse_qsl(query, keep_blank_values=True):
        if _is_secret_query_key(key):
            return True
    return False


def _is_secret_query_key(key: str) -> bool:
    raw_key = str(key or "").strip()
    if not raw_key:
        return False
    normalized_key = raw_key.lower()
    if normalized_key in SECRET_QUERY_KEYS:
        return True
    word_text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw_key)
    words = tuple(
        word for word in re.split(r"[^A-Za-z0-9]+", word_text.lower()) if word
    )
    compact = "".join(words)
    if compact in SECRET_QUERY_COMPACT_KEYS:
        return True
    if any(word in SECRET_QUERY_WORDS for word in words):
        return True
    return "key" in words and any(word in SECRET_QUERY_KEY_QUALIFIERS for word in words)
