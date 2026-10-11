# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""一次请求的上下文。

业务函数只收这个对象：请求内容、回包口、这条连接的编号、能用的服务。
服务清单背后是进程里的那一份工具箱。跟某一条连接绑死的几项，仍问当前这条连接。
业务函数不要拿服务器本身。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.app_services import (
    AppServices,
    parse_sandbox_host_port,
    resolve_adapter,
)
from jiuwenswarm.server.transports.ws_sink import ResponseSink, WSSink


def _app_services_from(source: Any) -> tuple[AppServices, Any]:
    """从真正的服务器取出那一份工具箱；测试用的假对象则按它身上已有的值现凑一份。"""

    ensure = getattr(source, "_ensure_app_services", None)
    if callable(ensure):
        return ensure(), source
    app = AppServices()
    copied = {
        "_agent_manager": "agent_manager",
        "_runtime": "runtime",
        "_session_message_service": "session_message_service",
        "_proactive_engine": "proactive_engine",
        "_scheduler_service": "scheduler_service",
        "_scheduler_agent": "scheduler_agent",
        "_jiuwenbox_runner": "jiuwenbox_runner",
        "_session_stream_tasks": "session_stream_tasks",
        "_model_cache": "model_cache",
        "_default_model": "default_model",
        "_tokenizer_service": "tokenizer_service",
    }
    source_dict = getattr(source, "__dict__", {})
    for old_name, new_name in copied.items():
        if old_name in source_dict:
            setattr(app, new_name, source_dict[old_name])
    return app, source


