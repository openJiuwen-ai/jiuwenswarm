# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process-local Heartbeat injection without importing any Agent implementation."""

from __future__ import annotations

from typing import Any

_heartbeat_job_service: Any | None = None


def set_heartbeat_job_service(service: Any | None) -> None:
    """Publish the AgentServer-owned service to single-Agent and Team builders."""
    global _heartbeat_job_service
    _heartbeat_job_service = service


def get_heartbeat_job_service() -> Any | None:
    """Return the process-owned service without constructing a Team runtime."""
    return _heartbeat_job_service
