# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Pure decoding: one registry image record (JSON) -> ``(agent_type, port)``.

No I/O, no config access. The etcd value is the full image record; only
``name`` and the ``access_mode`` row ``name=web_svc_port`` are used here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from gateway.extensions.agentos.registry_watcher import (
    WEB_SVC_PORT_ROW,
    in_default_port_range,
)

logger = logging.getLogger(__name__)

# A row counts as HTTP when its protocol/type/scheme is empty, http, or https.
_HTTP_LIKE_PROTOCOLS = frozenset({"", "http", "https"})


def parse_access_mode(raw: Any) -> list[dict[str, Any]]:
    """Normalize registry ``access_mode`` into a list of dict rows."""
    if not isinstance(raw, list):
        return []
    return [dict(row) for row in raw if isinstance(row, dict)]


def port_from_access_mode(access_mode: Any, row_name: str) -> int | None:
    """Return the ``port`` of the HTTP row whose ``name`` matches *row_name*."""
    want = str(row_name or "").strip().lower()
    if not want:
        return None
    for row in parse_access_mode(access_mode):
        if str(row.get("name") or "").strip().lower() != want:
            continue
        protocol = str(
            row.get("protocol") or row.get("type") or row.get("scheme") or ""
        ).strip().lower()
        if protocol not in _HTTP_LIKE_PROTOCOLS:
            continue
        try:
            port = int(str(row.get("port") or "").strip())
        except (TypeError, ValueError):
            continue
        if port > 0:
            return port
    return None


def _access_mode(record: dict[str, Any]) -> Any:
    """Registry records nest ``access_mode`` under ``data``; tolerate top-level."""
    data = record.get("data")
    if isinstance(data, dict) and data.get("access_mode") is not None:
        return data.get("access_mode")
    return record.get("access_mode")


def web_svc_port(record: Any) -> int | None:
    """Gateway northbound port from ``data.access_mode`` row ``name=web_svc_port``."""
    if not isinstance(record, dict):
        return None
    return port_from_access_mode(_access_mode(record), WEB_SVC_PORT_ROW)


def decode_record(value: bytes, key: str = "") -> tuple[str, int] | None:
    """Decode one etcd value into ``(agent_type, port)``, or ``None``.

    ``agent_type`` is the record's top-level ``name``. The etcd key is an opaque
    ``image_<sid>`` (one per version), so it is never used as a name fallback.
    ``None`` when the value is missing / not JSON / not an object, has no name,
    or has no ``web_svc_port`` row.
    """
    if not value:
        return None
    try:
        record = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("[RegistryWatcher] bad record value key=%s: %s", key, exc)
        return None
    if not isinstance(record, dict):
        return None
    name = str(record.get("name") or "").strip()
    if not name:
        return None
    port = web_svc_port(record)
    if port is None:
        return None
    if not in_default_port_range(port):
        logger.warning(
            "[RegistryWatcher] port %s out of range for agent=%s; skip", port, name
        )
        return None
    return name, port


__all__ = [
    "parse_access_mode",
    "port_from_access_mode",
    "web_svc_port",
    "decode_record",
]
