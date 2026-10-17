# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""HealthCheck 模块，负责原 Heartbeat 的服务探活能力。

旧 ``gateway/heartbeat/heartbeat.py`` 的探活逻辑迁移到本命名空间,与新 Heartbeat
任务(线程续跑,``gateway/heartbeat/`` 下 models/store/scheduler/controller)严格区分。
旧 ``heartbeat.py`` 已删除，调用方应直接从本模块导入。
"""

from jiuwenswarm.gateway.health_check.health_check import (
    HEALTH_CHECK_CHANNEL_ID,
    HEALTH_CHECK_OK,
    HEALTH_CHECK_PROMPT,
    GatewayHealthCheckService,
    HealthCheckConfig,
    IHealthCheck,
    normalize_active_hours,
)

__all__ = [
    "HEALTH_CHECK_CHANNEL_ID",
    "HEALTH_CHECK_OK",
    "HEALTH_CHECK_PROMPT",
    "GatewayHealthCheckService",
    "HealthCheckConfig",
    "IHealthCheck",
    "normalize_active_hours",
]
