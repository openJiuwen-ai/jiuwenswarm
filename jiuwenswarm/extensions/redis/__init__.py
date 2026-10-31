# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""网关分布式 Redis 实现（内置于 ``jiuwenswarm.extensions``，与动态扩展目录无关）。"""

from jiuwenswarm.extensions.redis.redis_client import RedisClient, RedisConfig
from jiuwenswarm.extensions.redis.redis_keys import (
    cron_jobs_hash_key,
    cron_jobs_hash_rel,
    cron_live_channel_rel,
    cron_live_replay_key_rel,
    cron_permission_hold_rel,
    leader_lock_key,
    session_map_hash_key,
)
from jiuwenswarm.extensions.redis.redis_runtime import (
    get_declared_deployment_mode,
    get_effective_distributed_redis_active,
    get_gateway_instance_id,
    get_gateway_redis_client,
    get_gateway_redis_client_if_open,
    init_gateway_redis_from_config,
    is_redis_degraded,
    shutdown_gateway_redis,
)

__all__ = [
    "RedisClient",
    "RedisConfig",
    "cron_jobs_hash_key",
    "cron_jobs_hash_rel",
    "cron_live_channel_rel",
    "cron_live_replay_key_rel",
    "cron_permission_hold_rel",
    "get_declared_deployment_mode",
    "get_effective_distributed_redis_active",
    "get_gateway_instance_id",
    "get_gateway_redis_client",
    "get_gateway_redis_client_if_open",
    "init_gateway_redis_from_config",
    "is_redis_degraded",
    "leader_lock_key",
    "session_map_hash_key",
    "shutdown_gateway_redis",
]
