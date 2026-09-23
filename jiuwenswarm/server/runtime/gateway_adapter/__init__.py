"""Gateway 用户业务适配器层（AgentServer 侧）。

把 Gateway 转发的 E2A 用户业务请求（session / config / workspace / project /
memory / harmonyos / cron 项目解析等）转换为 AgentServer 中用户态业务门面
可消费的形式，在当前 AgentServer 外部注入的 ``.jiuwenswarm`` 中执行。

注：Team 域请求（``team.*``）不经适配器层，由 ``agent_ws_server.py``
if/elif 链直接处理（已有 E2A handler）。

约束（方案第 6 章）：
- 适配器只负责 Gateway 请求与用户业务门面之间的兼容，不负责认证、鉴权、
  用户路由、平台连接或长期调度；
- 适配器不得反向依赖或导入 ``gateway.*``；可复用逻辑必须来自
  ``server/runtime`` 等中立模块；
- 适配器不得根据请求中的 ``user_id`` 选择、切换或推导用户目录。

Eager re-export is avoided so AgentServer Front can import
``gateway_adapter.base`` / ``session_adapter`` without loading MemoryAdapter
(Harness) or ConfigAdapter (OpenJiuwen).
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "AdapterRegistry",
    "GatewayAdapter",
    "SessionAdapter",
    "MemoryAdapter",
    "ProjectAdapter",
    "WorkspaceFileAdapter",
    "HarmonyOSAdapter",
    "ConfigAdapter",
]

_EXPORTS = {
    "AdapterRegistry": "jiuwenswarm.server.runtime.gateway_adapter.base",
    "GatewayAdapter": "jiuwenswarm.server.runtime.gateway_adapter.base",
    "SessionAdapter": "jiuwenswarm.server.runtime.gateway_adapter.session_adapter",
    "MemoryAdapter": "jiuwenswarm.server.runtime.gateway_adapter.memory_adapter",
    "ProjectAdapter": "jiuwenswarm.server.runtime.gateway_adapter.project_adapter",
    "WorkspaceFileAdapter": "jiuwenswarm.server.runtime.gateway_adapter.workspace_file_adapter",
    "HarmonyOSAdapter": "jiuwenswarm.server.runtime.gateway_adapter.harmonyos_adapter",
    "ConfigAdapter": "jiuwenswarm.server.runtime.gateway_adapter.config_adapter",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module_name), name)


def __dir__() -> list[str]:
    return sorted(list(globals()) + list(_EXPORTS))
