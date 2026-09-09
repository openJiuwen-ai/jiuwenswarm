# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""WebSocket transport for browser WebChannel (extends BaseWsChannel)."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import aiohttp
from websockets.exceptions import ConnectionClosed as WebSocketConnectionClosed

from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.gateway.channel_manager.base import ChannelMetadata, RobotMessageRouter
from jiuwenswarm.gateway.routing.base_ws_channel import BaseWsChannel
from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget
from jiuwenswarm.common.security.ws_origin import (
    extract_handshake_request,
    forbidden_origin_response,
    get_header_value,
    is_origin_check_enabled,
    is_allowed_browser_origin,
)
from jiuwenswarm.common.request_ext import (
    attach_to_metadata as _ext_attach,
    build_ext_from_source as _ext_build,
    set_current as _ext_set,
)
from jiuwenswarm.common.schema.message import EventType, Message, Mode, ReqMethod
from jiuwenswarm.common.ws_diagnostics import (
    describe_ws_exception,
    describe_ws_peer,
    format_ws_diagnostics,
)

logger = logging.getLogger(__name__)

_WEB_CONNECTION_USER_ID_ATTR = "_web_connection_user_id"

_HANDLER_BEFORE_CALLBACK_METHODS = frozenset({ReqMethod.CHAT_SEND.value})

# 带了 ws_id 但 peer 已不在时，这些事件不得按 session 兜底到其它连接，
# 否则旧 HTTP SSE abort 后残余 chat.delta / processing_status(false) 会打进
# 同会话新 outbound（粘泡或企业版 SSE 被误结束）。
_REQUEST_SCOPED_STREAM_EVENTS = frozenset({
    EventType.CHAT_DELTA.value,
    EventType.CHAT_REASONING.value,
    EventType.CHAT_FINAL.value,
    EventType.CHAT_PROCESSING_STATUS.value,
})

_STREAM_COALESCE_EVENT_TYPES = frozenset({"chat.delta", "chat.reasoning"})
_STREAM_COALESCE_MAX_FRAMES = 32

_WEB_FULL_PAYLOAD_EVENT_TYPES = frozenset(
    {
        "connection.ack",
        "todo.updated",
        "task.start",
        "task.update",
        "task.complete",
        "chat.tool_call",
        "chat.tool_update",
        "chat.tool_result",
        "chat.processing_status",
        "chat.interrupt_result",
        "chat.evolution_status",
        "chat.error",
        "heartbeat.relay",
        "context.usage",
        "context.compression_state",
        "chat.ask_user_question",
        "chat.ask_user_question_expired",
        "chat.subtask_update",
        "chat.symphony_status",
        "chat.notice",
        "history.message",
        "chat.session_result",
        "chat.usage_metadata",
        "chat.usage_summary",
        "chat.file",
        "chat.retract",
        "security.alert",
        "goal.snapshot",
        "goal.updated",
        "runtime.accepted",
        "execution.error",
        "proactive_recommendation",
    }
)

# ── 类型别名 ──────────────────────────────────────────────
# 方法处理器签名: (ws, req_id, params, session_id) -> None
MethodHandler = Callable[..., Awaitable[None]]
# 连接钩子签名: (ws) -> None | Awaitable[None]
ConnectHook = Callable[..., Any]


@dataclass(init=False)
class WebChannelConfig:
    """WebChannel bind settings (WS + HTTP are separate listeners)."""

    enabled: bool
    host: str
    ws_port: int
    http_port: int
    path: str
    allow_from: list[str]

    def __init__(
        self,
        *,
        enabled: bool = False,
        host: str = "127.0.0.1",
        ws_port: int | None = None,
        http_port: int | None = None,
        path: str = "/ws",
        allow_from: list[str] | None = None,
        port: int | None = None,
    ) -> None:
        if port is not None:
            ws_port = port
        if ws_port is None:
            ws_port = 19000
        self.enabled = enabled
        self.host = host
        self.ws_port = int(ws_port)
        self.http_port = int(http_port) if http_port is not None else self.ws_port + 2
        self.path = path
        self.allow_from = list(allow_from or [])
        self.__post_init__()

    def __post_init__(self) -> None:
        from jiuwenswarm.gateway.channel_manager.web.web_http_server import (
            resolve_web_http_port,
        )

        self.http_port = resolve_web_http_port(self.ws_port, http_port=self.http_port)

    @property
    def port(self) -> int:
        """Legacy alias for ``ws_port`` (``WEB_PORT`` / ``--port``)."""
        return self.ws_port


