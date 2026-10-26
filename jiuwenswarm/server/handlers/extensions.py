# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""扩展域 handler"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from jiuwenswarm.agents.harness.common.auto_harness import AutoHarnessService
from jiuwenswarm.agents.harness.common.plugins.rail_manager import get_rail_manager
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.common.hooks_config import load_hooks_config
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.context import RequestContext
from jiuwenswarm.server.handlers._shared import (
    _apply_resolved_mode_to_request,
    resolve_request_project_dir,
)
from jiuwenswarm.server.runtime.runtime_scope import RuntimeScopeKey

logger = logging.getLogger(__name__)


def _harness_error_code(exc: BaseException) -> str:
    """Map a harness package exception to a wire ``code`` for the frontend.

    Mirrors the import/export code mapping in app_web_handlers.py so the web UI
    can localize the error via ``err.code`` instead of showing the raw backend
    message (which is locale-unaware). Keep in sync with the frontend
    ``resolveHarnessError`` code→i18n mapping.
    """
    msg = str(exc).lower()
    if "already active" in msg or "already exists" in msg:
        return "CONFLICT"
    if "not found" in msg:
        return "NOT_FOUND"
    if "native" in msg:
        return "BAD_REQUEST"
    return "BAD_REQUEST"


async def handle_extensions_list(ctx: RequestContext) -> None:
    """获取所有 Rail 扩展列表."""
    request = ctx.request
    try:
        manager = get_rail_manager(RuntimeScopeKey.from_request(request))
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

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_extensions_import(ctx: RequestContext) -> None:
    """导入新的 Rail 扩展（文件夹结构）."""
    request = ctx.request
    try:
        params = request.params or {}
        folder_path = params.get("folder_path")

        if not folder_path:
            raise ValueError("缺少 folder_path 参数")

        source_path = Path(folder_path)
        if not source_path.exists() or not source_path.is_dir():
            raise ValueError(f"文件夹不存在或不是目录: {folder_path}")

        manager = get_rail_manager(RuntimeScopeKey.from_request(request))
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

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_extensions_delete(ctx: RequestContext) -> None:
    """删除 Rail 扩展."""
    request = ctx.request
    try:
        params = request.params or {}
        name = params.get("name")

        if not name:
            raise ValueError("缺少 name 参数")

        manager = get_rail_manager(RuntimeScopeKey.from_request(request))
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

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_extensions_toggle(ctx: RequestContext) -> None:
    """切换 Rail 扩展的启用状态，并触发热更新."""
    request = ctx.request
    try:
        params = request.params or {}
        name = params.get("name")
        enabled = params.get("enabled", False)

        if name is None:
            raise ValueError("缺少 name 参数")
        if enabled is None:
            raise ValueError("缺少 enabled 参数")

        manager = get_rail_manager(RuntimeScopeKey.from_request(request))

        # 1. 确保 agent 实例已设置（用于热更新）
        agent = ctx.services.agent_manager.get_agent_nowait()
        if agent is not None:
            agent_instance = await agent.ensure_instance()
            if agent_instance is not None:
                manager.set_agent_instance(agent_instance)

        # 2. 更新配置文件中的启用状态
        extension = manager.toggle_extension(name, enabled)

        # 3. 触发热更新：根据 enabled 状态注册或注销 rail
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

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_hooks_list(ctx: RequestContext) -> None:
    """获取当前 hooks 配置（供 TUI /hooks 命令浏览）."""
    request = ctx.request
    try:
        config_base = get_config()
        hooks_config = load_hooks_config(config_base)
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

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_harness_packages_get(ctx: RequestContext) -> None:
    """Handle harness.packages.get request - retrieve packages info."""
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

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_harness_packages_scan(ctx: RequestContext) -> None:
    """Handle harness.packages.scan request - scan runtime extensions."""
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

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_harness_packages_activate(ctx: RequestContext) -> None:
    """Handle harness.packages.activate request - activate a harness package."""
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    package_id = params.get("package_id")

    if not package_id:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "missing package_id", "code": "BAD_REQUEST"},
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        await ctx.sink.send_wire(wire)
        return

    try:
        # Get or create the agent instance (auto-create if not exists)
        mode, sub_mode = _apply_resolved_mode_to_request(request)
        agent_mode = "agent" if mode == "auto_harness" else mode
        channel_id = request.channel_id or "web"
        agent = await ctx.services.agent_manager.get_agent(
            channel_id=channel_id,
            mode=agent_mode,
            project_dir=resolve_request_project_dir(request),
            sub_mode=sub_mode
        )
        agent_instance = None
        if agent is not None:
            agent_instance = await agent.ensure_instance()
            logger.info(
                "[AgentServer] harness.packages.activate: agent_instance type=%s, has_load_harness_config=%s",
                type(agent_instance).__name__ if agent_instance else None,
                hasattr(agent_instance, "load_harness_config") if agent_instance else False,
            )

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

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_harness_packages_deactivate(ctx: RequestContext) -> None:
    """Handle harness.packages.deactivate request - deactivate a harness package."""
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    package_id = params.get("package_id")

    if not package_id:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "missing package_id", "code": "BAD_REQUEST"},
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        await ctx.sink.send_wire(wire)
        return

    try:
        # Get or create the agent instance (auto-create if not exists)
        channel_id = request.channel_id or "web"
        mode, sub_mode = _apply_resolved_mode_to_request(request)
        agent_mode = "agent" if mode == "auto_harness" else mode
        agent = await ctx.services.agent_manager.get_agent(
            channel_id=channel_id,
            project_dir=resolve_request_project_dir(request),
            mode=agent_mode,
            sub_mode=sub_mode
        )
        agent_instance = None
        if agent is not None:
            agent_instance = await agent.ensure_instance()

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

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)


async def handle_harness_packages_delete(ctx: RequestContext) -> None:
    """Handle harness.packages.delete request - delete a harness package."""
    request = ctx.request
    params = request.params if isinstance(request.params, dict) else {}
    package_id = params.get("package_id")

    if not package_id:
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "missing package_id", "code": "BAD_REQUEST"},
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        await ctx.sink.send_wire(wire)
        return

    if package_id == "native":
        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": "Cannot delete native agent version", "code": "BAD_REQUEST"},
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        await ctx.sink.send_wire(wire)
        return

    try:
        mode, sub_mode = _apply_resolved_mode_to_request(request)
        agent_mode = "agent" if mode == "auto_harness" else mode
        agent = await ctx.services.agent_manager.get_agent(
            channel_id=request.channel_id,
            project_dir=resolve_request_project_dir(request),
            mode=agent_mode,
            sub_mode=sub_mode
        )
        agent_instance = None
        if agent is not None:
            agent_instance = await agent.ensure_instance()

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

    wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
    await ctx.sink.send_wire(wire)
