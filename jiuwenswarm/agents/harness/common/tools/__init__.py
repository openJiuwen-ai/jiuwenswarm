# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Tools for JiuWenSwarm AgentServer.

子模块导入会先执行本文件。这里不做导入期重导出：shell 钩子导入
``bash_tool_safety`` 时不能顺带拉起 ``memory_tools``、``symphony_toolkits``
（后者会带进 Gateway 渠道和 openai/mcp）。``from tools import Name`` 仍可用，
名字在第一次访问时再导入。
"""

from __future__ import annotations

import importlib
from typing import Any

# (module, attr, optional)。optional 的 ImportError 保持旧行为：导出 None。
_LAZY: dict[str, tuple[str, str, bool]] = {
    "set_global_memory_manager": (".memory_tools", "set_global_memory_manager", False),
    "init_memory_manager_async": (".memory_tools", "init_memory_manager_async", False),
    "get_decorated_tools": (".memory_tools", "get_decorated_tools", False),
    "memory_search": (".memory_tools", "memory_search", False),
    "memory_get": (".memory_tools", "memory_get", False),
    "write_memory": (".memory_tools", "write_memory", False),
    "edit_memory": (".memory_tools", "edit_memory", False),
    "read_memory": (".memory_tools", "read_memory", False),
    "set_group_chat_mode": (".memory_tools", "set_group_chat_mode", False),
    "is_group_chat_mode": (".memory_tools", "is_group_chat_mode", False),
    "SendFileToolkit": (".send_file_to_user", "SendFileToolkit", False),
    "SendHtmlCardToolkit": (".send_html_card", "SendHtmlCardToolkit", False),
    "XiaoyiAppendReferenceToolkit": (".xiaoyi_append_reference", "XiaoyiAppendReferenceToolkit", False),
    "SkillToolkit": (".skill_toolkits", "SkillToolkit", False),
    "is_skill_retrieval_enabled": (".skill_retrieval_toolkits", "is_skill_retrieval_enabled", False),
    "SkillRetrievalToolkit": (".skill_retrieval_toolkits", "SkillRetrievalToolkit", False),
    "SymphonyToolkit": (".symphony_toolkits", "SymphonyToolkit", False),
    # Re-export deep openjiuwen symbols at ≤3-layer depth so task_tools.py can
    # comply with the G.IMP import-depth lint rule without creating additional files.
    "tool": ("openjiuwen.core.foundation.tool.tool", "tool", True),
    "ce_config": ("openjiuwen.extensions.context_evolver.core.config", "", True),
    "JSONFileConnector": (
        "openjiuwen.extensions.context_evolver.core.file_connector.json_file_connector",
        "JSONFileConnector",
        True,
    ),
    "TaskMemoryService": (
        "openjiuwen.extensions.context_evolver.service.task_memory_service",
        "TaskMemoryService",
        True,
    ),
    "AddMemoryRequest": (
        "openjiuwen.extensions.context_evolver.service.task_memory_service",
        "AddMemoryRequest",
        True,
    ),
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> Any:
    spec = _LAZY.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr, optional = spec
    package = __package__ if module_name.startswith(".") else None
    try:
        module = importlib.import_module(module_name, package)
        value = module if attr == "" else getattr(module, attr)
    except ImportError:
        if not optional:
            raise
        value = None
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
