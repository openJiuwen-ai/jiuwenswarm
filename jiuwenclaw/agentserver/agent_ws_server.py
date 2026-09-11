# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""AgentWebSocketServer - Gateway 与 AgentServer 之间的 WebSocket 服务端."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any, ClassVar

from openjiuwen_runtime.foundation.security.link_auth import (
    LINK_TOKEN_HEADER,
    NonceCache,
    InMemoryPinStore,
    build_token,
    generate_keypair,
    verify_and_pin,
)

from jiuwenclaw.agentserver.session_history import enrich_history_messages_session_id
from jiuwenclaw.agentserver.gateway_push.wire import build_server_push_wire
from jiuwenclaw.agentserver.tools.acp_output_tools import get_acp_output_manager
from jiuwenclaw.agentserver.agent_manager import AgentManager
from jiuwenclaw.utils import (
    FileTransferStartParams,
    get_agent_sessions_dir,
    get_config_file,
    get_multi_tenant_user_workspace_dir,
)
from jiuwenclaw.e2a.agent_compat import e2a_to_agent_request
from jiuwenclaw.e2a.constants import (
    FILE_TRANSFER_START,
    FILE_TRANSFER_CHUNK,
    FILE_TRANSFER_COMPLETE,
    FILE_TRANSFER_EVENT_TYPES,
)
from jiuwenclaw.e2a.gateway_normalize import (
    E2A_FALLBACK_FAILED_KEY,
    E2A_INTERNAL_CONTEXT_KEY,
    E2A_LEGACY_AGENT_REQUEST_KEY,
)
from jiuwenclaw.e2a.models import E2AEnvelope
from jiuwenclaw.e2a.wire_codec import (
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
    encode_json_parse_error_wire,
)
from jiuwenclaw.request_ext import lift_from_metadata as _tp_lift, reset_ext as _tp_reset
from jiuwenclaw.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenclaw.schema.hook_event import AgentServerHookEvents
from jiuwenclaw.agentserver.extensions import get_rail_manager
from jiuwenclaw.agentserver.permissions.patterns import persist_cli_trusted_directory
from jiuwenclaw.schema.hooks_context import (
    AgentServerChatHookContext,
    AgentServerListeningHookContext,
    AgentWsServerStartHookContext,
)
from jiuwenclaw.agentserver.agent_manager import AgentManager, ACP_DEFAULT_CAPABILITIES
from jiuwenclaw.e2a.acp.protocol import build_acp_session_new_result
from jiuwenclaw.agentserver.permissions.config_rpc import get_permissions_config_req_methods
from jiuwenclaw.agentserver.tenant_agent_pool import TenantAgentPool
from jiuwenclaw.agentserver.file_transfer_manager import get_file_transfer_manager
from jiuwenclaw.security.ws_origin import (
    extract_handshake_request,
    forbidden_origin_response,
    get_header_value,
    is_allowed_browser_origin,
    unauthorized_response,
)


logger = logging.getLogger(__name__)

# 将 websockets.server 日志级别设为 WARNING，屏蔽 K8s 健康检查产生的大量日志
logging.getLogger("websockets.server").setLevel(logging.WARNING)

# 流式处理心跳间隔：当 Agent 处理时间超过此阈值时，发送心跳 chunk 保持 WebSocket 连接活跃
# 避免 ping_timeout 导致连接关闭。默认 10 秒，小于服务端 ping_timeout=20s。
_STREAM_HEARTBEAT_INTERVAL_SECONDS = 10.0

_SYSTEM_PROMPT_USER_HISTORY_PATTERN = re.compile(r"(\[[^\]\n]*用户\]\s*)(.*?)(\s*\[/对话历史\])", re.DOTALL)


def _mask_text_for_log(value: str) -> str:
    return "******" if len(value) <= 20 else f"{value[:5]}******{value[-5:]}"


def _mask_system_prompt_for_log(system_prompt: str) -> str:
    return _SYSTEM_PROMPT_USER_HISTORY_PATTERN.sub(
        lambda match: f"{match.group(1)}{_mask_text_for_log(match.group(2).strip())}{match.group(3)}",
        system_prompt,
    )


def _mask_query_for_log(data: dict[str, Any]) -> dict[str, Any]:
    params = data.get("params")
    if not isinstance(params, dict):
        return data

    masked_params = dict(params)
    query = params.get("query")
    if isinstance(query, str) and query:
        masked_params["query"] = _mask_text_for_log(query)

    system_prompt = params.get("system_prompt")
    if isinstance(system_prompt, str) and system_prompt:
        masked_params["system_prompt"] = _mask_system_prompt_for_log(system_prompt)

    if masked_params == params:
        return data
    return {**data, "params": masked_params}


def _payload_to_request(data: dict[str, Any]) -> AgentRequest:
    """将 Gateway 发送的 JSON 载荷解析为 AgentRequest."""
    from jiuwenclaw.schema.message import ReqMethod

    req_method = data.get("req_method")
    if req_method is not None and isinstance(req_method, str):
        req_method = ReqMethod(req_method)

    return AgentRequest(
        request_id=data["request_id"],
        channel_id=data.get("channel_id", ""),
        session_id=data.get("session_id"),
        req_method=req_method,
        params=data.get("params", {}),
        is_stream=data.get("is_stream", False),
        timestamp=data.get("timestamp", 0.0),
        metadata=data.get("metadata"),
    )


