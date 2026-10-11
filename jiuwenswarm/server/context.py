# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""一次请求的上下文。

业务函数只收这个对象：请求内容、回包口、这条连接的编号、能用的服务。
服务白名单这一层仍从现有 WebSocket 服务器上取，第 2 层再换成独立工具箱。
业务函数不要拿服务器本身。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.transports.ws_sink import ResponseSink, WSSink


class AgentServerServices:
    """业务函数能用的服务白名单。

    现在背后还是 WebSocket 服务器上已有的对象。handler 只通过这里取值，
    这样第 2 层把状态搬进独立工具箱时，函数签名不用再改。
    """

    def __init__(self, host: Any) -> None:
        self._host = host

    @property
    def agent_manager(self) -> Any:
        return self._host._agent_manager

    @property
    def jiuwenbox_runner(self) -> Any:
        """本进程负责拉起的沙箱进程。"""
        return self._host._jiuwenbox_runner

    def parse_sandbox_host_port(self, url: str) -> Any:
        """从沙箱地址里拆出主机名和端口。"""
        return self._host._parse_sandbox_host_port(url)

    def allocate_internal_jiuwenbox_port(self, host: str, preferred_port: int) -> int:
        """内部启动沙箱时，优先用配置里的端口；被占用就换一个空闲端口。"""
        return self._host._allocate_internal_jiuwenbox_port(host, preferred_port)

    def resolve_adapter(self, agent: Any) -> Any:
        """取出这个 Agent 的适配器。回退对话用的是同一套。"""
        return self._host._resolve_adapter(agent)

    @property
    def proactive_engine(self) -> Any:
        """主动推荐引擎。没装好时是空的。"""
        return self._host._proactive_engine

    def resolve_model(self, model_name: Optional[str] = None) -> Optional[Any]:
        return self._host._resolve_model(model_name)

    def schedule_tokenizer_warmup(self, config: Any, *, reason: str) -> None:
        """后台预热分词器，不挡住这次请求。"""
        self._host._schedule_tokenizer_warmup(config, reason=reason)

    def schedule_image_modality_warmup(self, *, reason: str, reset_cache: bool = False) -> None:
        """后台重探模型能不能看图，不挡住这次请求。"""
        self._host.schedule_image_modality_warmup(reason=reason, reset_cache=reset_cache)

    async def send_push(self, msg: Any) -> bool:
        """往当前连接主动推一条消息。压缩上下文这类命令会在回包之外再用它。"""
        return await self._host.send_push(msg)


@dataclass
class RequestContext:
    """一次请求的小书包。"""

    request: AgentRequest
    sink: ResponseSink
    connection_id: str
    services: AgentServerServices


def build_ws_request_context(
    server: Any,
    ws: Any,
    request: AgentRequest,
    send_lock: Any,
) -> RequestContext:
    """为当前这条 WebSocket 连接造上下文。

    ``connection_id`` 用连接对象的编号，和现有 ACP 能力按连接存放时用的是同一个对象。
    对象还在，编号就不变。
    """

    return RequestContext(
        request=request,
        sink=WSSink(ws, send_lock),
        connection_id=str(id(ws)),
        services=AgentServerServices(server),
    )
