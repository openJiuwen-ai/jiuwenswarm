# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""临时态协议：namespace 隔离的 KV + Hash；值为不透明 bytes，丢了可重建。"""

from __future__ import annotations

from typing import Protocol


class EphemeralStore(Protocol):
    """临时态存储（进程内存或 Redis）。操作对象是 ``namespace`` 内的 KV / Hash。

    值不解析业务含义；结构化对象在调用侧 encode / decode。

    - **KV**（``get`` / ``set`` / ``delete``）：一个名字 → 一份完整值。
      适合点查这一份东西，整份覆盖，``set`` 还可带 TTL。
    - **Hash**（``hget`` / ``hset`` / ``hdel`` / ``hgetall``）：一个名字 → 一组 field。
      适合一组相关项，要单改、也要一次列出。``hset`` 无字段级 TTL。

    ::

        ws = ctx.ephemeral("web_ws")

        # KV：这条连接本身是什么
        await ws.set("conn:sess-abc", b'{"pod":"gw-1"}', ttl=3600)
        await ws.get("conn:sess-abc")

        # Hash：这个 session 现在有哪些连接
        await ws.hset("session:sess-abc", "conn-1", b"pod-gw-1")
        await ws.hset("session:sess-abc", "conn-2", b"pod-gw-2")
        await ws.hdel("session:sess-abc", "conn-1")
        all_conns = await ws.hgetall("session:sess-abc")
        # → {"conn-2": b"pod-gw-2"}
    """

    @property
    def namespace(self) -> str:
        """逻辑命名空间；backend 用它做 key 前缀隔离。"""

    async def get(self, key: str) -> bytes | None:
        """按 key 取一份完整值；不存在返回 None。如 ``conn:sess-abc``。"""

    async def set(self, key: str, value: bytes, *, ttl: int | None = None) -> None:
        """整 key 覆盖写。``ttl`` 为秒，None 表示不过期（backend 可不实现过期）。

        ::

            await ws.set("conn:sess-abc", b'{"pod":"gw-1"}', ttl=3600)

        同一条 ``web`` 记录，对比 ``PersistentStore.update``（浅合并）::

            # 原数据 {"id": "web", "send_file_allowed": True, "enabled": False}
            await ephem.set("channel:web", b'{"enabled": true}')
            # → {"enabled": true}    send_file_allowed 没了
        """

    async def delete(self, key: str) -> None:
        """删除这一份 KV；不存在视为成功。"""


    async def hget(self, hash_key: str, field: str) -> bytes | None:
        """取 Hash 的一个 field；hash 或字段不存在返回 None。"""

    async def hset(self, hash_key: str, field: str, value: bytes) -> None:
        """覆盖写单个 Hash 字段；hash 不存在则创建，其它 field 保留。

        ::

            await ws.hset("session:sess-abc", "conn-1", b"pod-gw-1")

        同一条 ``web`` 记录::

            # 原数据 field: send_file_allowed=true, enabled=false
            await ephem.hset("channel:web", "enabled", b"true")
            # → send_file_allowed=true, enabled=true
        """

    async def hdel(self, hash_key: str, field: str) -> None:
        """删除 Hash 的一个 field；不存在视为成功。"""

    async def hgetall(self, hash_key: str) -> dict[str, bytes]:
        """列出该 Hash 全部 field；不存在返回空 dict。如 ``session:sess-abc``。"""


__all__ = ["EphemeralStore"]
