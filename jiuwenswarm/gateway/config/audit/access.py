# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""audit_log_config 进程入口：apply / 冷加载。

Gateway 热更与 config_poll 使用 ``service=gateway``；
AgentServer 启动冷加载使用 ``service=agentserver``。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from openjiuwen_runtime.foundation.audit import (
    AuditLogConfig,
    SchemaValidationError,
    get_audit_manager,
)

logger = logging.getLogger(__name__)

AUDIT_LOG_CONFIG_TABLE = "audit_log_config"
SERVICE_GATEWAY = "gateway"
SERVICE_AGENTSERVER = "agentserver"


def _with_process_service(
    payload: Mapping[str, Any] | None,
    *,
    service: str,
) -> dict[str, Any]:
    data = dict(payload or {})
    data.pop("service", None)
    data["service"] = service
    return data


def apply_audit_log_config_payload(
    payload: Mapping[str, Any] | None,
    *,
    validate: bool = True,
    service: str = SERVICE_GATEWAY,
) -> dict[str, Any]:
    """热加载审计配置。``payload`` 为权威 body（§5.2）；None 表示恢复 SDK 默认。"""
    mgr = get_audit_manager()
    process_service = str(service or SERVICE_GATEWAY).strip() or SERVICE_GATEWAY
    if payload is None:
        mgr.apply_config({"service": process_service})
        return {"ok": True, "source": "default", "service": process_service}

    data = _with_process_service(payload, service=process_service)
    if validate:
        try:
            AuditLogConfig.from_dict(data, validate=True)
        except SchemaValidationError as exc:
            raise ValueError(str(exc)) from exc
    mgr.apply_config(data)
    return {"ok": True, "source": "db", "service": process_service}


def apply_audit_log_config_row(
    row: Mapping[str, Any] | None,
    *,
    service: str = SERVICE_GATEWAY,
) -> dict[str, Any]:
    """从 DB 行应用配置；权威取 ``body``。"""
    if not isinstance(row, Mapping):
        return apply_audit_log_config_payload(None, service=service)
    body = row.get("body")
    if isinstance(body, dict) and body:
        return apply_audit_log_config_payload(body, service=service)
    return apply_audit_log_config_payload(None, service=service)


async def reload_audit_log_config_from_db(
    *,
    service: str = SERVICE_GATEWAY,
) -> dict[str, Any]:
    """从 Gateway 本地库冷加载 audit_log_config（企业版）；失败不阻断启动。"""
    process_service = str(service or SERVICE_GATEWAY).strip() or SERVICE_GATEWAY
    try:
        from jiuwenswarm.edition import is_enterprise

        if not is_enterprise():
            return apply_audit_log_config_payload(None, service=process_service)
    except Exception:  # noqa: BLE001
        return apply_audit_log_config_payload(None, service=process_service)

    try:
        from jiuwenswarm.gateway.config.enterprise.access import (
            get_enterprise_record_repository,
        )

        repo = get_enterprise_record_repository(AUDIT_LOG_CONFIG_TABLE)
        if repo is not None:
            row = await repo.get()
        else:
            from jiuwenswarm.server.runtime.enterprise_config import db_queries

            rows = await db_queries.list_records(AUDIT_LOG_CONFIG_TABLE)
            row = rows[0] if rows else None
        result = apply_audit_log_config_row(
            row if isinstance(row, dict) else None,
            service=process_service,
        )
        logger.info(
            "[audit] cold load ok source=%s service=%s has_row=%s",
            result.get("source"),
            process_service,
            isinstance(row, dict),
        )
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("[audit] cold load skipped: %s", exc, exc_info=True)
        try:
            apply_audit_log_config_payload(None, service=process_service)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "source": "default", "service": process_service}


__all__ = [
    "AUDIT_LOG_CONFIG_TABLE",
    "SERVICE_AGENTSERVER",
    "SERVICE_GATEWAY",
    "apply_audit_log_config_payload",
    "apply_audit_log_config_row",
    "reload_audit_log_config_from_db",
]
