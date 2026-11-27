# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AgentServer → Gateway 链路探活配置（Sandbox / OA 模式）。"""

from __future__ import annotations

import os
from dataclasses import dataclass

from jiuwenclaw.e2a.link_heartbeat import build_link_heartbeat_wire

__all__ = [
    "LinkHeartbeatConfig",
    "build_link_heartbeat_wire",
]


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, *, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return float(str(raw).strip())


@dataclass(frozen=True)
class LinkHeartbeatConfig:
    enabled: bool = True
    interval_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> LinkHeartbeatConfig:
        return cls(
            enabled=_env_bool("AGENTSERVER_LINK_HEARTBEAT_ENABLED", default=True),
            interval_seconds=max(
                1.0,
                _env_float("AGENTSERVER_LINK_HEARTBEAT_INTERVAL", default=5.0),
            ),
        )
