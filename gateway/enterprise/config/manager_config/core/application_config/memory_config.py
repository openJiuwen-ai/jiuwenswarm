# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""Memory 配置：持久化配置；路由通知 Runtime，由执行侧读取并应用。"""

from __future__ import annotations

import logging
from typing import Any

from ...infrastructure.repository_access import require_memory_repository

logger = logging.getLogger(__name__)


def _document_to_dict(document) -> dict[str, Any]:
    return {
        "body": dict(document.body),
        "source": document.source,
        "revision": document.revision,
    }


class MemoryConfigService:

    async def upsert(
        self,
        *,
        body: dict[str, Any] | None = None,
        source: str = "manager",
        **_extra: Any,
    ) -> dict[str, Any] | None:
        if body is None and isinstance(_extra.get("body"), dict):
            body = _extra["body"]
        if body is None:
            cand = {k: v for k, v in _extra.items() if k != "source"}
            if cand:
                body = cand
        if not isinstance(body, dict):
            raise ValueError("memory_config.body must be an object for upsert")

        repo = require_memory_repository()
        saved = await repo.upsert_body(
            body,
            source=str(source or "manager"),
        )
        result = _document_to_dict(saved)
        logger.info(
            "[ManagerConfigReceiver] memory_config saved revision=%s",
            result.get("revision"),
        )
        return result

    async def delete(self) -> None:
        repo = require_memory_repository()
        await repo.delete()
        logger.info("[ManagerConfigReceiver] memory_config deleted")
