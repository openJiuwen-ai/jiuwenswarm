# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""扩展、hooks、harness 包。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext

logger = logging.getLogger(__name__)


def _harness_error_code(exc: BaseException) -> str:
    """把 harness 包的异常变成前端能识别的错误码。"""

    msg = str(exc).lower()
    if "already active" in msg or "already exists" in msg:
        return "CONFLICT"
    if "not found" in msg:
        return "NOT_FOUND"
    if "native" in msg:
        return "BAD_REQUEST"
    return "BAD_REQUEST"


async def _send(ctx: RequestContext, response: AgentResponse) -> None:
    await ctx.sink.send_response(response)


async def handle_extensions_list(ctx: RequestContext) -> None:
    """获取所有 Rail 扩展列表."""
    from jiuwenswarm.agents.harness.common.plugins.rail_manager import get_rail_manager

    request = ctx.request
    try:
        manager = get_rail_manager()
        extensions = manager.list_extensions()
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"extensions": extensions},
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] extensions.list failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    await _send(ctx, resp)


async def handle_extensions_import(ctx: RequestContext) -> None:
    """导入新的 Rail 扩展（文件夹结构）."""
    from jiuwenswarm.agents.harness.common.plugins.rail_manager import get_rail_manager

    request = ctx.request
    try:
        params = request.params or {}
        folder_path = params.get("folder_path")
        if not folder_path:
            raise ValueError("缺少 folder_path 参数")

        source_path = Path(folder_path)
        if not source_path.exists() or not source_path.is_dir():
            raise ValueError(f"文件夹不存在或不是目录: {folder_path}")

        manager = get_rail_manager()
        extension = manager.import_extension(folder_path)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=extension,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] extensions.import failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    await _send(ctx, resp)


async def handle_extensions_delete(ctx: RequestContext) -> None:
    """删除 Rail 扩展."""
    from jiuwenswarm.agents.harness.common.plugins.rail_manager import get_rail_manager

    request = ctx.request
    try:
        params = request.params or {}
        name = params.get("name")
        if not name:
            raise ValueError("缺少 name 参数")

        manager = get_rail_manager()
        manager.delete_extension(name)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"deleted": True, "name": name},
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] extensions.delete failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    await _send(ctx, resp)


async def handle_extensions_toggle(ctx: RequestContext) -> None:
    """切换 Rail 扩展的启用状态，并触发热更新."""
    from jiuwenswarm.agents.harness.common.plugins.rail_manager import get_rail_manager

    request = ctx.request
    try:
        params = request.params or {}
        name = params.get("name")
        enabled = params.get("enabled", False)
        if name is None:
            raise ValueError("缺少 name 参数")
        if enabled is None:
            raise ValueError("缺少 enabled 参数")

        manager = get_rail_manager()
        agent = ctx.services.agent_manager.get_agent_nowait()
        if agent is not None:
            agent_instance = await agent.ensure_instance()
            if agent_instance is not None:
                manager.set_agent_instance(agent_instance)

        extension = manager.toggle_extension(name, enabled)
        await manager.hot_reload_rail(name, enabled)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=extension,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[AgentWebSocketServer] extensions.toggle failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    await _send(ctx, resp)


async def handle_hooks_list(ctx: RequestContext) -> None:
    """获取当前 hooks 配置（供 TUI /hooks 命令浏览）."""
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.common.hooks_config import load_hooks_config

    request = ctx.request
    try:
        hooks_config = load_hooks_config(get_config())
        summary = hooks_config.get_event_summary()
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "events": summary,
                "disable_all_hooks": hooks_config.disable_all_hooks,
                "source": "config.yaml",
            },
        )
    except Exception as e:
        logger.exception("[AgentWebSocketServer] hooks.list failed: %s", e)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        )
    await _send(ctx, resp)


async def handle_harness_packages_get(ctx: RequestContext) -> None:
    """Handle harness.packages.get request - retrieve packages info."""
    from jiuwenswarm.agents.harness.common.auto_harness import AutoHarnessService

    request = ctx.request
    try:
        service = AutoHarnessService(rail=None, agent=None)
        payload = await asyncio.to_thread(service.get_packages_info)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
        )
    except Exception as exc:
        logger.exception("[AgentServer] harness.packages.get failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc)},
        )
    await _send(ctx, resp)


async def handle_harness_packages_scan(ctx: RequestContext) -> None:
    """Handle harness.packages.scan request - scan runtime extensions."""
    from jiuwenswarm.agents.harness.common.auto_harness import AutoHarnessService

    request = ctx.request
    try:
        service = AutoHarnessService(rail=None, agent=None)
        payload = await asyncio.to_thread(service.scan_runtime_extensions)
        await asyncio.to_thread(service.save_packages, payload)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
        )
    except Exception as exc:
        logger.exception("[AgentServer] harness.packages.scan failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc)},
        )
    await _send(ctx, resp)


