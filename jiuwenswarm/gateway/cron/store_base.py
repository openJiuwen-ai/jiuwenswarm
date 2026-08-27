"""Cron job persistence backend protocol (file or etcd)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from jiuwenswarm.gateway.cron.models import CronJob


class CronJobStoreBackend(Protocol):
    """Cron job persistence backend.

    Personal edition uses the file store; HA appliance opts into etcd.
    ``CronRunState`` is never part of this protocol (memory-only).
    """

    supports_watch: bool

    async def list_jobs(self) -> list[CronJob]:
        ...

    async def get_job(self, job_id: str) -> CronJob | None:
        ...

    async def create_job(
        self,
        *,
        job_id: str | None = None,
        name: str,
        cron_expr: str,
        timezone: str,
        description: str,
        targets: str,
        enabled: bool = True,
        wake_offset_seconds: int | None = None,
        session_id: str | None = None,
        chat_type: str | None = None,
        mode: str | None = None,
        delete_after_run: bool | None = None,
        timeout_seconds: int | None = None,
        project_id: str = "",
        model_name: str | None = None,
        app_id: str = "",
        work_mode: str = "work",
        user_id: str = "",
    ) -> CronJob:
        ...

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> CronJob:
        ...

    async def delete_job(self, job_id: str, *, force: bool = False) -> bool:
        ...

    async def get_revision(self) -> int:
        ...

    async def watch(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Block until cancelled, invoking ``callback`` on remote changes.

        File backend does not implement this; scheduler uses ``supports_watch``.
        """
        ...

    async def aclose(self) -> None:
        ...
