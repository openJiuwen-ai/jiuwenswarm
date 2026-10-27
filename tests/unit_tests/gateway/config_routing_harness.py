# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A4g config 面板统一 E2A 后的测试路由 harness。

config/models 方法不再有 gateway 本地执行回落，测试经本 harness 的
fake client 把 E2A 请求路由到真实 ``ConfigAdapter``（与 server 侧等价
路径）：common 层的 monkeypatch 继续生效，响应经 ``proxy_unary_request``
原路回写 channel.responses。需要断言 reload 回调的用例经
``on_config_saved`` 装配（对应 server 侧 ``_reload_after_config_save``
的位置）。
"""
from __future__ import annotations

from typing import Any


def make_config_routing_client(on_config_saved: Any = None) -> Any:
    """构造把 config 域 E2A 请求交给真实 ConfigAdapter 执行的 client。"""
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import ConfigAdapter

    adapter = ConfigAdapter(on_config_saved=on_config_saved)

    class _ConfigRoutingClient:
        # proxy_unary_request 以显式 False 表示传输不可用；恒 True 走本路由
        server_ready = True

        async def send_request(self, env: Any, timeout: float | None = None) -> Any:
            request = AgentRequest(
                request_id=str(getattr(env, "request_id", "") or ""),
                channel_id=str(getattr(env, "channel", "") or ""),
                session_id=getattr(env, "session_id", None),
                req_method=ReqMethod(str(getattr(env, "method", "") or "")),
                params=dict(getattr(env, "params", None) or {}),
                user_id=getattr(env, "user_id", None),
            )
            return await adapter.handle(request)

    return _ConfigRoutingClient()


def make_legacy_config_routing_client(on_config_saved: Any = None) -> Any:
    """legacy 单用户形态的路由 client。

    ``is_legacy_shared_directory_client`` 以 ``WebSocketAgentServerClient``
    类型判定；继承之使安装门控等 legacy 分支语义可测，请求仍经真实
    ConfigAdapter 执行（legacy 单用户的 AgentServer 与 Gateway 同机共享目录，
    测试中以同进程执行等价）。
    """
    from gateway.routing.agent_client import WebSocketAgentServerClient

    base = make_config_routing_client(on_config_saved)

    class _LegacyConfigRoutingClient(WebSocketAgentServerClient):
        def __init__(self) -> None:  # pylint: disable=super-init-not-called
            # 仅借用类型判定，不建立真实连接
            self._routing = base

        @property
        def server_ready(self) -> bool:
            return True

        async def send_request(self, env: Any, timeout: float | None = None) -> Any:
            return await self._routing.send_request(env, timeout)

    return _LegacyConfigRoutingClient()
