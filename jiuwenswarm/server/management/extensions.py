# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Extensions management; resources remain owned by its injected host."""

from __future__ import annotations

import logging
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse
from typing import Any
from pathlib import Path
import asyncio
from jiuwenswarm.agents.harness.common.plugins.rail_manager import get_rail_manager

logger = logging.getLogger(__name__)


class ExtensionsManagement:
    def __init__(self, host):
        self.host = host

    async def _handle_extensions_list(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """获取所有 Rail 扩展列表."""
        host = self.host
        try:
            manager = get_rail_manager()
            extensions = manager.list_extensions()
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"extensions": extensions},
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] extensions.list failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_extensions_import(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """导入新的 Rail 扩展（文件夹结构）."""
        host = self.host
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
        except Exception as e:
            logger.exception("[AgentWebSocketServer] extensions.import failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_extensions_delete(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """删除 Rail 扩展."""
        host = self.host
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
        except Exception as e:
            logger.exception("[AgentWebSocketServer] extensions.delete failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    async def _handle_extensions_toggle(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """切换 Rail 扩展的启用状态，并触发热更新."""
        host = self.host
        try:
            params = request.params or {}
            name = params.get("name")
            enabled = params.get("enabled", False)
            if name is None:
                raise ValueError("缺少 name 参数")
            if enabled is None:
                raise ValueError("缺少 enabled 参数")
            manager = get_rail_manager()
            agent = host._agent_manager.get_agent_nowait()
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
        except Exception as e:
            logger.exception("[AgentWebSocketServer] extensions.toggle failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)
