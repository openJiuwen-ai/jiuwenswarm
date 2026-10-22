# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Construct the neutral Runtime host behind required preparation barriers."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from jiuwenswarm.server.worker import runtime_backend

logger = logging.getLogger(__name__)


async def construct_runtime_server(
    host: str,
    port: int,
    *,
    on_constructed: Callable[[Any], Awaitable[None]] | None = None,
    telemetry: Any,
) -> Any:
    """Bind SDK registry ownership before concurrent Runtime construction."""
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.worker.callback_registry import bind_callback_registry_owner

    registry_owner = bind_callback_registry_owner(Runner.callback_framework)
    published = False

    async def publish(server: Any) -> None:
        nonlocal published
        server.worker_callback_registry_owner = registry_owner
        if on_constructed is not None:
            # The receiver owns cleanup even if its bind operation then fails.
            published = True
            await on_constructed(server)

    try:
        return await _construct_runtime_server(
            host, port, on_constructed=publish, telemetry=telemetry,
        )
    except BaseException:
        if not published:
            await registry_owner.aclose()
        raise


async def _construct_runtime_server(
    host: str,
    port: int,
    *,
    on_constructed: Callable[[Any], Awaitable[None]] | None,
    telemetry: Any,
) -> Any:
    """Join construction, execution definitions and extension registration.

    Call only after backend preparation. A constructed server is published to
    its cleanup owner before a sibling preparation failure is propagated.
    """
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.extensions.manager import ExtensionManager
    from jiuwenswarm.extensions.registry import ExtensionRegistry
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    extension_registry = ExtensionRegistry.create_instance(
        callback_framework=Runner.callback_framework,
        config={},
        logger=logger,
    )
    extension_manager = ExtensionManager(registry=extension_registry)
    telemetry.stage("extension_manager_created")

    def construct_server() -> Any:
        return AgentWebSocketServer.get_instance(host=host, port=port)

    async def load_extensions() -> None:
        # Shared adapter definitions install SDK compatibility patches. User
        # extension callbacks must observe those patches before registration.
        await execution_code_task
        await extension_manager.load_all_extensions(include_transport_extensions=False)
        telemetry.stage("extensions_loaded")

    # Neutral construction overlaps shared definitions. Extensions follow the
    # compatibility patch barrier; all three must finish before Runner starts.
    execution_code_task = asyncio.create_task(
        asyncio.to_thread(runtime_backend.preload_shared_execution_code)
    )
    extensions, server, execution_code = await asyncio.gather(
        load_extensions(),
        asyncio.to_thread(construct_server),
        execution_code_task,
        return_exceptions=True,
    )
    if isinstance(server, BaseException):
        if isinstance(extensions, BaseException):
            raise extensions
        raise server
    # No Runtime consumer starts before this joined barrier succeeds, but the
    # cleanup owner must retain a successfully constructed server on failure.
    if on_constructed is not None:
        await on_constructed(server)
    if isinstance(extensions, BaseException):
        raise extensions
    if isinstance(execution_code, BaseException):
        raise execution_code
    logger.info(
        "[Worker] 扩展加载完成，共 %d 个 (elapsed %.2fs)",
        len(extension_manager.list_extensions()), telemetry.elapsed,
    )
    await asyncio.sleep(0)
    telemetry.stage("runtime_constructed")
    return server


def bind_runtime_signals(server: Any, signals: Any) -> None:
    """Install supported readiness hooks without changing admission phases."""
    set_hooks = getattr(server, "set_runtime_lifecycle_hooks", None)
    if not callable(set_hooks):
        return
    hooks = {
        "on_ready": getattr(signals, "runtime_base_ready", signals.agent_ready),
        "on_warmup_retry": signals.warmup_retry,
        "on_failed": signals.failed,
    }
    parameters = inspect.signature(set_hooks).parameters
    if "on_capabilities" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    ):
        def on_capabilities(readiness: Any, fully_warm: bool) -> None:
            signals.capabilities(readiness, fully_warm=fully_warm)

        hooks["on_capabilities"] = on_capabilities
    if "on_execution_min_ready" in parameters:
        hooks["on_execution_min_ready"] = signals.execution_min_ready
    set_hooks(**hooks)
