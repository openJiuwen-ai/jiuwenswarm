from __future__ import annotations

from typing import Any, Protocol

from jiuwenswarm.gateway.cron.models import CronJob
from jiuwenswarm.common.work_mode import DEFAULT_WEB_WORK_MODE


class CronJobStoreBackend(Protocol):
    """Cron job persistence backend (file or Redis)."""

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
        work_mode: str = DEFAULT_WEB_WORK_MODE,
    ) -> CronJob:
        ...

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> CronJob:
        ...

    async def delete_job(self, job_id: str, *, force: bool = False) -> bool:
        ...

    async def get_revision(self) -> int:
        ...
