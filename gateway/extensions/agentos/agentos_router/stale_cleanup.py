# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Startup clean-slate: list registry instances and destroy leftover sandboxes.

Gateway memory is empty after restart. YuanRong sandboxes and registry rows
from the previous process are still there. This module lists every instance
from the registry and, asynchronously, deletes the YuanRong sandbox (by
``instance_id``) then unregisters the row.

Live runtimes created after connect are skipped so a concurrent first request
cannot have its new sandbox reaped by the startup pass. Local live snapshots
are a same-process fast path only: a sibling worker's upsert is invisible to
this ``AgentManager``, and an upsert can still land during the DELETE HTTP
round-trip after the re-read. Unregister is therefore a registry CAS on the
deleted ``instance_id``, not an unconditional ``service_id`` delete.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from gateway.extensions.agentos.agentos_router.agent_manager import AgentManager
from gateway.extensions.agentos.agentos_router.logutil import log_agentos
from gateway.telemetry import (
    bind_cleanup_trace, get_cleanup_result, record_cleanup_result, trace_operation,
)
from gateway.extensions.agentos.agentos_router.registry_client import (
    InstanceRecord,
    RegistryClient,
    RegistryNotFoundError,
    instance_service_id,
)
from gateway.extensions.yuanrong_frontend_client import YuanrongFrontendAgentClient

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
    with trace_operation("gw.agent.sweep", root=True, attributes={
        "source": "startup.sweep", "instance_id": str(record.instance_id or ""),
        "service_id": str(record.service_id or ""), "user_id": record.user,
        "agent_type": record.framework,
    }) as operation, bind_cleanup_trace(operation):
        await _cleanup_one_impl(record, yuanrong=yuanrong, registry=registry, agent_manager=agent_manager)


