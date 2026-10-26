# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""TaskMemory 配置：写入 Gateway 本地库。"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.gateway.config.enterprise.tables.application_config_models import TASK_MEMORY_CONFIG_TABLE_DEF
from ...infrastructure.repository_access import require_enterprise_repository
from ...infrastructure.utils import format_ts, utc_now

_TABLE = TASK_MEMORY_CONFIG_TABLE_DEF.table_name
logger = logging.getLogger(__name__)


def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "enabled": row.get("enabled", False),
        "llm_model": row.get("llm_model", ""),
        "embedding_model": row.get("embedding_model", ""),
        "api_key": row.get("api_key", ""),
        "api_base": row.get("api_base", ""),
        "retrieval_algo": row.get("retrieval_algo", ""),
        "summary_algo": row.get("summary_algo", ""),
        "created_at": format_ts(row.get("created_at")),
        "updated_at": format_ts(row.get("updated_at")),
    }


async def _upsert_task_memory_config_record(
    request: dict[str, Any],
) -> dict[str, Any]:
    repo = require_enterprise_repository(_TABLE)
    existing = await repo.get()
    now = utc_now()
    if existing is not None:
        update_data: dict[str, Any] = {"updated_at": now}
        if "enabled" in request:
            update_data["enabled"] = bool(request.get("enabled", False))
        if request.get("llm_model") != "":
            update_data["llm_model"] = request.get("llm_model")
        if request.get("embedding_model") != "":
            update_data["embedding_model"] = request.get("embedding_model")
        if request.get("api_key") != "":
            update_data["api_key"] = request.get("api_key")
        if request.get("api_base") != "":
            update_data["api_base"] = request.get("api_base")
        if request.get("retrieval_algo") is not None:
            update_data["retrieval_algo"] = request.get("retrieval_algo")
        if request.get("summary_algo") is not None:
            update_data["summary_algo"] = request.get("summary_algo")
        updated = await repo.update({}, update_data)
        if updated is None:
            raise ValueError("failed to update task_memory config")
        return _row_to_dict(updated)

    row_data = {
        "enabled": bool(request.get("enabled", False)),
        "llm_model": request.get("llm_model"),
        "embedding_model": request.get("embedding_model"),
        "api_key": request.get("api_key"),
        "api_base": request.get("api_base"),
        "retrieval_algo": request.get("retrieval_algo", ""),
        "summary_algo": request.get("summary_algo", ""),
        "created_at": now,
        "updated_at": now,
    }
    created = await repo.create(row_data)
    return _row_to_dict(created)


class TaskMemoryConfigService:

    async def upsert(
        self,
        request: dict[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any] | None:
        payload = dict(request or {})
        payload.update({k: v for k, v in fields.items() if v is not None or k in fields})
        # HTTP body 可能直接摊平字段，也可能嵌套 task_memory
        if isinstance(payload.get("task_memory"), dict):
            payload = dict(payload["task_memory"])
        return await _upsert_task_memory_config_record(payload)

    async def delete(self) -> None:
        repo = require_enterprise_repository(_TABLE)
        await repo.delete()
        logger.info("[ManagerConfigReceiver] task_memory_config deleted")