class WebWsTransport(BaseWsChannel):
    """WebSocket listener and frame protocol; business RPC lives on ``WebRpcHost``."""

    name = "web"
    channel_id = "web"

    def __init__(
        self,
        config: WebChannelConfig,
        router: RobotMessageRouter,
        owner: Any,
    ) -> None:
        super().__init__(config, router)
        self._owner = owner
        self.config: WebChannelConfig = config
        self._server: Any = None
        # ws -> set[session_id]: 追踪每个连接上活跃的 session
        self._ws_sessions: dict[int, set[str]] = {}
        # session_id -> is_processing
        self._session_busy: dict[str, bool] = {}
        # req_id -> (ws, title): session.create 转发主路径的成功响应拦截落行用
        self._pending_session_creates: dict[str, tuple[Any, str]] = {}

    @staticmethod
    def _coalescible_stream_frame(
        frame: Any,
    ) -> tuple[dict[str, Any], str] | None:
        """Return (decoded frame, content) for a merge-safe stream frame.

        帧已是 dict（入队时不预序列化），故无需 json.loads；返回 decoded 与
        content 供 _coalesce 直接在 dict 层合并，省去 str↔dict 往返。
        """
        if not isinstance(frame, dict) or frame.get("type") != "event":
            return None
        if frame.get("event") not in _STREAM_COALESCE_EVENT_TYPES:
            return None
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            return None
        content = payload.get("content")
        if not isinstance(content, str):
            return None
        return frame, content

    @staticmethod
    def _same_stream_identity(
        a: dict[str, Any],
        b: dict[str, Any],
    ) -> bool:
        """两帧除 payload.content 外是否同流（可合并）。

        逐键比对 payload 非 content 字段 + 外层 event 等键，避免构造 comparable
        dict 副本与整 dict 哈希比对的开销。
        """
        a_payload = a["payload"]
        b_payload = b["payload"]
        if a.get("event") != b.get("event"):
            return False
        if a.get("type") != b.get("type"):
            return False
        for key in set(a_payload) | set(b_payload):
            if key == "content":
                continue
            if a_payload.get(key) != b_payload.get(key):
                return False
        return True

    def _coalesce(
        self,
        first_frame: Any,
        queue: asyncio.Queue,
    ) -> list[Any]:
        """Merge only contiguous stream frames with identical non-content data."""
        parsed = self._coalescible_stream_frame(first_frame)
        if parsed is None:
            return [first_frame]

        decoded, merged_content = parsed
        merged_count = 1
        trailing: list[Any] = []

        while merged_count < _STREAM_COALESCE_MAX_FRAMES:
            try:
                candidate = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if candidate is None:
                trailing.append(None)
                break
            candidate_parsed = self._coalescible_stream_frame(candidate)
            if (
                candidate_parsed is None
                or not self._same_stream_identity(decoded, candidate_parsed[0])
            ):
                trailing.append(candidate)
                break
            merged_content += candidate_parsed[1]
            merged_count += 1

        if merged_count == 1:
            return [first_frame, *trailing]

        merged_payload = {**decoded["payload"], "content": merged_content}
        return [{**decoded, "payload": merged_payload}, *trailing]

    # ── 公共属性 ──────────────────────────────────────────

    @property
    def clients(self) -> set[Any]:
        """当前活跃的 WebSocket 客户端集合（从 _clients_by_key 推导，只读副本）."""
        result: set[Any] = set()
        for ws_list in self._clients_by_key.values():
            result.update(ws_list)
        return result

    def lookup_ws_by_id(self, peer_id: str) -> Any | None:
        pid = str(peer_id or "").strip()
        if not pid:
            return None
        ws = self._ws_by_id.get(pid)
        if ws is not None and not getattr(ws, "closed", False):
            return ws
        return None

    def peers_for_session_ws(self, session_id: str) -> set[Any]:
        peers: set[Any] = set()
        sid = str(session_id or "").strip()
        if not sid:
            return peers
        for rk, ws_list in self._clients_by_key.items():
            if rk.session_id == sid:
                for w in ws_list:
                    if not getattr(w, "closed", False):
                        peers.add(w)
        return peers

    @property
    def ws_sessions(self) -> dict[int, set[str]]:
        return self._ws_sessions

    @property
    def send_queues(self) -> dict[str, "asyncio.Queue[str | bytes | None]"]:
        return self._send_queues

    def coalesce(
        self,
        first_frame: Any,
        queue: asyncio.Queue,
    ) -> list[Any]:
        return self._coalesce(first_frame, queue)

    async def writer_loop(self, ws: Any, ws_id: str) -> None:
        await self._writer_loop(ws, ws_id)

    async def handle_raw_message(
        self,
        ws: Any,
        raw: str,
        query: dict[str, list[str]],
    ) -> None:
        await self._handle_raw_message(ws, raw, query)

    # ── 帧发送 API（公开给处理器使用）─────────────────────

    async def send_response(
            self,
            ws: Any,
            req_id: str,
            *,
            ok: bool,
            payload: dict[str, Any] | None = None,
            error: str | None = None,
            code: str | None = None,
    ) -> None:
        """向指定客户端发送 ``res`` 帧."""
        frame: dict[str, Any] = {
            "type": "res",
            "id": req_id,
            "ok": ok,
            "payload": payload or {},
        }
        if not ok:
            frame["error"] = error or "request failed"
            if code:
                frame["code"] = code
        try:
            self._enqueue_send(ws, frame)
        except Exception as e:
            if bool(getattr(ws, "closed", False)):
                logger.debug(
                    "WebChannel send_response skipped on closed websocket: %s",
                    format_ws_diagnostics(
                        {"id": req_id},
                        describe_ws_peer(ws),
                        describe_ws_exception(e),
                    ),
                )
                return
            raise

    async def send_event(
            self,
            ws: Any,
            event: str,
            payload: dict[str, Any],
            *,
            seq: int | None = None,
            stream_id: str | None = None,
    ) -> None:
        """向指定客户端发送 ``event`` 帧."""
        frame: dict[str, Any] = {"type": "event", "event": event, "payload": payload}
        if seq is not None:
            frame["seq"] = seq
        if stream_id is not None:
            frame["stream_id"] = stream_id
        try:
            self._enqueue_send(ws, frame)
        except Exception as e:
            if bool(getattr(ws, "closed", False)):
                logger.debug(
                    "WebChannel send_event skipped on closed websocket: %s",
                    format_ws_diagnostics(
                        {"event": event, "seq": seq, "stream_id": stream_id},
                        describe_ws_peer(ws),
                        describe_ws_exception(e),
                    ),
                )
                return
            raise

    def _lookup_peer(self, peer_id: str) -> Any | None:
        return self._owner.lookup_peer(peer_id)

    def _peers_for_session(self, session_id: str) -> set[Any]:
        return self._owner.peers_for_session(session_id)

    def _record_history_frame(self, direction: str, data: Any) -> None:
        self._owner.rpc.record_history_frame(direction, data)

    def _inject_user_id_into_frame(self, ws: Any, raw: str) -> str:
        """注入连接级身份到 browser 帧的 params，供 history 回调落库归属。

        前端发送的 WS 帧不含 user_id/group_id/bot_id（它们在握手 query/Header 里）；
        history 回调从 ``params.user/user_id`` 取用户、``params.group_id/bot_id`` 取
        身份 scope，缺失时 user 回退 guest、身份列留空（NULL=通配）。这里在提交前把
        连接级权威身份无条件覆盖进 params——客户端伪造的 user/user_id/group_id/bot_id
        不能覆盖连接级身份（防冒充，影响 session 列表过滤、历史读取、置顶顺序、
        PG 会话行身份归属等多用户隔离场景）。
        """
        try:
            uid = WebWsTransport.connection_user_id(ws)
            routing = getattr(ws, "_web_routing", None)
            if not uid and not isinstance(routing, dict):
                return raw
            data = json.loads(raw)
            if not isinstance(data, dict):
                return raw
            params = data.get("params")
            if not isinstance(params, dict):
                params = {}
                data["params"] = params
            if uid:
                params["user_id"] = uid
                params["user"] = uid
            if isinstance(routing, dict):
                for field in ("group_id", "bot_id"):
                    value = str(routing.get(field) or "").strip()
                    if value:
                        params[field] = value
            return json.dumps(data, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return raw

    def _enqueue_send(self, ws: Any, data: Any) -> None:
        self._record_history_frame("uplink", data)
        self._capture_session_create_row(ws, data)
        if getattr(ws, "is_http_outbound", False):
            from jiuwenswarm.gateway.channel_manager.web.outbound import _normalize_frame

            frame = _normalize_frame(data)
            if frame is None:
                return
            accept = getattr(ws, "accept_frame", None)
            if callable(accept):
                accept(frame)
            return
        super()._enqueue_send(ws, data)

    def _capture_session_create_row(self, ws: Any, data: Any) -> None:
        """session.create 成功响应即落 web 库会话行（remote 转发主路径）。

        remote 模式 session.create 主路径转发 AgentServer、不经过本地 handler，
        这里在下行响应统一拦截（WS 帧与 HTTP outbound 共用 ``_enqueue_send``）：
        web 库是会话面元数据唯一事实源，create 时点写入连接级身份与标题/
        项目归属。ensure 幂等（行已存在仅补空缺列）；失败不阻塞——首条消息
        record_user 会兜底建行（身份来自帧注入）。
        """
        try:
            if not isinstance(data, dict) or data.get("type") != "res" or not data.get("ok"):
                return
            rid = data.get("id")
            if not isinstance(rid, str) or not rid:
                return
            pending = self._pending_session_creates.pop(rid, None)
            if pending is None:
                return
            create_ws, title = pending
            payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
            sid = str(payload.get("session_id") or payload.get("sessionId") or "").strip()
            if not sid:
                return
            from jiuwenswarm.channels.web.history_store.api import ensure_session_row_sync

            routing = getattr(create_ws, "_web_routing", None)
            routing = routing if isinstance(routing, dict) else {}
            uid = self.connection_user_id(create_ws) \
                or str(routing.get("user_id") or "").strip() \
                or "guest"

            def _ensure_row() -> None:
                try:
                    ensure_session_row_sync(
                        sid,
                        None,
                        user=uid,
                        group_id=str(routing.get("group_id") or "").strip() or None,
                        bot_id=str(routing.get("bot_id") or "").strip() or None,
                        project_id=str(payload.get("projectId") or payload.get("project_id") or "").strip() or None,
                        work_mode=str(payload.get("workMode") or payload.get("work_mode") or "").strip() or None,
                        title=title or None,
                        ts=time.time(),
                    )
                except Exception:
                    logger.warning(
                        "[WebChannel] session.create 落 web 库会话行失败（首条消息兜底）: session_id=%s",
                        sid,
                        exc_info=True,
                    )

            threading.Thread(target=_ensure_row, daemon=True, name="web-create-row").start()
        except Exception:
            logger.debug("[WebChannel] session.create 落行拦截异常", exc_info=True)

    @staticmethod
    def _extract_query_user_id(flat_query: dict[str, str]) -> str | None:
        uid = str(flat_query.get("user_id", "") or "").strip()
        return uid or None

    @staticmethod
    def _extract_ws_header_user_id(ws: Any) -> str | None:
        headers = (
            getattr(getattr(ws, "request", None), "headers", None)
            or getattr(ws, "request_headers", None)
        )
        raw = get_header_value(headers, "X-User-Id")
        if raw is None:
            return None
        text = str(raw).strip()
        return text or None

    @classmethod
    def _resolve_connection_user_id(cls, flat_query: dict[str, str], ws: Any) -> str | None:
        connection_user_id = cls._extract_query_user_id(flat_query) or cls._extract_ws_header_user_id(ws)
        setattr(ws, _WEB_CONNECTION_USER_ID_ATTR, connection_user_id)
        return connection_user_id

    @staticmethod
    def connection_user_id(ws: Any) -> str | None:
        """返回 Web 连接建立时缓存的 user_id（query 或 X-User-Id Header）。"""
        uid = getattr(ws, _WEB_CONNECTION_USER_ID_ATTR, None)
        if uid is None:
            return None
        text = str(uid).strip()
        return text or None

    @staticmethod
    def _routing_key_user_id(connection_user_id: str | None, remote: Any) -> str:
        if connection_user_id:
            return connection_user_id
        return str(remote or "unknown")

    @classmethod
    def _resolve_ws_identity(
        cls,
        ws: Any,
        flat_query: dict[str, str],
        remote: Any,
        *,
        route_type: str = "ws",
    ) -> tuple[str | None, str]:
        """解析 ws 连接身份,供 /ws 和 /ws/git 共用(设计文档 §5.3.7)。

        Args:
            route_type: ``"ws"`` 主路由或 ``"git"`` /ws/git 路由,仅用于日志区分。

        Returns:
            ``(connection_user_id, routing_key_user_id)``
        """
        connection_user_id = cls._resolve_connection_user_id(flat_query, ws)
        routing_key_user_id = cls._routing_key_user_id(connection_user_id, remote)
        return connection_user_id, routing_key_user_id

    async def broadcast_event(
            self,
            event: str,
            payload: dict[str, Any],
            *,
            seq: int | None = None,
            stream_id: str | None = None,
            exclude_ws: Any = None,
    ) -> None:
        """向所有已连接客户端广播 ``event`` 帧.

        exclude_ws: 排除单个发起方 ws（如 config.changed 的保存发起方），
        避免发起方收到自身触发的广播而误弹「丢弃草稿」确认框。发起方靠
        保存响应的本地乐观合并自行刷新，无需这条广播。
        """
        frame: dict[str, Any] = {"type": "event", "event": event, "payload": payload}
        if seq is not None:
            frame["seq"] = seq
        if stream_id is not None:
            frame["stream_id"] = stream_id
        clients = self.clients
        if exclude_ws is not None:
            clients = {c for c in clients if c is not exclude_ws}
        await self._broadcast_to(frame, clients)

    # ── WebSocket 生命周期 ──────────────────────────────────

    async def start(self) -> None:
        await self.start_ws_server()

    async def stop(self) -> None:
        await self.stop_ws_server()

    async def start_ws_server(self) -> None:
        if self._running:
            logger.warning("WebWsTransport 已在运行")
            return
        if not self.config.enabled:
            logger.warning("WebWsTransport 未启用（enabled=False）")
            return

        try:
            from websockets.legacy.server import serve as ws_serve
        except Exception:  # pragma: no cover
            import websockets

            ws_serve = websockets.serve

        from jiuwenswarm.common.ws_limits import WEB_WS_MAX_MESSAGE_BYTES

        self._server = await ws_serve(
            self._connection_handler,
            self.config.host,
            self.config.ws_port,
            process_request=self._process_request,
            ping_interval=20,
            ping_timeout=60,
            max_size=WEB_WS_MAX_MESSAGE_BYTES,
        )
        self._running = True
        logger.info(
            f"WebChannel WS 已启动: ws://{self.config.host}:{self.config.ws_port}{self.config.path}"
        )

    async def wait_closed(self) -> None:
        if self._server is not None:
            await self._server.wait_closed()

    async def stop_ws_server(self) -> None:
        """Stop WebSocket server and clean up connections."""
        self._running = False
        all_clients = list(self.clients)
        close_tasks = [client.close(code=1001, reason="server shutdown") for client in all_clients]
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)
        self._clients_by_key.clear()

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        # 兜底清理未走正常断连路径的 writer 协程（正常断连已由 unregister_ws 清理）
        await self._shutdown_all_writers()
        logger.info("WebChannel WS 已停止")

    async def _process_request(self, *args: Any) -> Any:
        """在握手阶段执行 Origin 校验，兼容 legacy/new websockets APIs。"""
        path, request_headers = extract_handshake_request(args)
        origin = get_header_value(request_headers, "Origin")
        enable_origin_check = is_origin_check_enabled()
        if not enable_origin_check:
            logger.info(
                "WebChannel 握手检查 path=%s origin=%s enable_origin_check=%s allowed=%s",
                path,
                origin,
                enable_origin_check,
                True,
            )
            return None

        allowed = is_allowed_browser_origin(origin)
        logger.info(
            "WebChannel 握手检查 path=%s origin=%s enable_origin_check=%s allowed=%s",
            path,
            origin,
            enable_origin_check,
            allowed,
        )
        if allowed:
            return None

        logger.warning(
            "WebChannel 握手拒绝 path=%s origin=%s reason=origin_not_allowed",
            path,
            origin,
        )
        return forbidden_origin_response(args)

    @staticmethod
    def _should_preserve_full_payload(event_name: str) -> bool:
        return (
            event_name in _WEB_FULL_PAYLOAD_EVENT_TYPES
            or event_name.startswith("team.")
            or event_name.startswith("harness.")
        )

    @classmethod
    def _build_event_payload(cls, msg: Message, event_name: str) -> dict[str, Any]:
        """Build the Web event payload without dropping structured control fields."""
        if isinstance(msg.payload, dict):
            if cls._should_preserve_full_payload(event_name):
                payload = {**msg.payload}
                if "session_id" not in payload and msg.session_id:
                    payload["session_id"] = msg.session_id
                if event_name.startswith("chat.") and "request_id" not in payload and msg.id:
                    payload["request_id"] = msg.id
                return payload

            content = str(msg.payload.get("content", "") or "")
            if not content and not getattr(msg, "ok", True) and msg.payload.get("error"):
                content = str(msg.payload.get("error", ""))
            payload = {
                "session_id": msg.session_id,
                "content": content,
            }
            for _key in ("role", "member_name", "member_action", "source_channel", "user_id", "display_name"):
                _val = msg.payload.get(_key)
                if _val is not None:
                    payload[_key] = _val
            # chat.delta/final 等非 full-payload 事件也带 request_id，供
            # 历史回调 _handle_uplink 按 request_id 累积 delta + 落盘 assistant。
            if event_name.startswith("chat.") and "request_id" not in payload and msg.id:
                payload["request_id"] = msg.id
            if event_name == "chat.final":
                cron_extra = msg.payload.get("cron")
                if isinstance(cron_extra, dict):
                    payload["cron"] = cron_extra
                source = msg.payload.get("source")
                if source:
                    payload["source"] = source
                ptype = msg.payload.get("proactive_type")
                if ptype:
                    payload["proactive_type"] = ptype
                if source == "proactive_recommendation":
                    logger.info(
                        "[WebChannel] proactive push frame: source=%s proactive_type=%s "
                        "content_len=%d payload_keys=%s",
                        source, ptype, len(str(payload.get("content", ""))), list(payload.keys()),
                    )
            return payload

        content = str((msg.params or {}).get("content", "") or "")
        return {
            "session_id": msg.session_id,
            "content": content,
        }

    async def send(
        self,
        msg: Message,
        *,
        routing_target: RoutingTarget | None = None,
    ) -> None:
        """向客户端发送消息。

        V2: 当 routing_target 非空时，按其 routing_keys 精确路由（_clients_by_key）。
        否则回退到全量广播（向后兼容）。
        """
        _pl = getattr(msg, "payload", None) or {}
        _et = _pl.get("event_type", "") if isinstance(_pl, dict) else ""
        _has_fanout = bool((getattr(msg, "metadata", None) or {}).get("fan_out_targets"))
        logger.debug(
            "[WebChannel] send() called: id=%s event_type=%s payload_et=%s has_fanout=%s"
            " has_routing_target=%s client_count=%s",
            getattr(msg, "id", ""), getattr(msg, "event_type", None), _et,
            _has_fanout, routing_target is not None, len(self.clients),
        )
        # ── 心跳 relay：临时 session_id（heartbeat_{ts}_{suffix}）不匹配任何前端连接，
        # 按常规 session_id 路由会被当作"无连接"丢弃。心跳状态是全局的（非会话级），
        # 前端 setHeartbeatStatus 也是全局 store，因此直接广播给所有 web 客户端。
        # 与 wechat 等 IM 渠道在 send() 中对 HEARTBEAT_RELAY 的专属分支对齐。
        if msg.event_type == EventType.HEARTBEAT_RELAY:
            frame = self._serialize_frame(msg, None)  # 返回 dict，由 writer 统一序列化
            clients = self.clients
            for w in clients:
                self._enqueue_send(w, frame)
            logger.debug(
                "[WebChannel] heartbeat.relay broadcast to %d client(s) id=%s",
                len(clients), getattr(msg, "id", ""),
            )
            return

        # ── 定时任务推 web：原设计绑定 job.session_id，但关闭 tab/换设备后旧会话再无连接，
        # 按 session_id 路由会被丢弃。cron 推送（占位 + 结果）带 payload.cron 标记，普通对话
        # chat.final 不带，以此为识别条件广播给所有 web 客户端。前端 _push_to_targets 已对 web
        # 置空 session_id，shouldHandleSessionEvent 放行，消息进当前活跃会话流（含 placeholder 替换）。
        if (
            msg.event_type == EventType.CHAT_FINAL
            and isinstance(msg.payload, dict)
            and isinstance(msg.payload.get("cron"), dict)
        ):
            frame = self._serialize_frame(msg, None)  # 返回 dict，由 writer 统一序列化
            clients = self.clients
            for w in clients:
                self._enqueue_send(w, frame)
            logger.debug(
                "[WebChannel] cron push broadcast to %d client(s) id=%s run_id=%s",
                len(clients), getattr(msg, "id", ""),
                (msg.payload.get("cron") or {}).get("run_id", ""),
            )
            return

        # ── 主动推荐系统通知推 web：与 cron 推送同理——后端主动推、无前端 session_id 绑定，
        # 按 session_id 路由会被当"无 session"丢弃（旧路径 580 行 if not msg.session_id 兜底丢弃）。
        # proactive notification（"今日已达上限"等系统提醒）带 payload.source ==
        # "proactive_notification" 标记，据此广播给所有 web 客户端。前端 shouldHandleSessionEvent
        # 对无 session_id 的 payload 放行，作为普通 assistant 消息渲染。
        if (
            msg.event_type == EventType.CHAT_FINAL
            and isinstance(msg.payload, dict)
            and msg.payload.get("source") == "proactive_notification"
        ):
            frame = self._serialize_frame(msg, None)  # 返回 dict，由 writer 统一序列化
            clients = self.clients
            for w in clients:
                self._enqueue_send(w, frame)
            logger.debug(
                "[WebChannel] proactive_notification broadcast to %d client(s) id=%s",
                len(clients), getattr(msg, "id", ""),
            )
            return

        if msg.type == "res":
            if isinstance(msg.payload, dict):
                res_payload = {**msg.payload}
            elif msg.payload is None:
                res_payload = {}
            else:
                res_payload = {"content": str(msg.payload)}

            frame: dict[str, Any] = {
                "type": "res",
                "id": msg.id,
                "ok": bool(msg.ok),
                "payload": res_payload,
            }
            if not msg.ok:
                # Prefer explicit error; fall back to message (e.g. command.goal
                # unary failures put the human-readable text in payload.message).
                error_text = res_payload.get("error") or res_payload.get("message")
                if isinstance(error_text, str) and error_text:
                    frame["error"] = error_text
                code_text = res_payload.get("code")
                if isinstance(code_text, str) and code_text:
                    frame["code"] = code_text

            ws_set: set[Any] = set()
            metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
            request_ws_id = str(metadata.get("ws_id") or "").strip()
            if request_ws_id:
                ws = self._lookup_peer(request_ws_id)
                if ws is not None:
                    ws_set.add(ws)

            if not ws_set and routing_target is not None:
                delivery = routing_target.delivery
                if delivery is not None:
                    ws_id = getattr(delivery, "ws_id", "")
                    if ws_id:
                        ws = self._lookup_peer(str(ws_id))
                        if ws is not None:
                            ws_set.add(ws)
                if not ws_set:
                    for rk in routing_target.routing_keys:
                        ws_list = self._clients_by_key.get(rk) or []
                        for w in ws_list:
                            if not getattr(w, "closed", False):
                                ws_set.add(w)

            if not ws_set and msg.session_id:
                ws_set |= self._peers_for_session(msg.session_id)

            if not ws_set:
                logger.debug(
                    "[WebChannel] response route miss: ws_id=%s session_id=%s id=%s",
                    request_ws_id,
                    msg.session_id,
                    getattr(msg, "id", ""),
                )
                return
            await self._broadcast_to(frame, ws_set)
            return

        # ── V2 精确路由 ──
        if routing_target is not None:
            routing_keys = routing_target.routing_keys
            member_names = list(routing_target.member_names)

            # ── 优先：按 delivery.ws_id 物理寻址 ──
            ws_set: set[Any] = set()
            delivery = routing_target.delivery
            if delivery is not None:
                ws_id = getattr(delivery, "ws_id", "")
                if ws_id:
                    ws = self._lookup_peer(str(ws_id))
                    if ws is not None:
                        ws_set.add(ws)

            # ── 兜底：按 routing_keys 5 维逻辑查 _clients_by_key ──
            if not ws_set and routing_keys:
                for rk in routing_keys:
                    ws_list = self._clients_by_key.get(rk) or []
                    for w in ws_list:
                        if not getattr(w, "closed", False):
                            ws_set.add(w)
            if ws_set:
                frame_data = self._serialize_frame(msg, routing_target, member_names=member_names)
                for w in ws_set:
                    self._enqueue_send(w, frame_data)
                return
            # V2 精确路由未命中 —— 回退到 session_id 路由
            logger.debug(
                "[WebChannel] V2 routing miss: looked up %d routing_keys + ws_id=%s,"
                " ws_set empty — falling back to session_id=%s",
                len(routing_keys), getattr(delivery, "ws_id", "") if delivery else "",
                getattr(msg, "session_id", ""),
            )

        # ── 旧路径：优先按请求 metadata.ws_id 物理寻址，再按 session_id 精确路由 ──
        # 普通 stream event（chat.delta/final/usage_summary 等）由 ChannelManager
        # 调 channel.send(msg)，不会携带 RoutingTarget；但原始 Web 请求注入的
        # metadata.ws_id 会经 _chunk_to_message 保留下来。先用它收窄到发起请求的
        # 物理连接，避免同一个 session 桶里的陈旧 ws 一起收到迟到事件。
        event_name = "chat.final"
        if msg.event_type is not None:
            event_name = msg.event_type.value
        elif isinstance(msg.payload, dict):
            payload_event_type = msg.payload.get("event_type")
            if isinstance(payload_event_type, str) and payload_event_type.strip():
                event_name = payload_event_type.strip()

        ws_set: set[Any] = set()
        metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
        request_ws_id = str(metadata.get("ws_id") or "").strip()
        if request_ws_id:
            ws = self._lookup_peer(request_ws_id)
            if ws is not None:
                ws_set.add(ws)

        if not ws_set and not msg.session_id:
            logger.warning(
                "[WebChannel] msg has no session_id, cannot route -- "
                "dropping msg id=%s to avoid cross-session broadcast",
                getattr(msg, "id", ""),
            )
            return
        if not ws_set:
            if request_ws_id and event_name in _REQUEST_SCOPED_STREAM_EVENTS:
                logger.debug(
                    "[WebChannel] drop stale stream event: event=%s ws_id=%s "
                    "session_id=%s id=%s",
                    event_name,
                    request_ws_id,
                    msg.session_id,
                    getattr(msg, "id", ""),
                )
                return
            ws_set |= self._peers_for_session(msg.session_id)
        if not ws_set:
            logger.debug(
                "[WebChannel] session_id=%s has no connected ws, dropping msg id=%s ws_id=%s",
                msg.session_id, getattr(msg, "id", ""), request_ws_id,
            )
            return
        all_clients = ws_set

        payload = self._build_event_payload(msg, event_name)

        # ── V2: 诊断日志 ──
        if routing_target is not None:
            logger.info(
                "[WebChannel] frame: id=%s event=%s intent=%s",
                getattr(msg, "id", ""), event_name, routing_target.intent,
            )
        if getattr(msg, "agent_ref", None):
            payload["agent_ref"] = msg.agent_ref if isinstance(msg.agent_ref, dict) else {
                "mode": getattr(msg.agent_ref, "mode", ""),
                "id": getattr(msg.agent_ref, "id", ""),
            }

        frame_data: dict[str, Any] = {
            "type": "event",
            "event": event_name,
            "payload": payload,
        }
        await self._broadcast_to(frame_data, all_clients)

        # remote 模式下维护会话索引：chat.final 时写入最近一条助手消息预览
        if event_name == "chat.final" and msg.session_id:
            try:
                from jiuwenswarm.gateway.routing.session_index import is_remote_storage, upsert_async
                if is_remote_storage():
                    _md = msg.metadata if isinstance(msg.metadata, dict) else {}
                    # metadata 缺 user_id 时跳过 upsert：保留 chat.send 写入的真实
                    # 归属。回退 guest 会把已有条目整条覆盖，真实用户的会话会从
                    # 兜底索引中"消失"（list_sessions_page 按真实 user 过滤）。
                    _raw_uid = str(_md.get("user_id") or "").strip()
                    if not _raw_uid:
                        logger.warning(
                            "[WebChannel] chat.final 缺少 user_id，跳过会话索引更新"
                            "（WebSocket 连接未携带 user_id）",
                        )
                    else:
                        await upsert_async(
                            msg.session_id,
                            "assistant",
                            str(payload.get("content") or ""),
                            time.time(),
                            user=_raw_uid,
                        )
            except Exception:
                logger.debug("[WebChannel] session_index upsert skipped", exc_info=True)

        # 维护 session busy 状态(供 /ws/git 写操作查询)
        if event_name == "chat.processing_status" and isinstance(payload, dict):
            sid = payload.get("session_id") or msg.session_id
            if sid:
                self._session_busy[sid] = bool(payload.get("is_processing", False))

        # interrupt_result 根据 intent 决定 is_processing 状态
        if event_name == "chat.interrupt_result":
            intent = payload.get("intent", "cancel") if isinstance(payload, dict) else "cancel"
            is_processing = intent in ("pause", "supplement", "resume")
            # 同步更新 busy 映射
            if msg.session_id:
                self._session_busy[msg.session_id] = is_processing
            await self._broadcast_to({
                "type": "event",
                "event": "chat.processing_status",
                "payload": {"session_id": msg.session_id, "is_processing": is_processing},
            }, all_clients)

    def is_session_busy(self, session_id: str) -> bool:
        """查询 session 是否正在执行(agent 处理中)。

        基于 ``chat.processing_status`` 事件维护的映射。
        未跟踪的 session 默认返回 False(不忙碌)。

        供 /ws/git 写操作(如 ``project.git.discard_turn_changes``)在执行前
        校验会话非忙碌,避免与正在进行的 agent 文件写入冲突。
        """
        return self._session_busy.get(session_id, False)

    def get_metadata(self) -> ChannelMetadata:
        """获取 Channel 元数据."""
        return ChannelMetadata(
            channel_id=self.channel_id,
            source="websocket",
            extra={"host": self.config.host, "port": self.config.ws_port, "path": self.config.path},
        )

    # ── 内部实现 ──────────────────────────────────────────

    async def _connection_handler(self, ws: Any, path: str | None = None) -> None:
        raw_path = path if path is not None else getattr(ws, "path", "")
        parsed = urlparse(raw_path)
        request_path = parsed.path or raw_path
        query = parse_qs(parsed.query)
        remote = getattr(ws, "remote_address", None)
        _flat_query = {k: (v[0] if v else "") for k, v in query.items()}

        # ── Path 分发(设计文档 §5.3.7) ──
        # /ws/git → GitDiffWebSocketHandler
        # /ws     → 现有主 RPC
        # 其他    → 1008 close
        if request_path == "/ws/git":
            await self._handle_git_ws_connection(ws, _flat_query, remote)
            return

        if request_path != self.config.path:
            await ws.close(code=1008, reason=f"unsupported path: {request_path}")
            return

        connection_user_id, _user_id = self._resolve_ws_identity(
            ws, _flat_query, remote, route_type="ws",
        )
        try:
            _conn_ext = _ext_build(query)
        except Exception:
            _conn_ext = None
        setattr(ws, "_web_request_ext", _conn_ext)
        # 连接级权威身份：user_id + group/bot/gateway（进站时再拆到顶层 / routing）。
        from jiuwenswarm.common.request_identity import normalize_routing_identity

        _routing = normalize_routing_identity(
            _flat_query,
            {"user_id": connection_user_id} if connection_user_id else None,
        )
        setattr(ws, "_web_routing", _routing)
        _ext_set(_conn_ext)
        uid_marker = "" if connection_user_id else " uid_empty=yes"
        if not _routing.get("bot_id"):
            logger.warning(
                "WebChannel 新连接缺少 bot_id: remote=%s routing=%s",
                remote,
                _routing,
            )
        logger.info(
            "WebChannel 新连接: remote=%s query=%s routing=%s user_id=%r%s",
            remote,
            query,
            _routing,
            connection_user_id,
            uid_marker,
        )

        # ── V2: 从 query 提取身份字段，构造默认 RoutingKey ──
        # session_id 和 agent_id 可能在首条消息中更新
        _app_id = _flat_query.get("app_id", "default")
        _mode = _flat_query.get("mode", "agent")
        _agent_id = _flat_query.get("agent_id", "default")
        _initial_sid = _flat_query.get("session_id", self._make_session_id())
        _initial_rk = RoutingKey(
            user_id=_user_id,
            channel_id=self.channel_id,
            app_id=_app_id,
            agent_ref=AgentRef(mode=_mode, id=_agent_id),
            session_id=_initial_sid,
        )
        await self.register_ws(ws, _initial_rk)
        # 将握手阶段占位 session_id 挂到 ws 上，供 _on_connect 等连接级钩子复用，
        # 确保 connection.ack 与 ws 在 _clients_by_key 中的注册 key 一致，
        # 否则 send() 按 session_id 反查会落空导致 ACK 丢弃。
        # 注：此 sid 仅为传输层占位，首条 chat.send 携带真实 session_id 时会 re-register 覆盖。
        setattr(ws, "_jiuwen_initial_sid", _initial_sid)

        # 触发连接钩子（如发送 connection.ack）
        for hook in self._owner.rpc.connect_hooks:
            try:
                result = hook(ws)
                if inspect.isawaitable(result):
                    await result
            except Exception as e:  # pragma: no cover
                logger.warning(
                    "WebChannel on_connect hook error: %s",
                    format_ws_diagnostics(
                        {"remote": remote, "path": request_path},
                        describe_ws_peer(ws),
                        describe_ws_exception(e),
                    ),
                )

        try:
            async for raw in ws:
                await self._handle_raw_message(ws, raw, query)
        except WebSocketConnectionClosed as e:  # pragma: no cover - 连接生命周期容错
            logger.info(
                "WebChannel 连接关闭: %s",
                format_ws_diagnostics(
                    {"remote": remote, "path": request_path},
                    describe_ws_peer(ws),
                    describe_ws_exception(e),
                ),
            )
        except Exception as e:  # pragma: no cover - 连接生命周期容错
            logger.warning(
                "WebChannel 连接异常: %s",
                format_ws_diagnostics(
                    {"remote": remote, "path": request_path},
                    describe_ws_peer(ws),
                    describe_ws_exception(e),
                ),
            )
        finally:
            await self.unregister_ws(ws)

            logger.info(
                "WebChannel 连接清理完成: %s",
                format_ws_diagnostics(
                    {"remote": remote, "path": request_path, "clients": len(self._clients_by_key)},
                    describe_ws_peer(ws),
                ),
            )
            # 取出该 ws 关联的 session_ids，清理映射
            ws_id = id(ws)
            disconnected_sessions = self._ws_sessions.pop(ws_id, set())
            logger.info(
                "WebChannel 连接关闭: remote=%s sessions=%s",
                remote,
                disconnected_sessions or "none",
            )
            # 注意:此处不清理 _session_busy。ws 断开不等价于 agent 已停止——
            # 用户关 tab / 刷新 / 网络断开期间,后端 run 仍可能在写文件。若按
            # ws ownership 清掉 busy,新的 discard_turn_changes 会通过 busy 校验,
            # 与仍在运行的 agent 文件写入并发,造成数据损坏。stale busy 的治理
            # 应基于 TTL / 心跳 / agentserver run 状态源,而非 ws 连接状态。
            # 触发断连钩子,传入 session_ids(签名: (ws, session_ids))
            for hook in self._owner.rpc.disconnect_hooks:
                try:
                    result = hook(ws, disconnected_sessions)
                    if inspect.isawaitable(result):
                        await result
                except Exception as e:  # pragma: no cover
                    logger.warning("WebChannel on_disconnect hook error: %s", e)

    async def _handle_git_ws_connection(
        self,
        ws: Any,
        flat_query: dict[str, str],
        remote: Any,
    ) -> None:
        """处理 /ws/git 路由的连接(设计文档 §5.3.7)。

        构建 ``AgentRef(mode="git", id="diff")`` 哨兵 RoutingKey,
        注册后委托 ``GitDiffWebSocketHandler.handle_connection`` 处理消息循环。
        断连 ``finally`` 先后调 ``unregister_ws(ws)`` 和
        ``git_watcher_registry.cleanup_ws(ws)``,避免 watcher 仍继续轮询推送。
        """
        registry = getattr(self._owner, "git_watcher_registry", None)
        if registry is None:
            await ws.close(code=1011, reason="git watcher registry not available")
            return

        from jiuwenswarm.gateway.channel_manager.web.git_ws_handler import (
            GitDiffWebSocketHandler,
        )
        handler = GitDiffWebSocketHandler(self, registry)

        connection_user_id, _user_id = self._resolve_ws_identity(
            ws, flat_query, remote, route_type="git",
        )
        _app_id = flat_query.get("app_id", "default")
        # session_id 为传输层占位,不是聊天会话(设计文档 §5.3.7)
        _session_id = flat_query.get("session_id") or f"gitws_{uuid.uuid4().hex[:12]}"
        _rk = RoutingKey(
            user_id=_user_id,
            channel_id=self.channel_id,
            app_id=_app_id,
            agent_ref=AgentRef(mode="git", id="diff"),
            session_id=_session_id,
        )
        await self.register_ws(ws, _rk)

        logger.info(
            "[WebChannel] /ws/git 新连接: remote=%s user_id=%r session_id=%s",
            remote,
            connection_user_id,
            _session_id,
        )

        try:
            await handler.handle_connection(ws, flat_query)
        except WebSocketConnectionClosed as e:
            logger.info(
                "[WebChannel] /ws/git 连接关闭: %s",
                format_ws_diagnostics(
                    {"remote": remote, "path": "/ws/git"},
                    describe_ws_peer(ws),
                    describe_ws_exception(e),
                ),
            )
        except Exception as e:
            logger.warning(
                "[WebChannel] /ws/git 连接异常: %s",
                format_ws_diagnostics(
                    {"remote": remote, "path": "/ws/git"},
                    describe_ws_peer(ws),
                    describe_ws_exception(e),
                ),
            )
        finally:
            await self.unregister_ws(ws)
            try:
                registry.cleanup_ws(ws)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[WebChannel] /ws/git cleanup_ws failed: %s", exc,
                )
            logger.info(
                "[WebChannel] /ws/git 连接清理完成: remote=%s",
                remote,
            )

    async def _handle_raw_message(self, ws: Any, raw: str, query: dict[str, list[str]]) -> None:
        self._record_history_frame("browser", self._inject_user_id_into_frame(ws, raw))
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self.send_response(ws, "", ok=False, error="invalid json", code="BAD_REQUEST")
            return

        if not isinstance(data, dict):
            await self.send_response(ws, "", ok=False, error="invalid request", code="BAD_REQUEST")
            return

        req_type = data.get("type")
        req_id = data.get("id")
        method = data.get("method")
        params = data.get("params")

        if req_type != "req" or not isinstance(req_id, str) or not isinstance(method, str):
            await self.send_response(
                ws,
                req_id if isinstance(req_id, str) else "",
                ok=False,
                error="invalid request",
                code="BAD_REQUEST",
            )
            return
        if not isinstance(params, dict):
            params = {}

        # remote 模式 session.create 走转发主路径（本地 _session_create 仅是
        # 转发未生效时的 fallback）：登记请求，成功响应在 _enqueue_send 拦截后
        # 落 web 库会话行（连接级身份 + 标题）。req_type/req_id/params 已在
        # 上方校验归一，此处只剩方法名与非空 id 两个条件。
        if method == "session.create" and req_id:
            if len(self._pending_session_creates) >= 512:
                self._pending_session_creates.clear()
            create_title = str(params.get("title") or "").strip()
            self._pending_session_creates[req_id] = (ws, create_title)

        # ── V2: session_id 解析 ──
        # 请求自带 session_id（如 chat.send）→ 用它更新 ws 路由注册。
        # 请求未带 session_id（如 memory.compute 心跳、updater.check、config.get
        # 等 ws 层 keepalive / 拉取请求）→ 这类请求与 session 无关，
        # 仅合成一个临时 id 供后续 Message 构造使用，但【不】参与 register_ws，
        # 保留 ws 上一次的真实 RoutingKey，避免把 ws 从其所属 team session 摘除。
        _explicit_session_id = params.get("session_id")
        has_explicit_session = (
            isinstance(_explicit_session_id, str) and bool(_explicit_session_id)
        )
        session_id = _explicit_session_id if has_explicit_session else self._make_session_id()

        # 追踪 ws → 真实 session_id，用于断连清理/日志。
        # 与 register_ws 一致：仅显式 session 入集；临时 id 只供 Message 构造，避免膨胀。
        if has_explicit_session:
            ws_id = id(ws)
            sessions = self._ws_sessions.get(ws_id)
            if sessions is None:
                sessions = set()
                self._ws_sessions[ws_id] = sessions
            sessions.add(session_id)

        params = await self._owner.rpc.process_files(params)

        # ── V2: 用实际的 session_id / mode / agent_id 更新 ws 注册 ──
        _flat_query = {k: (v[0] if v else "") for k, v in query.items()}
        _mode = params.get("mode", "agent")
        _agent_id = params.get("agent_id", "default")
        _app_id = _flat_query.get("app_id", "default")
        req_user_id = self.connection_user_id(ws)
        if has_explicit_session:
            _rk = RoutingKey(
                user_id=self._routing_key_user_id(req_user_id, getattr(ws, "remote_address", None)),
                channel_id=self.channel_id,
                app_id=_app_id,
                agent_ref=AgentRef(mode=_mode, id=_agent_id),
                session_id=session_id,
            )
            await self.register_ws(ws, _rk)
        # else: ws 层心跳 / 拉取请求，不更新路由注册，沿用 ws 已有的 RoutingKey。

        # Preserve client top-level is_stream (e.g. command.goal set/resume).
        # chat.send / history.get still become stream in _normalize_gateway_message
        # even when the client omits this field.
        from jiuwenswarm.common.request_identity import (
            apply_routing_metadata,
            normalize_routing_identity,
        )

        # 权威：顶层 user_id + metadata.routing(group/bot/gateway)；握手 query 仅作 wire 痕迹。
        _routing = getattr(ws, "_web_routing", None)
        if not isinstance(_routing, dict) or not _routing:
            _routing = normalize_routing_identity(
                _flat_query,
                {"user_id": req_user_id} if req_user_id else None,
            )
        _meta = apply_routing_metadata(
            {
                "query": query,
                "method": method,
                # V2: 注入 ws_id 供 MessageHandler 构造 WebDeliveryTarget(ws_id=真值)。
                "ws_id": getattr(ws, "_jiuwen_ws_id", ""),
            },
            _routing,
        )

        user_message = Message(
            id=req_id,
            type="req",
            channel_id=self.channel_id,
            session_id=session_id,
            params=params,
            timestamp=time.time(),
            ok=True,
            req_method=self._parse_req_method(method),
            mode=self._parse_mode(params.get("mode")),
            is_stream=bool(data.get("is_stream", False)),
            app_id=_app_id,
            agent_ref={"mode": _mode, "id": _agent_id},
            user_id=req_user_id,
            metadata=_ext_attach(
                _meta,
                ext=getattr(ws, "_web_request_ext", None),
            ),
        )

        from jiuwenswarm.gateway.channel_manager.web.invoke import dispatch_web_request

        await dispatch_web_request(
            self._owner,
            method=method,
            params=params,
            request_id=req_id,
            outbound=ws,
            session_id=session_id,
            user_message=user_message,
        )

    async def _broadcast_to(self, frame: dict[str, Any], clients: set[Any]) -> None:
        """向指定 clients 集合广播帧（走 per-ws writer，非阻塞入队）.

        入队 dict，由 writer 统一序列化一次，避免此处预 dumps。
        """
        if not clients:
            return
        for client in clients:
            self._enqueue_send(client, frame)

    # ── BaseWsChannel 抽象方法 ──

    def _serialize_frame(
        self,
        msg: Any,
        routing_target: RoutingTarget | None = None,
        *,
        member_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """将 Message 转为 Web 前端帧 dict（由 writer 统一序列化）."""
        event_name = "chat.final"
        if getattr(msg, "event_type", None) is not None:
            event_name = msg.event_type.value
        elif isinstance(getattr(msg, "payload", None), dict):
            et = msg.payload.get("event_type")
            if isinstance(et, str) and et.strip():
                event_name = et.strip()

        payload: dict[str, Any] = {}
        if isinstance(msg.payload, dict):
            payload = {**msg.payload}
            if "session_id" not in payload and getattr(msg, "session_id", None):
                payload["session_id"] = msg.session_id
        elif getattr(msg, "payload", None) is not None:
            payload = {"session_id": getattr(msg, "session_id", None), "content": str(msg.payload)}
        else:
            payload = {"session_id": getattr(msg, "session_id", None), "content": ""}

        agent_ref = getattr(msg, "agent_ref", None)
        if agent_ref:
            payload["agent_ref"] = agent_ref if isinstance(agent_ref, dict) else {
                "mode": getattr(agent_ref, "mode", ""),
                "id": getattr(agent_ref, "id", ""),
            }

        frame: dict[str, Any] = {
            "type": "event",
            "event": event_name,
            "payload": payload,
        }
        return frame

    @staticmethod
    def _parse_req_method(method: str) -> ReqMethod | None:
        for item in ReqMethod:
            if item.value == method:
                return item
        return None

    @staticmethod
    def _parse_mode(raw_mode: Any) -> Mode:
        return Mode.from_raw(raw_mode, default=Mode.AGENT)

    @staticmethod
    def _make_session_id() -> str:
        # 与前端 generateSessionId 保持一致：毫秒时间戳(16进制) + 6位随机16进制
        ts = format(int(time.time() * 1000), "x")
        suffix = secrets.token_hex(3)
        return f"sess_{ts}_{suffix}"

    parse_req_method = _parse_req_method
    parse_mode = _parse_mode
    make_session_id = _make_session_id
    build_event_payload = _build_event_payload
    coalescible_stream_frame = _coalescible_stream_frame
    extract_query_user_id = _extract_query_user_id
    extract_ws_header_user_id = _extract_ws_header_user_id
    resolve_connection_user_id = _resolve_connection_user_id
    routing_key_user_id = _routing_key_user_id