class AgentWebSocketServer:
    """Gateway 与 AgentServer 之间的 WebSocket 服务端（多例）.

    监听来自 Gateway (WebSocketAgentServerClient) 的连接，按协议约定处理请求：
    - 收到 JSON：E2AEnvelope（或过渡期 legacy + 兜底信封）
    - is_stream=False：``process_message`` → 一条 **E2AResponse** JSON（``jiuwenclaw.e2a.wire_codec``）
    - is_stream=True：逐条 **E2AResponse** JSON（chunk/complete/error）
    - 例外：首帧 ``connection.ack`` 仍为 ``type/event`` 事件帧

    支持 send_push：推送帧亦为 E2AResponse 线格式（由 chunk 编码）。
    """

    _instance: ClassVar[AgentWebSocketServer | None] = None

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18000,
        *,
        ping_interval: float | None = 30.0,
        ping_timeout: float | None = 300.0,
        max_size: int = 64 * 2**20,
    ) -> None:
        self._host = host
        self._port = port
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        # 单帧最大字节数（解压后）。websockets 默认 1 MiB，上行大请求（如带大
        # payload 的技能查询）超过该值时服务端会回 1009 断连；本链路为
        # Pod 内网可信链路，与 gateway 侧对齐放宽为 64 MiB。
        self._max_size = max_size
        self._server: Any = None
        # link-auth: AgentServer 身份密钥（Ed25519）。它是被 gateway 按需拉起的临时实例，
        # 故每进程启动时临时生成、不落库；connection.ack 反向签名用自身私钥。
        self._link_priv, self._link_pub = generate_keypair()
        # link-auth: 防重放 nonce 缓存（仅当 CLAW_LINK_AUTH_MODE != off 时生效）
        self._link_nonce_cache = NonceCache()
        # link-auth: 给连入的 gateway 做 TOFU 指纹固定（首次见到即固定其公钥指纹）
        self._gateway_pin_store = InMemoryPinStore()
        # 当前 Gateway 连接，用于 send_push 主动推送
        self._current_ws: Any = None
        self._current_send_lock: asyncio.Lock | None = None
        self._acp_client_capabilities_by_ws: dict[int, dict[str, Any]] = {}
        self._agent_manager = None # TenantAgentPool 实例
        get_acp_output_manager().set_send_push_callback(
            lambda msg: asyncio.create_task(self.send_push(msg))
        )

    @staticmethod
    def _ws_capabilities_key(ws: Any) -> int:
        return id(ws)

    def _set_ws_acp_client_capabilities(self, ws: Any, capabilities: dict[str, Any] | None) -> None:
        key = self._ws_capabilities_key(ws)
        if isinstance(capabilities, dict):
            self._acp_client_capabilities_by_ws[key] = dict(capabilities)
        else:
            self._acp_client_capabilities_by_ws.pop(key, None)

    def _get_ws_acp_client_capabilities(self, ws: Any) -> dict[str, Any]:
        key = self._ws_capabilities_key(ws)
        caps = self._acp_client_capabilities_by_ws.get(key)
        return dict(caps) if isinstance(caps, dict) else {}

    def _clear_ws_acp_client_capabilities(self, ws: Any) -> None:
        self._acp_client_capabilities_by_ws.pop(self._ws_capabilities_key(ws), None)

    @classmethod
    def get_instance(
        cls,
        *,
        host: str = "127.0.0.1",
        port: int = 18000,
        ping_interval: float | None = 30.0,
        ping_timeout: float | None = 300.0,
        max_size: int = 64 * 2**20,
    ) -> "AgentWebSocketServer":
        """返回多例实例。

        首次调用时创建实例，后续调用返回已存在的实例。
        """
        if cls._instance is not None:
            return cls._instance
        cls._instance = cls(
            host=host,
            port=port,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
            max_size=max_size,
        )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（仅用于测试）。"""
        cls._instance = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        """启动 WebSocket 服务端，开始监听连接。优先使用 legacy.server.serve 以与 Gateway 的 legacy client 握手兼容."""
        await self._trigger_before_ws_server_start_hook()
        # 初始化 TenantAgentPool
        if self._agent_manager is None:
            self._agent_manager = TenantAgentPool.get_instance()
            logger.info("[AgentWebSocketServer] 已初始化 TenantAgentPool")

        if self._server is not None:
            logger.warning("[AgentWebSocketServer] 服务端已在运行")
            return

        # 启动文件传输管理器的清理任务
        ft_manager = get_file_transfer_manager()
        if ft_manager.enabled:
            await ft_manager.start_cleanup_task()

        try:
            from websockets.legacy.server import serve as legacy_serve
            self._server = await legacy_serve(
                self._connection_handler,
                self._host,
                self._port,
                process_request=self._process_request,
                ping_interval=self._ping_interval,
                ping_timeout=self._ping_timeout,
                max_size=self._max_size,
            )
        except ImportError:
            import websockets
            self._server = await websockets.serve(
                self._connection_handler,
                self._host,
                self._port,
                process_request=self._process_request,
                ping_interval=self._ping_interval,
                ping_timeout=self._ping_timeout,
                max_size=self._max_size,
            )
        logger.info(
            "[AgentWebSocketServer] 已启动: ws://%s:%s", self._host, self._port
        )
        await self._trigger_agent_server_listening_hook()

    async def _process_request(self, *args: Any) -> Any:
        """在握手阶段执行 link-auth + Origin 校验，兼容 legacy/new websockets APIs。"""
        path, request_headers = extract_handshake_request(args)

        # link-auth: 校验链路令牌（Ed25519 + TOFU 指纹固定）。独立于 AGENT_RUNTIME 对 Origin 的旁路——
        # 沙箱/托管运行时下 Origin 全放行，此处仍按 CLAW_LINK_AUTH_MODE 强制。
        # mode=off 时 verify_and_pin 恒放行，行为与未引入时一致。
        link_token = get_header_value(request_headers, LINK_TOKEN_HEADER)
        link_result = verify_and_pin(
            self._gateway_pin_store,
            link_token,
            expect_type="gateway",
            nonce_cache=self._link_nonce_cache,
        )
        if not link_result.allowed:
            logger.warning(
                "[AgentWebSocketServer] 握手拒绝 path=%s reason=link_auth:%s",
                path,
                link_result.reason,
            )
            return unauthorized_response(args)

        origin = get_header_value(request_headers, "Origin")
        allowed = is_allowed_browser_origin(origin)

        # 避免k8s派来的探针产生大量日志
        is_probe = bool(path and "K8s-Readiness-Probe" in path)
        if not is_probe:
            logger.info(
                "[AgentWebSocketServer] 握手检查 path=%s origin=%s allowed=%s",
                path,
                origin,
                allowed,
            )
        if allowed:
            return None

        logger.warning(
            "[AgentWebSocketServer] 握手拒绝 path=%s origin=%s reason=origin_not_allowed",
            path,
            origin,
        )
        return forbidden_origin_response(args)

    async def stop(self) -> None:
        """停止 WebSocket 服务端."""
        # 停止文件传输管理器的清理任务
        ft_manager = get_file_transfer_manager()
        if ft_manager.enabled:
            await ft_manager.stop_cleanup_task()

        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        logger.info("[AgentWebSocketServer] 已停止")

    # ---------- 连接处理 ----------

    async def _connection_handler(self, ws: Any) -> None:
        """处理单个 Gateway WebSocket 连接，同一连接可并发处理多个请求."""
        import websockets

        # 如果是探针，走独立静音通道，避免干扰推送, 直接给它发个响应，证明握手和状态机是完全健康的
        path = getattr(ws, "path", "")
        is_probe = bool(path and "K8s-Readiness-Probe" in path)
        if is_probe:
            try:
                # 发送 connection.ack 给探针脚本
                ack_frame = {
                    "type": "event",
                    "event": "connection.ack",
                    "payload": {"status": "ready"},
                }
                await ws.send(json.dumps(ack_frame, ensure_ascii=False))
            except Exception:
                pass
            return

        # 和客户端连接成功后, 触发agentserver启动成功的回调事件
        await self._trigger_agent_server_started_hook()

        remote = ws.remote_address
        logger.info("[AgentWebSocketServer] 新连接: %s", remote)

        send_lock = asyncio.Lock()
        self._current_ws = ws
        self._current_send_lock = send_lock

        # 发送 connection.ack 事件，通知 Gateway 服务端已就绪
        try:
            # link-auth 双向：AgentServer 在 connection.ack 里附一枚自己签的令牌，
            # 供 Gateway 反向核验"对面确是合法 AgentServer"。off 时不签（payload 不含 link_token）。
            ack_payload: dict[str, Any] = {"status": "ready"}
            _ack_token = build_token(
                service_id=os.getenv("JIUWENCLAW_SERVICE_ID", "agentserver-1"),
                service_type="agent_server",
                private_b64=self._link_priv,
                public_b64=self._link_pub,
            )
            if _ack_token:
                ack_payload["link_token"] = _ack_token
            ack_frame = {
                "type": "event",
                "event": "connection.ack",
                "payload": ack_payload,
            }
            await ws.send(json.dumps(ack_frame, ensure_ascii=False))
            logger.info("[AgentWebSocketServer] 已发送 connection.ack: %s", remote)
        except Exception as e:
            logger.warning("[AgentWebSocketServer] 发送 connection.ack 失败: %s", e)

        tasks: set[asyncio.Task] = set()

        try:
            async for raw in ws:
                async def _run(raw=raw):
                    try:
                        await self._handle_message(ws, raw, send_lock)
                    except websockets.exceptions.ConnectionClosed as e:
                        logger.info("[AgentWebSocketServer] 连接已关闭: %s", e)
                    except Exception:
                        logger.exception("[AgentWebSocketServer] 处理消息失败")

                task = asyncio.create_task(_run())
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except websockets.exceptions.ConnectionClosed:
            logger.info("[AgentWebSocketServer] 连接关闭: %s", remote)
        except Exception as e:
            logger.exception("[AgentWebSocketServer] 连接处理异常 (%s): %s", remote, e)
        finally:
            self._current_ws = None
            self._current_send_lock = None
            self._clear_ws_acp_client_capabilities(ws)
            # Gateway 进程退出/端口关闭时，必须先取消各 session 内流式生产者（SessionManager）
            # 并中止 DeepAgent 内层循环；否则仅等待 _handle_message 任务结束会一直阻塞到任务自然完成。
            try:
                await self._agent_manager.cancel_all_inflight_work(
                    reason=f"[gateway ws closed {remote}] ",
                )
            except Exception:
                logger.exception("[AgentWebSocketServer] cancel_all_inflight_work failed")
            try:
                from jiuwenclaw.agentserver.team import get_team_manager

                await get_team_manager().cancel_all_stream_tasks(
                    reason=f"[gateway ws closed {remote}] ",
                )
            except Exception:
                logger.exception("[AgentWebSocketServer] team stream cancel failed")
            if tasks:
                for t in list(tasks):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_message(self, ws: Any, raw: str | bytes, send_lock: asyncio.Lock) -> None:
        """解析一条 JSON 请求并分发到 IAgentServer 处理."""
        try:
            data = json.loads(raw)
            logger.info(
                "[AgentWebSocketServer] inbound raw payload: %s",
                _mask_query_for_log(data),
            )
        except json.JSONDecodeError as e:
            wire = encode_json_parse_error_wire(
                request_id="",
                channel_id="",
                message=f"JSON 解析失败: {e}",
            )
            async with send_lock:
                await ws.send(json.dumps(wire, ensure_ascii=False))
            return

        try:
            env = E2AEnvelope.from_dict(data)
        except Exception as parse_err:
            logger.warning(
                "[AgentWebSocketServer] E2A from_dict 失败，按旧载荷解析: %s",
                parse_err,
            )
            request = _payload_to_request(data)
        else:
            jw = (env.channel_context or {}).get(E2A_INTERNAL_CONTEXT_KEY)
            if isinstance(jw, dict) and jw.get(E2A_FALLBACK_FAILED_KEY):
                legacy = jw.get(E2A_LEGACY_AGENT_REQUEST_KEY)
                logger.warning(
                    "[E2A][fallback] using legacy_agent_request request_id=%s",
                    env.request_id,
                )
                if not isinstance(legacy, dict):
                    raise ValueError("legacy_agent_request missing or not a dict")
                request = _payload_to_request(legacy)
            # 文件传输请求：method 不在 ReqMethod 枚举中，需要特殊处理
            elif env.method in (FILE_TRANSFER_START, FILE_TRANSFER_CHUNK, FILE_TRANSFER_COMPLETE):
                logger.info(
                    "[E2A][in] request_id=%s channel=%s method=%s is_stream=%s",
                    env.request_id,
                    env.channel,
                    env.method,
                    env.is_stream,
                )
                # 直接构造 AgentRequest，不走 e2a_to_agent_request
                request = AgentRequest(
                    request_id=env.request_id or "",
                    channel_id=env.channel or "",
                    session_id=env.session_id,
                    req_method=None,  # 文件传输没有对应的 ReqMethod
                    params=dict(env.params or {}),
                    is_stream=False,
                    timestamp=0.0,
                    metadata=None,
                )
            else:
                logger.info(
                    "[E2A][in] request_id=%s channel=%s method=%s is_stream=%s",
                    env.request_id,
                    env.channel,
                    env.method,
                    env.is_stream,
                )
                request = e2a_to_agent_request(env)

        logger.info(
            "[AgentWebSocketServer] 收到请求: request_id=%s channel_id=%s is_stream=%s",
            request.request_id,
            request.channel_id,
            request.is_stream,
        )

        _ext_token = _tp_lift(request.metadata)
        try:
            from jiuwenclaw.schema.message import ReqMethod

            if request.channel_id == "acp" and request.req_method != ReqMethod.INITIALIZE:
                metadata = dict(request.metadata or {})
                ws_caps = self._get_ws_acp_client_capabilities(ws)
                metadata.setdefault(
                    "acp_client_capabilities",
                    ws_caps or self._agent_manager.get_client_capabilities("acp"),
                )
                request.metadata = metadata

            await self._trigger_before_chat_request_hook(request)

            if request.req_method == ReqMethod.SESSION_LIST:
                await self._handle_session_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.SESSION_RENAME:
                await self._handle_session_rename(ws, request, send_lock)
                return
            if request.req_method in get_permissions_config_req_methods():
                await self._handle_permissions_config(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.HISTORY_GET:
                if request.is_stream:
                    await self._handle_history_get_stream(ws, request, send_lock)
                else:
                    await self._handle_history_get(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_ADD_DIR:
                await self._handle_command_add_dir(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_CHROME:
                await self._handle_command_chrome(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_COMPACT:
                await self._handle_command_compact(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_DIFF:
                await self._handle_command_diff(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_LS:
                await self._handle_command_ls(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_VIEW:
                await self._handle_command_view(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_MODEL:
                await self._handle_command_model(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_RESUME:
                await self._handle_command_resume(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.COMMAND_SESSION:
                await self._handle_command_session(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.BROWSER_START:
                await self._handle_browser_start(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.BROWSER_RUNTIME_RESTART:
                await self._handle_browser_runtime_restart(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.CONFIG_CACHE_CLEAR:
                await self._handle_config_cache_clear(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.AGENT_RELOAD_CONFIG:
                await self._handle_agent_reload_config(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.EXTENSIONS_LIST:
                await self._handle_extensions_list(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.EXTENSIONS_IMPORT:
                await self._handle_extensions_import(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.EXTENSIONS_DELETE:
                await self._handle_extensions_delete(ws, request, send_lock)
                return
            if request.req_method == ReqMethod.EXTENSIONS_TOGGLE:
                await self._handle_extensions_toggle(ws, request, send_lock)
                return
            # 文件传输处理
            event_type = request.params.get("event_type") if isinstance(request.params, dict) else None
            if event_type in FILE_TRANSFER_EVENT_TYPES:
                await self._handle_file_transfer(ws, request, send_lock)
                return
            if request.is_stream:
                await self._handle_stream(ws, request, send_lock)
            else:
                await self._handle_unary(ws, request, send_lock)
        except Exception as e:
            import websockets

            if isinstance(e, websockets.exceptions.ConnectionClosed):
                logger.info(
                    "[AgentWebSocketServer] 连接已关闭: request_id=%s: %s",
                    request.request_id,
                    e,
                )
                return
            logger.exception(
                "[AgentWebSocketServer] 处理请求失败: request_id=%s: %s",
                request.request_id,
                e,
            )
            try:
                async with send_lock:
                    if request.is_stream:
                        err_chunk = AgentResponseChunk(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            payload={
                                "event_type": "chat.error",
                                "error": str(e),
                            },
                            is_complete=True,
                        )
                        wire = encode_agent_chunk_for_wire(
                            err_chunk,
                            response_id=request.request_id,
                            sequence=0,
                        )
                    else:
                        error_resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"error": str(e)},
                        )
                        wire = encode_agent_response_for_wire(
                            error_resp, response_id=request.request_id
                        )
                    await ws.send(json.dumps(wire, ensure_ascii=False))
            except Exception as send_err:
                logger.warning(
                    "[AgentWebSocketServer] 错误回写失败: request_id=%s: %s",
                    request.request_id,
                    send_err,
                )
        finally:
            _tp_reset(_ext_token)

    @staticmethod
    async def _trigger_before_ws_server_start_hook() -> None:
        """在首次启动之前触发扩展；未初始化 ExtensionRegistry 时跳过。"""
        from jiuwenclaw.extensions.registry import ExtensionRegistry
        from jiuwenclaw.utils import get_agent_skills_dir

        ctx = AgentWsServerStartHookContext(skills_dir=str(get_agent_skills_dir()))
        await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.BEFORE_WS_SERVER_START, ctx)

    async def _trigger_agent_server_listening_hook(self) -> None:
        """listen 成功后触发一次（供 Claw Manager DMQ 等扩展使用，非每连接一次）。"""
        from jiuwenclaw.extensions.registry import ExtensionRegistry
        from jiuwenclaw.utils import get_agent_skills_dir

        ctx = AgentServerListeningHookContext(
            skills_dir=str(get_agent_skills_dir()),
            host=str(self._host),
            port=int(self._port),
        )
        await ExtensionRegistry.get_instance().trigger(
            AgentServerHookEvents.AGENT_SERVER_LISTENING, ctx
        )

    @staticmethod
    async def _trigger_agent_server_started_hook() -> None:
        """在agentserver启动成功触发扩展；未初始化 ExtensionRegistry 时跳过。"""
        from jiuwenclaw.extensions.registry import ExtensionRegistry
        from jiuwenclaw.utils import get_agent_skills_dir

        ctx = AgentWsServerStartHookContext(skills_dir=str(get_agent_skills_dir()))
        await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.AGENT_SERVER_STARTED, ctx)


    @staticmethod
    def _should_trigger_before_chat_request_hook(request: AgentRequest) -> bool:
        from jiuwenclaw.schema.message import ReqMethod

        return request.req_method in (
            ReqMethod.CHAT_SEND,
            ReqMethod.CHAT_RESUME,
            ReqMethod.CHAT_ANSWER,
        )

    async def _trigger_before_chat_request_hook(self, request: AgentRequest) -> None:
        if not self._should_trigger_before_chat_request_hook(request):
            return
        from jiuwenclaw.extensions.registry import ExtensionRegistry

        params = request.params if isinstance(request.params, dict) else {}
        if not isinstance(request.params, dict):
            request.params = params

        ctx = AgentServerChatHookContext(
            request_id=request.request_id,
            channel_id=request.channel_id,
            session_id=request.session_id,
            req_method=request.req_method.value if request.req_method is not None else None,
            params=params,
        )

        t_hook0 = time.monotonic()
        await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.BEFORE_CHAT_REQUEST, ctx)
        logger.info(
            "[AgentPerf] before_chat hook: request_id=%s elapsed_ms=%.1f",
            request.request_id, (time.monotonic() - t_hook0) * 1000,
        )

    async def _handle_unary(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """非流式处理：调用 process_message，返回一条 E2AResponse 线 JSON。"""
        from jiuwenclaw.schema.message import ReqMethod

        channel_id = request.channel_id or "default"

        if request.req_method == ReqMethod.INITIALIZE:
            await self._handle_initialize(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.SESSION_CREATE:
            await self._handle_session_create(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.SESSION_DELETE:
            await self._handle_session_delete(ws, request, send_lock)
            return

        if request.req_method == ReqMethod.ACP_TOOL_RESPONSE:
            await self._handle_acp_tool_response(ws, request, send_lock)
            return

        resp = await self._agent_manager.process_message(request)

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))
        logger.info(
            "[AgentWebSocketServer] 非流式响应已发送: request_id=%s",
            request.request_id,
        )

    async def _handle_stream(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """流式处理：调用 process_message_stream，逐条发送 E2AResponse 线 JSON。"""
        channel_id = request.channel_id or "default"

        chunk_count = 0
        # 心跳控制：当有真实 chunk 发送时重置，空闲时发送心跳
        heartbeat_event = asyncio.Event()
        heartbeat_task: asyncio.Task | None = None

        async def _heartbeat_loop() -> None:
            """后台心跳任务：在空闲期间定期发送 keepalive chunk."""
            try:
                while True:
                    # 等待心跳间隔，如果期间有真实 chunk 发送则 heartbeat_event 被设置，重置等待
                    try:
                        await asyncio.wait_for(
                            heartbeat_event.wait(),
                            timeout=_STREAM_HEARTBEAT_INTERVAL_SECONDS,
                        )
                        # 有真实 chunk 发送，重置 event 继续等待
                        heartbeat_event.clear()
                    except asyncio.TimeoutError:
                        # 超时：空闲超过心跳间隔，发送 keepalive chunk
                        heartbeat_chunk = AgentResponseChunk(
                            request_id=request.request_id,
                            channel_id=channel_id,
                            payload={"event_type": "keepalive"},
                            is_complete=False,
                        )
                        wire = encode_agent_chunk_for_wire(
                            heartbeat_chunk,
                            response_id=request.request_id,
                            sequence=-1,  # 心跳使用特殊序列号 -1
                        )
                        async with send_lock:
                            await ws.send(json.dumps(wire, ensure_ascii=False))
                        logger.debug(
                            "[AgentWebSocketServer] keepalive chunk 发送: request_id=%s",
                            request.request_id,
                        )
            except asyncio.CancelledError:
                pass

        # 启动心跳任务
        heartbeat_task = asyncio.create_task(_heartbeat_loop())

        try:
            async for chunk in self._agent_manager.process_message_stream(request):
                chunk_count += 1
                # 通知心跳任务有真实 chunk 发送，重置心跳计时
                heartbeat_event.set()
                wire = encode_agent_chunk_for_wire(
                    chunk,
                    response_id=request.request_id,
                    sequence=chunk_count - 1,
                )
                async with send_lock:
                    await ws.send(json.dumps(wire, ensure_ascii=False))
                # 清除 event，让心跳任务重新开始计时
                heartbeat_event.clear()
        finally:
            # 停止心跳任务
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

        logger.info(
            "[AgentWebSocketServer] 流式响应已发送: request_id=%s 共 %s 个 chunk",
            request.request_id,
            chunk_count,
        )

    async def _handle_session_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 session.list 请求：扫描 sessions 目录，返回历史会话基础信息列表.

        使用 TenantAgentPool.extract_ids 获取租户 ID，默认为 ('default', 'default')。
        """
        from jiuwenclaw.agentserver.session_metadata import get_session_metadata

        # extract_ids 现在总是返回有效值（默认或指定的 tenant ID）
        _, _, workspace_key = TenantAgentPool.extract_ids(request)
        sessions_dir = get_multi_tenant_user_workspace_dir(
            workspace_key=workspace_key
        ) / "agent" / "sessions"
        sessions = []

        try:
            if sessions_dir.exists():
                for entry in sorted(sessions_dir.iterdir(), key=lambda e: e.stat().st_mtime, reverse=True):
                    if not entry.is_dir():
                        continue
                    meta = get_session_metadata(entry.name)
                    if not meta:
                        meta = {
                            "session_id": entry.name,
                            "channel_id": "",
                            "title": "",
                            "message_count": 0,
                            "last_message_at": entry.stat().st_mtime,
                        }
                    sessions.append(meta)
        except Exception as exc:
            logger.warning("[AgentWebSocketServer] 扫描 sessions 目录失败: %s", exc)

        resp = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"sessions": sessions},
            metadata=request.metadata,
        )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_session_rename(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 session.rename：与 CLI Gateway 本地回退共用 apply_session_rename。"""
        from jiuwenclaw.agentserver.session_rename import apply_session_rename

        sid = request.session_id or ""
        ch = (request.channel_id or "").strip() or "tui"
        ok, payload, err, code = apply_session_rename(
            request.params,
            sid,
            init_channel_id=ch,
        )
        if ok:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=payload or {},
                metadata=request.metadata,
            )
        else:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": err or "session.rename failed", "code": code or ""},
                metadata=request.metadata,
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_permissions_config(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """处理 permissions.* E2A 请求（与 Web ``register_method`` 同名 method）。"""
        from jiuwenclaw.agentserver.permissions.config_rpc import dispatch_permissions_config_request

        resp = dispatch_permissions_config_request(request)
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_history_get(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id")
        page_idx = params.get("page_idx")
        if os.getenv("AGENT_RUNTIME", "").strip():
            _, _, workspace_key = TenantAgentPool.extract_ids(request)
            sessions_dir = get_multi_tenant_user_workspace_dir(
                workspace_key=workspace_key
            ) / "agent" / "sessions"
        else:
            sessions_dir = get_agent_sessions_dir()
        data = self.get_conversation_history(sessions_dir, session_id=session_id, page_idx=page_idx)
        if data is None:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": "invalid page_idx or session history not found"},
            )
        else:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=data,
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_history_get_stream(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        params = request.params if isinstance(request.params, dict) else {}
        session_id = params.get("session_id")
        page_idx = params.get("page_idx")
        if os.getenv("AGENT_RUNTIME", "").strip():
            _, _, workspace_key = TenantAgentPool.extract_ids(request)
            sessions_dir = get_multi_tenant_user_workspace_dir(
                workspace_key=workspace_key
            ) / "agent" / "sessions"
        else:
            sessions_dir = get_agent_sessions_dir()
        data = self.get_conversation_history(sessions_dir, session_id=session_id, page_idx=page_idx)
        if data is None:
            err_chunk = AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={
                    "event_type": "chat.error",
                    "error": "invalid page_idx or session history not found",
                },
                is_complete=True,
            )
            wire = encode_agent_chunk_for_wire(
                err_chunk,
                response_id=request.request_id,
                sequence=0,
            )
            async with send_lock:
                await ws.send(json.dumps(wire, ensure_ascii=False))
            return

        messages = data.get("messages", [])
        total_pages = data.get("total_pages")
        page = data.get("page_idx")
        if isinstance(messages, list):
            for seq, item in enumerate(messages):
                chunk = AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={
                        "event_type": "history.message",
                        "message": item,
                        "total_pages": total_pages,
                        "page_idx": page,
                    },
                    is_complete=False,
                )
                wire = encode_agent_chunk_for_wire(
                    chunk,
                    response_id=request.request_id,
                    sequence=seq,
                )
                async with send_lock:
                    await ws.send(json.dumps(wire, ensure_ascii=False))

        done_chunk = AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={
                "event_type": "history.message",
                "status": "done",
                "total_pages": total_pages,
                "page_idx": page,
            },
            is_complete=True,
        )
        done_seq = len(messages) if isinstance(messages, list) else 0
        wire_done = encode_agent_chunk_for_wire(
            done_chunk,
            response_id=request.request_id,
            sequence=done_seq,
        )
        async with send_lock:
            await ws.send(json.dumps(wire_done, ensure_ascii=False))

    async def _handle_command_add_dir(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            params = request.params or {}
            directory_path = params.get("path")
            remember = params.get("remember", False)
            persist: dict[str, Any]
            if directory_path is None or (
                isinstance(directory_path, str) and not directory_path.strip()
            ):
                persist = {"ok": False, "error": "path is required"}
            else:
                persist = persist_cli_trusted_directory(str(directory_path))
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=bool(persist.get("ok", False)),
                payload={
                    "path": directory_path,
                    "remember": remember,
                    "persist": persist,
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.add_dir failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_command_chrome(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.chrome failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_command_ls(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from datetime import datetime
        from jiuwenclaw.agentserver.session_metadata import get_resolved_project_dir
        from jiuwenclaw.utils import get_agent_sessions_dir
        logger.info("[AgentWebSocketServer] command.ls %s", request.params)
        try:
            params = request.params or {}
            relative_path = str(params.get("path", ".")).strip()

            session_id = request.session_id or "default"
            workspace_dir = Path(get_resolved_project_dir(session_id, get_agent_sessions_dir()))
            logger.info("[AgentWebSocketServer] command.ls workspace_dir: %s", workspace_dir)
            target_path = (workspace_dir / relative_path).resolve()
            logger.info("[AgentWebSocketServer] command.ls target_path: %s", target_path)

            try:
                target_path.relative_to(workspace_dir.resolve())
            except ValueError:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "Path outside workspace"},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                async with send_lock:
                    await ws.send(json.dumps(wire, ensure_ascii=False))
                return

            entries = []
            if target_path.is_dir():
                for item in sorted(target_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                    try:
                        stat = item.stat()
                        entries.append({
                            "name": item.name,
                            "is_dir": item.is_dir(),
                            "size": stat.st_size if item.is_file() else None,
                            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        })
                    except OSError as e:
                        entries.append({
                            "name": item.name,
                            "is_dir": item.is_dir(),
                            "error": str(e),
                        })
            logger.info("[AgentWebSocketServer] command.ls entries: %s", entries)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "path": str(target_path),
                    "relative_path": relative_path,
                    "entries": entries,
                },
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.ls failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_command_view(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenclaw.agentserver.session_metadata import get_resolved_project_dir
        from jiuwenclaw.utils import get_agent_sessions_dir
        logger.info("[AgentWebSocketServer] command.view %s", request.params)
        try:
            params = request.params or {}
            relative_path = str(params.get("path", "")).strip()
            from_line = int(params.get("from_line", 1))
            lines = params.get("lines")

            if not relative_path:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "Missing path parameter"},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                async with send_lock:
                    await ws.send(json.dumps(wire, ensure_ascii=False))
                return

            session_id = request.session_id or "default"
            workspace_dir = Path(get_resolved_project_dir(session_id, get_agent_sessions_dir()))
            target_path = (workspace_dir / relative_path).resolve()

            try:
                target_path.relative_to(workspace_dir.resolve())
            except ValueError:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "Path outside workspace"},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                async with send_lock:
                    await ws.send(json.dumps(wire, ensure_ascii=False))
                return

            if not target_path.is_file():
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": f"Not a file: {relative_path}"},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                async with send_lock:
                    await ws.send(json.dumps(wire, ensure_ascii=False))
                return

            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    all_lines = f.readlines()
            except UnicodeDecodeError:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "Binary file, cannot display"},
                )
                wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
                async with send_lock:
                    await ws.send(json.dumps(wire, ensure_ascii=False))
                return

            start_idx = max(0, from_line - 1)
            if lines is not None and lines > 0:
                end_idx = min(len(all_lines), start_idx + lines)
            else:
                end_idx = len(all_lines)

            selected_lines = all_lines[start_idx:end_idx]

            numbered = []
            for i, line in enumerate(selected_lines, start=start_idx + 1):
                numbered.append(f"{i:4d} | {line.rstrip()}")
            numbered_content = '\n'.join(numbered)
            summary = (
                f"\n\n---\n"
                f"📄 文件: `{relative_path}`\n"
                f"📊 总行数: {len(all_lines)}, 显示: {len(selected_lines)} 行 "
                f"(第 {start_idx + 1}-{end_idx} 行)"
            )
            
            content = f"```\n{numbered_content}\n```{summary}"
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "path": str(target_path),
                    "relative_path": relative_path,
                    "content": content,
                    "from_line": from_line,
                    "total_lines": len(all_lines),
                },
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.view failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_command_compact(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            params = request.params or {}
            custom_instructions = params.get("instructions")
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"instructions": custom_instructions},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.compact failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_command_diff(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        from jiuwenclaw.agentserver.diff_service import get_diff_service

        try:
            session_id = request.session_id or "default"
            diff_service = get_diff_service()
            turns = diff_service.get_turn_diffs(session_id)

            logger.info(
                "[AgentWebSocketServer] command.diff response: session_id=%s turns=%s",
                session_id,
                turns,
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "type": "list",
                    "turns": turns,
                },
            )
        except Exception as e:
            logger.exception("[AgentWebSocketServer] command.diff failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_command_model(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            params = request.params or {}
            action = params.get("action")

            if action == "add_model":
                target = str(params.get("target", "")).strip()
                logger.info("[command.model] add_model: target=%s", target)
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"type": "model_added", "name": target},
                )

            elif action == "switch_model":
                target = str(params.get("model", "")).strip()
                env_updates = params.get("env_updates", {})
                logger.info(
                    "[command.model] switch_model: target=%s, env_updates=%s",
                    target,
                    {k: (v if k != "API_KEY" else "***") for k, v in env_updates.items()},
                )

                if not env_updates:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={"error": "No env_updates provided"},
                    )
                else:
                    for k, v in env_updates.items():
                        os.environ[k] = v
                    logger.info("[command.model] os.environ 已更新, MODEL_NAME=%s", os.getenv("MODEL_NAME", "unknown"))

                    try:
                        from jiuwenclaw.agentserver.memory.config import (clear_config_cache,
                                                                          clear_embed_config_db_cache)
                        clear_config_cache()
                        clear_embed_config_db_cache()
                        logger.info("[command.model] config cache 已清除")
                    except Exception as e:
                        logger.debug("[command.model] clear_config_cache skipped: %s", e)

                    try:
                        await self._agent_manager.reload_agents_config(None, env_updates)
                        logger.info("[command.model] agent config 已重载")
                    except Exception as e:
                        logger.debug("[command.model] reload_agents_config skipped: %s", e)

                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=True,
                        payload={
                            "current": os.getenv("MODEL_NAME", "unknown"),
                            "requested": target,
                            "type": "switched",
                            "applied": True,
                        },
                    )
                    logger.info("[command.model] 切换完成: current=%s", os.getenv("MODEL_NAME", "unknown"))

            else:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload={"current": os.getenv("MODEL_NAME", "unknown"), "available": ["default-model"]},
                )

        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.model failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_command_resume(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            params = request.params or {}
            query = params.get("query")
            session_id = query if isinstance(query, str) and query.strip() else "sess_mock_resume"
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "session_id": session_id,
                    "query": query if isinstance(query, str) else "",
                    "resumed": True,
                    "preview": "Mock resumed conversation",
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.resume failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_command_session(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            session_id = request.session_id or "sess_mock"
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "session_id": session_id,
                    "remote_url": f"https://example.com/session/{session_id}",
                    "qr_text": f"session:{session_id}",
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] command.session failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_browser_start(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """启动浏览器并返回执行结果（returncode）。"""
        try:
            from jiuwenclaw.agentserver.tools.browser_start_client import start_browser

            config_path = str(get_config_file())
            returncode = start_browser(dry_run=False, config_file=config_path)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"returncode": returncode},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] browser.start failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_browser_runtime_restart(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            from openjiuwen.harness.tools.browser_move import restart_local_browser_runtime_server

            result = restart_local_browser_runtime_server()
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"result": result},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] browser.runtime_restart failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_config_cache_clear(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            from jiuwenclaw.agentserver.memory.config import clear_config_cache, clear_embed_config_db_cache

            clear_config_cache()
            clear_embed_config_db_cache()
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"cleared": True},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] config.cache_clear failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_agent_reload_config(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        try:
            params = request.params or {}
            config_payload = params.get("config")
            env_overrides = params.get("env")

            await self._agent_manager.reload_agents_config(
                config=config_payload,
                env=env_overrides,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"reloaded": True},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] agent.reload_config failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_extensions_list(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """获取所有 Rail 扩展列表."""
        try:
            manager = get_rail_manager()
            extensions = manager.list_extensions()

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"extensions": extensions},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] extensions.list failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_extensions_import(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """导入新的 Rail 扩展（文件夹结构）."""
        try:
            params = request.params or {}
            folder_path = params.get("folder_path")

            if not folder_path:
                raise ValueError("缺少 folder_path 参数")

            source_path = Path(folder_path)
            if not source_path.exists() or not source_path.is_dir():
                raise ValueError(f"文件夹不存在或不是目录: {folder_path}")

            manager = get_rail_manager()
            extension = manager.import_extension(folder_path)

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=extension,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] extensions.import failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_extensions_delete(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """删除 Rail 扩展."""
        try:
            params = request.params or {}
            name = params.get("name")

            if not name:
                raise ValueError("缺少 name 参数")

            manager = get_rail_manager()
            manager.delete_extension(name)

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"deleted": True, "name": name},
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] extensions.delete failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_extensions_toggle(self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock) -> None:
        """切换 Rail 扩展的启用状态，并触发热更新."""
        try:
            params = request.params or {}
            name = params.get("name")
            enabled = params.get("enabled", False)

            if name is None:
                raise ValueError("缺少 name 参数")
            if enabled is None:
                raise ValueError("缺少 enabled 参数")

            manager = get_rail_manager()

            # 1. 更新配置文件中的启用状态
            extension = manager.toggle_extension(name, enabled)

            # 2. 触发热更新：根据 enabled 状态注册或注销 rail
            await manager.hot_reload_rail(name, enabled)

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=extension,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentWebSocketServer] extensions.toggle failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def send_push(self, msg) -> None:
        """AgentServer 主动向 Gateway 推送消息。

        payload 格式与 AgentResponse.payload 一致，
        可含 event_type 等字段供 Gateway 转为 Message 派发到 Channel。
        """
        if self._current_ws is None or self._current_send_lock is None:
            logger.warning(
                "[AgentWebSocketServer] send_push 失败: 无活跃 Gateway 连接"
            )
            return

        try:
            wire = build_server_push_wire(msg)
            async with self._current_send_lock:
                await self._current_ws.send(json.dumps(wire, ensure_ascii=False))
            response_kind = str(msg.get("response_kind") or "").strip()
            if response_kind:
                logger.info(
                    "[AgentWebSocketServer] send_push response_kind wire sent: channel_id=%s kind=%s",
                    msg.get("channel_id", ""),
                    response_kind,
                )
            else:
                logger.info(
                    "[AgentWebSocketServer] send_push 已发送(E2A wire): channel_id=%s",
                    msg.get("channel_id", ""),
                )
        except Exception as e:
            logger.warning("[AgentWebSocketServer] send_push 失败: %s", e)

    def get_agent(self):
        """获取 default agent 实例（向后兼容）."""
        return self._agent_manager.get_agent_nowait()

    def get_agent_manager(self) -> TenantAgentPool:
        """获取 AgentManager 实例."""
        return self._agent_manager

    # =========================================================================
    # 文件传输处理
    # =========================================================================

    async def _handle_file_transfer(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理文件传输请求（Gateway -> AgentServer）.

        支持：
        - file.transfer.start: 开始传输
        - file.transfer.chunk: 传输分片
        - file.transfer.complete: 完成传输
        """
        params = request.params if isinstance(request.params, dict) else {}
        event_type = params.get("event_type", "")
        transfer_id = params.get("transfer_id", "")

        ft_manager = get_file_transfer_manager()

        # 检查是否启用分布式模式
        if not ft_manager.enabled:
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": "file transfer not enabled (distributed mode required)",
                    "event_type": event_type,
                },
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await ws.send(json.dumps(wire, ensure_ascii=False))
            return

        try:
            if event_type == FILE_TRANSFER_START:
                ft_params = FileTransferStartParams(
                    transfer_id=transfer_id,
                    filename=params.get("filename", "unnamed"),
                    file_size=params.get("file_size", 0),
                    sha256=params.get("sha256", ""),
                    total_chunks=params.get("total_chunks", 0),
                    chunk_size=params.get("chunk_size", 65536),
                    mime_type=params.get("mime_type", ""),
                    session_id=request.session_id or "",
                )
                result = await ft_manager.handle_transfer_start(ft_params)
            elif event_type == FILE_TRANSFER_CHUNK:
                result = await ft_manager.handle_transfer_chunk(
                    transfer_id=transfer_id,
                    chunk_index=params.get("chunk_index", 0),
                    base64_data=params.get("base64_data", ""),
                )
            elif event_type == FILE_TRANSFER_COMPLETE:
                result = await ft_manager.handle_transfer_complete(
                    transfer_id=transfer_id,
                    sha256=params.get("sha256", ""),
                )
            else:
                result = {
                    "accepted": False,
                    "error": f"unknown event_type: {event_type}",
                }

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=result.get("success", result.get("accepted", False)),
                payload={
                    "event_type": event_type,
                    **result,
                },
            )
        except Exception as e:
            logger.exception(
                "[AgentWebSocketServer] 文件传输处理失败: %s",
                e,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": str(e),
                    "event_type": event_type,
                },
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))
            
    @staticmethod
    def get_conversation_history(
        sessions_dir: Path | str | None,
        *,
        session_id: str,
        page_idx: int,
    ) -> dict[str, Any] | None:
        # 按照 session_id 和分页消息获取历史记录
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        if not isinstance(page_idx, int) or page_idx <= 0:
            return None

        # 优先使用调用方解析好的 sessions_dir（多租户/托管运行时），
        # 回退到默认单租户路径以保持向后兼容。
        if sessions_dir is None:
            sessions_dir = get_agent_sessions_dir()
        history_path: Path = Path(sessions_dir) / session_id.strip() / "history.json"
        logger.info(
            "[AgentWebSocketServer] get_conversation_history: history_path=%s exists=%s",
            history_path,
            history_path.exists(),
        )
        if not history_path.exists():
            return None
        try:
            from jiuwenclaw.agentserver.session_history import read_history_records
            raw = read_history_records(history_path)
        except Exception as e:
            logger.warning(
                "[AgentWebSocketServer] get_conversation_history: read_history_records failed: %s",
                e,
            )
            return None
        if not isinstance(raw, list):
            return None

        page_size = 50
        total = len(raw)
        total_pages = max(1, math.ceil(total / page_size))
        if page_idx > total_pages:
            return None

        ordered = list(reversed(raw))
        start = (page_idx - 1) * page_size
        end = start + page_size
        resolved_sid = session_id.strip()
        page_slice = ordered[start:end]
        messages_out = enrich_history_messages_session_id(page_slice, resolved_sid)
        return {
            "messages": messages_out,
            "total_pages": total_pages,
            "page_idx": page_idx,
        }

    async def _handle_initialize(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 initialize 方法（非流式）.

        调用 AgentManager.initialize 完成初始化，返回 capabilities。

        Args:
            ws: WebSocket 连接
            request: AgentRequest
            send_lock: 发送锁
        """
        logger.info("[AgentServer] initialize: request_id=%s channel_id=%s", request.request_id, request.channel_id)

        try:
            params = request.params if isinstance(request.params, dict) else {}
            client_capabilities = params.get("clientCapabilities", {})
            logger.info(
                "[AgentServer] initialize clientCapabilities: %s",
                client_capabilities,
            )

            extra_config = {
                "protocol_version": params.get("protocolVersion", "0.1.0"),
                "client_capabilities": client_capabilities,
            }
            if request.channel_id == "acp":
                self._set_ws_acp_client_capabilities(ws, client_capabilities)

            channel_id = request.channel_id or "default"
            capabilities = await self._agent_manager.initialize(
                channel_id=channel_id,
                extra_config=extra_config,
            )
            if capabilities is None:
                capabilities = ACP_DEFAULT_CAPABILITIES.copy()

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=capabilities,
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await ws.send(json.dumps(wire, ensure_ascii=False))

            logger.info("[AgentServer] initialize completed: capabilities=%s", capabilities)

        except Exception as e:
            logger.exception("[AgentServer] initialize failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_session_create(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 session.create 方法.

        调用 AgentManager.create_session 创建会话，返回 session_id。

        Args:
            ws: WebSocket 连接
            request: AgentRequest
            send_lock: 发送锁
        """
        logger.info("[AgentServer] session.create: request_id=%s", request.request_id)

        try:
            channel_id = request.channel_id or "default"
            params = request.params if isinstance(request.params, dict) else {}
            explicit_session_id = params.get("session_id")
            session_id = await self._agent_manager.create_session(
                channel_id=channel_id,
                session_id=str(explicit_session_id).strip() if isinstance(explicit_session_id, str) else None,
            )

            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload=build_acp_session_new_result(session_id),
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await ws.send(json.dumps(wire, ensure_ascii=False))

            logger.info("[AgentServer] session.create completed: session_id=%s", session_id)

        except Exception as e:
            logger.exception("[AgentServer] session.create failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )
            wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
            async with send_lock:
                await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_session_delete(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """处理 session.delete 请求：删除 Agent 本机 sessions 目录下的会话目录。"""
        import shutil

        from jiuwenclaw.agentserver.session_id_safe import (
            normalize_safe_session_id,
            resolve_session_dir_under_root,
        )

        logger.info("[AgentServer] session.delete: request_id=%s", request.request_id)

        try:
            params = request.params if isinstance(request.params, dict) else {}
            raw_sid = str(params.get("session_id") or "").strip()
            if not raw_sid:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "session_id is required", "code": "BAD_REQUEST"},
                )
            else:
                safe_sid = normalize_safe_session_id(raw_sid)
                if safe_sid is None:
                    resp = AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=False,
                        payload={"error": "invalid session_id", "code": "BAD_REQUEST"},
                    )
                else:
                    # 合法的永久删除请求首先回收进程内授权。即使会话目录已被外部删除
                    # 或随后文件删除失败，也不能让 ACTIVE/PENDING Grant 继续存活。
                    try:
                        from jiuwenclaw.agentserver.permissions.skill_authorization.runtime import (
                            clear_deleted_session,
                        )

                        clear_deleted_session(safe_sid)
                    except Exception:  # noqa: BLE001 — Grant 回收失败不掩盖原删除结果
                        logger.warning(
                            "[AgentServer] session.delete skill grant cleanup failed: "
                            "session_id=%s",
                            safe_sid,
                            exc_info=True,
                        )
                    workspace_session_dir = get_agent_sessions_dir()
                    session_dir = resolve_session_dir_under_root(workspace_session_dir, safe_sid)
                    if session_dir is None:
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"error": "invalid session_id", "code": "BAD_REQUEST"},
                        )
                    elif not session_dir.exists():
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"error": "session not found", "code": "NOT_FOUND"},
                        )
                    elif not session_dir.is_dir():
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=False,
                            payload={"error": "session is not a directory", "code": "BAD_REQUEST"},
                        )
                    else:
                        shutil.rmtree(session_dir)
                        resp = AgentResponse(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            ok=True,
                            payload={"session_id": safe_sid},
                        )
        except Exception as e:  # noqa: BLE001
            logger.exception("[AgentServer] session.delete failed: %s", e)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(e)},
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def _handle_acp_tool_response(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        params = request.params if isinstance(request.params, dict) else {}
        jsonrpc_id = params.get("jsonrpc_id")
        response_payload = params.get("response")
        if not isinstance(response_payload, dict):
            response_payload = {}

        if get_acp_output_manager().complete_jsonrpc_response(jsonrpc_id, response_payload):
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"accepted": True},
            )
        else:
            logger.info(
                "[AgentServer] ignore unknown/late acp tool response: jsonrpc_id=%s request_id=%s",
                jsonrpc_id,
                request.request_id,
            )
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "accepted": False,
                    "ignored": True,
                    "reason": "unknown_or_late_response",
                    "jsonrpc_id": jsonrpc_id,
                },
            )

        wire = encode_agent_response_for_wire(resp, response_id=request.request_id)
        async with send_lock:
            await ws.send(json.dumps(wire, ensure_ascii=False))

    async def handle_acp_tool_response_for_test(
        self,
        ws: Any,
        request: AgentRequest,
        send_lock: asyncio.Lock,
    ) -> None:
        """Public test helper that delegates to ACP tool-response handling."""
        await self._handle_acp_tool_response(ws, request, send_lock)

    def is_working(self) -> bool:
        """返回 Agent 是否正在工作.

        用于沙箱保活校验。

        Returns:
            bool: 是否正在工作
        """
        return self._agent_manager.is_working()
