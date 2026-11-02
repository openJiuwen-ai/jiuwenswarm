# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""Logging 配置：写入 Gateway 本地库并热更新日志级别。"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen_protocol.logging_config import (
    LOGGING_LEVEL_FIELDS,
    LOG_CONFIG_OP_DELETE,
    LOG_CONFIG_OP_FIELD,
)
from jiuwenswarm.common.utils import apply_logging_config_payload

from ...infrastructure.repository_access import require_logging_repository

logger = logging.getLogger(__name__)


def _apply_log_levels(payload: dict[str, Any]) -> None:
    apply_logging_config_payload(payload)
    logger.info(
        "[ManagerConfigReceiver] logging_config hot-reload level=%s console=%s gateway=%s "
        "channel=%s agent_server=%s full=%s",
        payload.get("level"),
        payload.get("console_level"),
        payload.get("gateway"),
        payload.get("channel"),
        payload.get("agent_server"),
        payload.get("full"),
    )


class LoggingConfigService:

    async def upsert(
        self,
        *,
        level: str = "INFO",
        console_level: str | None = None,
        gateway: str | None = None,
        channel: str | None = None,
        agent_server: str | None = None,
        full: str | None = None,
        **_extra: Any,
    ) -> dict[str, Any] | None:
        repo = require_logging_repository()
        provided = {
            "level": level,
            "console_level": console_level,
            "gateway": gateway,
            "channel": channel,
            "agent_server": agent_server,
            "full": full,
        }
        # 以本方法实际提供的字段为准，再按协议字段清单过滤：常量新增键时不会产生
        # 缺失的占位项，也不依赖 provided 与常量键集合完全相等。
        fields = {key: value for key, value in provided.items() if key in LOGGING_LEVEL_FIELDS}
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if value is not None:
                updates[key] = value
        if "level" not in updates:
            updates["level"] = level
        saved = await repo.merge_levels(updates)
        result = dict(saved.body)
        _apply_log_levels(fields)
        return result

    async def delete(self) -> None:
        repo = require_logging_repository()
        await repo.delete()
        apply_logging_config_payload({LOG_CONFIG_OP_FIELD: LOG_CONFIG_OP_DELETE})
        logger.info("[ManagerConfigReceiver] logging_config deleted")
