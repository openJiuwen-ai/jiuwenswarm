# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Tool exports, loaded when the corresponding capability is requested."""
from importlib import import_module

_EXPORTS = {
    "set_global_memory_manager": (".memory_tools", "set_global_memory_manager"),
    "init_memory_manager_async": (".memory_tools", "init_memory_manager_async"),
    "get_decorated_tools": (".memory_tools", "get_decorated_tools"),
    "memory_search": (".memory_tools", "memory_search"),
    "memory_get": (".memory_tools", "memory_get"),
    "write_memory": (".memory_tools", "write_memory"),
    "edit_memory": (".memory_tools", "edit_memory"),
    "read_memory": (".memory_tools", "read_memory"),
    "set_group_chat_mode": (".memory_tools", "set_group_chat_mode"),
    "is_group_chat_mode": (".memory_tools", "is_group_chat_mode"),
    "SendFileToolkit": (".send_file_to_user", "SendFileToolkit"),
    "SkillToolkit": (".skill_toolkits", "SkillToolkit"),
    "build_model_discovery_settings": (".skill_retrieval_toolkits", "build_model_discovery_settings"),
    "is_skill_retrieval_enabled": (".skill_retrieval_toolkits", "is_skill_retrieval_enabled"),
    "is_skill_retrieval_index_enabled": (".skill_retrieval_toolkits", "is_skill_retrieval_index_enabled"),
    "skill_sources_from_manager": (".skill_retrieval_toolkits", "skill_sources_from_manager"),
    "SkillRetrievalToolkit": (".skill_retrieval_toolkits", "SkillRetrievalToolkit"),
    "SymphonyToolkit": (".symphony_toolkits", "SymphonyToolkit"),
    "tool": ("openjiuwen.core.foundation.tool.tool", "tool"),
    "ce_config": ("openjiuwen.extensions.context_evolver.core", "config"),
    "JSONFileConnector": (
        "openjiuwen.extensions.context_evolver.core.file_connector.json_file_connector",
        "JSONFileConnector",
    ),
    "TaskMemoryService": ("openjiuwen.extensions.context_evolver.service.task_memory_service", "TaskMemoryService"),
    "AddMemoryRequest": ("openjiuwen.extensions.context_evolver.service.task_memory_service", "AddMemoryRequest"),
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, symbol = _EXPORTS[name]
    try:
        value = getattr(import_module(module, __name__), symbol)
    except ImportError:
        if module.startswith("."):
            raise
        value = None  # Retain the optional SDK export contract.
    globals()[name] = value
    if isinstance(value, type) or callable(value):
        from jiuwenswarm.runtime.kernel import KernelIdentity, ResourceKey, process_kernel
        identity = KernelIdentity.for_agent(project_dir="", work_mode="tool-factory",
            definition=(module, symbol), capabilities="factory", configuration="python-module")
        key = ResourceKey(identity, "tool-factory", name, module, "", symbol)
        value = process_kernel().recipe(key, lambda: value)
        globals()[name] = value
    return value
