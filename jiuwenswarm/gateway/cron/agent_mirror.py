# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Mirror Gateway cron mutations to Agent tenant ``agent/home/cron_jobs.json``."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jiuwenswarm.common import utils as _jiuwenswarm_utils
from jiuwenswarm.gateway.cron.store import CronJobStore
from jiuwenswarm.gateway.tenant_paths import workspace_key_from_channel_ids

logger = logging.getLogger(__name__)


def _agent_cron_jobs_path(workspace_key: str = "default") -> Path:
    wk = str(workspace_key or "default").strip() or "default"
    # Module attribute lookup so tests can monkeypatch utils helpers.
    return _jiuwenswarm_utils.resolve_tenant_agent_root_dir(wk) / "home" / "cron_jobs.json"


def _agent_store(workspace_key: str = "default") -> CronJobStore:
    return CronJobStore(path=_agent_cron_jobs_path(workspace_key))


async def mirror_job_upsert(
    job: dict[str, Any],
    *,
    service_id: str,
    agent_id: str,
) -> None:
    data = dict(job or {})
    data.setdefault("service_id", service_id)
    data.setdefault("agent_id", agent_id)
    wk = workspace_key_from_channel_ids(service_id, agent_id)
    store = _agent_store(wk)
    await store.upsert_from_dict(data)
    logger.debug(
        "[CronMirror] upsert job=%s service_id=%s agent_id=%s path=%s",
        data.get("id"),
        service_id,
        agent_id,
        store.path,
    )


async def mirror_job_delete(
    job_id: str,
    *,
    service_id: str,
    agent_id: str,
) -> None:
    wk = workspace_key_from_channel_ids(service_id, agent_id)
    store = _agent_store(wk)
    deleted = await store.delete_job(job_id, force=True)
    logger.debug(
        "[CronMirror] delete job=%s deleted=%s service_id=%s agent_id=%s",
        job_id,
        deleted,
        service_id,
        agent_id,
    )
