# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Runtime Management Agent Client.

通过 openjiuwen_runtime.management.orchestrator.Access 与 AgentServer 通信。
"""

from __future__ import annotations

import os
import hashlib
import json
import logging
import threading
from typing import Any, AsyncIterator, Optional

from jiuwenclaw.e2a.agent_compat import e2a_to_agent_request
from jiuwenclaw.e2a.models import E2AEnvelope, E2AResponse
from jiuwenclaw.e2a.wire_codec import (
    is_e2a_response_wire_dict,
    parse_agent_server_wire_chunk,
)
from jiuwenclaw.gateway.agent_client import AgentServerClient, _wire_request_id_key
from jiuwenclaw.schema import AgentRequest
from jiuwenclaw.schema.agent import AgentResponse, AgentResponseChunk

from openjiuwen_runtime.management.session.access import Access
from openjiuwen_runtime.management.session.dual_queue import PriorityDualAsyncQueues
from openjiuwen_runtime.management.session.interfaces import (
    IResponseParser,
    IServiceInstanceFactory,
    IServiceHandler,
)
from openjiuwen_runtime.management.session.k8s_service_handler import (
    K8sDeployController,
    K8sServiceHandler,
)
from openjiuwen_runtime.management.session.models import AccessConfig, SessionConfig
from openjiuwen_runtime.management.session.service_handler import ServiceHandler
from openjiuwen_runtime.management.session.service_manager import (
    ServiceManager,
    QueueItem,
)
from openjiuwen_runtime.management.session.timer import Timer
from openjiuwen_runtime.management.session.ws_client_channel import WSServiceMessageChannel
from openjiuwen_runtime.management.session.interfaces import ISessionRequest
from openjiuwen_runtime.management.session.models import MessagePriority

logger = logging.getLogger(__name__)

_session_id_lock = threading.Lock()
_session_id_to_service_pair: dict[str, tuple[str, str | None]] = {}


def _md5_chat_bot_id(chat_id: str, bot_id: str) -> str:
    return hashlib.md5("::".join((chat_id, bot_id)).encode("utf-8")).hexdigest()


def _session_id_to_invoke_ids(session_id: str) -> tuple[str, str | None]:
    """将网关 session_id 转为 invoker 的 (service_id, agent_id/space_id)。"""
    with _session_id_lock:
        cached = _session_id_to_service_pair.get(session_id)
        if cached:
            return cached

    from jiuwenclaw.gateway.session_map import load_session_map_scope
    _ = load_session_map_scope()
    
    parts = session_id.split("::")
    if len(parts) == 6:
        _provider, chat_id, bot_id, user_id, _ts, _suffix = parts
        pair = (_md5_chat_bot_id(chat_id, bot_id), user_id)
    elif len(parts) == 5:
        _provider, chat_id, bot_id, _ts, _suffix = parts
        pair = (_md5_chat_bot_id(chat_id, bot_id), None)
    else:
        pair = (hashlib.md5(session_id.encode("utf-8")).hexdigest(), None)

    with _session_id_lock:
        _session_id_to_service_pair.setdefault(session_id, pair)
        return _session_id_to_service_pair[session_id]


class _SessionRequest(ISessionRequest):
    """ISessionRequest 实现。"""

    def __init__(self, msg: AgentRequest, envelope: E2AEnvelope) -> None:
        self._req = msg
        self._envelope = envelope
        service_id, agent_id = _session_id_to_invoke_ids(self._req.session_id or "")
        self._service_id = service_id
        self._req.service_id = service_id
        self._req.agent_id = agent_id or ""
        self._envelope.service_id = service_id
        self._envelope.agent_id = agent_id or None

    @property
    def session_id(self) -> str:
        return self._service_id

    @property
    def session_concurrency(self) -> int:
        return 10

    @property
    def session_ttl(self) -> int:
        return 20

    @property
    def priority(self) -> "MessagePriority":
        return MessagePriority.LOW

    @property
    def request_id(self) -> Optional[str]:
        return self._req.request_id

    @property
    def raw_msg(self) -> Any:
        return json.dumps(self._envelope.to_dict(), ensure_ascii=False)


class E2aEnvelopResponseParser(IResponseParser):
    """解析 AgentServer WebSocket 下行 JSON。"""

    def request_id(self, data: dict[str, Any]) -> Optional[str]:
        rid = data.get("request_id")
        if rid is None:
            rid = data.get("response_id")
        if rid is None:
            rid = data.get("id")
        if rid is None:
            return None
        out = _wire_request_id_key(rid)
        return out if out else None

    def is_completed(self, data: dict[str, Any]) -> bool:
        if not isinstance(data, dict):
            return True
        if data.get("type") == "event":
            return False
        if is_e2a_response_wire_dict(data):
            try:
                e2a = E2AResponse.from_dict(dict(data))
                return bool(e2a.is_final)
            except Exception:
                return False
        return bool(data.get("is_complete"))

    def response(self, data: dict[str, Any]) -> Any:
        return parse_agent_server_wire_chunk(data)


class RuntimeManagementAgentClient(AgentServerClient):
    """Runtime Management Agent Client."""

    def __init__(self) -> None:
        """初始化 Runtime Management 客户端。"""
        
        agent_image = os.getenv("AGENT_SERVER_IMAGE")
        namespace = os.getenv("AGENT_SERVER_NAMESPACE")
        container_name = os.getenv("AGENT_SERVER_CONTAINER_NAME")
        container_port = int(os.getenv("AGENT_SERVER_PORT"))
        port_name = os.getenv("AGENT_SERVER_PORT_NAME")
        image_pull_policy = os.getenv("AGENT_SERVER_IMAGE_PULL_POLICY")
        min_idle_services = int(os.getenv("AGENT_SERVER_MIN_IDLE_SERVICES"))
        max_services = int(os.getenv("AGENT_SERVER_MAX_SERVICES"))
        service_concurrency = int(os.getenv("AGENT_SERVER_SERVICE_CONCURRENCY"))
        service_ttl = int(os.getenv("AGENT_SERVER_SERVICE_TTL"))
        autoscale_interval = float(os.getenv("AGENT_SERVER_AUTOSCALE_INTERVAL"))
        nfs_server = os.getenv("AGENT_SERVER_NFS_SERVER", "")
        nfs_path = os.getenv("AGENT_SERVER_NFS_PATH", "/")
        nfs_mount_path = os.getenv("AGENT_SERVER_NFS_MOUNT_PATH")
        kubeconfig = os.getenv("AGENT_SERVER_KUBECONFIG") or None
        readiness_initial_delay = int(os.getenv("AGENT_SERVER_READINESS_INITIAL_DELAY"))
        readiness_period = int(os.getenv("AGENT_SERVER_READINESS_PERIOD"))
        ready_timeout = int(os.getenv("AGENT_SERVER_READY_TIMEOUT"))
        ready_poll_interval = int(os.getenv("AGENT_SERVER_READY_POLL_INTERVAL"))
        
        model_provider = os.getenv("MODEL_PROVIDER")
        model_name = os.getenv("MODEL_NAME")
        api_base = os.getenv("API_BASE")
        api_key = os.getenv("API_KEY")

        class _Factory(IServiceInstanceFactory):
            async def new_service(
                    self, response_parser: IResponseParser
            ) -> IServiceHandler:
                k8s = K8sServiceHandler(
                    agent_image,
                    name_prefix="jiuwenclaw",
                    namespace=namespace,
                    container_name=container_name,
                    container_port=container_port,
                    port_name=port_name,
                    image_pull_policy=image_pull_policy,
                    env_vars={
                        "AGENT_SERVER_HOST": "0.0.0.0",
                        "AGENT_RUNTIME": "runtime",
                        "MODEL_PROVIDER": model_provider,
                        "MODEL_NAME": model_name,
                        "API_BASE": api_base,
                        "API_KEY": api_key,
                    } if api_key else {
                        "AGENT_SERVER_HOST": "0.0.0.0",
                        "AGENT_RUNTIME": "runtime",
                    },
                    kubeconfig=kubeconfig,
                    readiness_initial_delay=readiness_initial_delay,
                    readiness_period=readiness_period,
                    ready_timeout=ready_timeout,
                    ready_poll_interval=ready_poll_interval,
                    nfs_server=nfs_server,
                    nfs_path=nfs_path,
                    nfs_mount_path=nfs_mount_path
                )
                ch = WSServiceMessageChannel(
                    target_port=container_port,
                    invoke_path="",
                    ws_use_tls=False,
                )
                return ServiceHandler(
                    total_concurrency=service_concurrency,
                    message_channel=ch,
                    response_parser=response_parser,
                    deploy_controller=K8sDeployController(k8s),
                )

        dual_q: PriorityDualAsyncQueues[QueueItem] = PriorityDualAsyncQueues(1000, 100)
        factory = _Factory()
        sm = ServiceManager(
            service_factory=factory,
            dual_queue=dual_q,
            timer=Timer(),
            service_concurrency=service_concurrency,
            min_idle_services=min_idle_services,
            max_services=max_services,
            autoscale_interval=autoscale_interval,
            service_idle_ttl=service_ttl,
        )
        self._access: Any = Access(sm)
        self._connected = False

    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        return None

    async def connect(self, uri: str) -> None:
        """建立连接并初始化 Access。"""
        if self._connected:
            return

        agent_image = os.getenv("AGENT_SERVER_IMAGE")
        service_concurrency = int(os.getenv("AGENT_SERVER_SERVICE_CONCURRENCY"))
        min_idle_services = int(os.getenv("AGENT_SERVER_MIN_IDLE_SERVICES"))
        max_services = int(os.getenv("AGENT_SERVER_MAX_SERVICES"))
        target_port = int(os.getenv("AGENT_SERVER_PORT"))
        service_ttl = int(os.getenv("AGENT_SERVER_SERVICE_TTL"))
        message_timeout = int(os.getenv("AGENT_SERVER_MESSAGE_TIMEOUT"))
        autoscale_interval = float(os.getenv("AGENT_SERVER_AUTOSCALE_INTERVAL"))
        session_concurrency = int(os.getenv("AGENT_SERVER_SESSION_CONCURRENCY"))
        session_ttl = int(os.getenv("AGENT_SERVER_SESSION_TTL"))
        
        acc_cfg = AccessConfig(
            image=agent_image,
            service_concurrency=service_concurrency,
            min_idle_services=min_idle_services,
            max_services=max_services,
            target_port=target_port,
            invoke_path="",
            ws_use_tls=False,
            service_ttl=service_ttl,
            message_timeout=message_timeout,
            autoscale_interval=autoscale_interval,
        )

        session_cfg = SessionConfig(
            concurrency=session_concurrency,
            ttl=session_ttl,
        )

        await self._access.init(
            response_parser=E2aEnvelopResponseParser(),
            config=acc_cfg,
            session_config=session_cfg,
        )
        self._connected = True

    async def disconnect(self) -> None:
        """断开连接并清理资源。"""
        try:
            if self._access and hasattr(self._access, "shutdown"):
                await self._access.shutdown()
        except Exception as exc:
            logger.warning("[RuntimeManagementAgentClient] disconnect error: %s", exc)
        finally:
            self._connected = False

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("client not connected")

    async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
        """发送非流式请求。"""
        self._ensure_connected()
        request = e2a_to_agent_request(envelope)
        session_request = _SessionRequest(request, envelope)

        try:
            async for chunk in self._access.send_message(session_request):
                return chunk
        except Exception as exc:
            logger.exception("[RuntimeManagementAgentClient] send_request failed: %s", exc)
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata={},
            )

    async def send_request_stream(self, envelope: E2AEnvelope) -> AsyncIterator[AgentResponseChunk]:
        """发送流式请求。"""
        self._ensure_connected()
        request = e2a_to_agent_request(envelope)
        session_request = _SessionRequest(request, envelope)

        try:
            async for chunk in self._access.send_message(session_request):
                yield chunk
        except Exception as exc:
            logger.exception("[RuntimeManagementAgentClient] send_request_stream failed: %s", exc)
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"error": str(exc)},
                is_complete=True,
            )
