# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Gateway 进程内 Redis 生命周期：配置解析、连接、健康检查（§3.3.4）。

``deployment_mode=standalone`` 或未成功连上 Redis 时，不创建客户端；业务模块可通过
``get_gateway_redis_client()`` 判空后回退本地实现。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import uuid
from importlib.metadata import entry_points
from typing import Any

from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.extensions.redis.redis_client import RedisConfig

logger = logging.getLogger(__name__)

_GATEWAY_REDIS_EP_GROUP = "jiuwenswarm.gateway.redis"
_DEFAULT_GATEWAY_REDIS_EP_NAME = "redis"


def _load_gateway_redis_client_class():
    """从 entry-points 解析 Redis 客户端类；缺失或加载失败时使用内置 RedisClient。"""
    name = (os.getenv("GATEWAY_REDIS_PLUGIN_NAME") or _DEFAULT_GATEWAY_REDIS_EP_NAME).strip()
    name = name or _DEFAULT_GATEWAY_REDIS_EP_NAME

    eps = ()
    try:
        eps = tuple(entry_points(group=_GATEWAY_REDIS_EP_GROUP))
    except Exception as exc:
        logger.debug("[GatewayRedis] entry_points(group=%s): %s", _GATEWAY_REDIS_EP_GROUP, exc)

    for ep in eps:
        if ep.name != name:
            continue
        try:
            cls = ep.load()
            logger.debug("[GatewayRedis] client impl entry-point %s=%s", name, getattr(ep, "value", ep))
            return cls
        except Exception as exc:
            logger.warning(
                "[GatewayRedis] entry-point %s load failed: %s; fall back to builtin RedisClient",
                getattr(ep, "value", ep),
                exc,
            )
            break

    module = importlib.import_module("jiuwenswarm.extensions.redis.redis_client")
    return getattr(module, "RedisClient")


_MAX_START_PINGS = 3
_MAX_CONSECUTIVE_HEALTH_FAILURES = 3

_redis_client: Any | None = None
_declared_deployment_mode: str = "standalone"
_effective_distributed_redis: bool = False
_gateway_instance_id: str | None = None
_redis_degraded: bool = False
_consecutive_ping_failures: int = 0

_health_task: asyncio.Task[None] | None = None
_shutdown_sentinel: asyncio.Event | None = None


def get_declared_deployment_mode() -> str:
    return _declared_deployment_mode


def get_effective_distributed_redis_active() -> bool:
    """声明为 active-standby 且 Redis 已连接、未因健康检查标记为 degraded。"""
    return _effective_distributed_redis and not _redis_degraded and _redis_client is not None


def get_gateway_instance_id() -> str | None:
    return _gateway_instance_id


def is_redis_degraded() -> bool:
    return _redis_degraded


def get_gateway_redis_client() -> Any | None:
    """供 SessionMap / Cron 等后续接入；单机模式或未初始化时为 ``None``。"""
    if not get_effective_distributed_redis_active():
        return None
    return _redis_client


async def _ping_with_retries(client: Any, *, attempts: int) -> bool:
    for i in range(attempts):
        if await client.ping():
            return True
        if i + 1 < attempts:
            await asyncio.sleep(0.3)
    return False


async def _health_loop(interval_s: float) -> None:
    global _consecutive_ping_failures, _redis_degraded
    client = _redis_client
    while client is not None and (_shutdown_sentinel is None or not _shutdown_sentinel.is_set()):
        await asyncio.sleep(interval_s)
        if _shutdown_sentinel is not None and _shutdown_sentinel.is_set():
            break
        client = _redis_client
        if client is None:
            break
        ok = await client.ping()
        if ok:
            _consecutive_ping_failures = 0
            if _redis_degraded:
                logger.info("[GatewayRedis] ping recovered; clearing degraded flag")
            _redis_degraded = False
            continue
        _consecutive_ping_failures += 1
        logger.warning(
            "[GatewayRedis] health ping failed (%s/%s)",
            _consecutive_ping_failures,
            _MAX_CONSECUTIVE_HEALTH_FAILURES,
        )
        if _consecutive_ping_failures >= _MAX_CONSECUTIVE_HEALTH_FAILURES:
            _redis_degraded = True
            logger.error(
                "[GatewayRedis] degraded: %s consecutive ping failures (§3.3.6)",
                _MAX_CONSECUTIVE_HEALTH_FAILURES,
            )


