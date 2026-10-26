# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""审计 SRCIP/DSTIP：本端 Pod IP 缓存 + 对端 URL/host 解析。"""

from __future__ import annotations

import logging
import os
import socket
from typing import Any, Mapping
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_PLACEHOLDER = "-"
_local_ip_cache: str | None = None


def clear_local_ip_cache() -> None:
    """测试辅助：清空本端 IP 缓存。"""
    global _local_ip_cache
    _local_ip_cache = None


def get_local_ip() -> str:
    """本端 IP：优先 ``POD_IP``，否则探测默认路由网卡；失败返回 ``-``。"""
    global _local_ip_cache
    if _local_ip_cache is not None:
        return _local_ip_cache
    env_ip = str(os.getenv("POD_IP") or "").strip()
    if env_ip:
        _local_ip_cache = env_ip
        return _local_ip_cache
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            ip = str(probe.getsockname()[0] or "").strip()
        finally:
            probe.close()
        if ip:
            _local_ip_cache = ip
            return _local_ip_cache
    except OSError as exc:
        logger.debug("[audit_net] local ip probe failed: %s", exc)
    _local_ip_cache = _PLACEHOLDER
    return _local_ip_cache


def resolve_peer_ip(host_or_url: str | None) -> str:
    """从 URL 或 host 解析对端 IP；失败返回 ``-``。"""
    text = str(host_or_url or "").strip()
    if not text or text == _PLACEHOLDER:
        return _PLACEHOLDER
    host = text
    try:
        parsed = urlparse(text if "://" in text else f"//{text}", scheme="")
        if parsed.hostname:
            host = parsed.hostname
    except Exception:  # noqa: BLE001
        host = text.split("/")[0].split(":")[0]
    host = str(host or "").strip().strip("[]")
    if not host or host == _PLACEHOLDER:
        return _PLACEHOLDER
    try:
        socket.inet_pton(socket.AF_INET, host)
        return host
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return host
    except OSError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        for info in infos:
            addr = info[4][0] if info and info[4] else ""
            if addr:
                return str(addr)
    except OSError as exc:
        logger.debug("[audit_net] resolve peer failed host=%s: %s", host, exc)
    return _PLACEHOLDER


def enrich_extra_with_hop_ips(
    merged_extra: dict[str, Any],
    ctx: Mapping[str, str] | None = None,
) -> None:
    """打点已传 ``DSTIP``（或 ctx.dst_ip）时自动补 ``SRCIP``=本端；单组件不改。"""
    ctx = ctx or {}

    def _blank(value: Any) -> bool:
        text = str(value or "").strip()
        return text == "" or text == _PLACEHOLDER

    dst = str(merged_extra.get("DSTIP") or ctx.get("dst_ip") or "").strip()
    if _blank(dst):
        return
    if _blank(merged_extra.get("DSTIP")):
        merged_extra["DSTIP"] = dst
    if _blank(merged_extra.get("SRCIP")):
        merged_extra["SRCIP"] = get_local_ip()