async def _load_harness_agent(ctx: RequestContext):
    """按请求里的模式拿到当前 Agent，供启用、停用、删除 harness 包使用。"""
    from jiuwenswarm.runtime.request import (
        apply_resolved_mode_to_request,
        resolve_request_project_dir,
    )

    request = ctx.request
    mode, sub_mode = apply_resolved_mode_to_request(request)
    agent_mode = "agent" if mode == "auto_harness" else mode
    channel_id = request.channel_id or "web"
    agent = await ctx.services.agent_manager.get_agent(
        channel_id=channel_id,
        mode=agent_mode,
        project_dir=resolve_request_project_dir(request),
        sub_mode=sub_mode,
    )
    agent_instance = None
    if agent is not None:
        agent_instance = await agent.ensure_instance()
    return channel_id, agent, agent_instance


async def handle_harness_packages_activate(ctx: RequestContext) -> None:
    """Handle harness.packages.activate request - activate a harness package."""
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    package_id = params.get("package_id")
    if not package_id:
        await _send(
            ctx,
            AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "missing package_id", "code": "BAD_REQUEST"},
            ),
        )
        return

    try:
        channel_id, agent, agent_instance = await _load_harness_agent(ctx)
        if agent is not None:
            logger.info(
                "[AgentServer] harness.packages.activate: agent_instance type=%s, has_load_harness_config=%s",
                type(agent_instance).__name__ if agent_instance else None,
                hasattr(agent_instance, "load_harness_config") if agent_instance else False,
            )
        from jiuwenswarm.agents.harness.common.auto_harness import AutoHarnessService

        service = AutoHarnessService(
            rail=None,
            agent=agent_instance,
            agent_manager=ctx.services.agent_manager,
        )
        payload = await service.activate_package(package_id, channel_id=channel_id)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
        )
    except ValueError as exc:
        logger.warning("[AgentServer] harness.packages.activate validation error: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": _harness_error_code(exc)},
        )
    except Exception as exc:
        logger.exception("[AgentServer] harness.packages.activate failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": "INTERNAL_ERROR"},
        )
    await _send(ctx, resp)


async def handle_harness_packages_deactivate(ctx: RequestContext) -> None:
    """Handle harness.packages.deactivate request - deactivate a harness package."""
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    package_id = params.get("package_id")
    if not package_id:
        await _send(
            ctx,
            AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "missing package_id", "code": "BAD_REQUEST"},
            ),
        )
        return

    try:
        channel_id, _agent, agent_instance = await _load_harness_agent(ctx)
        from jiuwenswarm.agents.harness.common.auto_harness import AutoHarnessService

        service = AutoHarnessService(
            rail=None,
            agent=agent_instance,
            agent_manager=ctx.services.agent_manager,
        )
        payload = await service.deactivate_package(package_id, channel_id=channel_id)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
        )
    except ValueError as exc:
        logger.warning("[AgentServer] harness.packages.deactivate validation error: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": _harness_error_code(exc)},
        )
    except Exception as exc:
        logger.exception("[AgentServer] harness.packages.deactivate failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": "INTERNAL_ERROR"},
        )
    await _send(ctx, resp)


async def handle_harness_packages_delete(ctx: RequestContext) -> None:
    """Handle harness.packages.delete request - delete a harness package."""
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    package_id = params.get("package_id")
    if not package_id:
        await _send(
            ctx,
            AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "missing package_id", "code": "BAD_REQUEST"},
            ),
        )
        return

    if package_id == "native":
        await _send(
            ctx,
            AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "Cannot delete native agent version", "code": "BAD_REQUEST"},
            ),
        )
        return

    try:
        from jiuwenswarm.runtime.request import (
            apply_resolved_mode_to_request,
            resolve_request_project_dir,
        )

        mode, sub_mode = apply_resolved_mode_to_request(request)
        agent_mode = "agent" if mode == "auto_harness" else mode
        agent = await ctx.services.agent_manager.get_agent(
            channel_id=request.channel_id,
            project_dir=resolve_request_project_dir(request),
            mode=agent_mode,
            sub_mode=sub_mode,
        )
        agent_instance = None
        if agent is not None:
            agent_instance = await agent.ensure_instance()
        from jiuwenswarm.agents.harness.common.auto_harness import AutoHarnessService

        service = AutoHarnessService(
            rail=None,
            agent=agent_instance,
            agent_manager=ctx.services.agent_manager,
        )
        payload = await service.delete_package(package_id, channel_id=request.channel_id)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
        )
    except ValueError as exc:
        logger.warning("[AgentServer] harness.packages.delete validation error: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": _harness_error_code(exc)},
        )
    except Exception as exc:
        logger.exception("[AgentServer] harness.packages.delete failed: %s", exc)
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(exc), "code": "INTERNAL_ERROR"},
        )
    await _send(ctx, resp)
