from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import secrets
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, cast
from urllib.parse import parse_qs, urlparse

from jiuwenclaw.channel.base import BaseChannel
from jiuwenclaw.e2a.gateway_normalize import e2a_from_agent_fields

if TYPE_CHECKING:
    from jiuwenclaw.gateway.channel_manager import ChannelManager
from jiuwenclaw.channel.vibeskill_session import (
    VIBESKILL_CHANNEL_ID,
    VibeSkillSession,
    VibeSkillSessionState,
    VibeSkillSessionStore,
    _VIBESKILL_ORIGINAL_SESSION_ID_KEY,
)
from jiuwenclaw.channel.vibeskill_session_dcs_store import VibeSkillSessionDcsStore

from jiuwenclaw.channel.vibeskill_file_utils import skilldev_tree_to_file_tree_nodes
from jiuwenclaw.channel.vibeskill_security import (
    validate_file_part,
    validate_file_path,
)
from jiuwenclaw.agentserver.skilldev.session_history.restore_chunks import (
    RestoreChunkDecodeError,
    decode_restore_payload_from_stream_with_retry,
)
from jiuwenclaw.agentserver.skilldev.file_read_chunks import (
    FileReadChunkDecodeError,
    decode_file_read_payload_from_stream_with_retry,
)
from jiuwenclaw.agentserver.skilldev.file_write_chunks import (
    FileWriteChunkError,
    FileWriteTransportError,
    send_file_write_with_chunks,
)
from jiuwenclaw.schema.message import Message, ReqMethod
from jiuwenclaw.log import interface_info

logger = logging.getLogger(__name__)
interface_logger = interface_info.interface_logger


def _create_query_url_obs() -> Any:
    from jiuwenclaw.gateway.query_url_obs import QueryUrlOSMS

    return QueryUrlOSMS()


async def _delete_obs_url(
    url: str,
    *,
    session_id: str,
    reason: str,
) -> bool:
    """Delete an OBS object via OSMS. Failures are logged; caller decides whether to continue."""
    obs_url = str(url or "").strip()
    if not obs_url:
        return False
    try:
        deleted = bool(await _create_query_url_obs().invoking_osms_delete(obs_url))
    except Exception:
        logger.exception(
            "[VibeSkillChannel] Failed to delete OBS object: session_id=%s reason=%s url=%s",
            session_id,
            reason,
            obs_url,
        )
        return False
    if deleted:
        logger.info(
            "[VibeSkillChannel] Deleted OBS object: session_id=%s reason=%s url=%s",
            session_id,
            reason,
            obs_url,
        )
    else:
        logger.warning(
            "[VibeSkillChannel] OSMS delete returned false: session_id=%s reason=%s url=%s",
            session_id,
            reason,
            obs_url,
        )
    return deleted


def _configure_interface_log_path() -> None:
    """若设置环境变量 `INTERFACE_LOG_PATH`，写入独立接口日志文件。"""
    interface_info.configure_interface_log_path()


def _interface_log_severity_for_status(status: int | None) -> str:
    if status is not None and status >= 500:
        return "ERROR"
    if status is not None and status >= 400:
        return "WARN"
    return "INFO"


def _log_interface_event(
    *,
    severity: str,
    session_id: str | None = None,
    source: str | None = None,
    destination: str | None = None,
    interface_type: str,
    interface_name: str | None = None,
    http_url: str | None = None,
    http_status: int | None = None,
    ws_event: str | None = None,
    ws_result: str | None = None,
    response_time_ms: int | None = None,
) -> None:
    """Compatibility facade for the fixed-schema interface logger."""
    resolved_interface_name = interface_name
    if (
        resolved_interface_name is None
        and str(interface_type or "").upper() == "HTTP"
        and http_url
    ):
        method, _, path = str(http_url).partition(" ")
        resolved_interface_name = _http_interface_name(path, method)
    interface_info.log_interface_event(
        severity=severity,
        session_id=session_id,
        source=source,
        destination=destination,
        interface_type=interface_type,
        interface_name=resolved_interface_name,
        http_url=http_url,
        http_status=http_status,
        ws_event=ws_event,
        ws_result=ws_result,
    )


def _interface_response_time_ms(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))


def _extract_session_id_from_http_path(path: str) -> str:
    request_path = urlparse(str(path or "")).path
    if request_path.startswith("/api/v1/session/"):
        rest = request_path[len("/api/v1/session/"):].strip("/")
        return rest.split("/")[0] if rest else ""
    return ""


def _http_interface_name(path: str, method: str) -> str:
    """Map VibeSkill REST route to the fixed HTTP interface name column."""
    request_path = urlparse(str(path or "")).path.rstrip("/") or "/"
    meth_u = str(method or "").strip().upper()
    if request_path == "/api/v1/session" and meth_u == "POST":
        return "SessionCreate"
    if not request_path.startswith("/api/v1/session/"):
        return ""

    rest = request_path[len("/api/v1/session/"):].strip("/")
    parts = [part for part in rest.split("/") if part]
    if not parts:
        return ""
    if len(parts) == 1:
        if meth_u == "GET":
            return "SessionQuery"
        if meth_u == "DELETE":
            return "SessionDelete"
        return ""

    tail = parts[1:]
    if tail == ["abort"] and meth_u == "POST":
        return "SessionAbort"
    if tail == ["messages"] and meth_u == "GET":
        return "SessionRestore"
    if tail == ["file"] and meth_u == "GET":
        return "FileList"
    if tail == ["file", "content"] and meth_u == "GET":
        return "FileRead"
    if tail == ["file", "content"] and meth_u == "PUT":
        return "FileWrite"
    if tail == ["attachments"] and meth_u == "DELETE":
        return "AttachmentDelete"
    if tail == ["export"] and meth_u == "POST":
        return "SkillExport"
    return ""


_ATTACHMENT_DELETE_TYPES = frozenset(
    {"file", "skill", "toolDefinition", "agentDefinition", "cliDefinition"}
)


def validate_attachment_delete_items(
    items: Any,
) -> tuple[str | None, list[dict[str, Any]] | None]:
    """Validate DELETE /attachments request body ``items``.

    Returns ``(None, normalized_items)`` on success, or ``(error, None)`` on failure.
    """
    if not isinstance(items, list) or not items:
        return "items must be a non-empty array", None

    normalized: list[dict[str, Any]] = []
    for idx, raw in enumerate(items):
        if not isinstance(raw, dict):
            return f"items[{idx}] must be an object", None
        item_type = str(raw.get("type") or "").strip()
        if item_type not in _ATTACHMENT_DELETE_TYPES:
            return (
                f"items[{idx}].type must be one of "
                f"{', '.join(sorted(_ATTACHMENT_DELETE_TYPES))}",
                None,
            )
        item: dict[str, Any] = {"type": item_type}
        if item_type in {"file", "skill"}:
            filename = str(raw.get("filename") or raw.get("name") or "").strip()
            if not filename:
                return f"items[{idx}] requires filename", None
            contains_unsafe_segment = any(
                segment in filename for segment in ("/", "\\", "..")
            )
            if contains_unsafe_segment or filename == ".":
                return f"items[{idx}].filename is illegal", None
            item["filename"] = filename
        elif item_type == "toolDefinition":
            plugin_id = str(
                raw.get("pluginId") or raw.get("bundleName") or raw.get("plugin_id") or ""
            ).strip()
            tool_name = str(raw.get("toolName") or raw.get("name") or "").strip()
            if not plugin_id or not tool_name:
                return f"items[{idx}] requires pluginId/bundleName and toolName", None
            item["pluginId"] = plugin_id
            item["toolName"] = tool_name
        elif item_type == "agentDefinition":
            agent_id = str(raw.get("agentId") or raw.get("agent_id") or "").strip()
            if not agent_id:
                return f"items[{idx}] requires agentId", None
            item["agentId"] = agent_id
        else:  # cliDefinition
            name = str(raw.get("name") or "").strip()
            if not name:
                return f"items[{idx}] requires name", None
            item["name"] = name
        normalized.append(item)
    return None, normalized


def _http_response_log_context(path: str, method: str) -> tuple[str, str]:
    """返回 HTTP 响应主日志用的 (session_id, path)。"""
    request_path = urlparse(str(path or "")).path
    meth_u = (method or "").strip().upper() or "?"
    if request_path == "/api/v1/session" and meth_u == "POST":
        sid_resp = "n/a"
    elif request_path.startswith("/api/v1/session/"):
        rest = request_path[len("/api/v1/session/"):].strip("/")
        sid_resp = rest.split("/")[0] if rest else "n/a"
    else:
        sid_resp = "n/a"
    path_for_log = str(path or "")
    if len(path_for_log) > 512:
        path_for_log = f"{path_for_log[:512]}...<truncated>"
    return sid_resp, path_for_log


def _extract_session_id_from_http_response(
    path: str,
    method: str,
    body: bytes,
) -> str:
    request_path = urlparse(str(path or "")).path
    if request_path != "/api/v1/session" or str(method or "").upper() != "POST":
        return ""
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("sessionID") or payload.get("sessionId") or "").strip()


