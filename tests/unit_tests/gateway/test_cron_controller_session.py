from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.gateway.cron.controller import CronController
from jiuwenswarm.gateway.cron.store import FileCronJobStore


@pytest.fixture
def cron_controller(tmp_path: Path):
    CronController.reset_instance()
    store = FileCronJobStore(path=tmp_path / "cron_jobs.json")
    scheduler = MagicMock()
    scheduler.reload = AsyncMock(return_value=None)
    with patch(
        "jiuwenswarm.gateway.cron.controller.enterprise_cron_enabled",
        return_value=False,
    ), patch(
        "jiuwenswarm.gateway.cron.controller.is_enterprise",
        return_value=False,
    ):
        yield CronController.get_instance(store=store, scheduler=scheduler)


@pytest.mark.asyncio
async def test_update_description_preserves_web_session_id(cron_controller: CronController) -> None:
    created = await cron_controller.create_job(
        {
            "name": "daily",
            "cron_expr": "*/5 * * * *",
            "timezone": "Asia/Shanghai",
            "description": "before",
            "targets": "web",
            "session_id": "sess_abc",
        }
    )
    job_id = created["id"]

    updated = await cron_controller.update_job(job_id, {"description": "after"})

    assert updated["session_id"] == "sess_abc"


@pytest.mark.asyncio
async def test_update_with_web_session_id_in_patch_keeps_session(cron_controller: CronController) -> None:
    created = await cron_controller.create_job(
        {
            "name": "daily",
            "cron_expr": "*/5 * * * *",
            "timezone": "Asia/Shanghai",
            "description": "before",
            "targets": "web",
            "session_id": "sess_abc",
        }
    )
    job_id = created["id"]

    updated = await cron_controller.update_job(
        job_id,
        {"description": "after", "session_id": "sess_abc"},
    )

    assert updated["session_id"] == "sess_abc"
