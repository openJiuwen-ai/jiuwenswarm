# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""Audit-log 配置：写入 Gateway 本地库并热加载 AuditManager。"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from jiuwenswarm.gateway.config.audit.access import apply_audit_log_config_payload
from jiuwenswarm.gateway.config.enterprise.tables.application_config_models import (
    AUDIT_LOG_CONFIG_TABLE_DEF,
)

from ...infrastructure.repository_access import require_enterprise_repository
from ...infrastructure.utils import format_ts, utc_now

_TABLE = AUDIT_LOG_CONFIG_TABLE_DEF.table_name
logger = logging.getLogger(__name__)


def _project_flat(body: Mapping[str, Any]) -> dict[str, Any]:
    fmt = body.get("format") if isinstance(body.get("format"), Mapping) else {}
    otel = body.get("otel") if isinstance(body.get("otel"), Mapping) else {}
    ntp = body.get("ntp") if isinstance(body.get("ntp"), Mapping) else {}
    servers = ntp.get("servers") if isinstance(ntp, Mapping) else []
    if not isinstance(servers, list):
        servers = list(servers) if servers else []
    return {
        "schema_version": str(
            body.get("schema_version")
            or fmt.get("schema_version")
            or "1.0.0"
        ),
        "otel_enabled": bool(otel.get("enabled", True))
        if "otel_enabled" not in body
        else bool(body.get("otel_enabled", True)),
        "otel_endpoint": body.get("otel_endpoint", otel.get("endpoint")),
        "otel_protocol": str(
            body.get("otel_protocol") or otel.get("protocol") or "grpc"
        ),
        "data_center": str(body.get("data_center") or "N"),
        "system_code": str(body.get("system_code") or "-"),
        "node": body.get("node"),
        "ntp_servers": body.get("ntp_servers")
        if isinstance(body.get("ntp_servers"), list)
        else servers,
        "ntp_sync_interval": str(
            body.get("ntp_sync_interval") or ntp.get("sync_interval") or "300s"
        ),
        "ntp_max_offset_ms": int(
            body.get("ntp_max_offset_ms") or ntp.get("max_offset_ms") or 500
        ),
        "ntp_failover": bool(
            body.get("ntp_failover")
            if "ntp_failover" in body
            else ntp.get("failover", True)
        ),
    }


def _resolve_body(request: Mapping[str, Any]) -> dict[str, Any]:
    """Manager push：扁平 + body；权威 body；无 body 时从扁平拼装。"""
    raw_body = request.get("body")
    if isinstance(raw_body, dict) and raw_body:
        body = dict(raw_body)
        body.pop("service", None)
        # 顶层 identity 可覆盖 body 内同名字段（与 Manager 推送对齐）
        for key in ("data_center", "system_code", "node"):
            if key in request and request.get(key) is not None:
                body[key] = request.get(key)
        return body

    # 无嵌套 body：把整包当 §5.2（去掉元数据列）
    skip = {
        "schema_version",
        "otel_enabled",
        "otel_endpoint",
        "otel_protocol",
        "ntp_servers",
        "ntp_sync_interval",
        "ntp_max_offset_ms",
        "ntp_failover",
        "source",
        "revision",
        "id",
        "created_at",
        "updated_at",
        "service",
        "body",
    }
    body = {k: v for k, v in request.items() if k not in skip}
    if "format" not in body and not body:
        raise ValueError("audit_log_config.body is required")
    return body


def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    body = row.get("body")
    return {
        "id": row.get("id"),
        "schema_version": row.get("schema_version", "1.0.0"),
        "otel_enabled": bool(row.get("otel_enabled", True)),
        "otel_endpoint": row.get("otel_endpoint"),
        "otel_protocol": row.get("otel_protocol", "grpc"),
        "data_center": row.get("data_center", "N"),
        "system_code": row.get("system_code", "-"),
        "node": row.get("node"),
        "ntp_servers": row.get("ntp_servers") or [],
        "ntp_sync_interval": row.get("ntp_sync_interval", "300s"),
        "ntp_max_offset_ms": row.get("ntp_max_offset_ms", 500),
        "ntp_failover": bool(row.get("ntp_failover", True)),
        "body": dict(body) if isinstance(body, dict) else body,
        "source": row.get("source", "manager"),
        "revision": int(row.get("revision") or 1),
        "created_at": format_ts(row.get("created_at")),
        "updated_at": format_ts(row.get("updated_at")),
    }


class AuditLogConfigService:
    async def upsert(self, **request: Any) -> dict[str, Any] | None:
        from openjiuwen_runtime.foundation.audit import (
            AuditLogConfig,
            SchemaValidationError,
        )

        body = _resolve_body(request)
        try:
            AuditLogConfig.from_dict(body, validate=True)
        except SchemaValidationError as exc:
            raise ValueError(str(exc)) from exc

        flat = _project_flat({**body, **{k: request.get(k) for k in request}})
        for key in (
            "schema_version",
            "otel_enabled",
            "otel_endpoint",
            "otel_protocol",
            "data_center",
            "system_code",
            "node",
            "ntp_servers",
            "ntp_sync_interval",
            "ntp_max_offset_ms",
            "ntp_failover",
        ):
            if key in request and request.get(key) is not None:
                flat[key] = request.get(key)

        source = str(request.get("source") or "manager")
        revision = int(request.get("revision") or 1)
        now = utc_now()
        repo = require_enterprise_repository(_TABLE)
        existing = await repo.get()

        if existing is not None:
            update_data = {
                **flat,
                "body": body,
                "source": source,
                "revision": revision,
                "updated_at": now,
            }
            updated = await repo.update({}, update_data)
            if updated is None:
                raise ValueError("failed to update audit log config")
            apply_audit_log_config_payload(body, validate=False)
            logger.info(
                "[ManagerConfigReceiver] audit_log_config upsert revision=%s",
                revision,
            )
            return _row_to_dict(updated)

        row_data = {
            **flat,
            "body": body,
            "source": source,
            "revision": revision,
            "created_at": now,
            "updated_at": now,
        }
        if not isinstance(row_data.get("ntp_servers"), list):
            row_data["ntp_servers"] = []
        created = await repo.create(row_data)
        apply_audit_log_config_payload(body, validate=False)
        logger.info(
            "[ManagerConfigReceiver] audit_log_config created revision=%s",
            revision,
        )
        return _row_to_dict(created)

    async def delete(self) -> None:
        repo = require_enterprise_repository(_TABLE)
        await repo.delete()
        apply_audit_log_config_payload(None, validate=False)
        logger.info("[ManagerConfigReceiver] audit_log_config deleted")
