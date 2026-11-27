# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Reject disallowed URLs before HTTP fetch (``ValueError`` on failure)."""

from __future__ import annotations

import os
import re
import socket
from struct import unpack
from socket import inet_aton
from urllib.parse import urlparse


def check_url_allowed_for_fetch(url: str) -> None:
    """Require http(s) URL and reject private/reserved IPs (unless SSRF_PROTECT_ENABLED=false)."""
    if not url or not str(url).strip():
        raise ValueError("url is empty")
    url = str(url).strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise ValueError("illegal url protocol (only http/https allowed)")
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL missing hostname")
    try:
        ip_address = socket.gethostbyname(hostname)
    except OSError as e:
        raise ValueError("resolving IP address failed") from e
    if _is_inner_ipaddress(ip_address):
        raise ValueError("illegal ip address (SSRF protection)")


def _is_inner_ipaddress(ip: str) -> bool:
    if os.getenv("SSRF_PROTECT_ENABLED", "true").lower() == "false":
        return False
    try:
        ip_long = _ip_to_long(ip)
    except OSError:
        return True
    return (
        _ip_to_long("10.0.0.0") <= ip_long <= _ip_to_long("10.255.255.255")
        or _ip_to_long("172.16.0.0") <= ip_long <= _ip_to_long("172.31.255.255")
        or _ip_to_long("192.168.0.0") <= ip_long <= _ip_to_long("192.168.255.255")
        or _ip_to_long("127.0.0.0") <= ip_long <= _ip_to_long("127.255.255.255")
        or ip_long == _ip_to_long("0.0.0.0")
    )


def _ip_to_long(ip_addr: str) -> int:
    return unpack("!L", inet_aton(ip_addr))[0]


def is_likely_public_http_url(url: str) -> bool:
    """Lightweight shape check (does not replace SSRF DNS resolution in check_url_allowed_for_fetch)."""
    if not url or not str(url).strip():
        return False
    return bool(re.match(r"^https?://\S+", str(url).strip(), re.IGNORECASE))
