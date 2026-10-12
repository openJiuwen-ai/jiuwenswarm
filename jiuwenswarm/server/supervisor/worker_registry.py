# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Worker process table. The supervisor owns lifecycle; Workers own execution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WorkerSlot:
    """One Worker endpoint known to the supervisor."""

    worker_id: str
    pid: int | None = None
    socket_path: str = ""
    ready: bool = False
    spare: bool = False


class WorkerRegistry:
    """Primary Worker plus an unused warm-spare slot."""

    def __init__(self) -> None:
        self._slots: dict[str, WorkerSlot] = {}
        self.spare_enabled = False

    def upsert(self, slot: WorkerSlot) -> None:
        self._slots[slot.worker_id] = slot

    def get(self, worker_id: str) -> WorkerSlot | None:
        return self._slots.get(worker_id)

    def mark_ready(self, worker_id: str, *, ready: bool) -> None:
        slot = self._slots.get(worker_id)
        if slot is not None:
            slot.ready = ready

    def ids(self) -> list[str]:
        return list(self._slots)
