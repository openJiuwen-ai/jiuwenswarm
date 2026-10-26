# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""持久化协议：name + dict 记录，不解析业务含义。"""

from __future__ import annotations

from typing import Any, Protocol


class PersistentStore(Protocol):
    """持久化存储（重启后仍保留）。操作对象是逻辑名 ``name`` 与 JSON 可序列化 dict。

    ::

        store = await ctx.persistent()
        await store.create("session_map", {
            "identity_key": "feishu:user123",
            "session_id": "sess-abc",
            "service_id": "default",
            "agent_id": "agent-1",
        })
        row = await store.get("session_map", {"identity_key": "feishu:user123"})
        await store.update(
            "session_map",
            {"identity_key": "feishu:user123"},
            {"session_id": "sess-xyz"},
        )
        jobs = await store.list(
            "cron_job",
            filters={"enabled": True},
            order_by="updated_at DESC",
            limit=50,
        )
    """

    async def ensure_ready(self) -> None:
        """初始化底层资源（连接、建表、目录等），应幂等。"""

    async def close(self) -> None:
        """释放底层资源。"""

    async def get(self, name: str, key: dict[str, Any]) -> dict[str, Any] | None:
        """按主键取一条；不存在返回 None。"""

    async def create(self, name: str, record: dict[str, Any]) -> dict[str, Any]:
        """插入一条记录。"""

    async def update(
        self,
        name: str,
        key: dict[str, Any],
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """按主键浅合并 ``updates``，其余字段保留。

        共同约定（文件 / 内存 / DB 相同）：

        - 找不到记录返回 None，不新建。
        - 只改顶层 key；``updates`` 里的嵌套 dict / list 整块替换。
        - 值为 ``None`` 会写成空值，不会删掉该字段。去掉字段或整条替换用 delete + create。

        同一条 ``web`` 记录，对比 ``EphemeralStore.set`` / ``hset``（覆盖写）::

            # 原数据 {"id": "web", "send_file_allowed": True, "enabled": False}
            await store.update("channel_config", {"id": "web"}, {"enabled": True})
            # → {"id": "web", "send_file_allowed": True, "enabled": True}
        """

    async def delete(self, name: str, key: dict[str, Any]) -> bool:
        """按主键删除；返回是否删到。"""

    async def list(
        self,
        name: str,
        *,
        filters: dict[str, Any] | None = None,
        order_by: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """等值过滤 + 可选排序/分页。"""


__all__ = ["PersistentStore"]