class AgentServerServices:
    """业务函数能用的服务白名单。

    进程里共用的对象从工具箱拿。写回某一条连接、编辑器能力这种只跟连接有关的，
    仍交给造这次请求时带上的连接对象。清单自己不另存一份状态。
    """

    def __init__(self, source: Any, *, connection: Any = None) -> None:
        if isinstance(source, AppServices):
            self._app = source
            self._connection = connection
        else:
            app, owner = _app_services_from(source)
            self._app = app
            self._connection = owner if connection is None else connection

    def _overridden(self, *names: str) -> Any:
        connection = self._connection
        source_dict = getattr(connection, "__dict__", {})
        for name in names:
            if name in source_dict:
                return source_dict[name]
        return None

    @property
    def agent_manager(self) -> Any:
        return self._app.agent_manager

    @property
    def jiuwenbox_runner(self) -> Any:
        """本进程负责拉起的沙箱进程。"""
        return self._app.jiuwenbox_runner

    def parse_sandbox_host_port(self, url: str) -> Any:
        """从沙箱地址里拆出主机名和端口。"""
        override = self._overridden("_parse_sandbox_host_port", "parse_sandbox_host_port")
        if override is not None:
            return override(url)
        return parse_sandbox_host_port(url)

    def allocate_internal_jiuwenbox_port(self, host: str, preferred_port: int) -> int:
        """内部启动沙箱时，优先用配置里的端口；被占用就换一个空闲端口。"""
        override = self._overridden(
            "_allocate_internal_jiuwenbox_port",
            "allocate_internal_jiuwenbox_port",
        )
        if override is not None:
            return override(host, preferred_port)
        return self._app.allocate_internal_jiuwenbox_port(host, preferred_port)

    def resolve_adapter(self, agent: Any) -> Any:
        """取出这个 Agent 的适配器。回退对话用的是同一套。"""
        override = self._overridden("_resolve_adapter", "resolve_adapter")
        if override is not None:
            return override(agent)
        return resolve_adapter(agent)

    def execution_runtime(self) -> Any:
        """会话和对话共用的运行时。切换会话、记下用户正在输入，都走这里。"""
        override = self._overridden("_execution_runtime", "execution_runtime")
        if override is not None:
            return override()
        return self._app.execution_runtime()

    @property
    def session_message_service(self) -> Any:
        """跨会话排队消息。没装好时是空的。"""
        return self._app.session_message_service

    @property
    def proactive_engine(self) -> Any:
        """主动推荐引擎。没装好时是空的。"""
        return self._app.proactive_engine

    def resolve_model(self, model_name: Optional[str] = None) -> Optional[Any]:
        override = self._overridden("_resolve_model", "resolve_model")
        if override is not None:
            return override(model_name)
        return self._app.resolve_model(model_name)

    @property
    def scheduler_service(self) -> Any:
        """定时任务服务。第一次用到之前是空的。"""
        return self._app.scheduler_service

    @scheduler_service.setter
    def scheduler_service(self, service: Any) -> None:
        self._app.scheduler_service = service

    def set_scheduler_agent(self, agent: Any) -> None:
        """记住定时任务正在用的那个 Agent，避免它被提前丢掉。"""
        override = self._overridden("_set_scheduler_agent", "set_scheduler_agent")
        if override is not None:
            override(agent)
            return
        self._app.set_scheduler_agent(agent)

    def schedule_tokenizer_warmup(self, config: Any, *, reason: str) -> None:
        """后台预热分词器，不挡住这次请求。"""
        override = self._overridden("_schedule_tokenizer_warmup", "schedule_tokenizer_warmup")
        if override is not None:
            override(config, reason=reason)
            return
        self._app.schedule_tokenizer_warmup(config, reason=reason)

    def schedule_image_modality_warmup(self, *, reason: str, reset_cache: bool = False) -> None:
        """后台重探模型能不能看图，不挡住这次请求。"""
        override = self._overridden("schedule_image_modality_warmup")
        if override is not None:
            override(reason=reason, reset_cache=reset_cache)
            return
        self._app.schedule_image_modality_warmup(reason=reason, reset_cache=reset_cache)

    def set_acp_client_capabilities(self, connection_id: str, capabilities: Any) -> None:
        """记下这条连接报上来的客户端能力。

        客户端能力就是对方说自己能做什么，比如能不能读文件。
        只对 ACP 这种编辑器连接有意义。存放用的编号，和连接对象的编号是同一个整数。
        HTTP 没有这种按连接存放的能力，调用了直接返回。
        """

        if self._connection is None:
            return
        key = int(connection_id)
        stored = self._connection._acp_client_capabilities_by_ws
        if isinstance(capabilities, dict):
            stored[key] = dict(capabilities)
        else:
            stored.pop(key, None)

    @property
    def session_stream_tasks(self) -> Any:
        """正在流式输出的任务。取消对话时靠它找到该停的那一个。"""
        return self._app.session_stream_tasks

    async def send_runtime_event(
        self,
        sink: Any,
        event: Any,
        *,
        streaming: bool,
        sequence: int,
    ) -> bool:
        """把运行时的一条事件写回当前连接。太大写不进去时返回 False。"""
        if self._connection is None:
            from jiuwenswarm.server.transports.http_sink import deliver_runtime_event

            return await deliver_runtime_event(
                sink,
                event,
                streaming=streaming,
                sequence=sequence,
            )
        return await self._connection._send_runtime_event(
            getattr(sink, "_ws", None),
            event,
            getattr(sink, "_send_lock", None),
            streaming=streaming,
            sequence=sequence,
        )

    async def open_session_message_resume(self, request: Any) -> Any:
        """如果这是在回答一个挂起的问题，先记下那封排队消息。"""
        if self._connection is None:
            return None
        return await self._connection._open_session_message_resume(request)

    async def finalize_session_message_resume(
        self,
        request: Any,
        resume_state: Any,
        *,
        outcome: str,
        error: str,
    ) -> None:
        """这一轮结束时，把排队消息收尾。"""
        if self._connection is None:
            return
        await self._connection._finalize_session_message_resume(
            request,
            resume_state,
            outcome=outcome,
            error=error,
        )

    async def persist_repeated_session_question(self, request: Any, **kwargs: Any) -> Any:
        """用户还要再答一次时，把下一题和上一题对上。"""
        if self._connection is None:
            return None
        return await self._connection._persist_repeated_session_question(request, **kwargs)

    async def cleanup_client_disconnect_session_runtime(self, request: Any) -> bool:
        """对方断开后，清掉这条连接上还占着的会话运行状态。"""
        if self._connection is None:
            return False
        return await self._connection._cleanup_client_disconnect_session_runtime(request)

    async def send_push(self, msg: Any) -> bool:
        """往当前连接主动推一条消息。压缩上下文这类命令会在回包之外再用它。"""
        if self._connection is None:
            return False
        return await self._connection.send_push(msg)


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
