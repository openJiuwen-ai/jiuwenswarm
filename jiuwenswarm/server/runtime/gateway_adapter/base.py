"""适配器层协议底座：GatewayAdapter 基类与 AdapterRegistry 注册表。

设计（方案第 6 章）：
- ``GatewayAdapter``：按业务域拆分的适配器基类，声明支持的 E2A method
  （``methods``），实现 ``handle(request) -> AgentResponse`` 纯业务契约；
- ``AdapterRegistry``：method → adapter 的注册表，供 ``_handle_message``
  在 if/elif 链顶部查询，命中则走适配器通用执行路径；
- 通用执行（响应编码、异常映射、发送）由 AgentServer 接入点统一完成，
  适配器本身不依赖 ws / send_lock 等传输层对象。

约束：
- 适配器共享协议底座，但不得形成承载全部 Gateway 职责的"大而全
  GatewayAdapter"；每个适配器只负责一个业务域。
"""

from __future__ import annotations

import logging
from typing import ClassVar

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.server.control.responses import (  # noqa: F401
    build_error_response,
    parse_int_param,
)

logger = logging.getLogger(__name__)


class GatewayAdapter:
    """用户业务域适配器基类。

    每个子类：
    - 声明 ``methods``（本适配器支持的 E2A method 值集合，如
      ``{ReqMethod.SESSION_LIST.value}``）；
    - 实现 ``handle``：接收已解析的 ``AgentRequest``，返回 ``AgentResponse``
      （``payload`` 为业务结果；失败时可用 ``build_error_response``）。
    """

    methods: ClassVar[frozenset[str]] = frozenset()

    async def handle(self, request: AgentRequest) -> AgentResponse:
        """处理一个已解析的 AgentRequest（E2A 信封 → AgentRequest 之后）。

        子类必须覆写。实现只读写当前 AgentServer 注入目录中的用户态数据，
        不得依赖 user_id 选择/切换/推导数据目录。
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement handle(request)"
        )


class AdapterRegistry:
    """method → GatewayAdapter 注册表。

    按业务域注册适配器（SessionAdapter、ConfigAdapter、WorkspaceFileAdapter、
    ProjectAdapter、MemoryAdapter、HarmonyOSAdapter 等）；未被注册表覆盖的
    method 继续走既有 if/elif 链。
    """

    def __init__(self) -> None:
        self._handlers: dict[str, GatewayAdapter] = {}

    def register(self, adapter: GatewayAdapter) -> None:
        for method in adapter.methods:
            self._handlers[method] = adapter

    def get(self, method: str | None) -> GatewayAdapter | None:
        if not method:
            return None
        return self._handlers.get(method)

    def contains(self, method: str | None) -> bool:
        return self.get(method) is not None

    def methods(self) -> frozenset[str]:
        return frozenset(self._handlers)
