# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""按租户路径幂等安装/切换 openjiuwen 持久化 checkpointer."""

from __future__ import annotations

import logging
import os
from typing import Any

from jiuwenclaw.utils import AsyncLRUCache, get_checkpoint_dir

logger = logging.getLogger(__name__)


# 使用 AsyncLRUCache 管理 checkpointer 实例，释放连接池，防止内存泄漏
async def _dispose_checkpointer(_key: str, checkpointer: Any) -> None:
    if checkpointer is None:
        return
    try:
        kv_store = getattr(checkpointer, "_kv_store", None)
        engine = getattr(kv_store, "engine", None) if kv_store is not None else None
        dispose = getattr(engine, "dispose", None)
        if callable(dispose):
            await dispose()
            logger.debug("[checkpoint_setup] disposed checkpointer engine (evicted)")
    except Exception as exc:
        logger.warning("[checkpoint_setup] dispose checkpointer failed: %s", exc)


_checkpointers = AsyncLRUCache(
    max_size=int(os.getenv("JIUWENCLAW_CHECKPOINTER_CACHE_MAX_SIZE", 20)),
    ttl_seconds=int(os.getenv("JIUWENCLAW_CHECKPOINTER_CACHE_TTL_SECONDS", 1800)),
    on_evict=_dispose_checkpointer,
)


async def ensure_persistent_checkpointer(
        service_id: str | None = None,
        agent_id: str | None = None,
) -> None:
    """Install or switch to the persistence checkpointer for the given tenant."""
    try:
        from openjiuwen.core.session.checkpointer import CheckpointerFactory
        from openjiuwen.core.session.checkpointer.checkpointer import CheckpointerConfig
        from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointerProvider

        checkpoint_dir = get_checkpoint_dir(service_id, agent_id)
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        key = str(checkpoint_dir.resolve())

        # 顺带清理 TTL 过期条目（释放 engine）
        expired = await _checkpointers.cleanup_expired()
        if expired > 0:
            logger.debug(
                "[checkpoint_setup] cleaned up %d expired checkpointer(s)",
                expired,
            )

        async def _create(_key: str):
            """在 get_or_create 锁内创建 checkpointer，消除 get→create→put 的 TOCTOU 竞态."""
            return await CheckpointerFactory.create(
                CheckpointerConfig(
                    type="persistence",
                    conf={
                        "db_type": "sqlite",
                        "db_path": f"{checkpoint_dir}/checkpoint",
                    },
                )
            )

        checkpointer, is_new = await _checkpointers.get_or_create(key, _create)
        if is_new:
            logger.info(
                "[session=%s] [checkpoint_setup] installed persistence checkpointer: %s",
                service_id,
                checkpoint_dir,
            )
        # 并发安全 get_or_create 返回的 checkpointer
        CheckpointerFactory.set_default_checkpointer(checkpointer)
    except Exception as exc:
        logger.error(
            "[session=%s] [checkpoint_setup] fail to setup checkpoint (service_id=%s, agent_id=%s): %s",
            service_id,
            service_id,
            agent_id,
            exc,
        )
