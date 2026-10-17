"""MemoryAdapter: execute TUI memory management in the user AgentServer.

The Gateway must not derive a workspace path or access memory files itself.
This adapter deliberately uses only ``get_agent_workspace_dir()`` from the
current process, whose data directory is supplied when the AgentServer starts.
``user_id`` carried by the E2A envelope is never used for directory selection.
"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.agents.harness.common.memory_rpc import (
    handle_memory_edit,
    handle_memory_list,
    handle_memory_open,
    handle_memory_status,
    handle_memory_toggle,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.server.runtime.gateway_adapter.base import (
    GatewayAdapter,
    build_error_response,
)

logger = logging.getLogger(__name__)


class MemoryAdapter(GatewayAdapter):
    """Adapter for the five existing TUI ``memory.*`` management methods."""

    methods: frozenset[str] = frozenset(
        {
            ReqMethod.MEMORY_LIST.value,
            ReqMethod.MEMORY_EDIT.value,
            ReqMethod.MEMORY_STATUS.value,
            ReqMethod.MEMORY_TOGGLE.value,
            ReqMethod.MEMORY_OPEN.value,
        }
    )

    async def handle(self, request: AgentRequest) -> AgentResponse:
        params = request.params if isinstance(request.params, dict) else {}
        workspace = str(get_agent_workspace_dir())
        method = request.req_method
        try:
            result = await self._dispatch(method, workspace, dict(params))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MemoryAdapter] %s failed: %s", method, exc)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=result,
            metadata=request.metadata,
        )

    @staticmethod
    def _resolve_project_dir(params: dict[str, Any]) -> str | None:
        """从 TUI 参数中解析项目记忆所属 project_dir（与迁移前 tui_connect 一致）。

        TUI 前端 ``memory.edit`` 只下发 ``trusted_dirs`` / ``cwd`` 而不下发
        ``project_dir``；原 Gateway 层在此补全后再调 ``handle_memory_*``。
        ``memory_rpc`` 的各 handler 只消费 ``params["project_dir"]``，若不补全，
        code 模式下项目目录（trusted_dirs[0] / cwd）内的记忆文件会被误判为
        越界而拒绝编辑。
        """
        project_dir = params.get("project_dir")
        if isinstance(project_dir, str) and project_dir:
            return project_dir
        trusted_dirs = params.get("trusted_dirs")
        if isinstance(trusted_dirs, list) and trusted_dirs:
            return str(trusted_dirs[0])
        cwd = params.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
        return None

    @staticmethod
    async def _dispatch(
        method: ReqMethod | None, workspace: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        mode = str(params.get("mode") or "plan")
        # 仅 memory.toggle 不需要 project_dir（原 TUI 实现未注入，保持原行为）；
        # 其余四个入口在迁移前都会把解析出的 project_dir 注入 params。
        if method in {
            ReqMethod.MEMORY_LIST,
            ReqMethod.MEMORY_EDIT,
            ReqMethod.MEMORY_STATUS,
            ReqMethod.MEMORY_OPEN,
        }:
            project_dir = MemoryAdapter._resolve_project_dir(params)
            if project_dir:
                params = {**params, "project_dir": project_dir}
        if method == ReqMethod.MEMORY_LIST:
            return await handle_memory_list(workspace, mode, params)
        if method == ReqMethod.MEMORY_EDIT:
            return await handle_memory_edit(workspace, params)
        if method == ReqMethod.MEMORY_STATUS:
            return await handle_memory_status(workspace, mode, params)
        if method == ReqMethod.MEMORY_TOGGLE:
            return await handle_memory_toggle(workspace, mode, params)
        if method == ReqMethod.MEMORY_OPEN:
            return await handle_memory_open(workspace, params)
        raise ValueError(f"unsupported method: {method}")
