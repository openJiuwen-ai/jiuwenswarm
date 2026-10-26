# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``RequestContext``：业务handler的入参"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.transports.sink import ResponseSink


#: 业务层可用的服务端能力白名单：``ctx.services`` 上的**公有名** → server 上的实际属性名。
#:
#: 为什么两边名字不一样：这些能力在 ``AgentWebSocketServer`` 上历来是受保护成员
#: （重构前它们只被 server 自己的方法以 ``self._xxx`` 访问，属类内自访问）。
#: 业务代码搬出去之后若继续写 ``ctx.services._xxx``，等于把受保护成员的访问
#: 散布到 15 个业务文件的上百个调用点（G.CLS.11「避免在类外访问受保护成员」）。
#:
#: 这里让**门面对外只暴露公有名**，下划线到此为止：
#:
#: - 业务层写 ``ctx.services.agent_manager``，看不到也写不出私有名；
#: - ``AgentWebSocketServer`` 侧**一个字不用改** —— 属性名、语义、调用方全不变；
#: - 唯一一处跨类访问收敛到下面 ``__getattr__`` 的那句 ``getattr``。
#:
#: 新增能力时在此登记一行；越界访问仍会当场 ``AttributeError``（语义与从前一致）。
SERVICE_MEMBERS: dict[str, str] = {
    # 共享状态（server 实例属性；server 自己也在用，故走委托而非持有）
    "agent_manager": "_agent_manager",
    "session_stream_tasks": "_session_stream_tasks",
    "scheduler_service": "_scheduler_service",
    "scheduler_agent": "_scheduler_agent",
    "jiuwenbox_runner": "_jiuwenbox_runner",
    "proactive_engine": "_proactive_engine",
    "model_cache": "_model_cache",
    "default_model": "_default_model",
    # 跨域行为
    "prepare_session_switch_owner": "_prepare_session_switch_owner",
    "dispatch_session_switch_kvc": "_dispatch_session_switch_kvc",
    "ensure_persistent_checkpointer_response": "_ensure_persistent_checkpointer_response",
    "resolve_adapter": "_resolve_adapter",
    "build_model_cache": "_build_model_cache",
    "tenant_pool": "_tenant_pool",
    # 沙箱端口分配
    "is_tcp_port_bindable": "_is_tcp_port_bindable",
    "pick_free_tcp_port": "_pick_free_tcp_port",
    # 默认路径需要的实例状态
    "stateless_fallback_agents": "_stateless_fallback_agents",
    # pipeline 按连接标识读 ACP 客户端能力（业务层不碰传输对象）
    "get_acp_client_capabilities": "_get_acp_client_capabilities",
    # 连接态相关（仍由 server 持有）。``send_push`` 在 server 上本就是公有名。
    "send_push": "send_push",
    "set_acp_client_capabilities": "_set_acp_client_capabilities",
}


class AgentServerServices:
    """``ctx.services``，业务层看得见的服务端面。

    只暴露 :data:`SERVICE_MEMBERS` 登记的公有名，读写都穿透到同一个 server 实例
    （身份保持），越界当场 ``AttributeError``。
    """

    __slots__ = ("_server",)

    def __init__(self, server: Any) -> None:
        object.__setattr__(self, "_server", server)

    @property
    def raw_server(self) -> Any:
        """逃生舱：仅供**传输层**自己使用，业务层不要碰。"""
        return object.__getattribute__(self, "_server")

    def __getattr__(self, name: str) -> Any:
        target = SERVICE_MEMBERS.get(name)
        if target is None:
            raise AttributeError(
                f"{name!r} 不在 AgentServerServices 的清单里。业务 handler 只能使用 "
                f"context.SERVICE_MEMBERS 中声明的协作者；若这是本域私有 helper，"
                f"应改为 handlers/<域>.py 的模块级函数。"
            )
        return getattr(object.__getattribute__(self, "_server"), target)

    def __setattr__(self, name: str, value: Any) -> None:
        target = SERVICE_MEMBERS.get(name)
        if target is None:
            raise AttributeError(f"{name!r} 不在 AgentServerServices 的清单里，不可写入。")
        setattr(object.__getattribute__(self, "_server"), target, value)


@dataclass(frozen=True)
class RequestContext:
    """一次请求的全部上下文。

    Attributes:
        request: 已解析的业务请求。
        sink: 响应出口（``WSSink`` / ``UnaryHTTPSink`` / ``SSESink``）。
        connection_id: 连接标识，供需要按连接分槽的逻辑使用
            （ACP 客户端能力、``session.switch`` 的切换锁）。
            WS 用 ``str(id(ws))``；HTTP 用常量 ``HTTP_CONNECTION_ID``。

            **传输层必须保证它跨请求稳定** —— 取每请求唯一的值会让上述逻辑
            静默退化成零互斥。HTTP 侧的取值理由见
            ``agent_http_server.HTTP_CONNECTION_ID`` 的说明。
        services: 业务层可用的服务端窄面，成员限于 :data:`SERVICE_MEMBERS`
            （见 :class:`AgentServerServices`）。
    """

    request: AgentRequest
    sink: ResponseSink
    connection_id: str
    services: Any = None

    @property
    def params(self) -> dict[str, Any]:
        """``request.params`` 的安全访问（非 dict 时返回空 dict）。"""
        return self.request.params if isinstance(self.request.params, dict) else {}

    @property
    def request_id(self) -> str:
        return self.request.request_id or ""

    @property
    def channel_id(self) -> str:
        return self.request.channel_id or "web"

    @property
    def session_id(self) -> str | None:
        return self.request.session_id
