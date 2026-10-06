# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Parse sandbox TCP endpoint host/port from a URL string."""

from __future__ import annotations

from urllib.parse import urlparse


def parse_sandbox_host_port(url: str) -> tuple[str, int]:
    """从 sandbox url 解析 host:port; 未显式指定 host 时默认 ``127.0.0.1:8321``.

    Explicit hosts (including ``0.0.0.0`` / LAN IPs) are returned as-is and are
    never rewritten. Only missing / unparseable hostnames fall back to loopback.
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8321
    except Exception:
        host, port = "127.0.0.1", 8321
    return host, int(port)
