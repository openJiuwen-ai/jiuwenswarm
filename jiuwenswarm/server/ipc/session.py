# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Connection sources for resident and Front-attached Workers."""

from __future__ import annotations

import os
from typing import Protocol

from jiuwenswarm.common.runtime_platform import is_harmony_runtime
from jiuwenswarm.server.ipc.stream import IpcConnection

RESIDENT = "resident"
FRONT_LOOPBACK = "front_loopback"


def runtime_ipc_mode() -> str:
    """Keep desktop IPC unchanged; accept overrides only on HarmonyOS."""
    if not is_harmony_runtime():
        return RESIDENT
    mode = os.environ.get("JIUWENSWARM_RUNTIME_IPC", "").strip()
    if not mode:
        mode = FRONT_LOOPBACK
    if mode not in {RESIDENT, FRONT_LOOPBACK}:
        raise ValueError("Unsupported JIUWENSWARM_RUNTIME_IPC; use resident or front_loopback")
    return mode


class IpcSessionSource(Protocol):
    """Supply sessions without coupling Runtime lifetime to a listener."""

    reconnectable: bool
    generation: int | None

    async def start(self) -> None:
        ...

    async def accept(self) -> IpcConnection:
        ...

    async def close(self) -> None:
        ...