def _ws_close_code(ws: Any) -> int | None:
    raw = getattr(ws, "close_code", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _ws_closed_ok(ws: Any, *, had_error: bool) -> bool:
    if had_error:
        return False
    return _ws_close_code(ws) in (1000, 1001)


_configure_interface_log_path()

_IMPORT_TYPE_VIBE = "vibeImport"
_IMPORT_TYPE_DIRECT = "directImport"
_VALID_MESSAGE_SEND_IMPORT_TYPES = frozenset({_IMPORT_TYPE_VIBE, _IMPORT_TYPE_DIRECT})

# chat.error 的 error payload 可能极大（例如 harness adapter 将整段 model response dump 进 error），
# 对外发送前截断，避免把 WS 写爆 / 撑爆前端展示。
_CHAT_ERROR_MAX_TEXT_LEN = 4096
_CHAT_ERROR_TRUNCATION_SUFFIX = "...(truncated)"

# chat.interrupt_result 在历史代码里曾被以 "chat.cancel" 字符串匹配，但 AgentServer 实际下发的
# event_type 始终是 "chat.interrupt_result"（intent in cancel/pause/resume/supplement）。为避免
# 行为悄悄退化，这里把两种命名都视为同一事件并集中处理。
_CHAT_INTERRUPT_RESULT_EVENT_TYPES = frozenset({"chat.interrupt_result", "chat.cancel"})
_OPENABILITY_BACKEND_INTERRUPTED_ERROR = "后端网络连接中断，稍等片刻再试试呢"

# skilldev.confirm_request 中映射为 review.asked 的 confirm_type 集合
_SKILLDEV_REVIEW_CONFIRM_TYPES = frozenset({"review", "static_review", "combined_review"})

# token 级流式 skilldev 事件：outbound_intercept 接收日志降为 DEBUG，避免热路径 INFO 刷屏
_SKILLDEV_STREAM_EVENT_TYPES = frozenset({
    "skilldev.agent_thinking",
    "skilldev.agent_output",
})

# Gateway 内部消费的 skilldev 事件：不向客户端转发，在 send() 入口统一处理（含无 WS 场景）
_SKILLDEV_GATEWAY_INTERNAL_EVENT_TYPES = frozenset({
    "skilldev.gate_obs_cleanup",
})


def _resolve_message_send_import_type(data: dict[str, Any]) -> str:
    """message.send 的 importType，默认 vibeImport。"""
    raw = data.get("importType")
    if raw is None:
        return _IMPORT_TYPE_VIBE
    value = str(raw).strip()
    if value in _VALID_MESSAGE_SEND_IMPORT_TYPES:
        return value
    if value:
        logger.warning(
            "[VibeSkillChannel] invalid importType=%r, using %s",
            raw,
            _IMPORT_TYPE_VIBE,
        )
    return _IMPORT_TYPE_VIBE


class _AiohttpWebSocketAdapter:
    """将 aiohttp ``WebSocketResponse`` 适配为 websockets 风格的连接对象。

    `_handle_ws_connection` 及相关方法以 websockets 的接口编写
    （``async for raw in ws`` / ``ws.send`` / ``ws.close`` / ``ws.request`` /
    ``ws.closed``）。本适配器让统一端口（aiohttp）下的业务逻辑无需改动。

    注意：实例需可哈希（作为 ``_session_to_ws`` 等字典的 key），默认对象身份哈希即满足。
    """

    def __init__(self, ws: Any, request: Any) -> None:
        self._ws = ws
        # request 暴露 .path / .query_string / .headers，与 _handle_ws_connection 约定一致
        self.request = request
        self.remote_address = getattr(request, "remote", None) or "unknown"
        self.close_code: int | None = None
        self.close_reason: str | None = None

    @property
    def closed(self) -> bool:
        return bool(getattr(self._ws, "closed", False))

    async def send(self, data: str) -> None:
        await self._ws.send_str(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason
        try:
            await self._ws.close(code=code, message=str(reason).encode("utf-8"))
        except Exception:
            logger.debug("[VibeSkillChannel] aiohttp ws close failed", exc_info=True)

    def __aiter__(self) -> "_AiohttpWebSocketAdapter":
        return self

    async def __anext__(self) -> str:
        from aiohttp import WSMsgType

        msg = await self._ws.receive()
        if msg.type == WSMsgType.TEXT:
            return msg.data
        if msg.type == WSMsgType.BINARY:
            return msg.data.decode("utf-8", errors="replace")
        self.close_code = getattr(self._ws, "close_code", None)
        if msg.type == WSMsgType.ERROR:
            exc = self._ws.exception()
            self.close_reason = str(exc) if exc is not None else "ws_error"
        # CLOSE / CLOSING / CLOSED / ERROR -> 结束迭代
        raise StopAsyncIteration


@dataclass
class VibeSkillConfig:
    """VibeSkill Channel 配置。

    端口策略：
    - 默认 **统一端口**（``port``，默认 19003）：HTTP 与 WebSocket 共用同一 aiohttp Server，
      主要由路径区分协议（``Upgrade`` 仅作为 WebSocket 握手校验）。
    - 兼容 **双端口**：当显式设置 ``http_port`` / ``ws_port`` 且二者不同（或与 ``port`` 不一致）时，
      回退到旧的「HTTP(asyncio) + WS(websockets) 双监听」实现，便于灰度回滚。
    """

    enabled: bool = True
    channel_id: str = VIBESKILL_CHANNEL_ID
    default_session_id: str = "vibeskill_session"
    # 统一监听端口（默认 19003）；HTTP 与 WebSocket 共用
    port: int = 19003
    # 兼容字段：显式设置后回退到双端口模式（废弃，后续大版本移除）
    http_port: int | None = None
    ws_port: int | None = None
    # 单 gateway 内 VibeSkill message.send busy session 限流；None/<=0 表示不限流。
    max_busy_sessions_user: int | None = None
    max_busy_sessions: int | None = None

    def resolved_http_port(self) -> int:
        return self.http_port if self.http_port is not None else self.port

    def resolved_ws_port(self) -> int:
        return self.ws_port if self.ws_port is not None else self.port

    def unified_port_enabled(self) -> bool:
        """是否启用单端口统一模式。

        仅当未显式指定双端口、或显式指定的两个端口与统一端口完全一致时为 True。
        """
        http_p = self.resolved_http_port()
        ws_p = self.resolved_ws_port()
        return http_p == ws_p


class _TimelineReplayStore:
    """No-op persistence boundary for history-only event conversion."""

    async def append_file_ready_obs_url(self, session_id: str, url: str) -> None:
        return None

    async def set_state(
        self,
        session_id: str,
        state: VibeSkillSessionState,
    ) -> None:
        return None

    async def set_exportable(self, session_id: str, exportable: bool) -> None:
        return None


class VibeSkillChannel(BaseChannel):
    """VibeSkill Channel.

    基于 BaseChannel 实现，统一接入 MessageHandler 总线。
    - HTTP Server：独立处理 REST 请求（不走 GatewayServer websockets）
    - WebSocket Server：独立处理 WebSocket 连接（不走 GatewayServer）
    """

    name = VIBESKILL_CHANNEL_ID

    def __init__(
        self,
        config: VibeSkillConfig,
        router,
        agent_client,
    ) -> None:
        super().__init__(config, router)
        self.config: VibeSkillConfig = config
        self._agent_client = agent_client
        _configure_interface_log_path()
        self._dcs_store: VibeSkillSessionDcsStore | None = VibeSkillSessionDcsStore.from_env()
        if self._dcs_store is not None:
            logger.info(
                "[VibeSkillChannel] DCS session persistence enabled (host=%s)",
                self._dcs_store.host,
            )
        else:
            logger.info(
                "[VibeSkillChannel] DCS session persistence disabled (SANDBOX_DCS_HOST not set)"
            )
        self._store = VibeSkillSessionStore(dcs_store=self._dcs_store)
        self._ws_sessions: dict[Any, set[str]] = {}  # ws -> set of session_ids
        self._session_to_ws: dict[str, Any] = {}  # session_id -> ws
        self._ws_sessions_lock = asyncio.Lock()
        self._clients: set[Any] = set()
        self._ws_skip_cancel_on_disconnect: set[Any] = set()
        self._on_message_cb: Callable[[Message], Any] | None = None
        self._http_server: asyncio.Server | None = None
        self._ws_server: Any | None = None
        # 统一端口模式（aiohttp）相关
        self._aiohttp_runner: Any | None = None
        self._aiohttp_site: Any | None = None
        self._ws_heartbeat_tasks: dict[Any, asyncio.Task] = {}
        self._message_ctx: dict[str, dict[str, Any]] = {}
        # question.replied 清空 _message_ctx 后，进行中的 tool 结果仍须落到 tool_call 时的 messageID/partID
        self._detached_tool_parts: dict[str, dict[str, dict[str, Any]]] = {}
        self._pending_confirms: dict[str, dict[str, Any]] = {}
        self._listen_host = self._get_local_ip()
        
        # 加载 AK/SK 鉴权配置
        auth_val = os.environ.get("JIUWEN_CLAW_AUTH_ENABLED") or os.environ.get("HTTP_WS_AUTH_ENABLED", "false")
        self._auth_enabled = auth_val.lower() == "true"
        logger.info("[Auth] init: auth_enabled=%s", self._auth_enabled)

    def on_message(self, callback: Callable[[Message], Any]) -> None:
        self._on_message_cb = callback

    @property
    def channel_id(self) -> str:
        return str(self.config.channel_id or self.name).strip() or self.name

    def _deliver_to_message_handler(self, msg: Message) -> None:
        interface_info.register_message(msg.id, session_id=msg.session_id or "")
        channel_manager = cast("ChannelManager", self.bus)
        channel_manager.deliver_to_message_handler(msg)

    @staticmethod
    def _effective_busy_limit(limit: int | None) -> int | None:
        if limit is None or limit <= 0:
            return None
        return limit

    @staticmethod
    def _busy_session_owner_id(session: VibeSkillSession) -> str:
        user_id = str((session.metadata or {}).get("user_id") or "").strip()
        return user_id or str(session.session_id or "").strip()

    async def _busy_limit_error_for_message_send(
        self,
        *,
        session: VibeSkillSession,
        user_id: str | None,
    ) -> str | None:
        user_limit = self._effective_busy_limit(self.config.max_busy_sessions_user)
        gateway_limit = self._effective_busy_limit(self.config.max_busy_sessions)
        if user_limit is None and gateway_limit is None:
            return None

        current_user_id = str(user_id or "").strip() or str(session.session_id or "").strip()
        sessions = await self._store.list_sessions()
        busy_sessions = [
            item for item in sessions
            if item.state == VibeSkillSessionState.BUSY
        ]

        if user_limit is not None:
            user_busy_count = sum(
                1
                for item in busy_sessions
                if self._busy_session_owner_id(item) == current_user_id
            )
            if user_busy_count >= user_limit:
                logger.warning(
                    "[VibeSkillChannel] busy session user limit exceeded: "
                    "user_id=%s session_id=%s busy_count=%d limit=%d",
                    current_user_id,
                    session.session_id,
                    user_busy_count,
                    user_limit,
                )
                return f"用户最多可同时运行{user_limit}个skill任务"

        if gateway_limit is not None and len(busy_sessions) >= gateway_limit:
            logger.warning(
                "[VibeSkillChannel] busy session gateway limit exceeded: "
                "session_id=%s busy_count=%d limit=%d",
                session.session_id,
                len(busy_sessions),
                gateway_limit,
            )
            return "服务端繁忙，请稍后再试"

        return None

    async def _send_message_send_busy_limit_error(
        self,
        ws: Any,
        data: dict[str, Any],
        *,
        session: VibeSkillSession,
        external_sid: str | None,
        error_text: str,
    ) -> None:
        for response in self._build_error_responses(
            session.session_id,
            external_sid,
            error_text,
        ):
            await self._send_ws_json(
                ws,
                response,
                source="message.send.busy_limit",
            )
        await self._send_ws_res_error(
            ws,
            data,
            error_text,
            source="message.send.busy_limit_ack",
        )

    def _is_openability_reconnecting(self, session: VibeSkillSession) -> bool:
        checker = getattr(self._agent_client, "is_openability_reconnecting", None)
        if not callable(checker):
            return False
        try:
            return bool(
                checker(
                    user_id=self._session_user_id(session.session_id),
                    session_id=session.session_id,
                )
            )
        except Exception:
            logger.exception(
                "[VibeSkillChannel] is_openability_reconnecting failed: session_id=%s",
                session.session_id,
            )
            return False

    async def _send_message_send_openability_error(
        self,
        ws: Any,
        data: dict[str, Any],
        *,
        session: VibeSkillSession,
        external_sid: str | None,
    ) -> None:
        try:
            await self._store.set_state(session.session_id, VibeSkillSessionState.IDLE)
            logger.info(
                "[VibeSkillChannel] session state -> idle, source=message.send.openability_disconnected, session_id=%s",
                session.session_id,
            )
        except Exception:
            logger.exception(
                "[VibeSkillChannel] set_state failed for OA disconnect error, session_id=%s",
                session.session_id,
            )

        for response in self._build_error_responses(
            session.session_id,
            external_sid,
            _OPENABILITY_BACKEND_INTERRUPTED_ERROR,
        ):
            await self._send_ws_json(
                ws,
                response,
                source="message.send.openability_disconnected",
            )
        await self._send_ws_res_error(
            ws,
            data,
            _OPENABILITY_BACKEND_INTERRUPTED_ERROR,
            source="message.send.openability_disconnected_ack",
        )

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        if self.config.unified_port_enabled():
            await self._start_unified()
        else:
            await self._start_dual_port()

    async def _start_unified(self) -> None:
        """统一端口模式：HTTP 与 WebSocket 共用同一 aiohttp Server。"""
        from aiohttp import web

        app = self._build_aiohttp_app()
        runner = web.AppRunner(app)
        await runner.setup()
        port = self.config.port
        site = web.TCPSite(runner, self._listen_host, port)
        await site.start()
        self._aiohttp_runner = runner
        self._aiohttp_site = site
        logger.info(
            "[VibeSkillChannel] unified server started: "
            "http://%s:%d/api/v1 (REST) + ws://%s:%d/api/v1/messages (WebSocket)",
            self._listen_host,
            port,
            self._listen_host,
            port,
        )

    async def _start_dual_port(self) -> None:
        """兼容模式：HTTP(asyncio) + WebSocket(websockets) 双端口监听。"""
        http_port = self.config.resolved_http_port()
        ws_port = self.config.resolved_ws_port()

        # 启动独立 HTTP 服务器处理 VibeSkill REST 请求
        self._http_server = await asyncio.start_server(
            self._handle_http_connection,
            self._listen_host,
            http_port,
        )
        logger.info(
            "[VibeSkillChannel] HTTP server started: http://%s:%d/api/v1",
            self._listen_host,
            http_port,
        )

        # 启动独立 WebSocket 服务器
        import websockets
        self._ws_server = await websockets.serve(
            self._handle_ws_connection,
            self._listen_host,
            ws_port,
            ping_interval=20,
            ping_timeout=60,
        )
        logger.info(
            "[VibeSkillChannel] WebSocket server started: ws://%s:%d/api/v1/messages",
            self._listen_host,
            ws_port,
        )

    def _build_aiohttp_app(self) -> Any:
        """构造统一端口 aiohttp Application（REST + WebSocket 路由）。"""
        from aiohttp import web

        app = web.Application()
        # WebSocket 入口（仅 GET + Upgrade）
        app.router.add_get("/api/v1/messages", self._aiohttp_ws_handler)
        # 其余 REST 接口，复用现有 http_handler 路由表
        app.router.add_route("*", "/api/v1/{tail:.*}", self._aiohttp_http_handler)
        return app

    async def _aiohttp_http_handler(self, request: Any) -> Any:
        """aiohttp REST 适配层：Request -> 现有 http_handler()。"""
        from aiohttp import web

        method = request.method
        path = request.path_qs  # 含 query string，与 http_handler 的 urlparse 约定一致
        http_url = f"{str(method or '').upper()} {path}"
        interface_name = _http_interface_name(path, method)
        session_id = _extract_session_id_from_http_path(path)
        timing_id = f"http-{int(time.time() * 1000):x}-{secrets.token_hex(4)}"
        with interface_info.inbound_context(
            session_id=session_id,
            interface_type="HTTP",
            ws_event="",
        ):
            interface_info.register_request(
                timing_id,
                session_id=session_id,
                interface_type="HTTP",
                interface_name=interface_name,
                http_url=http_url,
            )
            try:
                with interface_info.bind_request(timing_id):
                    headers = {k.lower(): v for k, v in request.headers.items()}
                    body = await request.read()
                    status, resp_headers, resp_body = await self.http_handler(
                        method, path, headers, body
                    )
            except Exception:
                interface_info.finish_request(
                    timing_id,
                    severity="ERROR",
                    http_status=500,
                )
                raise

        session_id = (
            session_id
            or _extract_session_id_from_http_response(path, method, resp_body)
        )
        interface_info.update_request_session(timing_id, session_id)
        interface_info.mark_channel_final_response_sent(timing_id)
        interface_info.finish_request(
            timing_id,
            severity=_interface_log_severity_for_status(status),
            http_status=status,
        )
        _sid_resp, _path_resp_log = _http_response_log_context(path, method)
        logger.info(
            "[VibeSkillChannel] HTTP 响应已发送, status=%s session_id=%s path=%s",
            status,
            _sid_resp,
            _path_resp_log,
        )
        # Connection 头由 aiohttp 自行管理，避免与 keep-alive 冲突
        resp_headers = {
            k: v for k, v in (resp_headers or {}).items() if k.lower() != "connection"
        }
        return web.Response(status=status, headers=resp_headers, body=resp_body)

    async def _aiohttp_ws_handler(self, request: Any) -> Any:
        """aiohttp WebSocket 适配层：Request -> 现有 _handle_ws_connection()。"""
        from aiohttp import web

        # 非 WebSocket 升级请求打到 /api/v1/messages 时，明确返回 426
        upgrade = str(request.headers.get("Upgrade", "")).strip().lower()
        if upgrade != "websocket":
            path = getattr(request, "path_qs", None) or getattr(request, "path", "/api/v1/messages")
            method = str(getattr(request, "method", "GET") or "GET").upper()
            timing_id = f"http-{int(time.time() * 1000):x}-{secrets.token_hex(4)}"
            interface_info.register_request(
                timing_id,
                interface_type="HTTP",
                interface_name=_http_interface_name(path, method),
                http_url=f"{method} {path}",
            )
            interface_info.mark_channel_final_response_sent(timing_id)
            interface_info.finish_request(
                timing_id,
                severity="WARN",
                http_status=426,
            )
            return web.Response(
                status=426,
                headers={"Content-Type": "application/json"},
                body=b'{"error": "Upgrade Required: use WebSocket"}',
            )

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        adapter = _AiohttpWebSocketAdapter(ws, request)
        await self._handle_ws_connection(adapter)
        return ws

    @staticmethod
    def _get_local_ip() -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                return str(sock.getsockname()[0] or "127.0.0.1")
        except OSError:
            return "127.0.0.1"

    async def stop(self) -> None:
        self._running = False
        # 统一端口模式：关闭 aiohttp site / runner
        if self._aiohttp_site is not None:
            try:
                await self._aiohttp_site.stop()
            except Exception:
                logger.exception("[VibeSkillChannel] aiohttp site stop failed")
            self._aiohttp_site = None
        if self._aiohttp_runner is not None:
            try:
                await self._aiohttp_runner.cleanup()
            except Exception:
                logger.exception("[VibeSkillChannel] aiohttp runner cleanup failed")
            self._aiohttp_runner = None
        # 双端口模式：关闭 asyncio HTTP / websockets server
        if self._http_server is not None:
            self._http_server.close()
            await self._http_server.wait_closed()
            self._http_server = None
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
            self._ws_server = None
        if self._dcs_store is not None:
            try:
                await self._dcs_store.close()
            except Exception:
                logger.exception("[VibeSkillChannel] DCS close failed")
            self._dcs_store = None

    async def _handle_ws_connection(self, ws: Any) -> None:
        """处理 WebSocket 连接。"""
        request = getattr(ws, 'request', None)
        if request is None:
            logger.warning("[VibeSkillChannel] No request object found")
            await ws.close(code=1008, reason="no request")
            _log_interface_event(
                severity="ERROR",
                interface_type="WebSocket",
                ws_event="close",
                ws_result="error",
            )
            return

        had_ws_error = False
        # aiohttp: request.path 是路径, request.query_string 是 query string
        path = getattr(request, 'path', '/')
        query_string = getattr(request, 'query_string', '')
        full_path = f"{path}?{query_string}" if query_string else path
        parsed = urlparse(full_path)
        request_path = parsed.path
        query_params = parse_qs(parsed.query or "")

        remote = getattr(ws, 'remote_address', 'unknown')
        logger.info(
            "[VibeSkillChannel] WS connection from %s, path=%s, query_string=%s",
            remote, path, query_string,
        )
        try:
            # 验证路径
            if request_path != "/api/v1/messages":
                logger.warning(f"[VibeSkillChannel] Invalid path: {request_path}")
                had_ws_error = True
                await ws.close(code=1008, reason="unsupported path")
                return

            # AK/SK 鉴权
            if self._auth_enabled:
                from jiuwenclaw.gateway.auth import check_ws_auth
                logger.info("[Auth] ws check start")
                ok, error_msg = check_ws_auth(self._auth_enabled, request.headers)
                if not ok:
                    logger.warning("[Auth] ws check fail: %s", error_msg)
                    had_ws_error = True
                    await ws.close(code=1008, reason="authentication failed")
                    return
                logger.info("[Auth] ws check ok")
                
            self._clients.add(ws)
            logger.info(f"[VibeSkillChannel] Clients: {len(self._clients)}")

            session_ids = [str(s).strip() for s in query_params.get("sessionID", []) if str(s).strip()]
            if session_ids:
                session_id = session_ids[0]
                session = await self._store.resolve_session(session_id)
                if not session:
                    logger.error(
                        "[VibeSkillChannel] WS connect session not found: session_id=%r remote=%s",
                        session_id,
                        remote,
                    )
                    self._clients.discard(ws)
                    had_ws_error = True
                    await ws.close(code=1008, reason="session_not_found")
                    return
                session_id = session.session_id
                async with self._ws_sessions_lock:
                    if ws not in self._ws_sessions:
                        self._ws_sessions[ws] = set()
                    self._ws_sessions[ws].add(session_id)
                    self._session_to_ws[session_id] = ws
            else:
                logger.error(
                    "[VibeSkillChannel] WS connect missing sessionID query: remote=%s",
                    remote,
                )

            await self._emit_ws_event(ws, "server.connected", {})
            logger.info("[VibeSkillChannel] server.connected sent")
            self._start_heartbeat_task(ws)

            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    raw_text = str(raw)
                    max_raw_length = 1000
                    if len(raw_text) > max_raw_length:
                        raw_for_log = f"{raw_text[:max_raw_length]}...<truncated>"
                    else:
                        raw_for_log = raw_text
                    logger.warning(
                        "[VibeSkillChannel] invalid inbound json, remote=%s, ws_close_code=%s, "
                        "ws_close_reason=%s, raw=%s",
                        remote,
                        getattr(ws, "close_code", None),
                        getattr(ws, "close_reason", None),
                        raw_for_log,
                    )
                    await self._send_ws_json(ws, {
                        "type": "res", "id": "", "ok": False, "error": "invalid json"
                    }, source="inbound.invalid_json")
                    _log_interface_event(
                        severity="WARN",
                        session_id=(
                            sorted(self._ws_sessions.get(ws, set()))[0]
                            if self._ws_sessions.get(ws)
                            else ""
                        ),
                        interface_type="WebSocket",
                        ws_event="invalid_json",
                        ws_result="error",
                    )
                    continue

                _inbound_sid = (
                    str(data.get("sessionID") or "").strip()
                    if isinstance(data, dict)
                    else ""
                )
                if not _inbound_sid:
                    _bound = self._ws_sessions.get(ws)
                    _inbound_sid = sorted(_bound)[0] if _bound else "n/a"
                logger.info(
                    "[VibeSkillChannel] WS 事件, type=%s, session_id=%s",
                    (
                        str(data.get("type") or "").strip()
                        if isinstance(data, dict)
                        else "(non-dict)"
                    )
                    or "(empty)",
                    _inbound_sid,
                )

                # 调用 inbound_intercept 处理入站消息。若请求进入 MessageHandler，
                # 接口日志延迟到最终响应，以便汇总跨层时间节点。
                _inbound_event = (
                    str(data.get("type") or "").strip()
                    if isinstance(data, dict)
                    else ""
                )
                try:
                    with interface_info.inbound_context(
                        session_id=_inbound_sid if _inbound_sid != "n/a" else "",
                        interface_type="WebSocket",
                        ws_event=_inbound_event,
                        ws=ws,
                    ) as timing_context:
                        handled = await self.inbound_intercept(ws, data)
                except Exception:
                    _log_interface_event(
                        severity="ERROR",
                        session_id=_inbound_sid if _inbound_sid != "n/a" else "",
                        interface_type="WebSocket",
                        ws_event=(
                            str(data.get("type") or "").strip()
                            if isinstance(data, dict)
                            else ""
                        ),
                        ws_result="error",
                    )
                    raise
                if not handled:
                    await self._send_ws_json(ws, {
                        "type": "res", "id": "", "ok": False, "error": "unhandled"
                    }, source="inbound.unhandled")
                if not timing_context.delivered:
                    _log_interface_event(
                        severity="INFO" if handled else "WARN",
                        session_id=_inbound_sid if _inbound_sid != "n/a" else "",
                        interface_type="WebSocket",
                        ws_event=_inbound_event,
                        ws_result="success" if handled else "error",
                    )
        except Exception as e:
            had_ws_error = True
            logger.exception(
                "[VibeSkillChannel] WS error: %s, type=%s, remote=%s, sessions=%s, "
                "ws_close_code=%s, ws_close_reason=%s",
                e,
                type(e).__name__,
                remote,
                sorted(self._ws_sessions.get(ws, set())),
                getattr(ws, "close_code", None),
                getattr(ws, "close_reason", None),
            )
        finally:
            bound_sessions = sorted(self._ws_sessions.get(ws, set()))
            close_session_id = bound_sessions[0] if bound_sessions else ""
            _closed_ok = _ws_closed_ok(ws, had_error=had_ws_error)
            interface_info.finish_requests_for_ws(
                ws,
                severity="INFO" if _closed_ok else "WARN",
                ws_result="disconnected" if _closed_ok else "error",
            )
            _log_interface_event(
                severity="INFO" if _closed_ok else "WARN",
                session_id=close_session_id,
                interface_type="WebSocket",
                ws_event="close",
                ws_result="success" if _closed_ok else "error",
            )
            self._clients.discard(ws)
            await self.cleanup(ws)
            logger.info(f"[VibeSkillChannel] WS disconnected, clients: {len(self._clients)}")

    async def _handle_http_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """处理 HTTP 连接（用于 REST 请求）。"""
        method = ""
        raw_path = ""
        timing_id = ""
        start = time.monotonic()
        try:
            # 读取请求行
            request_line = await reader.readline()
            if not request_line:
                return

            request_line_str = request_line.decode("utf-8", errors="replace").strip()
            parts = request_line_str.split(" ")
            if len(parts) < 2:
                return

            method = parts[0]
            raw_path = parts[1] if len(parts) > 1 else "/"
            start = time.monotonic()
            timing_id = f"http-{int(time.time() * 1000):x}-{secrets.token_hex(4)}"
            interface_info.register_request(
                timing_id,
                session_id=_extract_session_id_from_http_path(raw_path),
                interface_type="HTTP",
                interface_name=_http_interface_name(raw_path, method),
                http_url=f"{(method or '').strip().upper()} {raw_path}",
            )

            # 读取 headers
            headers: dict[str, str] = {}
            content_length = 0
            while True:
                line = await reader.readline()
                if not line or line == b"\r\n":
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if ":" in line_str:
                    key, value = line_str.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
                    if key.strip().lower() == "content-length":
                        try:
                            content_length = int(value.strip())
                        except ValueError:
                            pass

            # 读取 body
            body = b""
            if content_length > 0:
                body = await reader.readexactly(content_length)

            # 调用 http_handler 处理请求
            with interface_info.bind_request(timing_id):
                status, resp_headers, resp_body = await self.http_handler(
                    method, raw_path, headers, body
                )
            interface_info.update_request_session(
                timing_id,
                _extract_session_id_from_http_path(raw_path)
                or _extract_session_id_from_http_response(raw_path, method, resp_body),
            )

            _sid_resp, _path_resp_log = _http_response_log_context(raw_path, method)
            logger.info(
                "[VibeSkillChannel] HTTP 响应已发送, status=%s session_id=%s path=%s",
                status,
                _sid_resp,
                _path_resp_log,
            )

            # 发送响应
            resp_headers_str = "\r\n".join(f"{k}: {v}" for k, v in resp_headers.items())
            response = (
                f"HTTP/1.1 {status}\r\n"
                f"{resp_headers_str}\r\n"
                f"\r\n"
            )
            writer.write(response.encode("utf-8"))
            writer.write(resp_body)
            await writer.drain()
            interface_info.mark_channel_final_response_sent(timing_id)
            interface_info.finish_request(
                timing_id,
                severity=_interface_log_severity_for_status(status),
                http_status=status,
            )
        except Exception as e:
            logger.warning("[VibeSkillChannel] HTTP handler error: %s", e)
            if method and raw_path:
                if not interface_info.finish_request(
                    timing_id,
                    severity="ERROR",
                    http_status=500,
                ):
                    _log_interface_event(
                        severity="ERROR",
                        session_id=_extract_session_id_from_http_path(raw_path),
                        interface_type="HTTP",
                        interface_name=_http_interface_name(raw_path, method),
                        http_url=f"{(method or '').strip().upper()} {raw_path}",
                        response_time_ms=_interface_response_time_ms(start),
                        http_status=500,
                    )
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception as e:
                logger.debug("[VibeSkillChannel] Error closing writer: %s", e)

    async def _apply_outbound_session_state(self, msg: Message) -> None:
        """终态出站事件须更新 session store，与北向 WS 是否仍可投递无关。

        ``outbound_intercept`` 仅在 ``send()`` 找到可用 WS 时才会执行；skill 创建失败等场景下
        客户端可能已断连或映射已清理，若不在此处兜底置 idle，会残留 ghost busy session，
        进而触发 max_busy_sessions 限流。
        """
        if msg.type != "event" or not isinstance(msg.payload, dict):
            return

        session_id = str(msg.session_id or "").strip()
        if not session_id:
            return

        event_type = str(msg.payload.get("event_type") or "").strip()
        payload = msg.payload

        try:
            if event_type == "skilldev.error":
                await self._store.set_state(session_id, VibeSkillSessionState.IDLE)
                logger.info(
                    "[VibeSkillChannel] session state -> idle, source=skilldev.error(no_ws), session_id=%s",
                    session_id,
                )
                return

            if event_type == "skilldev.agent_completed":
                await self._store.set_state(session_id, VibeSkillSessionState.IDLE)
                await self._store.set_exportable(session_id, False)
                logger.info(
                    "[VibeSkillChannel] session state -> idle, source=skilldev.agent_completed(no_ws), session_id=%s",
                    session_id,
                )
                return

            if event_type == "skilldev.completed":
                await self._store.set_state(session_id, VibeSkillSessionState.COMPLETED)
                await self._store.set_exportable(session_id, True)
                logger.info(
                    "[VibeSkillChannel] session state -> completed, source=skilldev.completed(no_ws), session_id=%s",
                    session_id,
                )
                return

            if event_type == "chat.error":
                await self._store.set_state(session_id, VibeSkillSessionState.IDLE)
                logger.info(
                    "[VibeSkillChannel] session state -> idle, source=chat.error(no_ws), session_id=%s",
                    session_id,
                )
                return

            if event_type == "chat.final" or event_type in _CHAT_INTERRUPT_RESULT_EVENT_TYPES:
                session_obj = await self._store.get_session(session_id)
                if session_obj is None:
                    return
                intent = str(payload.get("intent") or "").strip().lower()
                is_terminal = event_type == "chat.final" or intent in ("", "cancel")
                if not is_terminal:
                    return
                if session_obj.mode == "Standard":
                    await self._store.set_state(session_id, VibeSkillSessionState.IDLE)
                    logger.info(
                        "[VibeSkillChannel] session state -> idle, source=%s(no_ws), session_id=%s",
                        event_type,
                        session_id,
                    )
                    return
                if (
                    session_obj.mode == "SkillCreate"
                    and event_type == "chat.final"
                    and await self._store.get_state(session_id) == VibeSkillSessionState.BUSY
                ):
                    await self._store.set_state(session_id, VibeSkillSessionState.IDLE)
                    logger.info(
                        "[VibeSkillChannel] session state -> idle, "
                        "source=chat.final.skillcreate_cancel(no_ws), session_id=%s",
                        session_id,
                    )
        except Exception:
            logger.exception(
                "[VibeSkillChannel] apply outbound session state failed, event_type=%s session_id=%s",
                event_type,
                session_id,
            )

    async def send(self, msg: Message) -> None:
        """ChannelManager 分发的出站消息，推送给对应的 WebSocket client。"""
        session_id = str(msg.session_id or "").strip()
        if not session_id:
            logger.warning("[VibeSkillChannel] send() called with empty session_id")
            return

        if msg.type == "event" and isinstance(msg.payload, dict):
            event_type = str(msg.payload.get("event_type") or "").strip()
            # Gateway MessageHandler 流结束时会补发 chat.processing_status；VibeSkill
            # 协议以 session.status(busy/idle/completed) 表达状态，此处直接丢弃。
            if event_type == "chat.processing_status":
                return
            if event_type in _SKILLDEV_GATEWAY_INTERNAL_EVENT_TYPES:
                await self._handle_skilldev_gateway_internal_event(
                    event_type,
                    msg.payload,
                    session_id,
                )
                return

        # 查找对应的 WebSocket
        ws = self._session_to_ws.get(session_id)
        if ws is None:
            logger.warning(
                f"[VibeSkillChannel] send() no ws found for session_id={session_id}, "
                f"req_method={msg.req_method.value if msg.req_method else None}"
            )
            await self._apply_outbound_session_state(msg)
            interface_info.finish_request(
                msg.id, severity="WARN", ws_result="disconnected"
            )
            return
        if bool(getattr(ws, "closed", False)):
            logger.warning(
                f"[VibeSkillChannel] send() ws already closed for session_id={session_id}, "
                f"req_method={msg.req_method.value if msg.req_method else None}"
            )
            await self._apply_outbound_session_state(msg)
            interface_info.finish_request(
                msg.id, severity="WARN", ws_result="disconnected"
            )
            return

        # ContextVar 让底层 ws.send 关联当前响应 request_id。
        try:
            with interface_info.bind_request(msg.id):
                await self._send_agent_message_to_ws(msg, ws, session_id)
        except Exception:
            interface_info.finish_request(
                msg.id, severity="ERROR", ws_result="error"
            )
            raise

    async def _send_agent_message_to_ws(
        self,
        msg: Message,
        ws: Any,
        session_id: str,
    ) -> None:
        handled = await self.outbound_intercept(msg, ws)
        if not handled:
            # fallback: 直接推送 chat.delta 或 chat.final
            if msg.type == "event" and isinstance(msg.payload, dict):
                event_type = str(msg.payload.get("event_type") or "").strip()
                if event_type == "chat.delta":
                    text = str(msg.payload.get("content") or "")
                    if text:
                        external_sid = await self._resolve_external_session_id(session_id, msg.metadata)
                        ctx = self._ensure_message_context(session_id)
                        part, _ = self._ensure_text_part(session_id, "text")
                        part["text"] = str(part.get("text") or "") + text
                        response = {
                            "type": "message.part.delta",
                            "properties": {
                                "sessionID": external_sid,
                                "messageID": ctx["message_id"],
                                "partID": part["id"],
                                "type": "text",
                                "text": text,
                            },
                        }
                        await self._send_ws_json(ws, response, source="fallback.chat.delta")
                elif event_type == "chat.final":
                    text = str(msg.payload.get("content") or "")
                    external_sid = await self._resolve_external_session_id(session_id, msg.metadata)
                    ctx = self._ensure_message_context(session_id)
                    part, _ = self._ensure_text_part(session_id, "text")
                    part["text"] = str(part.get("text") or "") + text
                    response = {
                        "type": "message.updated",
                        "properties": {
                            "info": {
                                "id": ctx["message_id"],
                                "sessionID": external_sid,
                                "role": "assistant",
                                "parts": self._serialize_parts(ctx["parts"], external_sid),
                            }
                        },
                    }
                    await self._send_ws_json(ws, response, source="fallback.chat.final")
                    await self._send_ws_json(
                        ws,
                        {"type": "task.completed", "properties": {}},
                        source="fallback.chat.final.completed",
                    )

        if self._is_terminal_agent_message(msg):
            interface_info.promote_last_channel_send_to_final(msg.id)
            payload = msg.payload if isinstance(msg.payload, dict) else {}
            event_type = str(payload.get("event_type") or "")
            interface_info.finish_request(
                msg.id,
                severity="ERROR" if event_type.endswith("error") else "INFO",
                ws_result=self._ws_result_for_terminal_message(msg),
            )

    @staticmethod
    def _is_terminal_agent_message(msg: Message) -> bool:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        event_type = str(payload.get("event_type") or "").strip()
        if event_type in {
            "chat.final",
            "chat.error",
            "skilldev.agent_completed",
            "skilldev.completed",
            "skilldev.error",
        }:
            return True
        return (
            event_type in _CHAT_INTERRUPT_RESULT_EVENT_TYPES
            and str(payload.get("intent") or "").strip().lower() in {"", "cancel"}
        )

    @staticmethod
    def _ws_result_for_terminal_message(msg: Message) -> str:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        event_type = str(payload.get("event_type") or "").strip()
        if event_type.endswith("error"):
            return "error"
        if (
            event_type in _CHAT_INTERRUPT_RESULT_EVENT_TYPES
            and str(payload.get("intent") or "").strip().lower() == "cancel"
        ):
            return "cancelled"
        return "success"

    async def inbound_intercept(self, ws: Any, data: dict[str, Any]) -> bool:
        """拦截 VibeSkill WebSocket 消息。

        处理 message.send，将其转换为 Message 送入 MessageHandler。
        """
        if not isinstance(data, dict):
            return False

        msg_type = str(data.get("type") or "").strip()

        if msg_type == "message.send":
            return await self._handle_message_send(ws, data)
        if msg_type == "question.replied":
            return await self._handle_question_replied(data)
        if msg_type == "review.replied":
            return await self._handle_review_replied(ws, data)
        if msg_type == "skillSearch.replied":
            return await self._handle_skill_search_replied(ws, data)

        return False

    @staticmethod
    def _message_send_file_part(
        file_info: dict[str, Any],
        *,
        resource_type: str | None = None,
    ) -> dict[str, Any]:
        """构造 message.send 的 file part（``url`` / ``innerurl`` 同级）。"""
        part: dict[str, Any] = {
            "type": "file",
            "filename": str(file_info.get("filename") or ""),
            "url": str(file_info.get("url") or ""),
            "innerurl": str(file_info.get("innerurl") or file_info.get("innerUrl") or ""),
            "mime": str(file_info.get("mime") or ""),
        }
        if resource_type:
            part["resourceType"] = resource_type
        return part

    def _extract_parts_to_skilldev_params(self, parts: list[Any]) -> dict[str, Any]:
        """从 message.send / skillSearch.replied 的 parts 提取 skilldev.chat params 字段。"""
        query = ""
        files: list[dict[str, Any]] = []
        skill_packages: list[dict[str, Any]] = []
        tools: list[dict[str, Any]] = []
        agent_definitions: list[dict[str, Any]] = []
        cli_definitions: list[dict[str, Any]] = []

        for part in parts:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "").strip()
            if part_type == "text":
                query += str(part.get("text") or "")
            elif part_type == "file":
                innerurl = str(part.get("innerurl") or part.get("innerUrl") or "")
                file_info = {
                    "filename": part.get("filename", ""),
                    "url": part.get("url", ""),
                    "innerurl": innerurl,
                    "mime": part.get("mime", ""),
                }
                resource_type = part.get("resourceType", "")
                if resource_type == "skill":
                    skill_packages.append({
                        "filename": part.get("filename", ""),
                        "url": part.get("url", ""),
                        "innerurl": innerurl,
                    })
                else:
                    files.append(file_info)
            elif part_type == "toolDefinition":
                tool_output_schema = part.get("outputSchema") or part.get("output_schema") or {}
                if not isinstance(tool_output_schema, dict):
                    tool_output_schema = {}
                tools.append({
                    "bundleName": part.get("bundleName") or part.get("pluginId") or "",
                    "pluginType": part.get("pluginType", ""),
                    "toolType": part.get("toolType", ""),
                    "toolName": part.get("toolName", ""),
                    "description": part.get("description", ""),
                    "arguments": part.get("arguments", {}),
                    "outputSchema": tool_output_schema,
                    "protocol": part.get("protocol", ""),
                    "deviceCommand": part.get("deviceCommand"),
                })
            elif part_type == "agentDefinition":
                parameters = part.get("parameters", {})
                if not isinstance(parameters, dict):
                    parameters = {}
                agent_output_schema = part.get("outputSchema") or part.get("output_schema") or {}
                if not isinstance(agent_output_schema, dict):
                    agent_output_schema = {}
                agent_definitions.append({
                    "agentId": str(part.get("agentId") or part.get("agent_id") or ""),
                    "name": str(part.get("name") or ""),
                    "description": str(part.get("description") or ""),
                    "parameters": parameters,
                    "outputSchema": agent_output_schema,
                })
            elif part_type == "cliDefinition":
                input_schema = part.get("inputSchema") or part.get("input_schema") or {}
                if not isinstance(input_schema, dict):
                    input_schema = {}
                output_schema = part.get("outputSchema") or part.get("output_schema") or {}
                if not isinstance(output_schema, dict):
                    output_schema = {}
                require_permissions = part.get("requirePermissions") or part.get("require_permissions") or []
                if not isinstance(require_permissions, list):
                    require_permissions = []
                cli_definitions.append({
                    "name": str(part.get("name") or ""),
                    "version": str(part.get("version") or ""),
                    "description": str(part.get("description") or ""),
                    "executeSide": str(part.get("executeSide") or part.get("execute_side") or ""),
                    "requirePermissions": require_permissions,
                    "inputSchema": input_schema,
                    "outputSchema": output_schema,
                })

        params: dict[str, Any] = {}
        if query:
            params["query"] = query
        if files:
            params["files"] = files
        if skill_packages:
            params["skill_packages"] = skill_packages
        if tools:
            params["tool_spec_files"] = tools
        if agent_definitions:
            params["agent_definitions"] = agent_definitions
        if cli_definitions:
            params["cli_definitions"] = cli_definitions
        return params

    async def _handle_message_send(self, ws: Any, data: dict[str, Any]) -> bool:
        """处理 message.send 类型的消息，封装为 skilldev.chat 并发送到 MessageHandler。"""
        session_id = str(data.get("sessionID") or "").strip()
        parts = data.get("parts")
        msg_model = data.get("model")
        agent = data.get("agent", "coder")
        system_prompt = data.get("system")
        request_id = f"vibeskill-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"

        # Resolve session first: sessionID is required (WS-bound session is a fallback).
        session: VibeSkillSession | None = None
        if session_id:
            session = await self._store.resolve_session(session_id)
            if not session:
                logger.error(
                    "[VibeSkillChannel] message.send session not found: session_id=%r request_id=%s",
                    session_id,
                    request_id,
                )
                await self._send_ws_res_error(
                    ws, data, "session_not_found", source="message.send.session_not_found"
                )
                return True
        else:
            bound = self._ws_sessions.get(ws)
            if bound:
                session = await self._store.get_session(sorted(bound)[0])
            if not session:
                logger.error(
                    "[VibeSkillChannel] message.send missing sessionID: request_id=%s",
                    request_id,
                )
                await self._send_ws_res_error(
                    ws, data, "missing_session_id", source="message.send.missing_session_id"
                )
                return True

        # Validate parts must be a list
        if not isinstance(parts, list):
            await self._send_ws_res_error(
                ws, data, "parts must be an array", source="message.send.invalid_parts_type"
            )
            return True

        if not parts:
            external_sid = (
                session_id
                or await self._resolve_external_session_id(session.session_id)
                or session.session_id
            )
            error_text = "parts must not be empty"
            for response in self._build_error_responses(
                session.session_id,
                external_sid,
                error_text,
            ):
                await self._send_ws_json(
                    ws,
                    response,
                    source="message.send.empty_parts",
                )
            await self._send_ws_res_error(
                ws,
                data,
                error_text,
                source="message.send.empty_parts_ack",
            )
            return True

        # Validate each file part (commit 73266ced: harden file endpoints)
        for part in parts:
            if not isinstance(part, dict):
                continue
            if str(part.get("type") or "").strip() != "file":
                continue
            err = validate_file_part(part)
            if err:
                logger.warning(
                    "[VibeSkillChannel] message.send rejected (file part), session_id=%s reason=%s",
                    session_id,
                    err,
                )
                await self._send_ws_res_error(
                    ws,
                    data,
                    err,
                    source="message.send.file_part_rejected",
                )
                return True

        # Validate model: optional, but if provided must be a dict containing
        # both providerID and modelID.
        if msg_model is not None:
            if not isinstance(msg_model, dict):
                await self._send_ws_res_error(
                    ws, data, "model must be an object", source="message.send.invalid_model_type"
                )
                return True

            if not msg_model.get("providerID"):
                await self._send_ws_res_error(
                    ws, data, "model.providerID is required", source="message.send.missing_model_provider_id"
                )
                return True

            if not msg_model.get("modelID"):
                await self._send_ws_res_error(
                    ws, data, "model.modelID is required", source="message.send.missing_model_id"
                )
                return True

        session_user_id = self._session_user_id(session.session_id)
        if session_user_id and not self._store.get_user_id(session.session_id):
            await self._store.set_metadata(session.session_id, {"user_id": session_user_id})

        external_sid = (
            session_id
            or await self._resolve_external_session_id(session.session_id)
            or session.session_id
        )
        if self._is_openability_reconnecting(session):
            await self._send_message_send_openability_error(
                ws,
                data,
                session=session,
                external_sid=external_sid,
            )
            return True

        busy_limit_error = await self._busy_limit_error_for_message_send(
            session=session,
            user_id=session_user_id,
        )
        if busy_limit_error is not None:
            await self._send_message_send_busy_limit_error(
                ws,
                data,
                session=session,
                external_sid=external_sid,
                error_text=busy_limit_error,
            )
            return True

        # 根据 session mode 路由
        if session.mode == "Standard":
            return await self._handle_chat_message(ws, data, session, session_id)

        # 新一轮 skilldev 执行使用新的 assistant message_id
        self._clear_message_context_for_session(session.session_id)
        self._clear_detached_tool_parts_for_session(session.session_id)

        await self._store.set_state(session.session_id, VibeSkillSessionState.BUSY)
        logger.info(
            "[VibeSkillChannel] session state -> busy, source=message.send.skillcreate, session_id=%s",
            session.session_id,
        )
        await self._emit_session_status(
            ws=ws,
            external_sid=external_sid,
            status_type=VibeSkillSessionState.BUSY.value,
        )

        async with self._ws_sessions_lock:
            if ws not in self._ws_sessions:
                self._ws_sessions[ws] = set()
            self._ws_sessions[ws].add(session.session_id)
            self._session_to_ws[session.session_id] = ws

        # 构建 skilldev.chat 格式的 params
        params: dict[str, Any] = {"task_id": session.session_id, **self._extract_parts_to_skilldev_params(parts)}
        # SkillCreate 与 Standard 一致，按 session 维度做租户隔离
        self._apply_tenant_service_id(
            params,
            session.session_id,
            client_session_id=session_id or None,
        )
        if "query" not in params:
            params["query"] = ""

        if "skillSearch" in data:
            params["enable_skill_search"] = bool(data.get("skillSearch"))

        params["import_type"] = _resolve_message_send_import_type(data)

        if "skillContext" in data:
            params["skill_context"] = data["skillContext"]

        # 可选字段
        inbound_agent_id = str(data.get("agent_id") or data.get("agentId") or "").strip()
        if inbound_agent_id:
            params["agent_id"] = inbound_agent_id

        # model
        if msg_model and isinstance(msg_model, dict):
            if msg_model.get("providerID"):
                params["provider_id"] = msg_model["providerID"]
            if msg_model.get("modelID"):
                params["model_id"] = msg_model["modelID"]

        if agent:
            params["agent"] = agent

        if system_prompt:
            params["system"] = system_prompt

        metadata_dict = {}
        if session_id:
            metadata_dict[_VIBESKILL_ORIGINAL_SESSION_ID_KEY] = session_id
        msg_metadata = metadata_dict if metadata_dict else None

        # 构建 Message 并直接发送到 MessageHandler
        msg = Message(
            id=request_id,
            type="req",
            channel_id=VIBESKILL_CHANNEL_ID,
            session_id=session.session_id,
            params=params,
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.SKILLDEV_CHAT,
            is_stream=True,
            metadata=msg_metadata,
            user_id=session_user_id,
        )

        logger.info(
            "[VibeSkillChannel] skilldev.chat sent, session_id=%s",
            session.session_id,
        )
        # 直接发送到 MessageHandler
        self._deliver_to_message_handler(msg)

        return True

    async def _handle_chat_message(
        self,
        ws: Any,
        data: dict[str, Any],
        session: VibeSkillSession,
        client_session_id: str | None,
    ) -> bool:
        """处理 Standard mode 的 chat 消息，走 jiuwenclaw 标准流程。

        通过 MessageHandler 发送 CHAT_SEND 请求到 AgentServer，
        响应通过 outbound_intercept 接收（chat.delta/chat.final）。
        """
        parts = data.get("parts", [])
        msg_model = data.get("model")
        agent = data.get("agent", "coder")
        request_id = f"vibeskill-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"

        # 提取 query (text parts)
        query = ""
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                query += str(part.get("text") or "")

        # 设置 session 状态
        await self._store.set_state(session.session_id, VibeSkillSessionState.BUSY)
        logger.info(
            "[VibeSkillChannel] session state -> busy, source=message.send.standard, session_id=%s",
            session.session_id,
        )
        await self._emit_session_status(
            ws=ws,
            external_sid=(
                client_session_id
                or await self._resolve_external_session_id(session.session_id)
                or session.session_id
            ),
            status_type=VibeSkillSessionState.BUSY.value,
        )

        async with self._ws_sessions_lock:
            if ws not in self._ws_sessions:
                self._ws_sessions[ws] = set()
            self._ws_sessions[ws].add(session.session_id)
            self._session_to_ws[session.session_id] = ws

        # 构建 chat.send 格式的 params
        params: dict[str, Any] = {
            "query": query,
        }
        # Standard 模式也按 session 维度做租户隔离，避免落到 default_service_id 共享工作区
        params["service_id"] = str(client_session_id or session.session_id).strip()

        # model
        if msg_model and isinstance(msg_model, dict):
            if msg_model.get("providerID"):
                params["provider_id"] = msg_model["providerID"]
            if msg_model.get("modelID"):
                params["model_id"] = msg_model["modelID"]

        if agent:
            params["agent"] = agent

        msg_metadata = (
            {_VIBESKILL_ORIGINAL_SESSION_ID_KEY: client_session_id}
            if client_session_id
            else None
        )

        # 构建 Message 并通过 MessageHandler 发送到 AgentServer
        msg = Message(
            id=request_id,
            type="req",
            channel_id=VIBESKILL_CHANNEL_ID,
            session_id=session.session_id,
            params=params,
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.CHAT_SEND,
            is_stream=True,
            metadata=msg_metadata,
            user_id=self._session_user_id(session.session_id),
        )

        self._deliver_to_message_handler(msg)

        return True

    async def _handle_question_replied(self, data: dict[str, Any]) -> bool:
        """处理 question.replied，回写到 AgentServer。

        - Standard mode（``chat.ask_user_question`` 发起）→ ``chat.user_answer``（CHAT_ANSWER）；
        - SkillCreate mode（``skilldev.ask_user_question`` 发起）→ ``skilldev.user_answer``。

        会话 mode 优先用 ``VibeSkillSession.mode``；该 mode 与 ``_pending_confirms`` 中
        登记的 ``dispatch`` 字段必须一致，不一致时记 warning 并以 session.mode 为准。
        """
        properties = data.get("properties") if isinstance(data.get("properties"), dict) else data
        if not isinstance(properties, dict):
            return False
        session_id = str(properties.get("sessionID") or "").strip()
        request_id = str(properties.get("requestID") or "").strip()
        raw_answers = properties.get("answers", [])
        if not request_id:
            return False

        if not session_id:
            return False

        pending = self._pending_confirms.pop(request_id, None)
        self._snapshot_detached_tool_parts(session_id)
        self._clear_message_context_for_session(session_id)

        answers: list[dict[str, Any]] = []
        if isinstance(raw_answers, list):
            for item in raw_answers:
                if isinstance(item, list):
                    answers.append({"selected_options": [str(x) for x in item]})
                else:
                    answers.append(
                        {"selected_options": [str(item)]} if item not in (None, "") else {"selected_options": []}
                    )

        session_obj = await self._store.get_session(session_id)
        session_mode = session_obj.mode if session_obj else None
        pending_dispatch = str((pending or {}).get("dispatch") or "").strip() if pending else ""
        if pending_dispatch and session_mode:
            expected = "chat" if session_mode == "Standard" else "skilldev"
            if pending_dispatch != expected:
                logger.warning(
                    "[VibeSkillChannel] question.replied dispatch mismatch, "
                    "session_mode=%s pending_dispatch=%s; using session_mode",
                    session_mode,
                    pending_dispatch,
                )

        if session_mode == "Standard":
            return self._dispatch_chat_user_answer(
                session_id=session_id,
                request_id=request_id,
                answers=answers,
            )

        return self._dispatch_skilldev_user_answer(
            session_id=session_id,
            request_id=request_id,
            answers=answers,
        )

    @staticmethod
    def _build_review_replied_skilldev_chat_query(accept: bool, feedback: str) -> str:
        """根据审阅结果组装 skilldev.chat 的 query。"""
        if accept:
            return "已对测试结果进行审阅，无需进行迭代优化，请进行下一步操作"
        query = "请根据测试结果以及反馈意见继续改进生成的skill，反馈意见如下"
        if feedback:
            query = f"{query}：{feedback}"
        return query

    async def _handle_review_replied(self, ws: Any, data: dict[str, Any]) -> bool:
        """处理 review.replied，封装为 skilldev.chat。"""
        properties = data.get("properties") if isinstance(data.get("properties"), dict) else data
        if not isinstance(properties, dict):
            return False
        session_id = str(properties.get("sessionID") or "").strip()
        request_id = str(properties.get("id") or "").strip()
        if not request_id:
            return False

        if not session_id:
            return False

        pending = self._pending_confirms.get(request_id, {})
        task_id = (
            str(pending.get("task_id") or "").strip()
            or str(pending.get("session_id") or "").strip()
            or session_id
        )
        accept = bool(properties.get("accept", False))
        feedback = str(properties.get("feedback") or "").strip()

        self._pending_confirms.pop(request_id, None)

        session_obj = await self._store.get_session(session_id)
        if session_obj and session_obj.mode == "Standard":
            return False

        self._clear_message_context_for_session(session_id)

        await self._store.set_state(session_id, VibeSkillSessionState.BUSY)
        logger.info(
            "[VibeSkillChannel] session state -> busy, source=review.replied, session_id=%s",
            session_id,
        )
        await self._emit_session_status(
            ws=ws,
            external_sid=session_id,
            status_type=VibeSkillSessionState.BUSY.value,
        )

        async with self._ws_sessions_lock:
            if ws not in self._ws_sessions:
                self._ws_sessions[ws] = set()
            self._ws_sessions[ws].add(session_id)
            self._session_to_ws[session_id] = ws

        params: dict[str, Any] = {
            "task_id": task_id,
            "query": self._build_review_replied_skilldev_chat_query(accept, feedback),
        }
        self._apply_tenant_service_id(params, session_id)

        msg = Message(
            id=f"vibeskill-review-{int(time.time() * 1000):x}-{secrets.token_hex(3)}",
            type="req",
            channel_id=VIBESKILL_CHANNEL_ID,
            session_id=session_id,
            params=params,
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.SKILLDEV_CHAT,
            is_stream=True,
            metadata={_VIBESKILL_ORIGINAL_SESSION_ID_KEY: session_id},
            user_id=self._session_user_id(session_id),
        )
        logger.info(
            "[VibeSkillChannel] skilldev.chat sent (review.replied), session_id=%s accept=%s",
            session_id,
            accept,
        )
        self._deliver_to_message_handler(msg)
        return True

    async def _handle_skill_search_replied(self, ws: Any, data: dict[str, Any]) -> bool:
        """处理 skillSearch.replied，封装为 skilldev.chat 并发送到 MessageHandler。"""
        properties = data.get("properties") if isinstance(data.get("properties"), dict) else data
        if not isinstance(properties, dict):
            return False
        session_id = str(properties.get("sessionID") or "").strip()
        action = str(properties.get("action") or "").strip().lower()
        if action not in ("ignore", "select"):
            return False

        parts = properties.get("parts", []) if isinstance(properties.get("parts"), list) else []
        request_id = f"vibeskill-skill-search-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"

        if not session_id:
            return False

        session_obj = await self._store.get_session(session_id)
        if session_obj and session_obj.mode == "Standard":
            return False

        self._clear_message_context_for_session(session_id)

        await self._store.set_state(session_id, VibeSkillSessionState.BUSY)
        logger.info(
            "[VibeSkillChannel] session state -> busy, source=skillSearch.replied, session_id=%s",
            session_id,
        )
        await self._emit_session_status(
            ws=ws,
            external_sid=session_id,
            status_type=VibeSkillSessionState.BUSY.value,
        )

        async with self._ws_sessions_lock:
            if ws not in self._ws_sessions:
                self._ws_sessions[ws] = set()
            self._ws_sessions[ws].add(session_id)
            self._session_to_ws[session_id] = ws

        params: dict[str, Any] = {
            "task_id": session_id,
            **self._extract_parts_to_skilldev_params(parts),
        }
        self._apply_tenant_service_id(params, session_id)
        if "query" not in params:
            params["query"] = ""

        if action == "select":
            skill = properties.get("skill")
            if isinstance(skill, dict) and skill:
                params["skill_searched"] = {
                    "skillId": str(skill.get("skillId") or skill.get("skill_id") or ""),
                    "skillName": str(skill.get("skillName") or skill.get("skill_name") or ""),
                    "url": str(skill.get("url") or ""),
                }

        msg = Message(
            id=request_id,
            type="req",
            channel_id=VIBESKILL_CHANNEL_ID,
            session_id=session_id,
            params=params,
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.SKILLDEV_CHAT,
            is_stream=True,
            metadata={_VIBESKILL_ORIGINAL_SESSION_ID_KEY: session_id},
            user_id=self._session_user_id(session_id),
        )
        logger.info(
            "[VibeSkillChannel] skilldev.chat sent (skillSearch.replied), session_id=%s action=%s",
            session_id,
            action,
        )
        self._deliver_to_message_handler(msg)
        return True

    async def outbound_intercept(self, msg: Message, ws: Any) -> bool:
        """拦截 AgentServer 出站消息，转换为 VibeSkill 流式事件。"""
        if msg.type != "event":
            return False

        payload = msg.payload
        if not isinstance(payload, dict):
            return False

        event_type = str(payload.get("event_type") or "").strip()
        external_sid = await self._resolve_external_session_id(msg.session_id, msg.metadata)

        # SkillDev 事件处理
        skilldev_events = self._get_skilldev_event_handlers()

        handler = skilldev_events.get(event_type)
        if handler:
            log_fn = logger.debug if event_type in _SKILLDEV_STREAM_EVENT_TYPES else logger.info
            log_fn(
                "[VibeSkillChannel] %s received, session_id=%s",
                event_type,
                str(msg.session_id or "").strip() or "n/a",
            )
            responses = await handler(payload, external_sid, msg.session_id)
            for response in responses:
                await self._send_ws_json(ws, response, source=f"skilldev.{event_type}")
            if event_type == "skilldev.agent_completed":
                # agent_completed 会主动关闭北向 WS。必须在 close 触发连接清理前
                # 固化最后一次成功发送时间并完成请求，否则终响列会缺失。
                interface_info.promote_last_channel_send_to_final(msg.id)
                interface_info.finish_request(
                    msg.id,
                    severity="INFO",
                    ws_result="success",
                )
                await self._disconnect_northbound_ws_after_agent_completed(msg.session_id, ws)
                return True
            if responses:
                return True
            return False

        # 通用 chat 事件处理
        #
        # 注意：``chat.cancel`` 在 AgentServer 端并不存在，真实事件名是 ``chat.interrupt_result``
        # （见 ``EventType.CHAT_INTERRUPT_RESULT``）。这里把两种命名都视为"取消/暂停/恢复"的统一
        # 信号，仅在 ``intent`` 表明任务确实结束（cancel）时将 Standard session 置 IDLE，避免把
        # pause/resume 也误判为任务结束。
        if event_type == "chat.final" or event_type in _CHAT_INTERRUPT_RESULT_EVENT_TYPES:
            intent = str(payload.get("intent") or "").strip().lower()
            is_terminal = (
                event_type == "chat.final"
                or intent in ("", "cancel")  # 无 intent 时按历史行为兜底置 idle
            )
            if msg.session_id and is_terminal:
                session_obj = await self._store.get_session(msg.session_id)
                if session_obj and session_obj.mode == "Standard":
                    await self._store.set_state(msg.session_id, VibeSkillSessionState.IDLE)
                    logger.info(
                        "[VibeSkillChannel] session state -> idle, source=%s, intent=%s, session_id=%s",
                        event_type,
                        intent or "n/a",
                        msg.session_id,
                    )

        if event_type == "chat.delta":
            text = str(payload.get("content") or "")
            if not text:
                return False
            ctx = self._ensure_message_context(msg.session_id)
            text_part, is_new = self._ensure_text_part(msg.session_id, "text")
            text_part["text"] = str(text_part.get("text") or "") + text

            if is_new:
                part_events: list[dict[str, Any]] = [{
                    "type": "message.part.updated",
                    "properties": self._serialize_part(text_part, external_sid),
                }]
            else:
                part_events = [{
                    "type": "message.part.delta",
                    "properties": {
                        "sessionID": external_sid,
                        "messageID": ctx.get("message_id"),
                        "partID": text_part.get("id"),
                        "type": "text",
                        "text": text,
                    },
                }]
            responses = self._prepend_message_announcement(ctx, external_sid, part_events)
            for response in responses:
                await self._send_ws_json(ws, response, source="chat.delta")
            return True

        if event_type == "chat.final":
            text = str(payload.get("content") or "")
            ctx = self._ensure_message_context(msg.session_id)
            text_part, _ = self._ensure_text_part(msg.session_id, "text")
            text_part["text"] = str(text_part.get("text") or "") + text
            response = {
                "type": "message.updated",
                "properties": {
                    "info": {
                        "id": ctx["message_id"],
                        "sessionID": external_sid,
                        "role": "assistant",
                        "parts": self._serialize_parts(ctx["parts"], external_sid),
                    }
                },
            }
            await self._send_ws_json(ws, response, source="chat.final")
            await self._send_ws_json(
                ws,
                {"type": "task.completed", "properties": {}},
                source="chat.final.completed",
            )
            return True

        if event_type == "chat.error":
            raw_error = payload.get("error") or payload.get("message") or "chat error"
            error_text = str(raw_error)
            if len(error_text) > _CHAT_ERROR_MAX_TEXT_LEN:
                error_text = (
                    error_text[:_CHAT_ERROR_MAX_TEXT_LEN] + _CHAT_ERROR_TRUNCATION_SUFFIX
                )

            if msg.session_id:
                try:
                    await self._store.set_state(msg.session_id, VibeSkillSessionState.IDLE)
                    logger.info(
                        "[VibeSkillChannel] session state -> idle, source=chat.error, session_id=%s",
                        msg.session_id,
                    )
                except Exception:
                    logger.exception(
                        "[VibeSkillChannel] set_state error for chat.error, session_id=%s",
                        msg.session_id,
                    )

            responses = self._build_error_responses(
                msg.session_id,
                external_sid,
                error_text,
                include_task_completed=True,
            )
            for response in responses:
                await self._send_ws_json(ws, response, source="chat.error")
            return True

        if event_type == "chat.tool_call":
            responses = await self._handle_chat_tool_call(payload, external_sid, msg.session_id)
            for response in responses:
                await self._send_ws_json(ws, response, source="chat.tool_call")
            return bool(responses)

        if event_type == "chat.tool_result":
            responses = await self._handle_chat_tool_result(payload, external_sid, msg.session_id)
            for response in responses:
                await self._send_ws_json(ws, response, source="chat.tool_result")
            return bool(responses)

        if event_type == "chat.ask_user_question":
            responses = await self._handle_chat_ask_user_question(
                payload, external_sid, msg.session_id,
            )
            for response in responses:
                await self._send_ws_json(ws, response, source="chat.ask_user_question")
            return bool(responses)

        return False

    def _get_skilldev_event_handlers(self) -> dict[str, Callable[[dict, str | None, str | None], Any]]:
        """共享的 skilldev 事件处理映射。"""
        return {
            "skilldev.skill_name_ready": self._handle_skilldev_skill_name_ready,
            "skilldev.agent_thinking": self._handle_skilldev_agent_thinking,
            "skilldev.agent_output": self._handle_skilldev_agent_output,
            "skilldev.tool_call": self._handle_skilldev_tool_call,
            "skilldev.tool_result": self._handle_skilldev_tool_result,
            "skilldev.file_ready": self._handle_skilldev_file_ready,
            "skilldev.todos_update": self._handle_skilldev_todos_update,
            "skilldev.confirm_request": self._handle_skilldev_confirm_request,
            "skilldev.ask_user_question": self._handle_skilldev_ask_user_question,
            "skilldev.search_results": self._handle_skilldev_search_results,
            "skilldev.error": self._handle_skilldev_error,
            "skilldev.agent_completed": self._handle_skilldev_agent_completed,
            "skilldev.completed": self._handle_skilldev_completed,
        }

    async def _handle_skilldev_gateway_internal_event(
        self,
        event_type: str,
        payload: dict,
        session_id: str,
    ) -> None:
        """处理 Gateway 内部 skilldev 事件（不转发北向 WS）。"""
        if event_type == "skilldev.gate_obs_cleanup":
            await self._handle_skilldev_gate_obs_cleanup(payload, session_id)
            return
        logger.warning(
            "[VibeSkillChannel] unhandled gateway internal skilldev event: event_type=%s session_id=%s",
            event_type,
            session_id,
        )

    async def _handle_skilldev_gate_obs_cleanup(
        self,
        payload: dict,
        session_id: str,
    ) -> None:
        """skilldev.gate_obs_cleanup — 沙箱通知 Gateway 删除临时 OBS 对象。"""
        url = str(payload.get("url") or "").strip()
        if not url:
            logger.warning(
                "[VibeSkillChannel] skilldev.gate_obs_cleanup missing url, session_id=%s",
                session_id,
            )
            return
        logger.info(
            "[VibeSkillChannel] skilldev.gate_obs_cleanup received, session_id=%s url=%s",
            session_id,
            url,
        )
        await _delete_obs_url(
            url,
            session_id=session_id,
            reason="gate_obs_cleanup",
        )

    async def _handle_skilldev_skill_name_ready(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
    ) -> list[dict]:
        """skilldev.skill_name_ready - 技能名就绪"""
        skill_name = str(payload.get("skill_name") or "").strip()
        if not skill_name:
            return []
        return [{
            "type": "session.updated",
            "properties": {
                "sessionID": external_sid,
                "title": skill_name,
            },
        }]

    async def _handle_skilldev_agent_thinking(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
    ) -> list[dict]:
        """skilldev.agent_thinking — 字段仅有 ``delta``，按与上一条流式种类是否同为 thinking 合并或新建 part。"""
        return self._build_skilldev_agent_delta_events(
            session_id=session_id,
            external_sid=external_sid,
            stream_kind="thinking",
            delta=str(payload.get("delta") or ""),
        )

    async def _handle_skilldev_agent_output(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
    ) -> list[dict]:
        """skilldev.agent_output — 字段仅有 ``delta``，按与上一条流式种类是否同为 output 合并或新建 part。"""
        return self._build_skilldev_agent_delta_events(
            session_id=session_id,
            external_sid=external_sid,
            stream_kind="output",
            delta=str(payload.get("delta") or ""),
        )

    async def _handle_skilldev_tool_call(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
    ) -> list[dict]:
        """skilldev.tool_call - 工具调用"""
        if not session_id:
            return []
        ctx = self._ensure_message_context(session_id)
        call_id = str(
            payload.get("tool_call_id")
            or payload.get("toolCallId")
            or payload.get("callID")
            or f"call_{secrets.token_hex(4)}"
        ).strip()
        tool_name = str(payload.get("tool_name") or payload.get("tool") or "").strip()
        tool_input = payload.get("arguments") or payload.get("params") or payload.get("input") or {}
        now_ms = int(time.time() * 1000)
        part, is_new = self._ensure_tool_part(session_id, call_id, tool_name)
        part["state"] = {
            "status": "running",
            "input": tool_input,
            "title": payload.get("title") or f"执行 {tool_name or call_id}",
            "metadata": payload.get("metadata", {}),
            "time": {"start": now_ms, "end": None},
        }
        responses = []
        # 第一次创建此 tool part 时，发 message.part.updated
        if is_new:
            responses.append(
                {
                    "type": "message.part.updated",
                    "properties": self._serialize_part(part, external_sid),
                }
            )
            ctx["_skilldev_stream_last_kind"] = "tool"
        return self._prepend_message_announcement(ctx, external_sid, responses)

    async def _handle_skilldev_tool_result(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
    ) -> list[dict]:
        """skilldev.tool_result - 工具结果"""
        if not session_id:
            return []
        call_id = str(
            payload.get("tool_call_id")
            or payload.get("toolCallId")
            or payload.get("callID")
            or payload.get("task_id")
            or f"call_{secrets.token_hex(4)}"
        ).strip()
        tool_name = str(payload.get("tool_name") or payload.get("tool") or "").strip()
        success = bool(payload.get("success", True))
        result = payload.get("result") or payload.get("output") or ""
        result_input = payload.get("arguments") or payload.get("params") or payload.get("input")
        now_ms = int(time.time() * 1000)

        detached = self._pop_detached_tool_part(session_id, call_id)
        if detached is not None:
            part = detached["part"]
            message_id = str(detached.get("message_id") or part.get("messageID") or "").strip()
            if message_id:
                part["messageID"] = message_id
            existing_time = part.get("state", {}).get("time", {})
            existing_start = existing_time.get("start") if isinstance(existing_time, dict) else None
            existing_input = part.get("state", {}).get("input")
            part["state"] = {
                "status": "completed" if success else "error",
                "input": result_input if result_input is not None else existing_input,
                "output": result,
                "title": payload.get("title") or f"{tool_name or call_id} 执行结果",
                "metadata": payload.get("metadata", {}),
                "time": {
                    "start": existing_start if existing_start is not None else now_ms,
                    "end": now_ms,
                },
            }
            return [{
                "type": "message.part.updated",
                "properties": self._serialize_part(part, external_sid),
            }]

        ctx = self._ensure_message_context(session_id)
        part, _ = self._ensure_tool_part(session_id, call_id, tool_name)
        existing_time = part.get("state", {}).get("time", {})
        existing_start = existing_time.get("start") if existing_time else None
        existing_input = part.get("state", {}).get("input")
        part["state"] = {
            "status": "completed" if success else "error",
            "input": result_input if result_input is not None else existing_input,
            "output": result,
            "title": payload.get("title") or f"{tool_name or call_id} 执行结果",
            "metadata": payload.get("metadata", {}),
            "time": {
                "start": existing_start if existing_start is not None else now_ms,
                "end": now_ms,
            },
        }
        responses = []
        # 与 tool_call 不同：结果到达时 part 往往已存在（is_new=False），
        # 仍须推送 message.part.updated，否则前端拿不到 state.output。
        responses.append(
            {
                "type": "message.part.updated",
                "properties": self._serialize_part(part, external_sid),
            }
        )
        ctx["_skilldev_stream_last_kind"] = "tool"
        return self._prepend_message_announcement(ctx, external_sid, responses)

    async def _handle_skilldev_file_ready(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
    ) -> list[dict]:
        """skilldev.file_ready — upload_file 成功后的文件 part（type=file）。"""
        if not session_id:
            return []
        file_info = payload.get("file")
        if not isinstance(file_info, dict):
            return []
        url = str(file_info.get("url") or "").strip()
        if not url:
            return []
        ctx = self._ensure_message_context(session_id)
        part = self._append_file_part(
            session_id,
            url=url,
            name=str(file_info.get("name") or ""),
            size_bytes=file_info.get("size_bytes"),
            mime=str(file_info.get("mime") or ""),
        )
        ctx["_skilldev_stream_last_kind"] = "file"
        ctx["_skilldev_active_reasoning_part"] = None
        ctx["_skilldev_active_output_part"] = None
        try:
            await self._store.append_file_ready_obs_url(session_id, url)
        except Exception:
            logger.exception(
                "[VibeSkillChannel] append_file_ready_obs_url failed, session_id=%s url=%s",
                session_id,
                url,
            )
        responses = [{
            "type": "message.part.updated",
            "properties": self._serialize_part(part, external_sid),
        }]
        return self._prepend_message_announcement(ctx, external_sid, responses)

    async def _handle_skilldev_todos_update(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
    ) -> list[dict]:
        """skilldev.todos_update - Todo 更新"""
        return [{
            "type": "todo.updated",
            "properties": {
                "sessionID": external_sid,
                "todos": payload.get("todos", []),
            },
        }]

    @staticmethod
    def _build_review_asked_properties(
        confirm_type: str,
        *,
        request_id: str,
        session_id: str | None,
        external_sid: str | None,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """根据 skilldev.confirm_request 构造 review.asked 的 properties。"""
        properties: dict[str, Any] = {
            "id": request_id,
            "sessionID": external_sid or session_id,
            "type": confirm_type,
        }
        if confirm_type == "static_review":
            properties["benchmark"] = data.get("benchmark")
            properties["report"] = str(data.get("report") or "")
        elif confirm_type == "combined_review":
            properties["static_benchmark"] = data.get("static_benchmark")
            properties["dyn_benchmark"] = data.get("dyn_benchmark")
            properties["report"] = str(data.get("report") or "")
            properties["iteration"] = data.get("iteration")
        else:
            properties["benchmark"] = data.get("benchmark")
            properties["report"] = str(data.get("report") or "")
            properties["iteration"] = data.get("iteration")
        return properties

    async def _handle_skilldev_confirm_request(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
    ) -> list[dict]:
        """skilldev.confirm_request - 确认请求"""
        confirm_type = str(payload.get("confirm_type") or "").strip()
        request_id = str(payload.get("request_id") or f"req_{secrets.token_hex(4)}")
        task_id = str(payload.get("task_id") or "")
        if confirm_type not in _SKILLDEV_REVIEW_CONFIRM_TYPES:
            return []

        self._pending_confirms[request_id] = {
            "task_id": task_id,
            "session_id": session_id or "",
            "confirm_type": confirm_type,
        }
        data = payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}
        return [{
            "type": "review.asked",
            "properties": self._build_review_asked_properties(
                confirm_type,
                request_id=request_id,
                session_id=session_id,
                external_sid=external_sid,
                data=data,
            ),
        }]

    async def _handle_skilldev_ask_user_question(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
    ) -> list[dict]:
        """skilldev.ask_user_question → question.asked（与原 confirm_request 结构化提问字段一致）。"""
        return self._build_ask_user_question_response(
            payload,
            external_sid,
            session_id,
            dispatch="skilldev",
        )

    async def _handle_chat_ask_user_question(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
    ) -> list[dict]:
        """chat.ask_user_question → question.asked（Standard 模式下结构化提问入口）。

        相比 ``skilldev.ask_user_question``，仅在 ``_pending_confirms`` 中额外记录
        ``dispatch="chat"``，以便后续 ``question.replied`` 走 ``chat.user_answer``
        而不是 ``skilldev.user_answer``。
        """
        return self._build_ask_user_question_response(
            payload,
            external_sid,
            session_id,
            dispatch="chat",
            source=str(payload.get("source") or "").strip() or None,
        )

    def _build_ask_user_question_response(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
        *,
        dispatch: str,
        source: str | None = None,
    ) -> list[dict]:
        """共享的 ask_user_question → question.asked 构造逻辑。

        - ``dispatch`` 决定 ``question.replied`` 的回写路径：``skilldev`` → SKILLDEV_USER_ANSWER，
          ``chat`` → CHAT_ANSWER（chat.user_answer）。
        - ``source`` 用来区分 ``ask_tool`` / ``permission_interrupt`` 等子类型，
          目前仅用于诊断 / 后续扩展，不改变回写路径。
        """
        if not session_id:
            return []
        request_id = str(payload.get("request_id") or f"req_{secrets.token_hex(4)}")
        task_id = str(payload.get("task_id") or payload.get("session_id") or "").strip() or (session_id or "")

        raw_questions = payload.get("questions", []) if isinstance(payload.get("questions"), list) else []
        questions: list[dict[str, Any]] = []
        for idx, item in enumerate(raw_questions):
            if not isinstance(item, dict):
                continue
            options: list[dict[str, Any]] = []
            for option in item.get("options", []) or []:
                if not isinstance(option, dict):
                    continue
                options.append({
                    "id": str(option.get("id") or f"opt_{idx}_{len(options)}"),
                    "label": str(option.get("label") or ""),
                    "description": str(option.get("description") or ""),
                })
            questions.append({
                "id": str(item.get("id") or f"q_{idx + 1}"),
                "question": str(item.get("question") or ""),
                "header": str(item.get("header") or payload.get("title") or ""),
                "options": options,
                "multiple": bool(
                    item.get("multi_select")
                    or item.get("multiple")
                    or item.get("allow_multiple")
                    or False
                ),
            })
        self._pending_confirms[request_id] = {
            "task_id": task_id,
            "session_id": session_id or "",
            "dispatch": dispatch,
            "source": source or "",
        }
        return [{
            "type": "question.asked",
            "properties": {
                "id": request_id,
                "sessionID": external_sid or session_id,
                "questions": [
                    {
                        "question": q["question"],
                        "header": q["header"],
                        "options": [
                            {"label": opt["label"], "description": opt.get("description", "")}
                            for opt in q["options"]
                        ],
                        "multiple": q["multiple"],
                    }
                    for q in questions
                ],
                "tool": str(payload.get("tool") or ""),
            },
        }]

    async def _handle_chat_tool_call(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
    ) -> list[dict]:
        """chat.tool_call → message.part.updated（tool part, running）。

        AgentServer 的 chat.tool_call payload 形如::

            {
                "event_type": "chat.tool_call",
                "tool_call": {
                    "id": "<call_id>",
                    "function": {"name": "<tool_name>", "arguments": "<json string>"},
                    ...
                },
                "task_id": "<optional>",
            }

        ``arguments`` 通常是 JSON 字符串；解析失败时保留原始字符串放在 ``_raw_arguments`` 里，
        前端仍可读到工具名与原始参数串，避免静默丢失。
        """
        if not session_id:
            return []
        tool_call_obj = payload.get("tool_call")
        if not isinstance(tool_call_obj, dict):
            return []
        fn = tool_call_obj.get("function") if isinstance(tool_call_obj.get("function"), dict) else {}
        args_raw = (fn or {}).get("arguments")
        if args_raw is None:
            args_raw = tool_call_obj.get("arguments")
        tool_input: Any
        if isinstance(args_raw, str) and args_raw.strip():
            try:
                tool_input = json.loads(args_raw)
            except (ValueError, TypeError):
                tool_input = {"_raw_arguments": args_raw}
        elif isinstance(args_raw, (dict, list)):
            tool_input = args_raw
        else:
            tool_input = {}

        normalized = {
            "tool_call_id": (
                tool_call_obj.get("id")
                or tool_call_obj.get("tool_call_id")
                or tool_call_obj.get("callID")
            ),
            "tool_name": (fn or {}).get("name") or tool_call_obj.get("name"),
            "arguments": tool_input,
            "title": payload.get("title"),
            "metadata": payload.get("metadata", {}),
        }
        return await self._handle_skilldev_tool_call(normalized, external_sid, session_id)

    async def _handle_chat_tool_result(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
    ) -> list[dict]:
        """chat.tool_result → message.part.updated（tool part, completed/error）。

        chat.tool_result payload 字段是扁平的（``result`` / ``tool_name`` / ``tool_call_id``
        / 可选 ``raw_output``，见 ``agentserver/stream_utils.py``）；skilldev 端有等价处理。
        这里做一次字段名归一化后复用 ``_handle_skilldev_tool_result``，避免实现漂移。
        """
        if not session_id:
            return []
        normalized = {
            "tool_call_id": (
                payload.get("tool_call_id")
                or payload.get("toolCallId")
                or payload.get("callID")
            ),
            "tool_name": payload.get("tool_name") or payload.get("name"),
            "result": payload.get("result"),
            "raw_output": payload.get("raw_output"),
            "success": payload.get("success", True),
            "title": payload.get("title"),
            "metadata": payload.get("metadata", {}),
        }
        return await self._handle_skilldev_tool_result(normalized, external_sid, session_id)

    async def _handle_skilldev_search_results(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
    ) -> list[dict]:
        """skilldev.search_results → skillSearch.asked（检索结果呈现）。"""
        request_id = str(
            payload.get("request_id") or payload.get("id") or f"req_{secrets.token_hex(4)}"
        ).strip()
        skill_list = payload.get("skillList")
        if skill_list is None:
            skill_list = payload.get("skill_list", [])
        if not isinstance(skill_list, list):
            skill_list = []
        num = payload.get("num", 0)
        total = payload.get("total", 0)
        return [{
            "type": "skillSearch.asked",
            "properties": {
                "id": request_id,
                "sessionID": external_sid or session_id,
                "skillList": skill_list,
                "num": num,
                "total": total,
            },
        }]

    def _dispatch_skilldev_user_answer(
        self,
        session_id: str,
        request_id: str,
        answers: list[dict[str, Any]],
    ) -> bool:
        answer_params: dict[str, Any] = {
            "session_id": session_id,
            "task_id": session_id,
            "request_id": request_id,
            "source": "ask_tool",
            "answers": answers,
        }
        self._apply_tenant_service_id(answer_params, session_id)
        msg = Message(
            id=f"vibeskill-user-answer-{int(time.time() * 1000):x}-{secrets.token_hex(3)}",
            type="req",
            channel_id=VIBESKILL_CHANNEL_ID,
            session_id=session_id,
            params=answer_params,
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.SKILLDEV_USER_ANSWER,
            is_stream=False,
            metadata={_VIBESKILL_ORIGINAL_SESSION_ID_KEY: session_id},
            user_id=self._session_user_id(session_id),
        )
        logger.info(
            "[VibeSkillChannel] skilldev.user_answer sent, session_id=%s",
            session_id,
        )
        self._deliver_to_message_handler(msg)
        return True

    def _dispatch_chat_user_answer(
        self,
        session_id: str,
        request_id: str,
        answers: list[dict[str, Any]],
    ) -> bool:
        """Standard 模式下 ``question.replied`` 的回写：派发 CHAT_ANSWER（``chat.user_answer``）。

        params 字段与前端 ``request('chat.user_answer', {...})`` 一致：
        ``session_id`` / ``request_id`` / ``answers``。``answers`` 仍沿用
        ``{"selected_options": [...]}`` 的结构，与 ask_user_question_tool 在 Registry
        中等待的反序列化格式对齐（同时也是 SkillCreate 路径长期使用的格式）。
        """
        answer_params: dict[str, Any] = {
            "session_id": session_id,
            "request_id": request_id,
            "answers": answers,
        }
        self._apply_tenant_service_id(answer_params, session_id)
        msg = Message(
            id=f"vibeskill-chat-user-answer-{int(time.time() * 1000):x}-{secrets.token_hex(3)}",
            type="req",
            channel_id=VIBESKILL_CHANNEL_ID,
            session_id=session_id,
            params=answer_params,
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.CHAT_ANSWER,
            is_stream=False,
            metadata={_VIBESKILL_ORIGINAL_SESSION_ID_KEY: session_id},
            user_id=self._session_user_id(session_id),
        )
        logger.info(
            "[VibeSkillChannel] chat.user_answer sent, session_id=%s request_id=%s",
            session_id,
            request_id,
        )
        self._deliver_to_message_handler(msg)
        return True

    async def _handle_skilldev_error(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
    ) -> list[dict]:
        """skilldev.error - 错误"""
        if session_id:
            try:
                await self._store.set_state(session_id, VibeSkillSessionState.IDLE)
                logger.info(
                    "[VibeSkillChannel] session state -> idle, source=skilldev.error, session_id=%s",
                    session_id,
                )
            except Exception:
                logger.exception("[VibeSkillChannel] set_state error for skilldev.error, session_id=%s", session_id)

        error_text = str(payload.get("error") or payload.get("message") or "skilldev error")
        return self._build_error_responses(session_id, external_sid, error_text)

    async def _handle_skilldev_agent_completed(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
    ) -> list[dict]:
        """skilldev.agent_completed — 单轮 Agent 结束，等待用户确认时置 idle 并推送 session.status。"""
        if session_id:
            try:
                await self._store.set_state(session_id, VibeSkillSessionState.IDLE)
                await self._store.set_exportable(session_id, False)
                logger.info(
                    "[VibeSkillChannel] session state -> idle, source=skilldev.agent_completed, session_id=%s",
                    session_id,
                )
            except Exception:
                logger.exception(
                    "[VibeSkillChannel] set_state error for skilldev.agent_completed, session_id=%s",
                    session_id,
                )
            ctx = self._message_ctx.get(session_id)
            if isinstance(ctx, dict):
                ctx.pop("_skilldev_stream_last_kind", None)
                ctx.pop("_skilldev_active_reasoning_part", None)
                ctx.pop("_skilldev_active_output_part", None)
        if not external_sid:
            return []
        return [{
            "type": "session.status",
            "properties": {
                "sessionID": external_sid,
                "status": {"type": "idle"},
            },
        }]

    async def _handle_skilldev_completed(
        self,
        payload: dict,
        external_sid: str | None,
        session_id: str | None,
    ) -> list[dict]:
        """Handle skilldev.completed: Agent 已结束本轮 skill 流水线.

        触发来源: MessageHandler 入站 event, payload.event_type=skilldev.completed
        (对应同一会话上, 由客户端经 WebSocket 发送的 message.send 所启动的 skilldev 一次执行).

        北向(发往当前会话绑定的 WebSocket 对端)连续两帧, 与其它事件的 type+properties
        形状一致(见同文件 outbound_intercept 对 skilldev 事件的组帧方式):
        1) type=task.completed, properties 含 sessionID; 若入站 payload 含 needSandbox(bool),
           则透传为字符串 "true" / "false";
        2) type=session.status, 会话状态为 completed.
        """
        if session_id:
            try:
                await self._store.set_state(session_id, VibeSkillSessionState.COMPLETED)
                await self._store.set_exportable(session_id, True)
                logger.info(
                    "[VibeSkillChannel] session state -> completed, source=skilldev.completed, session_id=%s",
                    session_id,
                )
            except Exception:
                logger.exception("[VibeSkillChannel] set_state error for skilldev.completed, session_id=%s", session_id)
        task_completed_properties: dict[str, str] = {}
        if external_sid:
            task_completed_properties["sessionID"] = external_sid
        if "needSandbox" in payload and isinstance(payload.get("needSandbox"), bool):
            task_completed_properties["needSandbox"] = (
                "true" if payload["needSandbox"] else "false"
            )
        return [
            {"type": "task.completed", "properties": task_completed_properties},
            {
                "type": "session.status",
                "properties": {
                    "sessionID": external_sid,
                    "status": {"type": "completed"},
                },
            }
        ]

    async def _disconnect_northbound_ws_after_agent_completed(
        self,
        session_id: str | None,
        ws: Any,
    ) -> None:
        """单轮 Agent 结束后主动断开北向 WS；客户端下次会重连再发 message.send 等。"""
        if ws is None or bool(getattr(ws, "closed", False)):
            return
        self._ws_skip_cancel_on_disconnect.add(ws)
        logger.info(
            "[VibeSkillChannel] closing northbound WS after skilldev.agent_completed, session_id=%s",
            str(session_id or "").strip() or "n/a",
        )
        try:
            await ws.close(code=1000, reason="agent_completed")
        except Exception:
            logger.exception(
                "[VibeSkillChannel] close WS after skilldev.agent_completed failed, session_id=%s",
                str(session_id or "").strip() or "n/a",
            )

    async def cleanup(self, ws: Any) -> None:
        """ws 断开时清理关联的会话映射。"""
        skip_cancel = ws in self._ws_skip_cancel_on_disconnect
        if skip_cancel:
            self._ws_skip_cancel_on_disconnect.discard(ws)
        heartbeat = self._ws_heartbeat_tasks.pop(ws, None)
        if heartbeat and not heartbeat.done():
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("[VibeSkillChannel] cleanup heartbeat wait failed: %s", exc)
        session_ids = self._ws_sessions.pop(ws, set())
        for sid in session_ids:
            # 仅当该 session 仍绑定在当前 ws 上时才移除映射，避免 agent_completed
            # 主动断连后客户端已重连、新 ws 接管 session 时被旧 ws cleanup 误删。
            if self._session_to_ws.get(sid) is ws:
                self._session_to_ws.pop(sid, None)
            if skip_cancel:
                continue
            session_obj = await self._store.get_session(sid)
            mode = session_obj.mode if session_obj else "SkillCreate"
            await self._cancel_session_via_message_handler(sid, source="ws.disconnect", mode=mode)

    def _get_active_ws_for_session(self, session_id: str) -> Any | None:
        """返回 session 当前绑定的未关闭 WebSocket，不存在则返回 None。"""
        sid = str(session_id or "").strip()
        if not sid:
            return None
        ws = self._session_to_ws.get(sid)
        if ws is None or bool(getattr(ws, "closed", False)):
            return None
        return ws

    def _clear_pending_confirms_for_session(self, session_id: str) -> None:
        """Remove pending confirm/review state for a session."""
        sid = str(session_id or "").strip()
        if not sid:
            return
        stale_request_ids = [
            request_id
            for request_id, pending in self._pending_confirms.items()
            if str((pending or {}).get("session_id") or "").strip() == sid
        ]
        for request_id in stale_request_ids:
            self._pending_confirms.pop(request_id, None)

    async def _close_northbound_ws_for_session(
        self,
        session_id: str,
        *,
        reason: str = "session_deleted",
    ) -> bool:
        """Close the northbound WS bound to session_id without duplicate cancel."""
        ws = self._get_active_ws_for_session(session_id)
        if ws is None:
            return False

        self._ws_skip_cancel_on_disconnect.add(ws)
        async with self._ws_sessions_lock:
            self._session_to_ws.pop(session_id, None)
            bound_sessions = self._ws_sessions.get(ws)
            if bound_sessions is not None:
                bound_sessions.discard(session_id)
                if not bound_sessions:
                    self._ws_sessions.pop(ws, None)

        logger.info(
            "[VibeSkillChannel] closing northbound WS on session delete, session_id=%s",
            str(session_id or "").strip() or "n/a",
        )
        try:
            await ws.close(code=1000, reason=reason)
        except Exception:
            logger.exception(
                "[VibeSkillChannel] close WS on session delete failed, session_id=%s",
                str(session_id or "").strip() or "n/a",
            )
        return True

    async def _teardown_session(
        self,
        session_id: str,
        *,
        source: str = "http.session.delete",
    ) -> dict[str, Any]:
        """Cancel work, close WS, release sandbox tracking, then delete session metadata."""
        sid = str(session_id or "").strip()
        session_obj = await self._store.resolve_session(sid)
        if session_obj is None:
            return {"deleted": False, "sessionID": sid, "error": "session_not_found"}

        mode = session_obj.mode
        was_busy = session_obj.state == VibeSkillSessionState.BUSY

        await self._cancel_session_via_message_handler(sid, source=source, mode=mode)
        self._clear_pending_confirms_for_session(sid)
        websocket_closed = await self._close_northbound_ws_for_session(sid)

        export_obs_url = str(session_obj.last_export_obs_url or "").strip()
        file_ready_obs_urls = [
            str(url).strip()
            for url in (session_obj.file_ready_obs_urls or [])
            if str(url).strip()
        ]
        for obs_url in [export_obs_url, *file_ready_obs_urls]:
            if obs_url:
                await _delete_obs_url(
                    obs_url,
                    session_id=sid,
                    reason="session_delete",
                )

        workspace_purged = False
        workspace_obs_delete_attempted = False
        workspace_obs_deleted = False
        workspace_obs_url = ""
        sandbox_untracked = False
        release_fn = getattr(self._agent_client, "release_session", None)
        if callable(release_fn):
            try:
                release_result = await release_fn(
                    sid,
                    user_id=self._session_user_id(sid),
                )
                if isinstance(release_result, dict):
                    workspace_purged = bool(release_result.get("workspace_purged"))
                    workspace_obs_delete_attempted = bool(
                        release_result.get("workspace_obs_delete_attempted")
                    )
                    workspace_obs_deleted = bool(
                        release_result.get("workspace_obs_deleted")
                    )
                    workspace_obs_url = str(
                        release_result.get("workspace_obs_url") or ""
                    ).strip()
                    sandbox_untracked = bool(release_result.get("untracked"))
            except Exception:
                logger.exception(
                    "[VibeSkillChannel] release_session failed, session_id=%s",
                    sid,
                )

        await self._store.delete_session(sid)

        return {
            "deleted": True,
            "sessionID": sid,
            "cancelled": True,
            "wasBusy": was_busy,
            "websocketClosed": websocket_closed,
            "workspacePurged": workspace_purged,
            "workspaceObsDeleteAttempted": workspace_obs_delete_attempted,
            "workspaceObsDeleted": workspace_obs_deleted,
            "workspaceObsUrl": workspace_obs_url,
            "sandboxUntracked": sandbox_untracked,
        }

    async def _cancel_session_via_message_handler(
        self,
        session_id: str,
        *,
        source: str,
        mode: str = "SkillCreate",
    ) -> None:
        """清理流式上下文、busy→idle，经 MessageHandler 派发取消（与 message.send 入站 method 对称）。"""
        sid = str(session_id or "").strip()
        if not sid:
            return
        self._clear_message_context_for_session(sid)
        self._clear_detached_tool_parts_for_session(sid)
        try:
            state = await self._store.get_state(sid)
            if state == VibeSkillSessionState.BUSY:
                await self._store.set_state(sid, VibeSkillSessionState.IDLE)
                logger.info(
                    "[VibeSkillChannel] session state -> idle, source=%s, session_id=%s",
                    source,
                    sid,
                )
        except Exception:
            logger.exception("[VibeSkillChannel] set_state failed, source=%s, session_id=%s", source, sid)

        if mode == "Standard":
            req_method = ReqMethod.CHAT_CANCEL
            params: dict[str, Any] = {"intent": "cancel", "session_id": sid}
            self._apply_tenant_service_id(params, sid)
            cancel_method_label = "chat.interrupt"
        else:
            req_method = ReqMethod.SKILLDEV_CANCEL
            params = {"task_id": sid, "session_id": sid, "intent": "cancel"}
            self._apply_tenant_service_id(params, sid)
            cancel_method_label = "skilldev.cancel"

        try:
            cancel_msg = Message(
                id=f"vibeskill-{source}-{int(time.time() * 1000):x}-{secrets.token_hex(3)}",
                type="req",
                channel_id=VIBESKILL_CHANNEL_ID,
                session_id=sid,
                params=params,
                timestamp=time.time(),
                ok=True,
                req_method=req_method,
                is_stream=False,
                user_id=self._session_user_id(sid),
            )
            logger.info(
                "[VibeSkillChannel] %s sent, source=%s, session_id=%s, mode=%s",
                cancel_method_label,
                source,
                sid,
                mode,
            )
            self._deliver_to_message_handler(cancel_msg)
        except Exception:
            logger.exception(
                "[VibeSkillChannel] dispatch %s failed, source=%s, session_id=%s",
                cancel_method_label,
                source,
                sid,
            )

    async def _resolve_external_session_id(
        self,
        session_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """兼容接口：统一 ID 模式下直接返回 sessionId。"""
        sid = str(session_id or "").strip()
        if not sid:
            return None

        original = ""
        if isinstance(metadata, dict):
            original = str(metadata.get(_VIBESKILL_ORIGINAL_SESSION_ID_KEY) or "").strip()
        if original:
            return original

        return sid

    async def _send_ws_res_error(
        self,
        ws: Any,
        data: dict[str, Any] | None,
        error: str,
        *,
        source: str,
    ) -> None:
        await self._send_ws_json(
            ws,
            {
                "type": "res",
                "id": str((data or {}).get("id") or "").strip(),
                "ok": False,
                "error": error,
            },
            source=source,
        )

    @staticmethod
    def _is_streaming_chunk_ws_send(source: str, payload: dict[str, Any]) -> bool:
        if "delta" in source.lower():
            return True
        return str(payload.get("type") or "").strip() == "message.part.delta"

    async def _send_ws_json(self, ws: Any, payload: dict[str, Any], source: str) -> None:
        payload_str = json.dumps(payload, ensure_ascii=False)
        log_fn = (
            logger.debug
            if self._is_streaming_chunk_ws_send(source, payload)
            else logger.info
        )
        _sid_out = "n/a"
        _payload_type = str(payload.get("type") or "").strip() or "n/a"
        if isinstance(payload, dict):
            _props = payload.get("properties")
            if isinstance(_props, dict):
                _sid_out = str(_props.get("sessionID") or "").strip() or _sid_out
            if _sid_out == "n/a":
                _sid_out = str(payload.get("sessionID") or "").strip() or _sid_out
        log_fn(
            "[VibeSkillChannel] WS send: source=%s type=%s session_id=%s payload_bytes=%d",
            source,
            _payload_type,
            _sid_out,
            len(payload_str.encode("utf-8")),
        )
        try:
            await ws.send(payload_str)
            interface_info.mark_channel_response_sent()
        except Exception as exc:
            logger.exception(
                "[VibeSkillChannel] WS send failed (%s): %s, type=%s, ws_close_code=%s, "
                "ws_close_reason=%s",
                source,
                exc,
                type(exc).__name__,
                getattr(ws, "close_code", None),
                getattr(ws, "close_reason", None),
            )
            raise

    async def _emit_ws_event(self, ws: Any, event_type: str, properties: dict[str, Any]) -> None:
        await self._send_ws_json(
            ws,
            {"type": event_type, "properties": properties},
            source=f"event.{event_type}",
        )

    def _start_heartbeat_task(self, ws: Any) -> None:
        task = asyncio.create_task(self._heartbeat_loop(ws))
        self._ws_heartbeat_tasks[ws] = task

    async def _heartbeat_loop(self, ws: Any) -> None:
        try:
            while True:
                await asyncio.sleep(10)
                if ws not in self._clients or bool(getattr(ws, "closed", False)):
                    return
                try:
                    await self._emit_ws_event(ws, "server.heartbeat", {"timestamp": int(time.time() * 1000)})
                except Exception:
                    # ws 可能在检查后立刻关闭；结束 heartbeat 循环即可。
                    return
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("[VibeSkillChannel] heartbeat loop stopped: %s", exc)

    async def _emit_session_status(
        self,
        ws: Any,
        external_sid: str | None,
        status_type: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not external_sid:
            return
        status: dict[str, Any] = {"type": status_type}
        if extra:
            status.update(extra)
        await self._emit_ws_event(ws, "session.status", {"sessionID": external_sid, "status": status})

    def _clear_message_context_for_session(self, session_id: str | None) -> None:
        """移除某 session 下的 assistant message 聚合状态。"""
        sid = str(session_id or "").strip()
        if not sid:
            self._message_ctx.pop("_default", None)
            return
        self._message_ctx.pop(sid, None)

    def _clear_detached_tool_parts_for_session(self, session_id: str | None) -> None:
        """移除 question.replied 后暂存的进行中 tool part 快照。"""
        sid = str(session_id or "").strip()
        if not sid:
            self._detached_tool_parts.pop("_default", None)
            return
        self._detached_tool_parts.pop(sid, None)

    def _snapshot_detached_tool_parts(self, session_id: str | None) -> None:
        """在清空 _message_ctx 前，保存仍处 running 的 tool part（供后续 tool_result 复用 ID）。"""
        sid = str(session_id or "").strip()
        if not sid:
            return
        ctx = self._message_ctx.get(sid)
        if not isinstance(ctx, dict):
            return
        tool_parts = ctx.get("tool_parts")
        if not isinstance(tool_parts, dict) or not tool_parts:
            return
        store = self._detached_tool_parts.setdefault(sid, {})
        message_id = str(ctx.get("message_id") or "").strip()
        for call_id, part in tool_parts.items():
            if not isinstance(part, dict):
                continue
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            if str(state.get("status") or "") != "running":
                continue
            part_copy = copy.deepcopy(part)
            part_message_id = str(part_copy.get("messageID") or message_id or "").strip()
            if part_message_id:
                part_copy["messageID"] = part_message_id
            store[str(call_id).strip()] = {
                "message_id": part_message_id,
                "part": part_copy,
            }

    def _pop_detached_tool_part(
        self, session_id: str | None, call_id: str
    ) -> dict[str, Any] | None:
        sid = str(session_id or "").strip()
        cid = str(call_id or "").strip()
        if not sid or not cid:
            return None
        session_store = self._detached_tool_parts.get(sid)
        if not isinstance(session_store, dict):
            return None
        entry = session_store.pop(cid, None)
        if not session_store:
            self._detached_tool_parts.pop(sid, None)
        return entry if isinstance(entry, dict) else None

    def _ensure_message_context(self, session_id: str | None) -> dict[str, Any]:
        sid = str(session_id or "").strip()
        if not sid:
            sid = "_default"
        ctx = self._message_ctx.get(sid)
        if ctx is None:
            ctx = {
                "message_id": f"msg_{secrets.token_hex(6)}",
                "parts": [],
                "part_by_type": {},
                "tool_parts": {},
                "message_announced": False,
            }
            self._message_ctx[sid] = ctx
        return ctx

    def _ensure_text_part(
        self, session_id: str | None, part_type: str
    ) -> tuple[dict[str, Any], bool]:
        """获取或创建单个 text/reasoning part（按 part_type 去重，用于兜底，chat 等路径）。"""
        ctx = self._ensure_message_context(session_id)
        existing = ctx["part_by_type"].get(part_type)
        if existing is not None:
            return existing, False
        part = {
            "id": f"prt_{secrets.token_hex(6)}",
            "sessionID": session_id,
            "messageID": ctx["message_id"],
            "type": part_type,
            "text": "",
        }
        ctx["part_by_type"][part_type] = part
        ctx["parts"].append(part)
        return part, True

    def _append_text_part(self, session_id: str | None, part_type: str) -> dict[str, Any]:
        """始终创建并追加一个新的 text part（不会写入 part_by_type）。"""
        ctx = self._ensure_message_context(session_id)
        part = {
            "id": f"prt_{secrets.token_hex(6)}",
            "sessionID": session_id,
            "messageID": ctx["message_id"],
            "type": part_type,
            "text": "",
        }
        ctx["parts"].append(part)
        return part

    def _append_standalone_text_part(self, session_id: str | None, part_type: str) -> dict[str, Any]:
        """SkillDev agent 流式专用：追加 text/reasoning part，与同会话 chat 使用的 part_by_type 隔离。"""
        return self._append_text_part(session_id, part_type)

    def _append_file_part(
        self,
        session_id: str | None,
        *,
        url: str,
        name: str,
        size_bytes: Any,
        mime: str,
    ) -> dict[str, Any]:
        """追加 file part（upload_file 产物，发往小艺 message.part.updated）。"""
        ctx = self._ensure_message_context(session_id)
        part = {
            "id": f"prt_{secrets.token_hex(6)}",
            "sessionID": session_id,
            "messageID": ctx["message_id"],
            "type": "file",
            "url": url,
            "name": name,
            "size_bytes": size_bytes,
            "mime": mime,
        }
        ctx["parts"].append(part)
        return part

    def _ensure_tool_part(
        self, session_id: str | None, call_id: str, tool_name: str
    ) -> tuple[dict[str, Any], bool]:
        ctx = self._ensure_message_context(session_id)
        existing = ctx["tool_parts"].get(call_id)
        if existing is not None:
            return existing, False
        part = {
            "id": f"prt_{secrets.token_hex(6)}",
            "sessionID": session_id,
            "messageID": ctx["message_id"],
            "type": "tool",
            "callID": call_id,
            "tool": tool_name,
            "state": {},
        }
        ctx["tool_parts"][call_id] = part
        ctx["parts"].append(part)
        return part, True

    def _ensure_message_announced(self, ctx: dict[str, Any], external_sid: str | None) -> list[dict]:
        if ctx.get("message_announced"):
            return []
        ctx["message_announced"] = True
        return [{
            "type": "message.updated",
            "properties": {
                "info": {
                    "id": ctx["message_id"],
                    "sessionID": external_sid,
                    "role": "assistant",
                    "parts": self._serialize_parts(ctx["parts"], external_sid),
                }
            },
        }]

    def _prepend_message_announcement(
        self,
        ctx: dict[str, Any],
        external_sid: str | None,
        responses: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """ 创建 message.updated 事件，如果 responses 中没有 message.part.updated 事件。"""
        if not responses:
            return responses
        has_part_event = any(
            str(r.get("type") or "").startswith("message.part.")
            for r in responses
            if isinstance(r, dict)
        )
        if not has_part_event:
            return responses
        return self._ensure_message_announced(ctx, external_sid) + responses

    def _build_skilldev_agent_delta_events(
        self,
        session_id: str | None,
        external_sid: str | None,
        *,
        stream_kind: str,
        delta: str,
    ) -> list[dict[str, Any]]:
        """skilldev.agent_thinking / agent_output：仅用 delta；与上一条流式种类是否一致决定 delta 拼接或新建 part。"""
        if not session_id:
            return []
        delta_str = str(delta or "")
        if not delta_str:
            return []

        part_type = "reasoning" if stream_kind == "thinking" else "text"
        ctx = self._ensure_message_context(session_id)
        last = ctx.get("_skilldev_stream_last_kind")

        reuse = last == stream_kind
        active: dict[str, Any] | None = None
        if reuse:
            active = (
                ctx.get("_skilldev_active_reasoning_part")
                if stream_kind == "thinking"
                else ctx.get("_skilldev_active_output_part")
            )

        if active is None:
            part = self._append_standalone_text_part(session_id, part_type)
            part["text"] = delta_str
            ctx["_skilldev_stream_last_kind"] = stream_kind
            if stream_kind == "thinking":
                ctx["_skilldev_active_reasoning_part"] = part
            else:
                ctx["_skilldev_active_output_part"] = part
            responses = [{
                "type": "message.part.updated",
                "properties": self._serialize_part(part, external_sid),
            }]
            return self._prepend_message_announcement(ctx, external_sid, responses)

        active["text"] = str(active.get("text") or "") + delta_str
        ctx["_skilldev_stream_last_kind"] = stream_kind
        responses = [{
            "type": "message.part.delta",
            "properties": {
                "sessionID": external_sid,
                "messageID": ctx["message_id"],
                "partID": active["id"],
                "type": part_type,
                "text": delta_str,
            },
        }]
        return self._prepend_message_announcement(ctx, external_sid, responses)

    def _emit_skilldev_error_text_part(
        self,
        session_id: str | None,
        external_sid: str | None,
        text: str,
    ) -> list[dict[str, Any]]:
        """错误信息单独 text part，并让后续 agent_output 不会拼接到本条错误内容上。"""
        if not session_id:
            return []
        msg = str(text or "").strip()
        if not msg:
            return []
        ctx = self._ensure_message_context(session_id)
        part = self._append_standalone_text_part(session_id, "text")
        part["text"] = msg
        ctx["_skilldev_stream_last_kind"] = "output"
        ctx["_skilldev_active_output_part"] = None
        responses = [{
            "type": "message.part.updated",
            "properties": self._serialize_part(part, external_sid),
        }]
        return self._prepend_message_announcement(ctx, external_sid, responses)

    def _build_error_responses(
        self,
        session_id: str | None,
        external_sid: str | None,
        error_text: str,
        *,
        include_task_completed: bool = False,
    ) -> list[dict[str, Any]]:
        """统一构造错误收口事件序列：``task.error``
        [+ ``task.completed``] + ``session.status`` idle。

        - ``skilldev.error`` 通用错误收口（SkillCreate 模式）：不发 ``task.completed``，
          由后续 ``skilldev.completed`` / ``skilldev.agent_completed`` 自行收口；
        - ``chat.error``（Standard / 通用 chat 路径）：需要追加 ``task.completed`` 让前端
          spinner / busy 状态退出。

        注意：这里刻意不再下发 `message.updated` / `message.part.updated`，
        由 `task.error` / `res.error` / `session.status` 承担错误展示与收口，避免与 part 展示路径重复。
        """
        responses: list[dict[str, Any]] = []
        responses.append({
            "type": "task.error",
            "properties": {
                "error": error_text,
            },
        })
        if include_task_completed:
            responses.append({
                "type": "task.completed",
                "properties": {},
            })
        responses.append({
            "type": "session.status",
            "properties": {
                "sessionID": external_sid,
                "status": {
                    "type": "idle",
                    "message": error_text,
                },
            },
        })
        return responses

    def _serialize_parts(self, parts: list[dict[str, Any]], external_sid: str | None) -> list[dict[str, Any]]:
        return [self._serialize_part(part, external_sid) for part in parts]

    def _serialize_part(self, part: dict[str, Any], external_sid: str | None) -> dict[str, Any]:
        serialized = dict(part)
        part_id = serialized.pop("id", None)
        if part_id is not None:
            serialized["partID"] = part_id
        serialized["sessionID"] = external_sid
        return serialized

    def _merge_message_update_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """合并 message/part 更新事件。

        规则：
        - `message.updated` / `message.part.updated` / `message.part.delta` 归并到同一条 `message.updated`
        - message 顺序按首次出现 messageID
        - part 顺序按首次出现 partID
        """
        merged_events: list[dict[str, Any]] = []
        message_states: dict[str, dict[str, Any]] = {}
        message_index_map: dict[str, int] = {}

        def _part_id(part: dict[str, Any]) -> str:
            return str(part.get("partID") or part.get("id") or "").strip()

        def _ensure_message_state(
            message_id: str,
            session_id: str,
            role: str,
            event_role: str,
        ) -> dict[str, Any]:
            state = message_states.get(message_id)
            if state is not None:
                return state

            state = {
                "id": message_id,
                "sessionID": session_id,
                "role": role,
                "event_role": event_role,
                "parts": [],
                "part_by_id": {},
            }
            message_states[message_id] = state
            merged_events.append({
                "type": "message.updated",
                "role": event_role,
                "sessionID": session_id,
                "properties": {
                    "info": {
                        "id": message_id,
                        "sessionID": session_id,
                        "role": role,
                        "parts": [],
                    }
                },
            })
            message_index_map[message_id] = len(merged_events) - 1
            return state

        def _upsert_part(state: dict[str, Any], incoming_part: dict[str, Any], event_type: str) -> None:
            incoming = dict(incoming_part)
            incoming.pop("messageID", None)
            part_id = _part_id(incoming)

            if not part_id:
                state["parts"].append(incoming)
                return

            part_map = state["part_by_id"]
            existing = part_map.get(part_id)
            if existing is None:
                existing = {"partID": part_id}
                part_map[part_id] = existing
                state["parts"].append(existing)

            if event_type == "message.part.delta":
                delta_text = str(incoming.get("text") or "")
                existing_text = str(existing.get("text") or "")
                existing.update({k: v for k, v in incoming.items() if k != "text"})
                existing["text"] = existing_text + delta_text
                return

            existing.update(incoming)

        def _refresh_message_event(state: dict[str, Any]) -> None:
            message_id = str(state.get("id") or "")
            index = message_index_map.get(message_id)
            if index is None:
                return

            parts = [dict(part) for part in state.get("parts", []) if isinstance(part, dict)]
            merged_events[index] = {
                "type": "message.updated",
                "role": state.get("event_role") or "assistant",
                "sessionID": state.get("sessionID") or "",
                "properties": {
                    "info": {
                        "id": message_id,
                        "sessionID": state.get("sessionID") or "",
                        "role": state.get("role") or "assistant",
                        "parts": parts,
                    }
                },
            }

        for event in events:
            if not isinstance(event, dict):
                continue

            event_type = str(event.get("type") or "").strip()
            if event_type not in {"message.updated", "message.part.updated", "message.part.delta"}:
                merged_events.append(event)
                continue

            raw_properties = event.get("properties")
            properties: dict[str, Any] = raw_properties if isinstance(raw_properties, dict) else {}
            event_role = str(event.get("role") or "assistant")
            fallback_sid = str(event.get("sessionID") or "")

            if event_type == "message.updated":
                raw_info = properties.get("info")
                info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
                message_id = str(info.get("id") or "").strip()
                if not message_id:
                    merged_events.append(event)
                    continue

                session_id = str(info.get("sessionID") or fallback_sid or "").strip()
                role = str(info.get("role") or event_role or "assistant")
                state = _ensure_message_state(message_id, session_id, role, event_role)
                if session_id:
                    state["sessionID"] = session_id
                if role:
                    state["role"] = role

                raw_info_parts = info.get("parts")
                info_parts: list[Any] = raw_info_parts if isinstance(raw_info_parts, list) else []
                for part in info_parts:
                    if isinstance(part, dict):
                        _upsert_part(state, part, event_type)

                _refresh_message_event(state)
                continue

            message_id = str(properties.get("messageID") or "").strip()
            if not message_id:
                merged_events.append(event)
                continue

            session_id = str(properties.get("sessionID") or fallback_sid or "").strip()
            state = _ensure_message_state(message_id, session_id, "assistant", event_role)
            if session_id:
                state["sessionID"] = session_id

            _upsert_part(state, properties, event_type)
            _refresh_message_event(state)

        return merged_events

    async def _convert_timeline_to_messages(
        self, timeline_items: list[dict[str, Any]], session_id: str
    ) -> list[dict[str, Any]]:
        """将 skilldev timeline_items 转换为 client 消息格式。

        Args:
            timeline_items: skilldev.restore 返回的原始事件列表
            session_id: 外部会话 ID

        Returns:
            client 消息列表，按 seq 顺序排列
        """
        messages: list[dict[str, Any]] = []
        pending_confirms: list[dict[str, Any]] = []
        # HTTP history replay may overlap a live WebSocket stream. Bind the
        # existing event handlers to an isolated channel view so replay never
        # swaps or mutates the live channel's aggregation/persistence state.
        replay_channel = copy.copy(self)
        replay_channel._message_ctx = {}
        replay_channel._pending_confirms = {}
        replay_channel._detached_tool_parts = {}
        replay_channel._store = _TimelineReplayStore()

        # 需要 replay 的事件类型（使用流式处理器处理）
        replayable_event_keys = (
            "skilldev.skill_name_ready",
            "skilldev.agent_thinking",
            "skilldev.agent_output",
            "skilldev.tool_call",
            "skilldev.tool_result",
            "skilldev.file_ready",
            "skilldev.todos_update",
            "skilldev.confirm_request",
            "skilldev.ask_user_question",
            "skilldev.error",
            "skilldev.completed",
        )
        all_handlers = replay_channel._get_skilldev_event_handlers()
        replayable_handlers = {
            key: handler
            for key, handler in all_handlers.items()
            if key in replayable_event_keys
        }

        # 用户输入事件标记一次 Agent 回合开始：恢复时需要清理上一轮 assistant
        # message 聚合状态，使下一轮流式事件落到新的 message_id 上，避免多轮
        # Agent 对话被合并成同一条 assistant message。
        round_boundary_user_events = (
            "skilldev.user_start",
            "skilldev.user_chat",
            "skilldev.user_answer",
            "skilldev.user_respond",
        )

        try:
            for item in timeline_items:
                source = item.get("source", "assistant")
                event_type = item.get("event_type", "")
                payload = item.get("payload", {}) or {}
                role = "user" if source == "user" else "assistant"

                if event_type in round_boundary_user_events:
                    replay_channel._clear_message_context_for_session(session_id)

                # skilldev.agent_completed：Agent 单轮结束（Agent 模式专有）。
                # 仅用于划分回合，不向客户端补发 session.status / 关 WS 等副作用。
                # 实际清理放在下一条用户输入事件处统一处理。
                if event_type == "skilldev.agent_completed":
                    continue

                # 1. skilldev.user_start → message.send
                if event_type == "skilldev.user_start":
                    query = payload.get("query", "")
                    parts: list[dict[str, Any]] = []

                    if query:
                        parts.append({"type": "text", "text": query})

                    for file_info in payload.get("files", []) or []:
                        if not isinstance(file_info, dict):
                            continue
                        parts.append(self._message_send_file_part(file_info))

                    for skill_pkg in payload.get("skill_packages", []) or []:
                        if not isinstance(skill_pkg, dict):
                            continue
                        parts.append(
                            self._message_send_file_part(skill_pkg, resource_type="skill")
                        )

                    for tool_def in payload.get("tool_spec_files", []) or []:
                        if not isinstance(tool_def, dict):
                            continue
                        tool_output_schema = (
                            tool_def.get("outputSchema") or tool_def.get("output_schema") or {}
                        )
                        if not isinstance(tool_output_schema, dict):
                            tool_output_schema = {}
                        parts.append({
                            "type": "toolDefinition",
                            "bundleName": str(
                                tool_def.get("bundleName") or tool_def.get("pluginId") or ""
                            ),
                            "pluginType": str(tool_def.get("pluginType") or ""),
                            "toolType": str(tool_def.get("toolType") or ""),
                            "toolName": str(tool_def.get("toolName") or ""),
                            "description": str(tool_def.get("description") or ""),
                            "arguments": tool_def.get("arguments", {}),
                            "outputSchema": tool_output_schema,
                            "protocol": str(tool_def.get("protocol") or ""),
                            "deviceCommand": tool_def.get("deviceCommand"),
                        })

                    for agent_def in payload.get("agent_definitions", []) or []:
                        if not isinstance(agent_def, dict):
                            continue
                        parameters = agent_def.get("parameters", {})
                        if not isinstance(parameters, dict):
                            parameters = {}
                        agent_output_schema = (
                            agent_def.get("outputSchema") or agent_def.get("output_schema") or {}
                        )
                        if not isinstance(agent_output_schema, dict):
                            agent_output_schema = {}
                        parts.append({
                            "type": "agentDefinition",
                            "agentId": str(agent_def.get("agentId") or agent_def.get("agent_id") or ""),
                            "name": str(agent_def.get("name") or ""),
                            "description": str(agent_def.get("description") or ""),
                            "parameters": parameters,
                            "outputSchema": agent_output_schema,
                        })

                    for cli_def in payload.get("cli_definitions", []) or []:
                        if not isinstance(cli_def, dict):
                            continue
                        input_schema = cli_def.get("inputSchema") or cli_def.get("input_schema") or {}
                        if not isinstance(input_schema, dict):
                            input_schema = {}
                        output_schema = cli_def.get("outputSchema") or cli_def.get("output_schema") or {}
                        if not isinstance(output_schema, dict):
                            output_schema = {}
                        require_permissions = (
                            cli_def.get("requirePermissions") or cli_def.get("require_permissions") or []
                        )
                        if not isinstance(require_permissions, list):
                            require_permissions = []
                        parts.append({
                            "type": "cliDefinition",
                            "name": str(cli_def.get("name") or ""),
                            "version": str(cli_def.get("version") or ""),
                            "description": str(cli_def.get("description") or ""),
                            "executeSide": str(cli_def.get("executeSide") or cli_def.get("execute_side") or ""),
                            "requirePermissions": require_permissions,
                            "inputSchema": input_schema,
                            "outputSchema": output_schema,
                        })

                    messages.append({
                        "role": role,
                        "sessionID": session_id,
                        "type": "message.send",
                        "parts": parts,
                    })
                    continue

                if event_type == "skilldev.user_chat":
                    query = str(payload.get("message") or payload.get("query") or "")
                    chat_parts: list[dict[str, Any]] = []
                    if query:
                        chat_parts.append({"type": "text", "text": query})
                    messages.append({
                        "role": role,
                        "sessionID": session_id,
                        "type": "message.send",
                        "parts": chat_parts,
                    })
                    continue

                if event_type == "skilldev.user_answer":
                    request_id = str(payload.get("request_id") or "").strip()
                    answers_payload = payload.get("answers", [])
                    reply_answers: list[list[str]] = []
                    if isinstance(answers_payload, list):
                        for ans in answers_payload:
                            if isinstance(ans, dict):
                                sel = ans.get("selected_options", [])
                                if isinstance(sel, list):
                                    reply_answers.append([str(x) for x in sel])
                                else:
                                    reply_answers.append([])
                            else:
                                reply_answers.append([])
                    messages.append({
                        "role": role,
                        "sessionID": session_id,
                        "type": "question.replied",
                        "properties": {
                            "sessionID": session_id,
                            "requestID": request_id,
                            "answers": reply_answers,
                        },
                    })
                    continue

                # 2. skilldev.user_respond → *.replied（根据上一条 confirm_request 反推）
                if event_type == "skilldev.user_respond":
                    action = payload.get("action", "")
                    answers = payload.get("answers", [])
                    feedback = payload.get("feedback")
                    task_id = str(payload.get("task_id") or payload.get("session_id") or "").strip()
                    pending = None
                    if pending_confirms:
                        if task_id:
                            for idx in range(len(pending_confirms) - 1, -1, -1):
                                candidate = pending_confirms[idx]
                                candidate_task_id = str(candidate.get("task_id") or "").strip()
                                if not candidate_task_id or candidate_task_id == task_id:
                                    pending = pending_confirms.pop(idx)
                                    break
                        if pending is None:
                            pending = pending_confirms.pop()

                    confirm_type = str(
                        payload.get("confirm_type")
                        or (pending or {}).get("confirm_type")
                        or "question_clarify"
                    )
                    request_id = str((pending or {}).get("request_id") or "")

                    if confirm_type in _SKILLDEV_REVIEW_CONFIRM_TYPES:
                        messages.append({
                            "role": role,
                            "sessionID": session_id,
                            "type": "review.replied",
                            "properties": {
                                "id": request_id,
                                "sessionID": session_id,
                                "accept": str(action).strip() == "accept",
                                "feedback": feedback,
                            },
                        })
                        continue

                    pending_data = pending or {}
                    questions_raw = pending_data.get("questions")
                    question_defs = questions_raw if isinstance(questions_raw, list) else []
                    answer_map: dict[str, Any] = {}
                    if isinstance(answers, list):
                        for answer_item in answers:
                            if not isinstance(answer_item, dict):
                                continue
                            question_id = str(answer_item.get("question_id") or answer_item.get("id") or "").strip()
                            if question_id:
                                answer_map[question_id] = answer_item.get("answer")

                    reply_answers: list[list[str]] = []
                    for idx, question in enumerate(question_defs):
                        if not isinstance(question, dict):
                            reply_answers.append([])
                            continue
                        question_id = str(question.get("id") or f"q_{idx + 1}")
                        answer_value = answer_map.get(question_id, "")
                        if isinstance(answer_value, list):
                            reply_answers.append([str(v) for v in answer_value])
                        elif answer_value:
                            reply_answers.append([str(answer_value)])
                        else:
                            reply_answers.append([])

                    messages.append({
                        "role": role,
                        "sessionID": session_id,
                        "type": "question.replied",
                        "properties": {
                            "sessionID": session_id,
                            "requestID": request_id,
                            "answers": reply_answers,
                        },
                    })
                    continue

                if event_type == "skilldev.confirm_request":
                    payload = dict(payload)
                    request_id = str(payload.get("request_id") or f"req_{item.get('seq') or secrets.token_hex(4)}")
                    payload["request_id"] = request_id
                    task_id = str(payload.get("task_id") or "")
                    data = payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}
                    pending_confirms.append({
                        "request_id": request_id,
                        "task_id": task_id,
                        "confirm_type": str(payload.get("confirm_type") or "").strip(),
                        "questions": data.get("questions", []),
                    })

                if event_type == "skilldev.ask_user_question":
                    payload = dict(payload)
                    request_id = str(payload.get("request_id") or f"req_{item.get('seq') or secrets.token_hex(4)}")
                    payload["request_id"] = request_id
                    task_sid = str(payload.get("task_id") or payload.get("session_id") or "")
                    raw_q = payload.get("questions", []) if isinstance(payload.get("questions"), list) else []
                    pending_confirms.append({
                        "request_id": request_id,
                        "task_id": task_sid,
                        "confirm_type": "question_clarify",
                        "questions": raw_q,
                    })

                handler = replayable_handlers.get(event_type)
                if handler is not None:
                    responses = await handler(payload, session_id, session_id)
                    for response in responses:
                        if not isinstance(response, dict):
                            continue
                        replay_message = dict(response)
                        replay_message.setdefault("role", role)
                        replay_message.setdefault("sessionID", session_id)
                        messages.append(replay_message)
                    if responses:
                        continue

                logger.debug(
                    "[VibeSkillChannel] unhandled timeline event: event_type=%s",
                    event_type,
                )

            return self._merge_message_update_events(messages)
        finally:
            replay_channel._message_ctx.clear()
            replay_channel._pending_confirms.clear()
            replay_channel._detached_tool_parts.clear()

    async def http_handler(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        """处理 VibeSkill HTTP REST 请求。

        统一入口：HTTP 和 WebSocket 共用同一端口 /api/v1。
        """
        path_str = str(path or "").strip()
        parsed = urlparse(path_str)
        request_path = parsed.path

        _meth_u = (method or "").strip().upper() or "?"
        if request_path == "/api/v1/session" and _meth_u == "POST":
            _sid_http = "n/a"
        elif request_path.startswith("/api/v1/session/"):
            _rest_h = request_path[len("/api/v1/session/"):].strip("/")
            _sid_http = _rest_h.split("/")[0] if _rest_h else "n/a"
        else:
            _sid_http = "n/a"
        _path_for_log = path_str if len(path_str) <= 512 else f"{path_str[:512]}...<truncated>"
        logger.info(
            "[VibeSkillChannel] %s 接口请求, session_id=%s path=%s",
            _meth_u,
            _sid_http,
            _path_for_log,
        )

        # AK/SK 鉴权
        if self._auth_enabled:
            from jiuwenclaw.gateway.auth import check_http_auth
            logger.info("[Auth] http check start")
            ok, error_msg = check_http_auth(self._auth_enabled, headers)
            if not ok:
                logger.warning("[Auth] http check fail: %s", error_msg)
                return (401, {"Content-Type": "application/json"}, json.dumps({"error": error_msg}).encode("utf-8"))
            logger.info("[Auth] http check ok")

        # Session 路由
        if path_str == "/api/v1/session" and method == "POST":
            return await self._handle_http_session_create(headers, body)
        if request_path.startswith("/api/v1/session/") and request_path.endswith("/file/content") and method == "GET":
            session_id = request_path.split("/api/v1/session/", 1)[-1].replace("/file/content", "")
            return await self._handle_http_file_content(session_id, headers, path_str)
        if request_path.startswith("/api/v1/session/") and request_path.endswith("/file/content") and method == "PUT":
            session_id = request_path.split("/api/v1/session/", 1)[-1].replace("/file/content", "")
            return await self._handle_http_file_content_write(session_id, headers, path_str, body)
        if request_path.startswith("/api/v1/session/") and request_path.endswith("/abort") and method == "POST":
            session_id = request_path.replace("/api/v1/session/", "").replace("/abort", "")
            return await self._handle_http_session_abort(session_id)
        if request_path.startswith("/api/v1/session/") and request_path.endswith("/messages") and method == "GET":
            session_id = request_path.replace("/api/v1/session/", "").replace("/messages", "")
            return await self._handle_http_session_message(session_id, headers)
        if request_path.startswith("/api/v1/session/") and request_path.endswith("/summarize") and method == "POST":
            session_id = request_path.replace("/api/v1/session/", "").replace("/summarize", "")
            return await self._handle_http_session_summarize(session_id)
        if (
            request_path.startswith("/api/v1/session/")
            and request_path.endswith("/attachments")
            and method == "DELETE"
        ):
            session_id = request_path.split("/api/v1/session/", 1)[-1].replace("/attachments", "")
            return await self._handle_http_attachments_delete(session_id, body)
        if request_path.startswith("/api/v1/session/") and method == "DELETE":
            session_id = request_path.split("/api/v1/session/", 1)[-1]
            return await self._handle_http_session_delete(session_id)

        # 文件路由
        if request_path.startswith("/api/v1/session/") and request_path.endswith("/file") and method == "GET":
            session_id = request_path.split("/api/v1/session/", 1)[-1].replace("/file", "")
            return await self._handle_http_file_list(session_id, headers)
        if request_path.startswith("/api/v1/session/") and request_path.endswith("/file/status") and method == "GET":
            session_id = request_path.split("/api/v1/session/", 1)[-1].replace("/file/status", "")
            return await self._handle_http_file_status(session_id, headers)

        # 搜索路由
        if request_path.startswith("/api/v1/session/") and request_path.endswith("/find") and method == "GET":
            session_id = request_path.split("/api/v1/session/", 1)[-1].replace("/find", "")
            return await self._handle_http_find(session_id, headers)
        if request_path.startswith("/api/v1/session/") and request_path.endswith("/find/file") and method == "GET":
            session_id = request_path.split("/api/v1/session/", 1)[-1].replace("/find/file", "")
            return await self._handle_http_find_file(session_id, headers)

        # VCS 路由
        if request_path.startswith("/api/v1/session/") and request_path.endswith("/vcs") and method == "GET":
            session_id = request_path.split("/api/v1/session/", 1)[-1].replace("/vcs", "")
            return await self._handle_http_vcs(session_id)

        # 版本路由
        if request_path.startswith("/api/v1/session/") and request_path.endswith("/version") and method == "POST":
            session_id = request_path.split("/api/v1/session/", 1)[-1].replace("/version", "")
            return await self._handle_http_version_create(session_id, body)
        if request_path.startswith("/api/v1/session/") and request_path.endswith("/version") and method == "GET":
            session_id = request_path.split("/api/v1/session/", 1)[-1].replace("/version", "")
            return await self._handle_http_version_list(session_id, headers)
        if request_path.startswith("/api/v1/session/") and request_path.endswith("/rollback") and method == "POST":
            # /api/v1/session/{sessionID}/version/{commitHash}/rollback
            parts = request_path.replace("/api/v1/session/", "").replace("/rollback", "").split("/version/")
            session_id = parts[0]
            commit_hash = parts[1] if len(parts) > 1 else ""
            return await self._handle_http_version_rollback(session_id, commit_hash)
        if request_path.startswith("/api/v1/session/") and request_path.endswith("/diff") and method == "GET":
            # /api/v1/session/{sessionID}/version/{commitHash}/diff
            parts = request_path.replace("/api/v1/session/", "").replace("/diff", "").split("/version/")
            session_id = parts[0]
            commit_hash = parts[1] if len(parts) > 1 else ""
            return await self._handle_http_version_diff(session_id, commit_hash, headers)

        # 导出路由
        if request_path.startswith("/api/v1/session/") and request_path.endswith("/export") and method == "POST":
            session_id = request_path.split("/api/v1/session/", 1)[-1].replace("/export", "")
            return await self._handle_http_export(session_id, body)

        # 注册 skill 路由（仅支持 Standard mode）
        if (
            request_path.startswith("/api/v1/session/")
            and request_path.endswith("/register-skill")
            and method == "POST"
        ):
            session_id = request_path.split("/api/v1/session/", 1)[-1].replace("/register-skill", "")
            return await self._handle_http_register_skill(session_id, body)

        # Session GET 兜底必须放在最后，避免覆盖更具体的子路由（如 /file）。
        if request_path.startswith("/api/v1/session/") and method == "GET":
            session_id = request_path.split("/api/v1/session/", 1)[-1]
            return await self._handle_http_session_get(session_id)

        return (404, {"Content-Type": "application/json"}, b'{"error": "Not found"}')

    def _json_response(self, status: int, data: Any) -> tuple[int, dict[str, str], bytes]:
        """构建 JSON 响应。"""
        body = json.dumps(data, ensure_ascii=False)
        return (status, {"Content-Type": "application/json", "Connection": "close"}, body.encode("utf-8"))

    def _session_user_id(self, session_id: str | None) -> str | None:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        return self._store.get_user_id(sid) or sid

    @staticmethod
    def _session_service_id_for_tenant(
        session_id: str | None,
        *,
        client_session_id: str | None = None,
    ) -> str:
        """按 session 维度路由 AgentServer 租户 workspace（与 Standard chat.send 一致）。"""
        return str(client_session_id or session_id or "").strip()

    @classmethod
    def _apply_tenant_service_id(
        cls,
        params: dict[str, Any],
        session_id: str | None,
        *,
        client_session_id: str | None = None,
    ) -> None:
        service_id = cls._session_service_id_for_tenant(
            session_id,
            client_session_id=client_session_id,
        )
        if service_id:
            params["service_id"] = service_id

    @staticmethod
    def _is_restore_task_missing_error(exc: RestoreChunkDecodeError) -> bool:
        """skilldev.restore 在尚无 checkpoint 时返回「任务 xxx 不存在」。"""
        return "不存在" in str(exc)

    async def _empty_session_messages_response(
        self, session_id: str
    ) -> tuple[int, dict, bytes]:
        session_exists = await self._store.get_session(session_id) is not None
        if not session_exists:
            return self._json_response(404, {"error": "session_not_found"})
        return self._json_response(200, {"total": 0, "messages": []})

    async def _send_agent_request(self, env) -> Any:
        """发送请求到 AgentServer 并返回响应。"""
        request_id = str(getattr(env, "request_id", "") or "")
        interface_info.link_request(request_id)
        with interface_info.bind_request(request_id):
            return await self._agent_client.send_request(env)

    async def _send_agent_request_stream(self, env):
        """发送流式请求，并将内部 request_id 关联到当前 Channel 请求。"""
        request_id = str(getattr(env, "request_id", "") or "")
        interface_info.link_request(request_id)
        stream = self._agent_client.send_request_stream(env)
        try:
            while True:
                with interface_info.bind_request(request_id):
                    try:
                        chunk = await stream.__anext__()
                    except StopAsyncIteration:
                        return
                yield chunk
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                with interface_info.bind_request(request_id):
                    await close()

    async def _ensure_session_restored(self, session_id: str) -> VibeSkillSession | None:
        """从 DCS 恢复本 gateway 陌生的 session 元数据。

        工作区由 SandboxRouter 在后续 ``send_request`` 时按 session_id 自动从 DCS 恢复。
        """
        sid = str(session_id or "").strip()
        if not sid:
            return None
        return await self._store.resolve_session(sid)

    async def _handle_http_session_create(self, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
        """POST /api/v1/session - 创建会话。

        仅创建本地 session 记录，配置数据由 message.send 的 parts 传入。
        """
        # 解析请求体，获取 mode
        mode = "SkillCreate"  # 默认值
        requested_user_id = ""
        if body:
            try:
                req_body = json.loads(body.decode("utf-8"))
                mode = str(req_body.get("mode", "SkillCreate")).strip()
                requested_user_id = str(req_body.get("user_id") or req_body.get("userId") or "").strip()
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._json_response(400, {"error": "Invalid JSON format"})

        # 验证 mode
        if mode not in ("SkillCreate", "Standard"):
            return self._json_response(400, {"error": f"Invalid mode: {mode}"})

        if mode == "Standard":
            # 创建 jiuwenclaw 标准 session
            return await self._create_standard_session(requested_user_id)

        # 创建 VibeSkill session（SkillCreate 模式）
        session = await self._store.get_or_create(session_id=None, mode=mode)
        session_id = session.session_id
        user_id = requested_user_id or session_id
        await self._store.set_metadata(session_id, {"user_id": user_id})

        response_data = {
            "sessionID": session_id,
            "time": {
                "created": int(session.created_at * 1000),
                "updated": int(time.time() * 1000),
            },
            "status": {
                "sessionStatus": session.state.value,
                "sandboxStatus": "none",
            },
        }
        return self._json_response(200, response_data)

    async def _create_standard_session(self, requested_user_id: str = "") -> tuple[int, dict, bytes]:
        """创建 jiuwenclaw 标准 session（Standard mode）。

        通过 MessageHandler._create_agent_session 创建物理 session，
        并存储到本地 _store 中。
        """
        # 生成 session ID（与前端一致）
        ts = format(int(time.time() * 1000), "x")
        suffix = secrets.token_hex(3)
        session_id = f"sess_{ts}_{suffix}"
        user_id = str(requested_user_id or session_id).strip()

        # 通过 ChannelManager.create_agent_session 创建 session
        channel_manager = cast("ChannelManager", self.bus)
        created_session_id = await channel_manager.create_agent_session(session_id, user_id=user_id)

        # 存储到本地 _store，标记为 Standard mode
        await self._store.get_or_create(session_id=session_id, mode="Standard")
        await self._store.set_metadata(session_id, {"user_id": user_id})

        # HTTP 200 返回 response
        response_data = {
            "sessionID": created_session_id,
            "time": {
                "created": int(time.time() * 1000),
                "updated": int(time.time() * 1000),
            },
            "status": {
                "sessionStatus": "idle",
                "sandboxStatus": "none",
            },
        }
        return self._json_response(200, response_data)

    async def _handle_http_register_skill(self, session_id: str, body: bytes) -> tuple[int, dict, bytes]:
        """POST /api/v1/session/{sessionID}/register-skill - 注册远程 skill 包。

        仅支持 Standard mode 的 session。
        """
        # 解析 body
        if not body:
            return self._json_response(400, {"error": "Missing request body"})
        try:
            req_body = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json_response(400, {"error": "Invalid JSON"})
        skills = req_body.get("skills", [])
        if not skills:
            return self._json_response(400, {"error": "Missing skills field or empty list"})

        # 校验 session 存在
        session_obj = await self._store.get_session(session_id)
        if not session_obj:
            return self._json_response(404, {"error": f"Session not found: {session_id}"})

        # 仅支持 Standard mode
        if session_obj.mode != "Standard":
            err_msg = "register-skill is only supported for Standard mode sessions"
            return self._json_response(
                400,
                {"error": f"{err_msg}, current mode: {session_obj.mode}"}
            )

        # 注册每个 skill
        channel_manager = cast("ChannelManager", self.bus)
        for skill in skills:
            skill_url = skill.get("url", "").strip()
            if not skill_url:
                return self._json_response(400, {"error": "Missing url in skills"})
            # 验证 URL 格式有效性
            try:
                parsed = urlparse(skill_url)
                if not parsed.scheme or not parsed.netloc:
                    return self._json_response(400, {"error": "Invalid skill URL"})
            except AttributeError:
                return self._json_response(400, {"error": "Skill URL must be a string"})
            except Exception:
                return self._json_response(400, {"error": "Failed to parse skill URL"})
            await channel_manager.register_skill(
                session_id,
                skill_url,
                user_id=self._session_user_id(session_id),
            )

        return self._json_response(200, {"registered": True})

    async def _handle_http_session_get(self, session_id: str) -> tuple[int, dict, bytes]:
        """GET /api/v1/session/{id} - 查询会话状态。"""
        # Validate sessionID is not empty
        if not session_id:
            return self._json_response(400, {"error": "sessionID cannot be empty"})
        # Validate sessionID format: reject numeric-only IDs (type error)
        if session_id.isdigit():
            return self._json_response(400, {"error": "sessionID cannot be purely numeric"})
        # Validate sessionID for potentially dangerous characters (XSS prevention)
        if any(c in session_id for c in '<>"\'&=+'):
            return self._json_response(400, {"error": "invalid sessionID format"})

        session_obj = await self._store.get_session(session_id)
        if not session_obj:
            return self._json_response(404, {"error": "session_not_found"})

        response_data = {
            "sessionID": session_id,
            "time": {
                "created": int(session_obj.created_at * 1000),
                "updated": int(session_obj.updated_at * 1000),
            },
            "status": {
                "sessionStatus": session_obj.state.value,
                "sandboxStatus": "none",
            },
        }
        logger.info(
            "[VibeSkillChannel] http.session.get response session_id=%s body=%s",
            session_id,
            json.dumps(response_data, ensure_ascii=False),
        )
        return self._json_response(200, response_data)

    async def _handle_http_session_abort(self, session_id: str) -> tuple[int, dict, bytes]:
        """POST /api/v1/session/{id}/abort - 中止 AI 处理。

        要求该 session 仍有活跃的北向 WebSocket；取消路径与 WS 断连一致（SKILLDEV_CANCEL + MessageHandler）。
        """
        session_obj = await self._store.get_session(session_id)
        if not session_obj:
            return self._json_response(404, {"error": "session_not_found"})

        if self._get_active_ws_for_session(session_id) is None:
            return self._json_response(
                400,
                {"error": "websocket_not_connected", "message": "WebSocket connection does not exist"},
            )

        await self._cancel_session_via_message_handler(
            session_id,
            source="http.session.abort",
            mode=session_obj.mode,
        )
        return self._json_response(200, {"aborted": True})

    async def _handle_http_session_message(self, session_id: str, headers: dict) -> tuple[int, dict, bytes]:
        """GET /api/v1/session/{id}/messages - 获取历史消息。

        通过 skilldev.restore API 获取 SkillDev 会话的历史时间线。
        """
        if not session_id:
            return self._json_response(400, {"error": "missing_session_id"})

        try:
            restore_params: dict[str, Any] = {"task_id": session_id}
            self._apply_tenant_service_id(restore_params, session_id)

            def _open_restore_stream():
                request_id = (
                    f"vibeskill-session-msg-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"
                )
                env = e2a_from_agent_fields(
                    request_id=request_id,
                    channel_id=VIBESKILL_CHANNEL_ID,
                    session_id=session_id,
                    req_method=ReqMethod.SKILLDEV_RESTORE,
                    params=restore_params,
                    is_stream=True,
                    timestamp=time.time(),
                    user_id=self._session_user_id(session_id),
                )
                return self._send_agent_request_stream(env)

            try:
                payload = await decode_restore_payload_from_stream_with_retry(
                    _open_restore_stream,
                    log_label="VibeSkillChannel",
                )
            except RestoreChunkDecodeError as exc:
                if self._is_restore_task_missing_error(exc):
                    session_obj = await self._store.get_session(session_id)
                    if session_obj is not None:
                        logger.info(
                            "[VibeSkillChannel] skilldev.restore no checkpoint yet, "
                            "returning empty messages: session_id=%s",
                            session_id,
                        )
                        return self._json_response(200, {"total": 0, "messages": []})
                logger.info(
                    "[VibeSkillChannel] skilldev.restore stream error: session_id=%s code=%s error=%s",
                    session_id,
                    exc.code,
                    exc,
                )
                return self._json_response(500, {"error": str(exc), "code": exc.code})

            if not payload:
                return await self._empty_session_messages_response(session_id)

            # 转换 timeline_items 为 client 消息格式
            timeline_items = payload.get("timeline_items", []) if isinstance(payload, dict) else []
            messages = await self._convert_timeline_to_messages(timeline_items, session_id)

            logger.info(
                "[VibeSkillChannel] session messages: session_id=%s total=%d",
                session_id,
                len(messages),
            )
            return self._json_response(200, {"total": len(messages), "messages": messages})

        except Exception as exc:
            logger.warning("[VibeSkillChannel] Failed to get session messages: %s", exc)
            return self._json_response(500, {"error": str(exc)})

    async def _handle_http_session_summarize(self, session_id: str) -> tuple[int, dict, bytes]:
        """POST /api/v1/session/{id}/summarize - 触发会话总结。"""
        return self._json_response(202, {"triggered": True})

    async def _handle_http_session_delete(self, session_id: str) -> tuple[int, dict, bytes]:
        """DELETE /api/v1/session/{id} - 删除会话。"""
        sid = str(session_id or "").strip().strip("/")
        if not sid:
            return self._json_response(400, {"error": "sessionID cannot be empty"})
        session_obj = await self._store.resolve_session(sid)
        if session_obj is None:
            return self._json_response(404, {"error": "session_not_found"})
        result = await self._teardown_session(
            session_obj.session_id,
            source="http.session.delete",
        )
        return self._json_response(200, result)

    async def _handle_http_attachments_delete(
        self, session_id: str, body: bytes
    ) -> tuple[int, dict, bytes]:
        """DELETE /api/v1/session/{id}/attachments — skilldev.resource.delete."""
        sid = str(session_id or "").strip().strip("/")
        if not sid:
            return self._json_response(400, {"error": "sessionID cannot be empty"})
        session_obj = await self._store.resolve_session(sid)
        if session_obj is None:
            return self._json_response(404, {"error": "session_not_found"})

        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            return self._json_response(400, {"error": "invalid JSON body"})
        if not isinstance(payload, dict):
            return self._json_response(400, {"error": "body must be a JSON object"})

        err, items = validate_attachment_delete_items(payload.get("items"))
        if err or items is None:
            return self._json_response(400, {"error": err or "invalid items"})

        request_id = f"vibeskill-att-del-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"
        delete_params: dict[str, Any] = {
            "task_id": session_obj.session_id,
            "session_id": session_obj.session_id,
            "items": items,
        }
        self._apply_tenant_service_id(delete_params, session_obj.session_id)
        env = e2a_from_agent_fields(
            request_id=request_id,
            channel_id=VIBESKILL_CHANNEL_ID,
            session_id=session_obj.session_id,
            req_method=ReqMethod.SKILLDEV_RESOURCE_DELETE,
            params=delete_params,
            is_stream=False,
            timestamp=time.time(),
            user_id=self._session_user_id(session_obj.session_id),
        )
        logger.info(
            "[VibeSkillChannel] skilldev.resource.delete sent, session_id=%s items=%d",
            session_obj.session_id,
            len(items),
        )
        resp = await self._send_agent_request(env)
        if not resp.ok:
            pl = dict(resp.payload) if isinstance(resp.payload, dict) else {}
            return self._json_response(502, {"error": str(pl.get("error") or "request failed")})
        result = dict(resp.payload) if isinstance(resp.payload, dict) else {}
        if result.get("event_type") == "skilldev.error":
            return self._json_response(400, {"error": str(result.get("error") or "skilldev.error")})
        return self._json_response(
            200,
            {
                "ok": bool(result.get("ok", True)),
                "deleted": result.get("deleted") if isinstance(result.get("deleted"), list) else [],
                "notFound": (
                    result.get("notFound") if isinstance(result.get("notFound"), list) else []
                ),
                "errors": result.get("errors") if isinstance(result.get("errors"), list) else [],
            },
        )

    async def _handle_http_file_list(self, session_id: str, headers: dict) -> tuple[int, dict, bytes]:
        """GET /api/v1/.../file — 列目录（skilldev.file.list → FileTreeNode[] 嵌套树）。"""
        request_id = f"vibeskill-file-list-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"
        file_list_params: dict[str, Any] = {"task_id": session_id, "session_id": session_id}
        self._apply_tenant_service_id(file_list_params, session_id)
        env = e2a_from_agent_fields(
            request_id=request_id,
            channel_id=VIBESKILL_CHANNEL_ID,
            session_id=session_id,
            req_method=ReqMethod.SKILLDEV_FILE_LIST,
            params=file_list_params,
            is_stream=False,
            timestamp=time.time(),
            user_id=self._session_user_id(session_id),
        )
        logger.info(
            "[VibeSkillChannel] skilldev.file.list sent, session_id=%s",
            session_id,
        )
        resp = await self._send_agent_request(env)
        if not resp.ok:
            pl = dict(resp.payload) if isinstance(resp.payload, dict) else {}
            return self._json_response(502, {"error": str(pl.get("error") or "request failed")})
        payload = dict(resp.payload) if isinstance(resp.payload, dict) else {}
        if payload.get("event_type") == "skilldev.error":
            return self._json_response(400, {"error": str(payload.get("error") or "skilldev.error")})
        if not payload.get("ok", True):
            return self._json_response(400, {"error": str(payload.get("error") or "failed")})
        tree = payload.get("tree")
        if not isinstance(tree, list):
            tree = []
        file_tree = skilldev_tree_to_file_tree_nodes(tree, task_id=session_id)
        return self._json_response(200, file_tree)

    @staticmethod
    def _file_content_path(raw_request_path: str) -> str:
        query = parse_qs(urlparse(raw_request_path).query)
        values = query.get("path")
        if not values:
            return ""
        return str(values[0]).strip()

    async def _handle_http_file_content(
        self, session_id: str, headers: dict, raw_request_path: str
    ) -> tuple[int, dict, bytes]:
        """读取工作区文本文件，并将 Agent 的流式分块结果合并为 HTTP 响应。"""
        file_path = self._file_content_path(raw_request_path)
        if not file_path:
            return self._json_response(400, {"error": "path query parameter is required"})

        path_err = validate_file_path(file_path, operation="read")
        if path_err:
            logger.warning(
                "[VibeSkillChannel] file.read rejected (path traversal), session_id=%s path=%r reason=%s",
                session_id,
                file_path,
                path_err,
            )
            return self._json_response(400, {"error": path_err})

        file_read_params: dict[str, Any] = {
            "task_id": session_id,
            "path": file_path,
            "session_id": session_id,
        }
        self._apply_tenant_service_id(file_read_params, session_id)
        logger.info(
            "[VibeSkillChannel] skilldev.file.read sent, session_id=%s",
            session_id,
        )
        try:
            def _open_file_read_stream():
                request_id = (
                    f"vibeskill-file-read-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"
                )
                env = e2a_from_agent_fields(
                    request_id=request_id,
                    channel_id=VIBESKILL_CHANNEL_ID,
                    session_id=session_id,
                    req_method=ReqMethod.SKILLDEV_FILE_READ,
                    params=file_read_params,
                    is_stream=True,
                    timestamp=time.time(),
                    user_id=self._session_user_id(session_id),
                )
                return self._agent_client.send_request_stream(env)

            payload = await decode_file_read_payload_from_stream_with_retry(
                _open_file_read_stream,
                log_label="VibeSkillChannel.file.read",
            )
        except FileReadChunkDecodeError as exc:
            logger.info(
                "[VibeSkillChannel] skilldev.file.read stream error: session_id=%s code=%s error=%s",
                session_id,
                exc.code,
                exc,
            )
            if exc.code == "SKILLDEV_FILE_READ_ERROR":
                return self._json_response(400, {"error": str(exc), "code": exc.code})
            return self._json_response(500, {"error": str(exc), "code": exc.code})
        except Exception as exc:
            logger.warning(
                "[VibeSkillChannel] Failed to read session file content: session_id=%s error=%s",
                session_id,
                exc,
            )
            return self._json_response(500, {"error": str(exc)})
        if payload.get("event_type") == "skilldev.error":
            return self._json_response(400, {"error": str(payload.get("error") or "skilldev.error")})
        if not payload.get("ok", True):
            return self._json_response(400, {"error": str(payload.get("error") or "failed")})
        editable = payload.get("editable")
        if isinstance(editable, bool):
            editable_str = "true" if editable else "false"
        elif isinstance(editable, str) and editable.lower() in ("true", "false"):
            editable_str = editable.lower()
        else:
            editable_str = "true"
        out = {
            "type": "text",
            "content": str(payload.get("content") or ""),
            "editable": editable_str,
            "encoding": "utf-8",
            "mimeType": "text/plain",
        }
        return self._json_response(200, out)

    async def _handle_http_file_content_write(
        self,
        session_id: str,
        headers: dict,
        raw_request_path: str,
        body: bytes,
    ) -> tuple[int, dict, bytes]:
        """PUT .../file/content?path= — skilldev.file.write，覆盖写入文本文件。

        请求体对齐 opencode FileContent schema：``{"content": "..."}``。
        响应体均为 HTTP 200：成功 ``{"ok": True, "path": "...", "size": ...}``，
        业务失败 ``{"ok": False, "error": "..."}``（Agent 通信失败仍为 502）。
        """
        file_path = self._file_content_path(raw_request_path)
        if not file_path:
            return self._json_response(
                200, {"ok": False, "error": "999001500"} # path query parameter is required
            )

        path_err = validate_file_path(file_path, operation="write")
        if path_err:
            logger.warning(
                "[VibeSkillChannel] file.write rejected (path traversal), session_id=%s path=%r reason=%s",
                session_id,
                file_path,
                path_err,
            )
            return self._json_response(400, {"ok": False, "error": path_err})

        try:
            req_body = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json_response(200, {"ok": False, "error": "999001501"}) # invalid JSON body

        if not isinstance(req_body, dict):
            return self._json_response(
                200, {"ok": False, "error": "999001502"} # request body must be a JSON object
            )

        if "content" not in req_body:
            return self._json_response(
                200, {"ok": False, "error": "999001503"} # content field is required
            )

        content = req_body.get("content")
        if not isinstance(content, str):
            return self._json_response(
                200, {"ok": False, "error": "999001504"} # content must be a string
            )
        # Debug aid for mojibake: inspect what gateway actually receives.
        preview = content[:120]
        preview_escaped = preview.encode("unicode_escape").decode("ascii")
        logger.info(
            "[VibeSkillChannel] file.write inbound content preview, "
            "session_id=%s path=%s chars=%d preview=%s preview_unicode_escape=%s",
            session_id,
            file_path,
            len(content),
            repr(preview),
            preview_escaped,
        )

        file_write_params: dict[str, Any] = {
            "task_id": session_id,
            "path": file_path,
            "session_id": session_id,
        }
        self._apply_tenant_service_id(file_write_params, session_id)
        logger.info(
            "[VibeSkillChannel] skilldev.file.write sent, session_id=%s path=%s size=%d",
            session_id,
            file_path,
            len(content),
        )
        try:
            async def _send_write_request(
                phase_params: dict[str, Any], phase: str, attempt: int
            ):
                request_id = (
                    f"vibeskill-file-write-{phase}-{attempt}-"
                    f"{int(time.time() * 1000):x}-{secrets.token_hex(3)}"
                )
                env = e2a_from_agent_fields(
                    request_id=request_id,
                    channel_id=VIBESKILL_CHANNEL_ID,
                    session_id=session_id,
                    req_method=ReqMethod.SKILLDEV_FILE_WRITE,
                    params=phase_params,
                    is_stream=False,
                    timestamp=time.time(),
                    user_id=self._session_user_id(session_id),
                )
                return await self._send_agent_request(env)

            resp = await send_file_write_with_chunks(
                base_params=file_write_params,
                content=content,
                write_id=secrets.token_hex(16),
                send_request=_send_write_request,
            )
        except FileWriteChunkError as exc:
            return self._json_response(400, {"ok": False, "error": str(exc)})
        except FileWriteTransportError as exc:
            logger.warning(
                "[VibeSkillChannel] skilldev.file.write transport failed: session_id=%s error=%s",
                session_id,
                exc,
            )
            return self._json_response(502, {"ok": False, "error": str(exc)})
        if not resp or not getattr(resp, "ok", False):
            pl = dict(resp.payload) if resp and isinstance(resp.payload, dict) else {}
            err = str(pl.get("error") or "request failed")
            return self._json_response(502, {"ok": False, "error": err})

        payload = dict(resp.payload) if isinstance(resp.payload, dict) else {}
        if payload.get("event_type") == "skilldev.error":
            return self._json_response(
                200, {"ok": False, "error": str(payload.get("error") or "skilldev.error")}
            )
        if not payload.get("ok", False):
            return self._json_response(
                200, {"ok": False, "error": str(payload.get("error") or "failed")}
            )

        out: dict[str, Any] = {
            "ok": True,
            "path": str(payload.get("path") or file_path),
        }
        size = payload.get("size")
        if size is not None:
            out["size"] = size
        return self._json_response(200, out)

    async def _handle_http_file_status(self, session_id: str, headers: dict) -> tuple[int, dict, bytes]:
        """GET /api/v1/session/{id}/file/status - Git 状态。"""
        return self._json_response(200, [])

    async def _handle_http_find(self, session_id: str, headers: dict) -> tuple[int, dict, bytes]:
        """GET /api/v1/session/{id}/find - 全文搜索。"""
        return self._json_response(200, [])

    async def _handle_http_find_file(self, session_id: str, headers: dict) -> tuple[int, dict, bytes]:
        """GET /api/v1/session/{id}/find/file - 文件名搜索。"""
        return self._json_response(200, [])

    async def _handle_http_vcs(self, session_id: str) -> tuple[int, dict, bytes]:
        """GET /api/v1/session/{id}/vcs - VCS 信息。"""
        return self._json_response(200, {"branch": "main"})

    async def _handle_http_version_create(self, session_id: str, body: bytes) -> tuple[int, dict, bytes]:
        """POST /api/v1/session/{id}/version - 创建版本快照。"""
        try:
            _ = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._json_response(400, {"error": "Invalid JSON"})
        return self._json_response(200, {
            "commitHash": secrets.token_hex(6),
            "files": [],
            "time": {"created": int(time.time() * 1000)},
        })

    async def _handle_http_version_list(self, session_id: str, headers: dict) -> tuple[int, dict, bytes]:
        """GET /api/v1/session/{id}/version - 版本列表。"""
        return self._json_response(200, {"branch": "main", "total": 0, "commits": []})

    async def _handle_http_version_rollback(self, session_id: str, commit_hash: str) -> tuple[int, dict, bytes]:
        """POST /api/v1/session/{id}/version/{hash}/rollback - 版本回滚。"""
        return self._json_response(200, {"rolledBack": True})

    async def _handle_http_version_diff(
        self, session_id: str, commit_hash: str, headers: dict
    ) -> tuple[int, dict, bytes]:
        """GET /api/v1/session/{id}/version/{hash}/diff - 版本差异。"""
        return self._json_response(
            200,
            {
                "format": "STAT",
                "stats": {
                    "additions": 0,
                    "deletions": 0,
                    "files": 0,
                    "filesChanged": [],
                },
            },
        )

    async def _handle_http_export(self, session_id: str, body: bytes) -> tuple[int, dict, bytes]:
        """POST /api/v1/session/{id}/export - 导出 Skill 产物。"""
        try:
            json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._json_response(400, {"error": "Invalid JSON"})

        request_id = f"vibeskill-export-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"
        session = await self._ensure_session_restored(session_id)
        if not session:
            return self._json_response(404, {"error": "Session not found"})
        if session.mode == "Standard":
            return self._json_response(400, {"error": "Standard 模式不支持导出"})
        if not session.exportable:
            return self._json_response(200, {"error": "会话暂不可导出"})
        download_params: dict[str, Any] = {"task_id": session_id}
        self._apply_tenant_service_id(download_params, session_id)
        env = e2a_from_agent_fields(
            request_id=request_id,
            channel_id=VIBESKILL_CHANNEL_ID,
            session_id=session_id,
            req_method=ReqMethod.SKILLDEV_DOWNLOAD,
            params=download_params,
            is_stream=False,
            timestamp=time.time(),
            user_id=self._session_user_id(session_id),
        )
        logger.info(
            "[VibeSkillChannel] skilldev.download sent, session_id=%s",
            session_id,
        )
        resp = await self._send_agent_request(env)
        payload = resp.payload if isinstance(resp.payload, dict) else {}
        if not resp.ok or not bool(payload.get("ok", True)):
            err = str(payload.get("error") or "skilldev.download failed")
            return self._json_response(502, {"error": err})

        result = {
            "exportId": payload.get("exportId"),
            "url": payload.get("url"),
            "mimeType": payload.get("mimeType"),
            "exportedAt": payload.get("exportedAt"),
        }
        if not result["exportId"] or not result["url"] or not result["mimeType"]:
            return self._json_response(502, {"error": "Invalid response from skilldev.download"})

        new_export_url = str(result["url"] or "").strip()
        current = await self._store.get_session(session_id)
        old_export_url = str((current.last_export_obs_url if current else "") or "").strip()
        if old_export_url and old_export_url != new_export_url:
            await _delete_obs_url(
                old_export_url,
                session_id=session_id,
                reason="export_replace",
            )
        await self._store.set_last_export_obs_url(session_id, new_export_url)

        return self._json_response(200, result)
