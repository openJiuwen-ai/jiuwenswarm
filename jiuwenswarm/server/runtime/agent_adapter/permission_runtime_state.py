# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session permission installation state; the caller owns SDK rails and locks."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionPermissionState:
    """Mutable permission lifecycle fields shared by Smart permission flows."""

    permission_epoch: str | None = None
    pending_permission_epoch: str | None = None
    pending_installed_permissions: dict[str, Any] | None = None
    permission_update_in_progress: bool = False
    permission_isolated: bool = False
    permission_cleanup_complete: bool = False
    permission_cleanup_candidates: list[Any] = field(default_factory=list)

    def stage_pending_permission_capture(
        self, epoch: str, installed_permissions: dict[str, Any] | None,
    ) -> None:
        self.pending_permission_epoch = epoch
        self.pending_installed_permissions = installed_permissions

    def clear_pending_permission(self) -> None:
        self.pending_permission_epoch = None
        self.pending_installed_permissions = None

    def begin_permission_isolation(self) -> None:
        self.permission_isolated = True
        self.permission_cleanup_complete = False

    @contextmanager
    def updating(self):
        self.permission_update_in_progress = True
        try:
            yield
        finally:
            self.permission_update_in_progress = False

    def publish(self, epoch: str | None) -> None:
        self.permission_epoch = epoch
        self.permission_cleanup_candidates = []

    def track_cleanup(self, rails: list[Any]) -> None:
        self.permission_cleanup_candidates = rails

    def finish_cleanup(self, *, complete: bool) -> None:
        self.permission_cleanup_complete = complete
        if complete:
            self.permission_cleanup_candidates = []
