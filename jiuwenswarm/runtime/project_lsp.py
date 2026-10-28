# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Project-scoped LSP leases at the SDK tool boundary.

The pinned SDK tool uses a process-global manager. A context-local facade lets
concurrent projects use separate manager subclasses without switching globals.
The tool itself and its SysOperation remain owned by the invoking Agent.
"""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from typing import Any

from jiuwenswarm.runtime.kernel import KernelIdentity, ResourceKey, identity_reference, process_kernel

_CURRENT_MANAGER: ContextVar[Any] = ContextVar("jiuwenswarm_project_lsp_manager", default=None)
_INSTALLED = False


def install_project_lsp_leases() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from openjiuwen.harness.lsp.core.manager import LSPServerManager
    from openjiuwen.harness.lsp.types import CustomServerConfig, InitializeOptions
    from openjiuwen.harness.tools.lsp_tool import _tool
    from openjiuwen.harness.tools.lsp_tool._tool import _get_effective_cwd as get_effective_cwd

    original = _tool.call_lsp_tool

    class ManagerFacade:
        @staticmethod
        def get_instance():
            manager = _CURRENT_MANAGER.get()
            return manager.get_instance() if manager is not None else LSPServerManager.get_instance()

    @wraps(original)
    async def project_call(inputs, *, workspace=None, operation=None, **kwargs):
        from jiuwenswarm.common.config import get_config
        project = get_effective_cwd(workspace, operation)
        lsp_config = get_config().get("lsp", {})
        identity = KernelIdentity.for_agent(project_dir=str(project), work_mode="lsp",
                                            definition="lsp-service", capabilities=lsp_config,
                                            configuration=lsp_config)
        key = ResourceKey(identity, "lsp", "project-language-servers", "", "", identity_reference(lsp_config))

        async def prepare():
            class ProjectManager(LSPServerManager):
                _instance = None
                _lock = None
            custom = lsp_config.get("custom_servers") if isinstance(lsp_config, dict) else None
            custom_servers = (
                {name: CustomServerConfig(**options) for name, options in custom.items()} if custom else None
            )
            await ProjectManager.initialize(InitializeOptions(cwd=project, custom_servers=custom_servers))
            return ProjectManager

        lease = await process_kernel().acquire_async(key, prepare, close=lambda manager: manager.shutdown())
        token = _CURRENT_MANAGER.set(lease.value)
        try:
            if lease.value.get_instance() is None:
                return {"success": False, "error": "No LSP server configured for this project", "data": None}
            return await original(inputs, workspace=workspace, operation=operation, **kwargs)
        finally:
            _CURRENT_MANAGER.reset(token)
            await lease.release()

    _tool.LSPServerManager = ManagerFacade
    _tool.call_lsp_tool = project_call
    _INSTALLED = True