async def _cleanup_one_impl(
    record: InstanceRecord,
    *,
    yuanrong: YuanrongFrontendAgentClient,
    registry: RegistryClient,
    agent_manager: AgentManager,
) -> None:
    live_sandboxes = await _live_sandbox_ids(agent_manager)
    instance_id = str(record.instance_id or "").strip()
    service_id = str(record.service_id or "").strip()

    if instance_id and instance_id not in live_sandboxes:
        sandbox_is_live = False
        try:
            await yuanrong.delete_sandbox(instance_id)
            if get_cleanup_result("frontend.delete") is None:
                record_cleanup_result("frontend.delete", "incomplete", reason="delete_result_unconfirmed")
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
        except Exception as exc:  # noqa: BLE001 - best-effort per record
            outcome = (
                "timeout" if isinstance(exc, TimeoutError)
                or "timeout" in type(exc).__name__.lower()
                else "failed"
            )
            record_cleanup_result("frontend.delete", outcome, reason=type(exc).__name__)
            logger.exception(
                "[AgentOS] sandbox.cleanup.delete.fail sandbox_id=%s service_id=%s",
                instance_id,
                service_id,
            )
    elif not instance_id:
        sandbox_is_live = False
        record_cleanup_result("frontend.delete", "skipped", reason="instance_id_missing")
        logger.warning(
            "[AgentOS] sandbox.cleanup.skip_no_instance_id service_id=%s user=%s framework=%s",
            service_id,
            record.user,
            record.framework,
        )
    else:
        sandbox_is_live = True
        record_cleanup_result("frontend.delete", "skipped", reason="live_instance")

    if not service_id:
        record_cleanup_result("registry.unregister", "skipped", reason="service_id_missing")
        return
    # Same-process fast path: skip DELETE when this Gateway already has a
    # live runtime for the service. Do not treat this as the correctness
    # check — multi-worker upserts never appear here (RELPROC-004).
    live_service_ids = await _refresh_live_service_ids(agent_manager)
    if live_service_ids is not None:
        if service_id in live_service_ids:
            record_cleanup_result("registry.unregister", "skipped", reason="live_runtime")
            log_agentos(
                logger,
                logging.INFO,
                "sandbox.cleanup.unregister.skip_live",
                service_id=service_id,
                user_id=record.user,
                agent_type=record.framework,
                sandbox_id=instance_id,
                instance=instance_id,
            )
            return
    elif sandbox_is_live:
        record_cleanup_result("registry.unregister", "skipped", reason="live_check_failed")
        # Live sandbox is still in memory; do not wipe its registry row just
        # because the follow-up service-id snapshot failed.
        log_agentos(
            logger,
            logging.WARNING,
            "sandbox.cleanup.unregister.skip_live_check_fail",
            service_id=service_id,
            user_id=record.user,
            agent_type=record.framework,
            sandbox_id=instance_id,
            instance=instance_id,
        )
        return
    else:
        # Sandbox was treated as stale (deleted or missing id). Skipping
        # unregister would leave a row pointing at a gone instance. Fall
        # through to CAS unregister; a concurrent upsert of a new
        # instance_id is rejected by the registry instead of wiping the row.
        log_agentos(
            logger,
            logging.WARNING,
            "sandbox.cleanup.unregister.live_check_fail",
            service_id=service_id,
            user_id=record.user,
            agent_type=record.framework,
            sandbox_id=instance_id,
            instance=instance_id,
        )

    try:
        # Identity CAS: delete only if the row still points at the instance
        # this pass cleaned. Covers DELETE-round-trip and other workers.
        result = await registry.unregister_instance(
            service_id,
            expected_instance_id=instance_id,
        )
    except RegistryNotFoundError:
        record_cleanup_result("registry.unregister", "skipped", reason="already_absent")
        return
    except Exception as exc:  # noqa: BLE001 - best-effort per record
        outcome = "timeout" if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower() else "failed"
        record_cleanup_result("registry.unregister", outcome, reason=type(exc).__name__)
        logger.exception(
            "[AgentOS] sandbox.cleanup.unregister.fail service_id=%s",
            service_id,
        )
        return
    if get_cleanup_result("registry.unregister") is None:
        if result.get("deleted") is False:
            record_cleanup_result("registry.unregister", "skipped", reason=result.get("reason") or "already_absent")
        elif result.get("deleted") is True:
            record_cleanup_result("registry.unregister", "success")
        else:
            record_cleanup_result("registry.unregister", "incomplete", reason="unregister_result_unconfirmed")
    if result.get("reason") == "instance_id_mismatch":
        log_agentos(
            logger,
            logging.INFO,
            "sandbox.cleanup.unregister.skip_cas",
            service_id=service_id,
            user_id=record.user,
            agent_type=record.framework,
            sandbox_id=instance_id,
            instance=instance_id,
        )


async def _live_sandbox_ids(agent_manager: AgentManager) -> set[str]:
    sandboxes: set[str] = set()
    for runtime in await agent_manager.list_all_agents():
        sandbox_id = str(runtime.info.sandbox_id or "").strip()
        if sandbox_id:
            sandboxes.add(sandbox_id)
    return sandboxes


async def _live_service_ids(agent_manager: AgentManager) -> set[str]:
    service_ids: set[str] = set()
    for runtime in await agent_manager.list_all_agents():
        user = str(runtime.info.user_id or "").strip()
        framework = str(runtime.info.agent_type or "").strip()
        if user and framework:
            service_ids.add(instance_service_id(user, framework))
    return service_ids


async def _refresh_live_service_ids(agent_manager: AgentManager) -> set[str] | None:
    """Re-read live service ids after delete. None means both attempts failed."""
    for attempt in (1, 2):
        try:
            return await _live_service_ids(agent_manager)
        except Exception:  # noqa: BLE001 - caller chooses unregister fallback
            logger.exception(
                "[AgentOS] sandbox.cleanup.live_service_ids.fail attempt=%s",
                attempt,
            )
    return None
