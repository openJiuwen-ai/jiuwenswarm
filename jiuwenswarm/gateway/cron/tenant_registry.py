# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-tenant Gateway cron store + scheduler registry."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar

from jiuwenswarm.common.utils import resolve_cron_tenant_scope
from jiuwenswarm.gateway.cron.agent_mirror import mirror_job_delete, mirror_job_upsert
from jiuwenswarm.gateway.cron.controller import CronController
from jiuwenswarm.gateway.cron.job_access import create_tenant_cron_store
from jiuwenswarm.gateway.cron.models import CronTargetChannel
from jiuwenswarm.gateway.cron.scheduler import CronSchedulerService

logger = logging.getLogger(__name__)


class CronTenantRegistry:
    """Lazy per-(service_id, agent_id) CronController + CronSchedulerService."""

    _instance: ClassVar[CronTenantRegistry | None] = None

    def __init__(
        self,
        *,
        agent_client: Any,
        message_handler: Any,
        cron_run_ephemeral: Any | None = None,
    ) -> None:
        self._agent_client = agent_client
        self._message_handler = message_handler
        self._cron_run_ephemeral = cron_run_ephemeral
        self._controllers: dict[tuple[str, str], CronController] = {}
        self._controller_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()
        self._target_channel: CronTargetChannel | None = None

    @classmethod
    def get_instance(
        cls,
        *,
        agent_client: Any | None = None,
        message_handler: Any | None = None,
        cron_run_ephemeral: Any | None = None,
    ) -> CronTenantRegistry:
        if cls._instance is not None:
            return cls._instance
        if agent_client is None or message_handler is None:
            raise RuntimeError(
                "CronTenantRegistry not initialized. Call get_instance(agent_client=..., message_handler=...) first."
            )
        cls._instance = cls(
            agent_client=agent_client,
            message_handler=message_handler,
            cron_run_ephemeral=cron_run_ephemeral,
        )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def set_target_channel(self, channel: CronTargetChannel) -> None:
        self._target_channel = channel
        for controller in self._controllers.values():
            controller.set_target_channel(channel)

    @staticmethod
    def resolve_scope(
        *,
        service_id: str | None = None,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        p = dict(payload or {})
        return resolve_cron_tenant_scope(
            service_id=service_id or p.get("service_id"),
            agent_id=agent_id or p.get("agent_id"),
            metadata=metadata,
            params=params,
            log_prefix="[CronTenantRegistry]",
        )

    async def _lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        async with self._meta_lock:
            lock = self._controller_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._controller_locks[key] = lock
            return lock

    async def get_controller(
        self,
        service_id: str,
        agent_id: str,
    ) -> CronController:
        sid = str(service_id or "default").strip() or "default"
        aid = str(agent_id or "default").strip() or "default"
        key = (sid, aid)
        cached = self._controllers.get(key)
        if cached is not None:
            return cached

        lock = await self._lock_for(key)
        async with lock:
            cached = self._controllers.get(key)
            if cached is not None:
                return cached

            store = create_tenant_cron_store(sid, aid)
            scheduler = CronSchedulerService(
                store=store,
                agent_client=self._agent_client,
                message_handler=self._message_handler,
                service_id=sid,
                agent_id=aid,
                run_ephemeral=self._cron_run_ephemeral,
            )
            await scheduler.hydrate_runs_from_ephemeral()
            await scheduler.start()
            controller = CronController(
                store=store,
                scheduler=scheduler,
                service_id=sid,
                agent_id=aid,
            )
            if self._target_channel is not None:
                controller.set_target_channel(self._target_channel)

            existing = self._controllers.get(key)
            if existing is not None:
                try:
                    await scheduler.stop()
                except Exception:
                    logger.warning(
                        "[CronTenantRegistry] orphan scheduler stop failed "
                        "service_id=%s agent_id=%s",
                        sid,
                        aid,
                        exc_info=True,
                    )
                return existing

            self._controllers[key] = controller
            logger.info(
                "[CronTenantRegistry] initialized tenant cron service_id=%s agent_id=%s path=%s",
                sid,
                aid,
                store.path,
            )
            return controller

    @staticmethod
    async def _mirror_after_mutation(
        *,
        service_id: str,
        agent_id: str,
        job: dict[str, Any] | None = None,
        deleted_job_id: str | None = None,
    ) -> None:
        try:
            if job is not None:
                await mirror_job_upsert(job, service_id=service_id, agent_id=agent_id)
            elif deleted_job_id:
                await mirror_job_delete(
                    deleted_job_id, service_id=service_id, agent_id=agent_id
                )
        except Exception as exc:
            logger.warning(
                "[CronTenantRegistry] agent mirror failed service_id=%s agent_id=%s: %s",
                service_id,
                agent_id,
                exc,
            )

    async def handle_push_action(
        self,
        *,
        action: str,
        params: dict[str, Any],
        service_id: str,
        agent_id: str,
        request_mode: str | None = None,
        mirror_to_agent: bool = False,
    ) -> Any:
        controller = await self.get_controller(service_id, agent_id)
        if action == "list":
            return await controller.list_jobs()
        if action == "get":
            return await controller.get_job(str(params.get("job_id") or ""))
        if action == "create":
            if request_mode:
                params = dict(params)
                params["mode"] = request_mode
            data = await controller.create_job(params)
            if mirror_to_agent:
                await self._mirror_after_mutation(
                    service_id=service_id, agent_id=agent_id, job=data
                )
            return data
        if action == "update":
            job_id = str(params.get("job_id") or "")
            patch = dict(params.get("patch") or {})
            data = await controller.update_job(job_id, patch)
            if mirror_to_agent:
                await self._mirror_after_mutation(
                    service_id=service_id, agent_id=agent_id, job=data
                )
            return data
        if action == "delete":
            job_id = str(params.get("job_id") or "")
            deleted = await controller.delete_job(job_id)
            if mirror_to_agent and deleted:
                await self._mirror_after_mutation(
                    service_id=service_id,
                    agent_id=agent_id,
                    deleted_job_id=job_id,
                )
            return {"deleted": deleted}
        if action == "toggle":
            job_id = str(params.get("job_id") or "")
            enabled = bool(params.get("enabled"))
            data = await controller.toggle_job(job_id, enabled)
            if mirror_to_agent:
                await self._mirror_after_mutation(
                    service_id=service_id, agent_id=agent_id, job=data
                )
            return data
        if action == "preview":
            return await controller.preview_job(
                str(params.get("job_id") or ""),
                int(params.get("count", 5)),
            )
        if action == "run_now":
            return {"run_id": await controller.run_now(str(params.get("job_id") or ""))}
        return {"error": f"unknown cron action: {action}"}

    async def web_create_job(
        self, params: dict[str, Any], service_id: str, agent_id: str
    ) -> dict[str, Any]:
        data = await (await self.get_controller(service_id, agent_id)).create_job(params)
        await self._mirror_after_mutation(
            service_id=service_id, agent_id=agent_id, job=data
        )
        return data

    async def web_update_job(
        self,
        job_id: str,
        patch: dict[str, Any],
        service_id: str,
        agent_id: str,
        *,
        group_id: str | None = None,
        bot_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        data = await (await self.get_controller(service_id, agent_id)).update_job(
            job_id,
            patch,
            group_id=group_id,
            bot_id=bot_id,
            user_id=user_id,
        )
        await self._mirror_after_mutation(
            service_id=service_id, agent_id=agent_id, job=data
        )
        return data

    async def web_delete_job(
        self,
        job_id: str,
        service_id: str,
        agent_id: str,
        *,
        group_id: str | None = None,
        bot_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        deleted = await (await self.get_controller(service_id, agent_id)).delete_job(
            job_id,
            group_id=group_id,
            bot_id=bot_id,
            user_id=user_id,
        )
        if deleted:
            await self._mirror_after_mutation(
                service_id=service_id, agent_id=agent_id, deleted_job_id=job_id
            )
        return deleted

    async def web_toggle_job(
        self,
        job_id: str,
        enabled: bool,
        service_id: str,
        agent_id: str,
        *,
        group_id: str | None = None,
        bot_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        data = await (await self.get_controller(service_id, agent_id)).toggle_job(
            job_id,
            enabled,
            group_id=group_id,
            bot_id=bot_id,
            user_id=user_id,
        )
        await self._mirror_after_mutation(
            service_id=service_id, agent_id=agent_id, job=data
        )
        return data

    async def stop_all(self) -> None:
        for controller in list(self._controllers.values()):
            try:
                await controller.stop_scheduler()
            except Exception as exc:
                logger.warning("[CronTenantRegistry] scheduler stop failed: %s", exc)
        self._controllers.clear()
        self._controller_locks.clear()
