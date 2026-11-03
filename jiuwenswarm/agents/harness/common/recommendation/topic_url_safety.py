# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Public-target checks for URLs a topic lane is about to fetch.

Search results and redirect targets are remote input, so a URL is only fetched when its host
is a public internet address: private, loopback, link-local and metadata targets are refused
before any request leaves the process.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

FETCH_URL_SCHEMES = frozenset({"http", "https"})
INTERNAL_HOST_SUFFIXES = (
    ".internal",
    ".local",
    ".lan",
    ".home.arpa",
)
METADATA_HOSTS = frozenset(
    {
        "metadata",
        "metadata.google.internal",
    }
)


def normalize_fetch_host(host: str | None) -> str:
    """Return a canonical lowercase host, or an empty string when it cannot be read."""

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


def _host_rejection_reason(host: str) -> str | None:
    if not host:
        return "network_host_missing"
    if host in METADATA_HOSTS:
        return "network_metadata_host"
    if host.endswith(INTERNAL_HOST_SUFFIXES):
        return "network_internal_hostname"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            return "network_single_label_host"
        return None
    if address.is_link_local:
        # Cloud metadata endpoints live on the link-local range, never off the host.
        return "network_metadata_host"
    return None if address.is_global else "network_host_not_public"


def _endpoint_or_reason(url: str) -> tuple[str | None, str, int]:
    """The scheme/host checks and the host to resolve, or the reason to refuse it."""

    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() not in FETCH_URL_SCHEMES:
        return "network_scheme_not_http", "", 0
    host = normalize_fetch_host(parsed.hostname)
    reason = _host_rejection_reason(host)
    if reason is not None:
        return reason, host, 0
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError:
        return "network_url_invalid_port", host, 0
    return None, host, port


def _resolved_rejection_reason(resolved: list[Any]) -> str | None:
    for info in resolved:
        try:
            address = ipaddress.ip_address(str(info[4][0]))
        except (IndexError, ValueError):
            return "network_host_not_public"
        if not address.is_global:
            return "network_host_not_public"
    return None


async def public_fetch_url_rejection_reason(url: str) -> str | None:
    """Return why this URL must not be fetched, or None when its target is public.

    The hostname is resolved and every address must be globally routable, so a name that
    points into a private range is refused just like a private literal. A name that cannot
    be resolved is allowed: the request itself then fails without reaching anything internal.
    """

    reason, host, port = _endpoint_or_reason(url)
    if reason is not None:
        return reason
    try:
        resolved = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except (OSError, UnicodeError):
        return None
    return _resolved_rejection_reason(resolved)


def public_fetch_url_rejection_reason_sync(url: str) -> str | None:
    """The same check for a worker-thread caller following redirects hop by hop."""

    reason, host, port = _endpoint_or_reason(url)
    if reason is not None:
        return reason
    try:
        resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return None
    return _resolved_rejection_reason(resolved)
