# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""从 Gateway 库冷加载 logging_config 行，应用到本进程日志级别。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jiuwenswarm.common.utils import (
    _logging_config_row_to_dict,
    apply_logging_config_payload,
)
from jiuwenswarm.enterprise import is_enterprise

logger = logging.getLogger(__name__)

# 冷加载只是启动早期的一次“尽力而为”读库；DB 抖动 / 网络分区不能让 front 端口
# 监听与 worker 启动被无限期挂起（否则 k8s 就绪/存活探测失败会反复重启 Pod）。
_LOGGING_CONFIG_READ_TIMEOUT = 5.0


async def reload_logging_levels_from_gateway_db() -> dict[str, Any] | None:
    """Pod 拉起/重建时从 Gateway DB 读 ``logging_config`` 并热更本进程级别。

    Gateway 收到 Manager 的日志级别下发后只热更 gateway 进程自身，随后通知
    runtime 日落重建 Pod；Pod 进程没有 PersistentStore 装配，``setup_logger``
    只读 config.yaml 默认值——这里在启动早期经企业读库 facade 直读该行补齐。
    无行 / 非企业版 / 读库失败或超时时保持 config.yaml 默认级别，不阻断启动。
    """
    if not is_enterprise():
        return None
    try:
        # 企业平面读库 facade（与 memory/config、log_masking/engine 同路径）。
        from jiuwenswarm.enterprise.config.effective import db_queries

        rows = await asyncio.wait_for(
            db_queries.list_records("logging_config"),
            timeout=_LOGGING_CONFIG_READ_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[enterprise.logging] read logging_config timed out after %.1fs; "
            "keeping config.yaml defaults",
            _LOGGING_CONFIG_READ_TIMEOUT,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[enterprise.logging] read logging_config failed: %s", exc)
        return None
    row = rows[0] if rows else None
    if not isinstance(row, dict) or not row:
        return None
    payload = _logging_config_row_to_dict(row)
    apply_logging_config_payload(payload)
    logger.info(
        "[enterprise.logging] logging_config loaded from Gateway DB "
        "level=%s console=%s gateway=%s channel=%s agent_server=%s full=%s",
        payload.get("level"),
        payload.get("console_level"),
        payload.get("gateway"),
        payload.get("channel"),
        payload.get("agent_server"),
        payload.get("full"),
    )
    return payload