async def init_gateway_redis_from_config(full_cfg: dict[str, Any] | None) -> None:
    """在读取完整 ``get_config()`` 后调用；失败时按 §3.3.6 降级为无 Redis。"""
    global _redis_client, _declared_deployment_mode, _effective_distributed_redis
    global _gateway_instance_id, _redis_degraded, _consecutive_ping_failures
    global _health_task, _shutdown_sentinel

    await shutdown_gateway_redis()

    cfg_in = full_cfg if isinstance(full_cfg, dict) else {}
    gw = cfg_in.get("gateway")
    gw = gw if isinstance(gw, dict) else {}
    declared = str(gw.get("deployment_mode") or "standalone").strip().lower()
    _declared_deployment_mode = (
        declared
        if declared in ("standalone", "active-standby", "distributed")
        else "standalone"
    )

    iid = str(gw.get("instance_id") or "").strip()
    # active-standby / distributed 都需要 instance_id 标识（主备选主 / 多副本区分）
    if _declared_deployment_mode in ("active-standby", "distributed"):
        _gateway_instance_id = iid or uuid.uuid4().hex
        if not iid:
            logger.info("[GatewayRedis] gateway.instance_id unset; generated %s", _gateway_instance_id)
    else:
        _gateway_instance_id = iid if iid else None

    # 仅 standalone 跳过 Redis init；active-standby / distributed 都需要连 Redis
    if _declared_deployment_mode == "standalone":
        logger.debug("[GatewayRedis] deployment_mode=standalone; skip Redis init (§3.3.4)")
        return

    # 企业版特性：仅企业版开启时启用主备 Redis
    if not is_enterprise():
        logger.debug(
            "[GatewayRedis] deployment_mode=%s but not enterprise edition; skip Redis init",
            _declared_deployment_mode,
        )
        return

    r_cfg = RedisConfig.from_mapping(cfg_in.get("redis") if isinstance(cfg_in.get("redis"), dict) else {})
    redis_client_cls = _load_gateway_redis_client_class()
    client = redis_client_cls(r_cfg)
    try:
        await client.open()
    except ImportError:
        logger.exception(
            "[GatewayRedis] Redis package missing; install optional extra redis (e.g. pip install 'jiuwenswarm[redis]')"
        )
        await client.close()
        _effective_distributed_redis = False
        _redis_client = None
        return
    if not await _ping_with_retries(client, attempts=_MAX_START_PINGS):
        logger.error(
            "[GatewayRedis] Redis unreachable after %s pings; degraded to no Redis (§3.3.6)",
            _MAX_START_PINGS,
        )
        await client.close()
        _effective_distributed_redis = False
        _redis_client = None
        return

    _redis_client = client
    _effective_distributed_redis = True
    _redis_degraded = False
    _consecutive_ping_failures = 0
    _shutdown_sentinel = asyncio.Event()

    logger.info(
        "[GatewayRedis] connected host=%s port=%s instance_id=%s",
        r_cfg.host,
        r_cfg.port,
        _gateway_instance_id,
    )

    interval = float(r_cfg.health_check_interval)
    _health_task = asyncio.create_task(_health_loop(interval), name="gateway-redis-health")


async def shutdown_gateway_redis() -> None:
    global _redis_client
    global _effective_distributed_redis
    global _health_task
    global _shutdown_sentinel
    global _redis_degraded
    global _consecutive_ping_failures

    if _shutdown_sentinel is not None and not _shutdown_sentinel.is_set():
        _shutdown_sentinel.set()

    if _health_task is not None and not _health_task.done():
        _health_task.cancel()
        try:
            await _health_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("[GatewayRedis] task join: %s", exc)
    _health_task = None
    _shutdown_sentinel = None

    if _redis_client is not None:
        await _redis_client.close()
    _redis_client = None
    _effective_distributed_redis = False
    _redis_degraded = False
    _consecutive_ping_failures = 0
