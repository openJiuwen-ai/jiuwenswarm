# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Gateway-side proxy for AgentServer-owned Heartbeat jobs.

旧探活(HEARTBEAT.md 驱动的全局周期探活)已迁移到 ``gateway/health_check/``,
Gateway keeps public ``heartbeat.job.*`` adapters only. Store, controller,
scheduler, execution, and Agent tools live under the single-agent Heartbeat
Rail in AgentServer.
"""

from .proxy import HeartbeatControllerProxy, HeartbeatServiceUnavailableError

__all__ = ["HeartbeatControllerProxy", "HeartbeatServiceUnavailableError"]
