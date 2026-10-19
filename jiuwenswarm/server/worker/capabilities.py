# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Finite optional capability preparation and ownership of background tasks."""

from __future__ import annotations

import asyncio
import logging

from jiuwenswarm.server.worker.runtime_host import WorkerRuntime
from jiuwenswarm.server.worker.signals import RuntimeSignalSink
from jiuwenswarm.server.worker.telemetry import StartupTelemetry

logger = logging.getLogger(__name__)


def _spawn_teammate_bootstrap(stop_event: asyncio.Event, server: object) -> asyncio.Task:
    from jiuwenswarm.agents.harness.team.remote_member_bootstrap import run_teammate_bootstrap_daemon

    manager = getattr(server, "get_agent_manager", None)
    return asyncio.create_task(run_teammate_bootstrap_daemon(
        stop_event=stop_event, agent_manager=manager() if callable(manager) else None,
    ))


async def start_optional_preparation(
    runtime: WorkerRuntime, signals: RuntimeSignalSink, telemetry: StartupTelemetry,
) -> None:
    """Register factories after core startup; daemon lifetime is never readiness."""
    server = runtime.server

    def on_zen_models_ready() -> None:
        if runtime.stop_event.is_set():
            return
        reset = getattr(server, "reset_model_cache", None)
        if callable(reset):
            reset()
        logger.info("[Worker] zen free models ready: model cache reset for rebuild")
        push = getattr(server, "send_push", None)
        if callable(push):
            try:
                runtime.spawn(push({
                    "request_id": "zen-models-ready", "channel_id": "web",
                    "payload": {"event_type": "models.updated"},
                }), name="zen-models-ready-push")
            except Exception:  # noqa: BLE001
                logger.debug("[Worker] models.updated push failed", exc_info=True)

    async def prepare_zen() -> bool:
        from jiuwenswarm.server.runtime.opencode_zen import (
            register_models_ready_callback, set_main_event_loop, warm_zen_free_models,
        )

        set_main_event_loop(asyncio.get_running_loop())
        register_models_ready_callback(on_zen_models_ready)
        return await warm_zen_free_models(reason="startup", report_status=True) is not False

    async def prepare_image() -> bool:
        schedule = getattr(server, "schedule_image_modality_warmup", None)
        if callable(schedule):
            schedule(reason="startup")
            task = getattr(server, "_image_modality_refresh_task", None)
            if task is not None:
                return await asyncio.shield(task) is not False
        return True

    from jiuwenswarm.common.config import get_config

    config = get_config()
    proactive_config = config.get("proactive_recommendation", {}) if isinstance(config, dict) else {}

    async def prepare_proactive() -> bool:
        from jiuwenswarm.server.runtime.proactive_adapter import init_proactive_engine

        try:
            await init_proactive_engine(server, proactive_config)
            telemetry.stage("proactive_engine_initialized")
            return getattr(server, "_proactive_engine", None) is not None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Worker] proactive engine init failed: %s", exc)
            signals.degraded(str(exc))
            return False

    async def prepare_teammate() -> None:
        if not runtime.stop_event.is_set():
            runtime.teammate_task = _spawn_teammate_bootstrap(runtime.stop_event, server)

    preparation = getattr(server, "_preparation_registry", None)
    if preparation is not None:
        for name, factory, modules in (
            ("image", prepare_image, ()),
            ("zen", prepare_zen, ("jiuwenswarm.server.runtime.opencode_zen",)),
            ("proactive", prepare_proactive, ("jiuwenswarm.server.runtime.proactive_adapter",
                "jiuwenswarm.agents.harness.common.recommendation.proactive_engine")),
            ("teammate", prepare_teammate, ("jiuwenswarm.agents.harness.team.remote_member_bootstrap",)),
        ):
            preparation.register(name, factory, imports=modules)
        runtime.zen_task = preparation.start("zen")
        runtime.proactive_task = preparation.start("proactive")
    else:
        runtime.image_task = asyncio.create_task(prepare_image(), name="image-modality-prepare")
        runtime.zen_task = asyncio.create_task(prepare_zen(), name="zen-free-models-warmup")
        runtime.proactive_task = asyncio.create_task(prepare_proactive(), name="proactive-prepare")
        try:
            await prepare_teammate()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Worker] teammate bootstrap daemon start failed: %s", exc)
    server.proactive_preparation_task = runtime.proactive_task
