# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""对运行中的 redis-server 做联调：走 init_gateway_redis_from_config 与 RedisClient。

需已安装 optional：gateway-reliability；需本机可连 Redis（默认 127.0.0.1:6379）。
"""

from __future__ import annotations

import asyncio
import os
import sys

from jiuwenswarm.extensions.redis import (
    get_effective_distributed_redis_active,
    get_gateway_redis_client,
    init_gateway_redis_from_config,
    shutdown_gateway_redis,
)


def _config() -> dict:
    host = os.getenv("REDIS_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("REDIS_PORT", "6379") or "6379")
    password = os.getenv("REDIS_PASSWORD", "")
    db = int(os.getenv("REDIS_DB", "0") or "0")
    key_prefix = os.getenv("REDIS_KEY_PREFIX", "jiuwenswarm:").strip() or "jiuwenswarm:"
    return {
        "gateway": {
            "deployment_mode": "active-standby",
            "instance_id": os.getenv("GATEWAY_INSTANCE_ID", "").strip(),
        },
        "redis": {
            "host": host,
            "port": port,
            "password": password,
            "db": db,
            "key_prefix": key_prefix,
            "pool_size": 4,
            "connect_timeout": 5.0,
            "operation_timeout": 10.0,
            "health_check_interval": 30,
        },
    }


async def run_smoke() -> None:
    cfg = _config()
    await shutdown_gateway_redis()
    await init_gateway_redis_from_config(cfg)

    if not get_effective_distributed_redis_active():
        raise RuntimeError(
            "active-standby Redis 未生效（检查 deployment_mode=active-standby / 网络 / redis extra）"
        )

    client = get_gateway_redis_client()
    if client is None:
        raise RuntimeError("get_gateway_redis_client() 为 None")

    if not await client.ping():
        raise RuntimeError("RedisClient.ping() 失败")

    rel = "gateway:smoke:test_key"
    if not await client.set(rel, "ok", ttl_seconds=60):
        raise RuntimeError("SET 失败")

    if await client.get(rel) != "ok":
        raise RuntimeError("GET 返回值不一致")

    chan = "gateway:smoke:test_ch"
    done = asyncio.Event()
    got: list[str] = []

    async def _sub() -> None:
        async for m in client.subscribe(chan):
            got.append(m)
            done.set()
            break

    sub_task = asyncio.create_task(_sub())
    await asyncio.sleep(0.25)
    receivers = await client.publish(chan, "hello-smoke")
    if receivers < 1:
        sub_task.cancel()
        raise RuntimeError(f"PUBLISH 无订阅端: receivers={receivers}")

    await asyncio.wait_for(done.wait(), timeout=10.0)
    sub_task.cancel()
    try:
        await sub_task
    except asyncio.CancelledError:
        pass

    if got != ["hello-smoke"]:
        raise RuntimeError(f"SUB 消息异常: {got!r}")

    await client.delete(rel)
    await shutdown_gateway_redis()


def main() -> None:
    try:
        asyncio.run(run_smoke())
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
    print("PASS: Redis 联调自检（init + RedisClient ping/KV/pub-sub）")


if __name__ == "__main__":
    main()
