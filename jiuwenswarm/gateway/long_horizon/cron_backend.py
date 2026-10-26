# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Adapt Gateway CronController to the long-horizon job backend protocol."""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class CronBackend(Protocol):
    async def get_job(self, job_id: str) -> Any:
        ...

    async def create_job(self, *, job_id: str, **payload: Any) -> Any:
        ...

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> Any:
        ...

    async def delete_job(self, job_id: str, *, force: bool = False) -> bool:
        ...


async def resolve_live_cron_controller(
    host: Any,
    metadata: dict[str, Any] | None = None,
) -> Any | None:
    """Same controller as cron.job.list (tenant registry), not CronController.get_instance()."""
    registry = getattr(host, "_cron_registry", None)
    if registry is not None and hasattr(registry, "get_controller"):
        sid, aid = "default", "default"
        try:
            from jiuwenswarm.gateway.cron.tenant_registry import CronTenantRegistry

            sid, aid = CronTenantRegistry.resolve_scope(metadata=metadata)
        except Exception as exc:
            logger.debug(
                "[long_horizon] cron tenant scope fallback default/default: %s",
                exc,
            )
        return await registry.get_controller(sid, aid)
    if registry is not None and all(
        callable(getattr(registry, name, None))
        for name in ("get_job", "create_job", "update_job", "delete_job")
    ):
        return registry
    controller = getattr(host, "_cron_controller", None)
    if controller is not None:
        return controller
    return None


class CronControllerBackend:
    def __init__(self, controller: Any) -> None:
        self._cc = controller

    async def get_job(self, job_id: str) -> Any:
        return await self._cc.get_job(job_id)

    async def create_job(self, *, job_id: str, **payload: Any) -> Any:
        params = {"id": job_id, **payload}
        params.pop("expired", None)
        return await self._cc.create_job(params)

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> Any:
        return await self._cc.update_job(job_id, patch)

    async def delete_job(self, job_id: str, *, force: bool = False) -> bool:
        return await self._cc.delete_job(
            job_id, force=force, skip_ownership=True
        )
