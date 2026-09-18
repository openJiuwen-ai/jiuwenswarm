# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Startup clean-slate: list registry instances and destroy leftover sandboxes.

Gateway memory is empty after restart. YuanRong sandboxes and registry rows
from the previous process are still there. This module lists every instance
from the registry and, asynchronously, deletes the YuanRong sandbox (by
``instance_id``) then unregisters the row.

Live runtimes created after connect are skipped so a concurrent first request
cannot have its new sandbox reaped by the startup pass.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import AgentManager
from jiuwenswarm.extensions.agentos.agentos_router.logutil import log_agentos
from jiuwenswarm.extensions.agentos.agentos_router.registry_client import (
    InstanceRecord,
    RegistryClient,
    RegistryNotFoundError,
    instance_service_id,
)
from jiuwenswarm.extensions.yuanrong_frontend_client import YuanrongFrontendAgentClient

logger = logging.getLogger(__name__)


async def cleanup_stale_sandboxes(
    *,
    yuanrong: YuanrongFrontendAgentClient,
    registry: RegistryClient,
    agent_manager: AgentManager,
    is_closed: Callable[[], bool] | None = None,
) -> int:
    """List registry instances and best-effort destroy each leftover sandbox.

    Returns the number of records processed (attempted). Errors are logged and
    do not abort the remaining entries.
    """
    if not registry.enabled:
        return 0
    try:
        records = await registry.list_instances(include_unhealthy=True)
    except Exception:  # noqa: BLE001 - startup must not fail closed
        logger.exception("[AgentOS] sandbox.cleanup.list.fail")
        return 0

    log_agentos(
        logger,
        logging.INFO,
        "sandbox.cleanup.startup",
        count=len(records),
    )
    processed = 0
    for record in records:
        if is_closed is not None and is_closed():
            break
        try:
            await _cleanup_one(
                record,
                yuanrong=yuanrong,
                registry=registry,
                agent_manager=agent_manager,
            )
        except Exception:  # noqa: BLE001 - one bad row must not abort the rest
            logger.exception(
                "[AgentOS] sandbox.cleanup.record.fail service_id=%s",
                record.service_id,
            )
        processed += 1
    return processed


async def _cleanup_one(
    record: InstanceRecord,
    *,
    yuanrong: YuanrongFrontendAgentClient,
    registry: RegistryClient,
    agent_manager: AgentManager,
) -> None:
    live_sandboxes, live_service_ids = await _live_ids(agent_manager)
    instance_id = str(record.instance_id or "").strip()
    service_id = str(record.service_id or "").strip()

    if instance_id and instance_id not in live_sandboxes:
        try:
            await yuanrong.delete_sandbox(instance_id)
            log_agentos(
                logger,
                logging.INFO,
                "sandbox.cleanup.delete.ok",
                sandbox_id=instance_id,
                service_id=service_id,
                user_id=record.user,
                agent_type=record.framework,
                instance=instance_id,
            )
        except Exception:  # noqa: BLE001 - best-effort per record
            logger.exception(
                "[AgentOS] sandbox.cleanup.delete.fail sandbox_id=%s service_id=%s",
                instance_id,
                service_id,
            )
    elif not instance_id:
        logger.warning(
            "[AgentOS] sandbox.cleanup.skip_no_instance_id service_id=%s user=%s framework=%s",
            service_id,
            record.user,
            record.framework,
        )

    if service_id and service_id not in live_service_ids:
        try:
            await registry.unregister_instance(service_id)
        except RegistryNotFoundError:
            pass
        except Exception:  # noqa: BLE001 - best-effort per record
            logger.exception(
                "[AgentOS] sandbox.cleanup.unregister.fail service_id=%s",
                service_id,
            )


async def _live_ids(agent_manager: AgentManager) -> tuple[set[str], set[str]]:
    sandboxes: set[str] = set()
    service_ids: set[str] = set()
    for runtime in await agent_manager.list_all_agents():
        sandbox_id = str(runtime.info.sandbox_id or "").strip()
        if sandbox_id:
            sandboxes.add(sandbox_id)
        user = str(runtime.info.user_id or "").strip()
        framework = str(runtime.info.agent_type or "").strip()
        if user and framework:
            service_ids.add(instance_service_id(user, framework))
    return sandboxes, service_ids
