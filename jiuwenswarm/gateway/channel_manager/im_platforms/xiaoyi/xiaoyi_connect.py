# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""XiaoyiChannel - 华为小艺 A2A 协议客户端."""

from __future__ import annotations

import logging
import asyncio
import base64
import hmac
import hashlib
import inspect
import json
import os
import re
import ssl
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, List, Optional
from urllib.parse import urlparse, urlsplit

import aiohttp
import httpx

from jiuwenswarm.common.device_rpc.models import DeviceCommandRequest
from jiuwenswarm.common.local_proxy_auth import with_local_proxy_bearer
from jiuwenswarm.common.np_transport import (
    PipeClosedError,
    PipeError,
    PipeStream,
    is_named_pipe_url,
    named_pipe_transport_for,
    open_pipe,
    pipe_path_from_url,
)
from jiuwenswarm.common.secrets_bootstrap import get_secret
from jiuwenswarm.gateway.channel_manager.base import BaseChannel, ChannelMetadata, RobotMessageRouter
from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.gateway.routing.keys import XiaoyiDeliveryTarget
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.push import XiaoYiPushService, PushConfig
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.formatter import (
    get_status_state_for_event,
    get_status_text_for_event,
    should_send_as_reasoning_text,
    should_send_as_status_update,
    should_send_as_text,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.a2a_vars import (
    extract_model_name,
)
from jiuwenswarm.common.permission_profile import (
    normalize_permission_profile,
    resolve_client_workspace,
    resolve_trusted_dirs,
    with_workspace_directive,
)

logger = logging.getLogger(__name__)

# ==================== clientVariables 权限审批回复约定 ====================
# 手机端收到审批提示后直接回复文本（或携带 AgentEvent/PermissionReply data event）。
# 文本回复按归一化（去空白/标点、小写）后的精确匹配识别：
_APPROVAL_APPROVE_WORDS = frozenset({
    "同意", "允许", "批准", "确认", "好", "好的", "是", "可以", "执行", "继续", "本次允许",
    "yes", "y", "ok", "okay", "approve", "approved", "proceed", "allow", "confirm",
})
_APPROVAL_SESSION_WORDS = frozenset({
    "会话内允许", "会话内记住", "本次会话允许", "本次会话内允许", "本会话允许",
    "session_allow", "sessionallow",
})
_APPROVAL_ALWAYS_WORDS = frozenset({
    "永久允许", "永久记住", "总是允许", "始终允许", "一直允许",
    "always_allow", "allow_always", "alwaysallow", "always",
})
_APPROVAL_REJECT_WORDS = frozenset({
    "拒绝", "不同意", "不允许", "取消", "否", "不要", "算了",
    "no", "n", "reject", "rejected", "deny", "denied", "cancel",
})
# PermissionReply data event 的 action → 审批决定
_APPROVAL_EVENT_ACTIONS = {
    "approve": "approve",
    "allow": "approve",
    "agree": "approve",
    "session_allow": "session",
    "always_allow": "always",
    "reject": "reject",
    "deny": "reject",
    "refuse": "reject",
}
# 待答复审批有效期（超时后按普通消息处理，避免陈旧 request_id 误恢复）
_APPROVAL_EXPIRE_S = 30 * 60

_APPROVAL_REPLY_HINT = (
    "请直接回复：同意（仅本次）/ 会话内允许 / 永久允许 / 拒绝。"
    "回复其他内容将视为拒绝，并把该内容作为反馈转达给智能体。"
)


def _normalize_approval_text(text: str) -> str:
    """审批回复归一化：去全部空白与常见标点、转小写，便于精确匹配词表。"""
    return re.sub(r"[\s，。！？!?,.、；;：:~～…·'\"“”‘’（）()\[\]【】]+", "", text or "").lower()


def _is_data_event_status_success(status: Any) -> bool:
    if status is True:
        return True
    if status is None or status is False:
        return False
    return str(status).strip().lower() in ("success", "succeed", "successful", "ok")


def _gui_response_session_id(message: dict[str, Any]) -> str:
    session_id = str(message.get("sessionId") or "").strip()
    if session_id:
        return session_id
    params = message.get("params")
    if isinstance(params, dict):
        session_id = str(params.get("sessionId") or "").strip()
        if session_id:
            return session_id
    detail = message.get("msgDetail")
    if isinstance(detail, str):
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            return ""
        detail_params = parsed.get("params")
        if isinstance(detail_params, dict):
            return str(detail_params.get("sessionId") or "").strip()
    return ""


FILE_TYPE_TO_MIME_TYPE: dict[str, str] = {
    "txt": "text/plain",
    "html": "text/html",
    "css": "text/css",
    "js": "application/javascript",
    "json": "application/json",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "pdf": "application/pdf",
    "zip": "application/zip",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "mp3": "audio/mpeg",
    "mp4": "video/mp4",
}

# 全局 XiaoyiChannel 实例字典（供手机端工具调用使用）
_xiaoyi_channel_instances: dict[str, "XiaoyiChannel"] = {}


def get_xiaoyi_channel(channel_id: str = "xiaoyi") -> Optional["XiaoyiChannel"]:
    """获取指定 channel_id 的 XiaoyiChannel 实例（供手机端工具调用使用）."""
    return _xiaoyi_channel_instances.get(channel_id)


@dataclass
class DataEvent:
    """Data-only 事件数据结构（工具执行结果）."""
    intent_name: str
    outputs: dict
    status: str
    session_id: str = ""
    task_id: str = ""


@dataclass
class XiaoyiChannelConfig:
    """小艺通道配置（客户端模式）."""

    enabled: bool = False
    channel_id: str = ""  # 路由标识，始终为 "xiaoyi"
    mode: str = "xiaoyi_channel"  # xiaoyi_channel or xiaoyi_claw
    ak: str = ""
    sk: str = ""
    agent_id: str = ""
    ws_url1: str = ""
    ws_url2: str = ""
    enable_streaming: bool = True
    # Push notification configuration
    uid: str = ""
    api_key: str = ""
    api_id: str = ""
    push_id: str = ""
    push_url: str = ""
    file_upload_url: str = ""
    # Task timeout in milliseconds (default: 1 hour)
    task_timeout_ms: int = 3600000
    # Session cleanup timeout in milliseconds (default: 1 hour)
    session_cleanup_timeout_ms: int = 3600000


def _generate_signature(sk: str, timestamp: str) -> str:
    """生成 HMAC-SHA256 签名（Base64 编码）."""
    h = hmac.new(
        sk.encode("utf-8"),
        timestamp.encode("utf-8"),
        hashlib.sha256,
    )
    return base64.b64encode(h.digest()).decode("utf-8")


class XYFileUploadService:
    def __init__(self, base_url: str, api_key: str, uid: str):
        self.base_url = base_url.rstrip('/')
        # uid/api_key 强制 str：config.yaml 里纯数字 uid 会被 YAML 解析成 int，
        # 直接进请求头会触发 aiohttp「Cannot serialize non-str key」静默失败
        self.api_key = str(api_key) if api_key is not None else ""
        self.uid = str(uid) if uid is not None else ""
        self.session = None
        # np:// baseUrl（桌面命名管道形态）：phase1/3 经 httpx + 命名管道 transport，
        # phase2（OBS 签名 URL 直传，真实外网地址）仍走 aiohttp + 智能代理分流。
        self._use_named_pipe = is_named_pipe_url(self.base_url)
        self._pipe_client: httpx.AsyncClient | None = None

    @staticmethod
    def _smart_proxy_for(url: str) -> str | None:
        """按目标地址智能路由：环回/内网 IP 直连；公网域名经 CLAW_HTTP_PROXY 出网。

        桌面客户端在子进程环境注入 CLAW_HTTP_PROXY（= 系统代理，custom 优先）——
        OSMS prepare/complete 走 127.0.0.1 本地代理必须直连，而 OBS 直传签名 URL
        （公网域名）在公司内网直连不通，必须走代理。
        """
        try:
            host = urlsplit(url).hostname or ""
        except Exception:
            return None
        if not host:
            return None
        if host in ("localhost", "127.0.0.1", "::1"):
            return None
        if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
            return None
        proxy = (os.environ.get("CLAW_HTTP_PROXY") or "").strip()
        return proxy or None

    def _request(self, method: str, url: str, **kwargs: Any):
        """带智能路由的请求上下文管理器（公网域名自动挂代理）。"""
        kwargs.setdefault("proxy", self._smart_proxy_for(url))
        return self.session.request(method, url, **kwargs)

    async def _request_with_tls_fallback(self, method: str, url: str, **kwargs: Any):
        """证书链错误（公司代理 TLS 拦截私有根 CA 重签）时容忍拦截重试一次。

        仅在经代理访问时兜底；直连目标（本地代理/内网）不做此放宽。
        """
        try:
            async with self._request(method, url, **kwargs) as resp:
                return resp.status, await resp.read()
        except (ssl.SSLError, aiohttp.ClientSSLError, aiohttp.ClientConnectorCertificateError) as exc:
            if not kwargs.get("proxy") and not self._smart_proxy_for(url):
                raise
            logger.warning("[XY File Upload] 代理 TLS 拦截（%s），容忍拦截重试一次", exc)
            kwargs["ssl"] = False
            async with self._request(method, url, **kwargs) as resp:
                return resp.status, await resp.read()

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        if self._use_named_pipe:
            # 控制面（prepare/completeAndQuery）走命名管道；超时对齐 aiohttp 默认 total=300s
            self._pipe_client = httpx.AsyncClient(
                transport=named_pipe_transport_for(self.base_url),
                timeout=httpx.Timeout(300.0, connect=10.0),
            )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._pipe_client is not None:
            await self._pipe_client.aclose()
            self._pipe_client = None
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def _post_control_plane(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> tuple[int, bytes]:
        """phase1/3 控制面 POST（打向 baseUrl 派生地址，即本机代理或上游网关）。

        打本地代理（np:// 管道 / loopback 直连形态）时补 Authorization: Bearer
        <uploadToken>（密钥包下发，取不到则不带，兼容旧版桌面零鉴权代理）。
        np:// 经 httpx + 命名管道 transport；http(s):// 保持 aiohttp + TLS 拦截兜底。
        """
        headers = with_local_proxy_bearer(headers, url)
        if self._pipe_client is not None:
            resp = await self._pipe_client.post(url, json=payload, headers=headers)
            return resp.status_code, resp.content
        return await self._request_with_tls_fallback("POST", url, json=payload, headers=headers)

    async def upload_file(self, file_path: str, object_type: str = "TEMPORARY_MATERIAL_DOC") -> Optional[str]:
        try:
            with open(file_path, 'rb') as f:
                file_content = f.read()

            file_name = os.path.basename(file_path)
            file_size = len(file_content)
            file_sha256 = hashlib.sha256(file_content).hexdigest()

            prepare_url = f"{self.base_url}/osms/v1/file/manager/prepare"
            prepare_data = {
                "objectType": object_type,
                "fileName": file_name,
                "fileSha256": file_sha256,
                "fileSize": file_size,
                "fileOwnerInfo": {
                    "uid": self.uid,
                    "teamId": self.uid,
                },
                "useEdge": False,
            }

            headers = {
                "Content-Type": "application/json",
                "x-uid": self.uid,
                "x-request-from": "openclaw",
            }
            # api_key 可选：桌面客户端场景 file_upload_url 指向客户端本地代理
            #（127.0.0.1），鉴权由代理注入 businessCredential，无需 x-api-key
            if self.api_key:
                headers["x-api-key"] = self.api_key

            status, body = await self._post_control_plane(prepare_url, prepare_data, headers)
            if status < 200 or status >= 300:
                raise Exception(f"Prepare failed: HTTP {status}")
            prepare_resp = json.loads(body.decode("utf-8"))
            if prepare_resp.get("code") != "0":
                raise RuntimeError(f"Prepare failed: {prepare_resp.get('desc', 'Unknown error')}")

            object_id = prepare_resp.get("objectId")
            draft_id = prepare_resp.get("draftId")
            upload_infos = prepare_resp.get("uploadInfos", [])

            if not upload_infos:
                raise RuntimeError("No upload information returned")

            upload_info = upload_infos[0]
            upload_url = upload_info.get("url")
            upload_method = upload_info.get("method", "PUT")
            upload_headers = upload_info.get("headers", {})

            # 直传（公网 OBS 签名 URL）：智能路由挂代理 + TLS 拦截容忍
            status, _ = await self._request_with_tls_fallback(
                upload_method, upload_url, data=file_content, headers=upload_headers
            )
            if status < 200 or status >= 300:
                raise RuntimeError(f"Upload failed: HTTP {status}")

            # completeAndQuery：与桌面客户端上传链路（osms-upload.ts）同一端点；
            # /complete 在该网关 401（authFailed），completeAndQuery 携 credential 可用
            complete_url = f"{self.base_url}/osms/v1/file/manager/completeAndQuery"
            complete_data = {
                "objectId": object_id,
                "draftId": draft_id,
            }

            status, body = await self._post_control_plane(complete_url, complete_data, headers)
            if status < 200 or status >= 300:
                raise RuntimeError(f"Complete failed: HTTP {status}")

            complete_resp = json.loads(body.decode("utf-8"))
            if complete_resp.get("code") != "0":
                raise RuntimeError(f"Complete failed: {complete_resp.get('desc', 'Unknown error')}")

            return object_id

        except Exception as e:
            logger.error(f"[XY File Upload] Error: {e}")
            return None


def _generate_auth_headers(config: XiaoyiChannelConfig) -> dict[str, str]:
    """生成鉴权 Header."""
    if config.mode == "xiaoyi_claw":
        return {
            "x-uid": config.uid,
            "x-api-key": config.api_key,
            "x-agent-id": config.agent_id,
            "x-request-from": "openclaw"
        }
    timestamp = str(int(time.time() * 1000))
    signature = _generate_signature(config.sk, timestamp)
    return {
        "x-access-key": config.ak,
        "x-sign": signature,
        "x-ts": timestamp,
        "x-agent-id": config.agent_id
    }


def _resolve_relay_credentials(config: XiaoyiChannelConfig) -> tuple[str, str, str]:
    """本地中转（np:// 管道形态）的 (ak, sk, agent_id)。

    优先密钥包 vault（桌面 stdin 密钥包下发，不落 env/命令行）；
    取不到回退渠道配置（兼容旧版桌面形态）。
    """
    ak = get_secret("localAuth.ak") or config.ak or ""
    sk = get_secret("localAuth.sk") or config.sk or ""
    agent_id = get_secret("localAuth.agentId") or config.agent_id or ""
    return str(ak), str(sk), str(agent_id)


def _generate_pipe_auth_frame(config: XiaoyiChannelConfig) -> dict[str, Any]:
    """管道形态首帧鉴权：WS 握手头（x-access-key/x-sign/x-ts/x-agent-id）的下沉形态。

    ts 为毫秒时间戳字符串，sign = base64(HMAC-SHA256(sk, ts))——与桌面侧
    cloud-ws-relay verifyPipeAuthFrame / verifyLocalAuthFields 同一口径。
    """
    ak, sk, agent_id = _resolve_relay_credentials(config)
    timestamp = str(int(time.time() * 1000))
    return {
        "type": "auth",
        "agentId": agent_id,
        "ak": ak,
        "ts": timestamp,
        "sign": _generate_signature(sk, timestamp),
    }




class XiaoyiChannel(BaseChannel):
    """小艺通道：作为客户端连接到小艺服务器，实现 A2A 协议."""

    name = "xiaoyi"

    def __init__(self, config: XiaoyiChannelConfig, router: RobotMessageRouter):
        super().__init__(config, router)
        self.config: XiaoyiChannelConfig = config
        self._ws_connections: dict[str, Any] = {}  # Dual channel connections
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._running = False
        # ws/link 的 clawd_bot_init / heartbeat 由应用层（桌面客户端 cloud-ws-relay）
        # 统一构造发送；channel 层只发 agent_response，不再维护协议级心跳任务。
        self._connect_tasks: dict[str, asyncio.Task] = {}  # Connection tasks for each channel
        self._session_task_map: dict[str, str] = {}
        # Team responses are produced by the first request's long-lived
        # stream. Keep the current platform task for each stable session so
        # those responses reach the user's latest A2A request.
        self._latest_platform_tasks: dict[str, str] = {}
        self._session_heartbeat_tasks: dict[str, asyncio.Task] = {}  # Response heartbeat tasks for each session
        self._stream_text_buffers: dict[str, str] = {}
        self._task_last_activity: dict[str, float] = {}
        # V2: team ws 流式合并缓冲（task_id → 累积 delta 文本 + 延迟 flush 任务）
        self._ws_flush_buffers: dict[str, str] = {}
        self._ws_flush_tasks: dict[str, asyncio.Task] = {}
        self._on_message_cb: Callable[[Message], Any] | None = None
        # Task timeout management
        self._session_active: set[str] = set()  # Active sessions (concurrent request detection)
        self._active_tasks: set[tuple[str, str]] = set()
        self._task_timeout_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._session_timeout_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._sessions_waiting_for_push: dict[str, str] = {}  # {session: task} waiting for push
        self._team_sessions: set[str] = set()  # Team rounds stay active across member outputs
        self._team_tasks: set[tuple[str, str]] = set()
        self._team_last_leader_finals: dict[tuple[str, str], str] = {}
        # Session cleanup management
        self._sessions_marked_for_cleanup: dict[str, dict[str, Any]] = {}  # Session cleanup state
        # File upload service configuration
        self.file_upload_config = {
            "baseUrl": config.file_upload_url,
            "apiKey": config.api_key,
            "uid": config.uid,
        }
        # Save additional configuration fields
        self.api_id = config.api_id
        self.push_id = config.push_id
        self._accumulated_texts: dict[str, str] = {}  # Accumulated text per session for push notification
        # V2 Stream Routing: agent_id → (顶层 sessionId, task_id, push_id, ts) 活跃映射，出站判定 ws vs push 用
        self._active_push_sessions: dict[str, tuple[str, str, str, float]] = {}
        # V2: team 投递 ws 活跃窗口——最近 N 秒内有该 agent_id 的 inbound 视为手机端在线，走 ws；超窗走 push。
        # 手机端收到 final 或长时间无消息会主动关 ws（网关无法直接感知手机 ws 状态），靠 inbound 活跃度间接判断。
        self._team_ws_alive_window: float = float(
            getattr(config, "team_ws_alive_window", 60) or 60
        )
        # V2: 活跃映射超时清理任务，定期清掉异常断开的 stale 条目
        self._active_push_cleanup_task: asyncio.Task | None = None
        # V2: ws 保活任务——周期向活跃 agent_id 发 status-update（空内容），重置手机端空闲计时，
        # 避免手机端因长时间无消息主动关 ws（部分终端不能用 push，只能靠 ws 维持）。
        self._team_ws_keepalive_interval: float = float(
            getattr(config, "team_ws_keepalive_interval", 20) or 20
        )
        self._ws_keepalive_task: asyncio.Task | None = None
        # V2: push 合并窗口缓冲 push_id → [(ts, content, summary)]，避免短时多条 push 轰炸
        self._push_merge_buffers: dict[str, list[tuple[float, str, str]]] = {}
        # V2: push 延迟 flush 任务 push_id → asyncio.Task，窗口到期统一发送
        self._push_flush_tasks: dict[str, asyncio.Task] = {}
        # Data-event 处理器：intent_name -> list of handlers
        self._data_event_handlers: dict[str, List[Callable[[DataEvent], Any]]] = {}
        # InvokeJarvisGUIAgentResponse 原始事件回调列表
        self._gui_agent_handlers: List[Callable[[dict[str, Any]], Any]] = []
        # GUI 工具互斥：避免并发注册多个 handler 导致回包串单；不影响其他工具并发
        self._gui_tool_lock = asyncio.Lock()
        self._device_command_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._privilege_check_lock = asyncio.Lock()
        self._scheduled_device_command_lock = asyncio.Lock()
        # clientVariables 审批桥接：待答复审批（logical_session → 审批上下文）
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self._reload_permissions_cb: Callable[[], Any] | None = None

    @property
    def channel_id(self) -> str:
        return self.config.channel_id or self.name

    @property
    def app_id(self) -> str:
        """V2: app_id 维度，与 inbound bot_id=config.agent_id 对齐，
        使 ChannelManager 注册的 ChannelKey("xiaoyi", agent_id) 与
        Subscription 的 RoutingKey.app_id 一致，dispatch 时能命中。
        """
        return self.config.agent_id or "default"

    @property
    def gui_tool_lock(self) -> asyncio.Lock:
        """供 xiaoyi_gui_agent 串行执行，避免多路 GUI 回包互相唤醒。"""
        return self._gui_tool_lock

    @property
    def is_ready(self) -> bool:
        """当前 Channel 是否至少持有一条可发送的小艺连接."""
        return self._running and any(self._ws_connections.values())

    @property
    def clients(self) -> set[Any]:
        return set()

    def on_message(self, callback: Callable[[Message], None]) -> None:
        self._on_message_cb = callback

    async def start(self) -> None:
        if self._running:
            logger.warning("XiaoyiChannel 已在运行")
            return
        if not self.config.enabled:
            logger.warning("XiaoyiChannel 未启用（enabled=False）")
            return
        if self.config.mode == "xiaoyi_channel":
            if not self.config.ak or not self.config.sk or not self.config.agent_id:
                logger.error("XiaoyiChannel 未配置 ak/sk/agent_id")
                return

        self._running = True
        # 注册全局实例（供 tools 使用）
        _xiaoyi_channel_instances[self.channel_id] = self
        logger.info("XiaoyiChannel 已注册为全局实例")

        # Start dual channel connections
        for url_key, url in [("ws_url1", self.config.ws_url1), ("ws_url2", self.config.ws_url2)]:
            if url:
                self._connect_tasks[url_key] = asyncio.create_task(self._reconnect_loop(url_key, url))
        # V2: 启动活跃映射超时清理任务，每小时清掉异常断开的 stale 条目
        self._active_push_cleanup_task = asyncio.create_task(self._cleanup_active_push_sessions())
        # V2: 启动 ws 保活任务，周期发 status-update 重置手机端空闲计时
        if self._team_ws_keepalive_interval > 0:
            self._ws_keepalive_task = asyncio.create_task(self._ws_keepalive_loop())
        logger.info("XiaoyiChannel 已启动（客户端模式，双通道）")

    async def stop(self) -> None:
        self._running = False
        # 注销全局实例
        _xiaoyi_channel_instances.pop(self.channel_id, None)
        logger.info("XiaoyiChannel 已注销")
        # Cancel all connection tasks
        for url_key in list(self._connect_tasks.keys()):
            if self._connect_tasks[url_key]:
                self._connect_tasks[url_key].cancel()
                self._connect_tasks[url_key] = None
        # Cancel all session heartbeat tasks
        for session_id in list(self._session_heartbeat_tasks.keys()):
            if self._session_heartbeat_tasks[session_id]:
                self._session_heartbeat_tasks[session_id].cancel()
                self._session_heartbeat_tasks[session_id] = None
        # Cancel all task timeout tasks
        for task in self._task_timeout_tasks.values():
            if task:
                task.cancel()
        # Cancel all session timeout tasks
        for task in self._session_timeout_tasks.values():
            if task:
                task.cancel()
        # Close all websocket connections
        for url_key, ws in list(self._ws_connections.items()):
            if ws:
                try:
                    await self._close_transport(ws)
                except Exception as e:
                    logger.warning(f"关闭连接失败 ({url_key}): {e}")
                self._ws_connections[url_key] = None
        self._connect_tasks.clear()
        self._session_heartbeat_tasks.clear()
        self._task_timeout_tasks.clear()
        self._session_timeout_tasks.clear()
        self._ws_connections.clear()
        self._session_active.clear()
        self._active_tasks.clear()
        self._latest_platform_tasks.clear()
        self._sessions_waiting_for_push.clear()
        self._team_sessions.clear()
        self._team_tasks.clear()
        self._team_last_leader_finals.clear()
        self._sessions_marked_for_cleanup.clear()
        self._accumulated_texts.clear()
        # V2: 清理 push 合并窗口任务
        for _aid in list(self._push_flush_tasks.keys()):
            if self._push_flush_tasks[_aid]:
                self._push_flush_tasks[_aid].cancel()
        self._push_flush_tasks.clear()
        self._push_merge_buffers.clear()
        self._active_push_sessions.clear()
        # V2: 取消活跃映射超时清理任务
        if self._active_push_cleanup_task and not self._active_push_cleanup_task.done():
            self._active_push_cleanup_task.cancel()
        self._active_push_cleanup_task = None
        # V2: 取消 ws 保活任务
        if self._ws_keepalive_task and not self._ws_keepalive_task.done():
            self._ws_keepalive_task.cancel()
        self._ws_keepalive_task = None
        # V2: 清理 ws 流式合并缓冲
        for _tid in list(self._ws_flush_tasks.keys()):
            if self._ws_flush_tasks[_tid]:
                self._ws_flush_tasks[_tid].cancel()
        self._ws_flush_tasks.clear()
        self._ws_flush_buffers.clear()
        logger.info("XiaoyiChannel 已停止")

    async def _cleanup_active_push_sessions(self) -> None:
        """定期清理 _active_push_sessions 中超时的 stale 条目。

        异常断开的 agent_id（未走 final 清理）会在映射里残留。
        每小时扫描，清掉 1 小时无更新的条目，避免多用户长期运行内存累积。
        """
        try:
            while self._running:
                await asyncio.sleep(3600)
                cutoff = time.time() - 3600
                stale = [
                    aid for aid, entry in self._active_push_sessions.items()
                    if entry[3] < cutoff
                ]
                for aid in stale:
                    self._active_push_sessions.pop(aid, None)
                if stale:
                    logger.info("[XiaoyiChannel] 清理 %d 个 stale 活跃映射条目", len(stale))
        except asyncio.CancelledError:
            pass

    async def _ws_keepalive_loop(self) -> None:
        """周期向活跃 agent_id 发 status-update（空内容）保活。

        手机端长时间无消息会主动关 ws，部分终端不能用 push 只能靠 ws。
        每个保活周期扫描 _active_push_sessions，对 ws 仍连接的 agent_id 发一条
        kind=status-update（message 空），重置手机端空闲计时。
        仅在 ws_alive 窗口内的 agent_id 保活（超窗说明手机端可能已关，保活发不到）。
        """
        try:
            while self._running:
                await asyncio.sleep(self._team_ws_keepalive_interval)
                if not self._ws_connections:
                    continue
                # ws 连接是否可用（任一 OPEN）
                ws_usable = any(ws is not None for ws in self._ws_connections.values())
                if not ws_usable:
                    continue
                now = time.time()
                # 快照活跃映射，避免迭代中修改
                snapshot = list(self._active_push_sessions.items())
                kept = 0
                for aid, entry in snapshot:
                    sid, tid, _, ts = entry
                    # 仅对窗口内的 agent_id 保活（ws 大概率还开着）。
                    # 保活成功后刷新 last_seen，使 ws_active 持续为 True（防超窗后误走 push）。
                    if (now - ts) >= self._team_ws_alive_window:
                        continue
                    try:
                        for url_key, ws in self._ws_connections.items():
                            if ws:
                                await self._send_status_update_with_state(
                                    tid, sid, "", "working", url_key,
                                )
                        # 保活发出即视为链路仍活，刷新 last_seen 维持 ws_active
                        self._active_push_sessions[aid] = (sid, tid, entry[2], time.time())
                        kept += 1
                    except Exception as e:
                        logger.debug("[XiaoyiChannel] ws keepalive 失败 agent_id=%s: %s", (aid or "")[:8], e)
                if kept:
                    logger.info("[XiaoyiChannel] ws keepalive sent: %d agents", kept)
        except asyncio.CancelledError:
            pass

    def _extract_platform_receive_info(self, msg: Message) -> tuple[str, str]:
        """
        从消息中提取小艺平台会话 ID 与任务 ID。
        优先使用 metadata（避免 \new_session 覆盖 session_id 后无法回发），否则回退到 session_id 与 _session_task_map。
        """
        meta = getattr(msg, "metadata", None) or {}
        platform_session_id = (meta.get("xiaoyi_session_id") or "").strip()
        platform_task_id = (meta.get("xiaoyi_task_id") or "").strip()
        if platform_session_id or platform_task_id:
            return (
                platform_session_id or (msg.session_id or ""),
                platform_task_id or platform_session_id,
            )
        task_id = msg.id or ""
        session_id = self._session_task_map.get(task_id, task_id)
        return session_id, task_id

    async def send(self, msg: Message, *, routing_target: RoutingTarget | None = None) -> None:
        """发送消息到小艺服务端（A2A 格式，双通道发送）.

        V2 Stream Routing:
        - routing_target 为空（非 team 模式）→ 走 _send_legacy 原有单值路径
        - routing_target 非空（team 模式）→ 双通道投递：
          活跃会话走 ws 流式，非活跃走 push webhook 全文 final
        """
        # ── team 模式：双通道投递 ──
        if routing_target is not None:
            await self._send_team(msg, routing_target)
            return

        # ── 非 team 模式：原有单值路径 ──
        if not self._ws_connections:
            if str(msg.channel_id or "").strip().lower() == "xiaoyi":
                logger.warning(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_SEND_SKIPPED "
                    "message_id=%s reason=no_ws_connections payload=%r",
                    msg.id,
                    msg.payload,
                )
            return
        await self._send_legacy(msg)

    async def _send_legacy(self, msg: Message) -> None:
        """非 team 模式原有单值投递路径（保留兼容）."""
        logger.info(
            "[GUI_AGENT_DIAG] phase=XIAOYI_SEND_MESSAGE message_id=%s "
            "session_id=%s event_type=%s payload=%r enable_streaming=%s",
            msg.id,
            msg.session_id,
            getattr(msg.event_type, "value", msg.event_type),
            msg.payload,
            self.config.enable_streaming,
        )
        logger.info(f"XiaoyiChannel 发送消息: {msg}")
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        event_name = str(
            getattr(msg.event_type, "value", None) or payload.get("event_type") or ""
        ).strip()
        team_role = str(payload.get("role") or "").strip().lower()
        metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
        mode = str(metadata.get("mode") or "").strip().lower()
        reset_team_session = bool(metadata.get("reset_team_session"))
        terminal_notice = bool(metadata.get("terminal_notice"))
        session_id, task_id = self._extract_platform_receive_info(msg)
        is_team_mode = mode in {"team", "team.plan", "code.team"}
        is_team_event = not reset_team_session and (
            event_name.startswith("team.")
            or is_team_mode
        )
        latest_task_id = self._latest_platform_tasks.get(session_id)
        if (
            is_team_event
            and session_id not in self._team_sessions
            and (not latest_task_id or task_id != latest_task_id)
        ):
            logger.info(
                "[GUI_AGENT_DIAG] phase=XIAOYI_STALE_TEAM_EVENT_SKIPPED "
                "message_id=%s session_id=%s source_task_id=%s "
                "latest_task_id=%s event_type=%s",
                msg.id,
                session_id,
                task_id,
                latest_task_id,
                event_name,
            )
            return
        if is_team_event:
            task_id = latest_task_id or task_id
        team_task_key = (session_id, task_id)

        # Team control/state events are not user-visible.  They identify a
        # long-lived round whose chat.final frames are member outputs rather
        # than the terminal frame for the whole A2A task.
        if is_team_event and session_id:
            self._team_sessions.add(session_id)
            self._team_tasks.add(team_task_key)
        elif mode or reset_team_session:
            # An explicit non-Team mode overrides stale session-level Team
            # state. Clear every task marker for the platform session so an
            # older Team round cannot contaminate the new non-Team task.
            self._clear_team_session(session_id)
        is_team_session = team_task_key in self._team_tasks

        # keepalive 是 jiuwenswarm 流空窗期(如 team 长任务后台执行工具)每 10s
        # 发出的保活帧，payload 仅 {event_type: keepalive}，无 content。原本会漏到
        # 下方 text 管道，被当成 text="\n" 的空内容 artifact-update 推给小艺——
        # 小艺侧 120s 超时机制只认实质 content，收到一连串空帧判定超时，回
        # "稍等，稍等片刻再试试"。这里改为转成一条 A2A status-update(state=working)
        # 推给小艺：带实质状态文本、is_final=False，既保活又满足超时判定。
        if event_name == "keepalive":
            if session_id and self._is_session_active(session_id, task_id):
                for url_key in list(self._ws_connections.keys()):
                    await self._send_status_update_with_state(
                        task_id,
                        session_id,
                        "正在处理中，请稍候~",
                        "working",
                        url_key,
                    )
                logger.info(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_KEEPALIVE_TO_STATUS "
                    "message_id=%s session_id=%s task_id=%s "
                    "reason=keepalive_as_status_update_to_avait_peer_timeout",
                    msg.id,
                    session_id,
                    task_id,
                )
            else:
                logger.info(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_KEEPALIVE_SKIPPED "
                    "message_id=%s session_id=%s task_id=%s "
                    "reason=keepalive_no_active_session",
                    msg.id,
                    session_id,
                    task_id,
                )
            return

        if event_name == "team.runtime_ready":
            return

        # Match Feishu's low-noise policy: member/task lifecycle events remain
        # internal, while team.message is delivered as readable text.
        if event_name in {"team.member", "team.task"}:
            return

        # A completed Xiaoyi task cannot accept more artifacts. Team runtimes
        # can keep producing member events after the leader has ended the
        # current round, so drop those events until the next inbound platform
        # task becomes active. The runtime-level completion event is still
        # consumed below so its Team markers can be cleared.
        if (
            is_team_session
            and event_name != "team.completed"
            and not self._is_session_active(session_id, task_id)
        ):
            logger.info(
                "[GUI_AGENT_DIAG] phase=XIAOYI_TEAM_EVENT_SKIPPED "
                "message_id=%s session_id=%s task_id=%s event_type=%s "
                "reason=platform_task_already_completed",
                msg.id,
                session_id,
                task_id,
                event_name,
            )
            return

        if event_name == "team.message":
            event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
            from_member = str(event.get("from_member") or "团队成员").strip()
            to_member = str(event.get("to_member") or "").strip()
            message_type = str(event.get("type") or "").strip()
            content = str(event.get("content") or "").strip()
            if not content:
                logger.warning("XiaoyiChannel Team 消息缺少 content，跳过发送")
                return
            receiver = to_member if message_type == "team.message.p2p" and to_member else "全体"
            team_text = f"【团队消息｜{from_member} → {receiver}】\n{content}"
            self._accumulated_texts[team_task_key] = team_text
            for url_key in list(self._ws_connections.keys()):
                await self._send_text_response(
                    session_id,
                    task_id,
                    team_text,
                    url_key,
                    append=False,
                    last_chunk=True,
                    is_final=False,
                )
            return

        if event_name == "team.completed":
            if self._is_session_active(session_id, task_id):
                leader_summary = self._team_last_leader_finals.get(team_task_key, "")
                for url_key in list(self._ws_connections.keys()):
                    if leader_summary:
                        await self._send_text_response(
                            session_id,
                            task_id,
                            leader_summary,
                            url_key,
                            append=False,
                            last_chunk=True,
                            is_final=False,
                        )
                    await self._send_status_update_with_state(
                        task_id,
                        session_id,
                        "团队任务已完成~",
                        "completed",
                        url_key,
                    )
                await self._finalize_session(
                    session_id,
                    task_id,
                    leader_summary or None,
                )
            self._latest_platform_tasks.pop(session_id, None)
            self._session_task_map.pop(task_id, None)
            self._clear_task_timeout(session_id)
            self._clear_session_timeout(session_id)
            self._sessions_waiting_for_push.pop(session_id, None)
            self._clear_team_session(session_id)
            return

        # Team 模式仅保留 leader 的思考过程与正文；teammate 的 delta/reasoning/
        # subtask_update 是中间过程，不投递给端侧（对齐飞书 team 卡片语义）。
        # teammate 的 final/error 仍保留（作为 member 输出）。
        if (
            is_team_session
            and team_role == "teammate"
            and event_name in {"chat.delta", "chat.reasoning", "chat.subtask_update"}
        ):
            return

        # chat.reasoning 是模型推理/思考过程增量（llm_reasoning chunk），逐块流式
        # 投递为 reasoningText part（A2A 端侧支持该 kind，与其他三方 IM 不同）。
        # 必须在下方 SKIPPED 兜底（:791 not reasoning or text）之前截获，否则
        # CHAT_REASONING 不在任何分类集合里会被当 non_user_visible 丢弃。
        # reasoning chunk 不带 is_complete（见 interface_deep.py:6193），末块靠
        # chat.final / 下一个 chat.delta 到来作为思考结束信号——逐块 append=True、
        # lastChunk=False，端侧实时拼接思考流。
        if msg.event_type == EventType.CHAT_REASONING:
            reasoning_content = ""
            if isinstance(msg.payload, dict):
                reasoning_content = str(msg.payload.get("content", "") or "")
            if not reasoning_content:
                return
            for url_key, ws in self._ws_connections.items():
                if ws:
                    try:
                        await self._send_text_response(
                            session_id,
                            task_id,
                            reasoning_content,
                            url_key,
                            append=True,
                            last_chunk=False,
                            is_final=False,
                            kind="reasoningText",
                        )
                    except Exception as e:
                        logger.warning(f"XiaoyiChannel 发送思考消息失败 ({url_key}): {e}")
            return

        # Handle chat.file event
        # 两种 mode（xiaoyi_channel / xiaoyi_claw）都要处理文件投递：
        # 桌面客户端经本地 relay 接入时 mode=xiaoyi_channel，此前该分支被
        # mode 门控跳过——send_file_to_user 的 chat.file 被静默丢弃（且工具侧
        # 已标记已发送，重试被去重，用户永远收不到文件）。
        if msg.event_type == EventType.CHAT_FILE:
            files = msg.payload.get("files", {}) if isinstance(msg.payload, dict) else {}
            if files:
                for file_info in files:
                    # Convert file path to file info dict if it's a string
                    if isinstance(file_info, dict):
                        file_path = file_info.get("path", "")
                        file_name = file_info.get("name", os.path.basename(file_path))
                    else:
                        file_path = str(file_info)
                        file_name = os.path.basename(file_path)
                    file_info = {
                        "success": True,
                        "result_type": "file_created",
                        "fullPath": file_path,
                        "fileName": file_name
                    }

                    # Send file response
                    for url_key, ws in self._ws_connections.items():
                        if ws:
                            try:
                                await self._send_file_response(session_id, task_id, file_info, url_key)
                            except Exception as e:
                                logger.warning(f"XiaoyiChannel 发送文件响应失败 ({url_key}): {e}")
            return

        # Handle chat.html_card event（与 chat.file 同理：两种 mode 都要处理）
        if msg.event_type == EventType.CHAT_HTML_CARD:
            payload = msg.payload if isinstance(msg.payload, dict) else {}
            cards_info = payload.get("cardsInfo")
            if not isinstance(cards_info, list) or not cards_info:
                url = str(payload.get("url") or "").strip()
                if url:
                    cards_info = [
                        {
                            "cardName": "clawH5",
                            "cardData": {"url": url},
                            "displayType": "DisplayFaCard",
                        }
                    ]
            if cards_info:
                message_id = str(msg.id or task_id or "")
                for url_key, ws in self._ws_connections.items():
                    if ws:
                        try:
                            await self._send_html_card_response(
                                session_id, task_id, message_id, cards_info, url_key
                            )
                        except Exception as e:
                            logger.warning(
                                "XiaoyiChannel 发送 HTML 卡片失败 (%s): %s", url_key, e
                            )
            else:
                logger.warning(
                    "XiaoyiChannel chat.html_card 缺少 cardsInfo/url，跳过 session_id=%s",
                    session_id,
                )
            return

        # todos 帧：只下发当前执行中的 todo 步骤（status=in_progress）的
        # activeForm 作为 status-update 文本（覆盖式推进态；替代原工具状态文案）
        if event_name == "todo.updated":
            if session_id and self._is_session_active(session_id, task_id):
                active_form = ""
                if isinstance(payload, dict):
                    todos = payload.get("todos")
                    if isinstance(todos, list):
                        for item in todos:
                            if not isinstance(item, dict):
                                continue
                            if str(item.get("status", "")).strip().lower() == "in_progress":
                                active_form = str(
                                    item.get("activeForm", "") or item.get("content", "") or ""
                                ).strip()
                                break
                if active_form:
                    for url_key in list(self._ws_connections.keys()):
                        await self._send_status_update_with_state(
                            task_id, session_id, active_form, "working", url_key
                        )
                    logger.info(
                        "[GUI_AGENT_DIAG] phase=XIAOYI_TODO_STATUS message_id=%s "
                        "session_id=%s task_id=%s active_form=%r",
                        msg.id,
                        session_id,
                        task_id,
                        active_form,
                    )
            return

        if should_send_as_status_update(msg.event_type):
            is_processing = (
                msg.payload.get("is_processing", True)
                if isinstance(msg.payload, dict)
                else True
            )
            if (
                is_team_session
                and msg.event_type == EventType.CHAT_PROCESSING_STATUS
                and is_processing is False
            ):
                logger.info(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_TEAM_STATUS_DEFERRED "
                    "message_id=%s session_id=%s task_id=%s",
                    msg.id,
                    session_id,
                    task_id,
                )
                return
            if not is_processing and not self._is_session_active(session_id, task_id):
                logger.info(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_STATUS_SKIPPED "
                    "message_id=%s session_id=%s event_type=%s "
                    "reason=terminal_text_already_sent payload=%r",
                    msg.id,
                    session_id,
                    getattr(msg.event_type, "value", msg.event_type),
                    msg.payload,
                )
                return
            status_text = (
                "团队任务已完成~"
                if is_team_session and not is_processing
                else get_status_text_for_event(msg.event_type, msg.payload)
            )
            status_state = get_status_state_for_event(msg.event_type, msg.payload)
            for url_key in list(self._ws_connections.keys()):
                await self._send_status_update_with_state(
                    task_id, session_id, status_text, status_state, url_key
                )
            if status_state in {"completed", "failed", "canceled"} and session_id:
                await self._finalize_session(
                    session_id,
                    task_id,
                    preserve_team_session=(
                        is_team_session and status_state == "completed"
                    ),
                )
            return

        # 权限/确认审批（chat.ask_user_question）：渲染提示并登记待答复审批，
        # 用户下一条消息（文本约定或 PermissionReply 事件）将被转换为 interrupt resume
        # 恢复挂起的运行，而不是结束本轮等用户新开一轮。
        # 必须放在 non_user_visible SKIPPED 判断之前——ask_user_question 不在
        # should_send_as_text/reasoning_text 名单内，位置错会被静默吞掉。
        if msg.event_type == EventType.CHAT_ASK_USER_QUESTION:
            await self._send_ask_user_question_prompt(msg, session_id, task_id)
            return

        if not (
            should_send_as_reasoning_text(msg.event_type)
            or should_send_as_text(msg.event_type)
        ):
            logger.info(
                "[GUI_AGENT_DIAG] phase=XIAOYI_EVENT_SKIPPED message_id=%s "
                "session_id=%s event_type=%s reason=non_user_visible_event "
                "payload=%r",
                msg.id,
                session_id,
                getattr(msg.event_type, "value", msg.event_type),
                msg.payload,
            )
            return

        # 处理错误消息：发送 failed 状态 + 错误文本 + 结束会话
        if msg.event_type == EventType.CHAT_ERROR:
            error_text = get_status_text_for_event(msg.event_type, msg.payload)
            # 优先从 payload.error 提取详细错误信息
            if isinstance(msg.payload, dict):
                error_detail = msg.payload.get("error", "")
                if error_detail:
                    error_text = str(error_detail)

            # 发送 failed 状态更新
            for url_key in list(self._ws_connections.keys()):
                await self._send_status_update_with_state(
                    task_id, session_id, error_text, "failed", url_key
                )

            # 发送错误文本消息（is_final=True）
            for url_key, ws in self._ws_connections.items():
                if ws:
                    try:
                        await self._send_text_response(
                            session_id,
                            task_id,
                            error_text,
                            url_key,
                            append=True,
                            last_chunk=True,
                            is_final=True,
                        )
                    except Exception as e:
                        logger.warning(f"XiaoyiChannel 发送错误消息失败 ({url_key}): {e}")

            # 清理 session 状态
            if session_id:
                await self._stop_session_heartbeat(session_id)
                self._clear_task_timeout(session_id)
                self._clear_session_timeout(session_id)
                self._mark_session_completed(session_id)
                self._accumulated_texts.pop(session_id, None)

            logger.warning(f"XiaoyiChannel 发送错误消息: session={session_id}, error={error_text}")
            return

        if not (
            should_send_as_reasoning_text(msg.event_type)
            or should_send_as_text(msg.event_type)
        ):
            logger.info(
                "[GUI_AGENT_DIAG] phase=XIAOYI_EVENT_SKIPPED message_id=%s "
                "session_id=%s event_type=%s reason=non_user_visible_event "
                "payload=%r",
                msg.id,
                session_id,
                getattr(msg.event_type, "value", msg.event_type),
                msg.payload,
            )
            return

        content = ""
        cron_job_name = ""
        cron_job_id = ""
        if isinstance(msg.payload, dict):
            content = msg.payload.get("content", "\n")
            if msg.event_type == EventType.CHAT_ERROR:
                error_text = str(msg.payload.get("error") or "处理出错").strip()
                content = f"⚠️ {error_text}"
            if isinstance(content, dict):
                content = content.get("output", str(content))
            content = str(content)
            cron_block = msg.payload.get("cron", {})
            if isinstance(cron_block, dict):
                cron_job_name = str(cron_block.get("job_name") or "")
                cron_job_id = str(cron_block.get("job_id") or "")
        elif msg.payload:
            content = str(msg.payload)

        if (
            is_team_session
            and team_role != "teammate"
            and event_name == "chat.final"
            and content.strip()
        ):
            self._team_last_leader_finals[team_task_key] = content

        # 推送消息发送
        if msg.id.startswith("cron-push"):
            # 与 xy_channel cron-buffer.ts flushBufferedTexts + outbound.ts sendText 对齐：
            # 1. 持久化全文到 pushData（客户端 Trigger 回查看全文，不受截断影响）
            # 2. 经 push_broadcast 向所有已注册 pushId 广播（单 pushId 失败不影响其他）
            # pushDataId 为空时回落 kind="text" 内联，行为同现状。
            push_text = content[:1000] if len(content) > 1000 else content
            title = content.split("\n")[0][:57] if content else ""
            push_data_id = ""
            try:
                from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools.pushdata_manager import (
                    save_push_data,
                )
                cron_meta = None
                if cron_job_id or cron_job_name:
                    cron_meta = {
                        "cronJobId": cron_job_id,
                        "cronTitle": cron_job_name,
                    }
                push_data_id = save_push_data(content, cron_meta)
                logger.info(
                    "[CRON-PUSH] Saved pushData: pushDataId=%s cronId=%s cronTitle=%s",
                    push_data_id[:8] if push_data_id else "-",
                    cron_job_id or "-",
                    cron_job_name or "-",
                )
            except Exception as save_err:
                logger.error(
                    "[CRON-PUSH] Failed to save pushData: %s", save_err
                )

            try:
                from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.push import (
                    PushConfig as _PushConfig,
                    push_broadcast,
                )
                push_config = _PushConfig(
                    mode=self.config.mode,
                    api_id=self.config.api_id,
                    push_id=self.config.push_id,
                    push_url=self.config.push_url,
                    ak=self.config.ak,
                    sk=self.config.sk,
                    uid=self.config.uid,
                    api_key=self.config.api_key,
                )
                result = await push_broadcast(
                    config=push_config,
                    text=push_text,
                    title=title or cron_job_name or "任务已完成",
                    to=str(session_id or ""),
                    push_data_id=push_data_id,
                    cron_job_id=cron_job_id or None,
                    cron_title=cron_job_name or None,
                )
                logger.info(
                    "[CRON-PUSH] push_broadcast done: success=%d failure=%d cronId=%s",
                    result.success_count,
                    result.failure_count,
                    cron_job_id or "-",
                )
            except Exception as push_err:
                logger.error(
                    "[CRON-PUSH] push_broadcast failed, fallback to single push: %s",
                    push_err,
                )
                # 回退到原有的单 pushId 推送，保证推送不丢
                await self._send_push_notification(
                    cron_job_name or title, content
                )
            return

        # 如果禁用流式，总是作为完整消息发送
        if not self.config.enable_streaming:
            append = False
            last_chunk = True
            final = not (
                is_team_session
                and msg.event_type in {EventType.CHAT_FINAL, EventType.CHAT_ERROR}
            )
            if not final and team_role == "teammate":
                member_name = str(msg.payload.get("member_name") or "").strip()
                if member_name:
                    content = f"【{member_name}】\n{content}"
        else:
            # 流式模式：按事件类型计算增量与是否结束
            is_chat_final = msg.event_type == EventType.CHAT_FINAL
            is_final = bool(msg.payload.get("is_complete", False))

            # 累积当前文本
            self._accumulated_texts[team_task_key] = content

            # chat.final 携带完整正文，直接作为独立终帧发送，避免客户端
            # 等待后续空 status 帧提交正文。
            if is_team_session and msg.event_type in {
                EventType.CHAT_FINAL,
                EventType.CHAT_ERROR,
            }:
                if team_role == "teammate":
                    member_name = str(msg.payload.get("member_name") or "").strip()
                    if member_name:
                        content = f"【{member_name}】\n{content}"
                append = False
                last_chunk = True
                final = False
            elif is_chat_final:
                append = False
                last_chunk = True
                # Agent streams finish via processing_status; local notices do not.
                final = terminal_notice
            else:
                append = True
                last_chunk = is_final
                final = is_final

        logger.info(
            "[GUI_AGENT_DIAG] phase=XIAOYI_TEXT_FLAGS message_id=%s "
            "platform_session_id=%s platform_task_id=%s event_type=%s "
            "payload_is_complete=%r append=%s last_chunk=%s final=%s "
            "content=%r",
            msg.id,
            session_id,
            task_id,
            getattr(msg.event_type, "value", msg.event_type),
            (
                msg.payload.get("is_complete")
                if isinstance(msg.payload, dict)
                else None
            ),
            append,
            last_chunk,
            final,
            content,
        )

        # Get accumulated text for this session (for push notification)
        accumulated_text = self._accumulated_texts.get(team_task_key, "")
        self._accumulated_texts[team_task_key] = content

        # Send to all connections
        # 走到这里的事件 = should_send_as_reasoning_text or should_send_as_text 为真
        # 且未被前置 status_update / ask_user_question 分流者。当前 reasoning 集合里
        # 能落到此处的只有 CHAT_SUBTASK_UPDATE（CHAT_REASONING 在 :640 前置分流、
        # CHAT_PROCESSING_STATUS 在 :724 status_update 分流）。
        # kind 按 event_type 选，与 formatter 分类对齐。
        part_kind = (
            "reasoningText"
            if should_send_as_reasoning_text(msg.event_type)
            else "text"
        )
        for url_key, ws in self._ws_connections.items():
            if ws:
                try:
                    await self._send_text_response(
                        session_id,
                        task_id,
                        content,
                        url_key,
                        append=append,
                        last_chunk=last_chunk,
                        is_final=final,
                        kind=part_kind,
                    )
                except Exception as e:
                    logger.warning(f"XiaoyiChannel 发送消息失败 ({url_key}): {e}")

        if final and session_id:
            await self._stop_session_heartbeat(session_id)
            # Clean up tasks and mark session as completed
            self._clear_task_timeout(session_id)
            self._clear_session_timeout(session_id)
            self._mark_session_completed(session_id)
            # Check if session was waiting for push and send notification
            if self._is_session_waiting_for_push(session_id, task_id) and accumulated_text:
                summary = accumulated_text[:30] + "..." if len(accumulated_text) > 30 else accumulated_text
                await self._send_push_notification(summary, "后台任务已完成：" + summary)
                self._clear_session_waiting_for_push(session_id, task_id)

            # Clear accumulated text
            self._accumulated_texts.pop(session_id, None)
            # V2: 清理活跃映射（按 sessionId 反查 agent_id）
            _aid_to_clean = [
                aid for aid, (sid, _, _, _) in self._active_push_sessions.items()
                if sid == session_id
            ]
            for aid in _aid_to_clean:
                self._active_push_sessions.pop(aid, None)

    # ==================== V2 Stream Routing: team 双通道投递 ====================

    # team member 场景只投递结果类消息，丢弃中间过程（对齐飞书 team 卡片语义）。
    # 保留：TEAM_MESSAGE（团队消息）/ CHAT_FINAL（最终正文）/ CHAT_FILE（文件）/
    #       CHAT_ERROR（错误）/ CHAT_TOOL_CALL/RESULT（status 透传）/ CHAT_PROCESSING_STATUS（终态信号）。
    # 丢弃：CHAT_REASONING / CHAT_DELTA / CHAT_ASK_USER_QUESTION / usage / todo / symphony_status 等。
    _TEAM_ALLOWED_EVENTS = frozenset({
        EventType.TEAM_MESSAGE,
        EventType.CHAT_FINAL,
        EventType.CHAT_FILE,
        EventType.CHAT_ERROR,
        EventType.CHAT_TOOL_CALL,
        EventType.CHAT_TOOL_RESULT,
        EventType.CHAT_PROCESSING_STATUS,
    })

    async def _send_team(self, msg: Message, routing_target: RoutingTarget) -> None:
        """team 模式双通道投递：活跃会话走 ws 流式，非活跃走 push webhook 全文 final."""
        delivery = routing_target.delivery
        if delivery is None or not isinstance(delivery, XiaoyiDeliveryTarget):
            logger.warning(
                "[XiaoyiChannel] _send_team drop: delivery invalid type=%s event=%s intent=%s",
                type(delivery).__name__, msg.event_type, routing_target.intent,
            )
            return

        # 白名单过滤：team member 场景丢弃中间过程，只投递结果类消息
        if msg.event_type not in self._TEAM_ALLOWED_EVENTS:
            return

        agent_id = delivery.agent_id
        content = self._extract_team_content(msg)
        # 预解析活跃映射，供诊断日志与通道判定共用
        ws_session, ws_task, last_seen = self._resolve_active_ws(agent_id, delivery)
        # 手机端在线判定：最近 _team_ws_alive_window 秒内有该 agent_id 的 inbound。
        # 手机端收到 final 或长时间无消息会主动关 ws（网关无法直接感知手机 ws 状态），
        # 只能靠 inbound 活跃度间接判断：最近发过消息 → ws 大概率还开着 → 走 ws；否则走 push。
        now = time.time()
        ws_active = bool(
            ws_session and ws_task and last_seen
            and (now - last_seen) < self._team_ws_alive_window
        )
        logger.info(
            "[XiaoyiChannel] _send_team enter: event=%s intent=%s agent_id=%s content_len=%d "
            "ws_active=%s last_seen=%s push_id=%s",
            msg.event_type, routing_target.intent, (agent_id or "")[:8], len(content),
            ws_active, (f"{now - last_seen:.1f}s" if last_seen else "none"), bool(delivery.push_id),
        )

        # status_update / file 类事件透传（不参与 push 合并）
        if should_send_as_status_update(msg.event_type):
            if ws_session and ws_task:
                status_text = get_status_text_for_event(msg.event_type, msg.payload)
                status_state = get_status_state_for_event(msg.event_type, msg.payload)
                for url_key in list(self._ws_connections.keys()):
                    await self._send_status_update_with_state(
                        ws_task, ws_session, status_text, status_state, url_key
                    )
            return
        if msg.event_type == EventType.CHAT_FILE:
            if ws_session and ws_task:
                files = msg.payload.get("files", {}) if isinstance(msg.payload, dict) else {}
                for file_info in files:
                    for url_key, ws in self._ws_connections.items():
                        if ws:
                            try:
                                await self._send_file_response(ws_session, ws_task, file_info, url_key)
                            except Exception as e:
                                logger.warning(f"XiaoyiChannel 发送文件响应失败 ({url_key}): {e}")
            return

        # 判定投递通道
        if ws_active:
            # ① 活跃会话 → ws 流式投递
            logger.info("[XiaoyiChannel] _send_team → ws: agent_id=%s sid=%s", (agent_id or "")[:8],
                        (ws_session or "")[:8])
            await self._send_ws_to_user(ws_session, ws_task, msg, content)
        else:
            # ② 后台 → push webhook 全文 final（带 per-agent_id 合并窗口）
            # push_id 优先用 delivery 的，否则从活跃映射取（最后一次已知值）
            push_id = delivery.push_id
            if not push_id and agent_id:
                active = self._active_push_sessions.get(agent_id)
                if active:
                    push_id = active[2]
            if push_id and agent_id:
                logger.info("[XiaoyiChannel] _send_team → push: agent_id=%s push_id=%s", (agent_id or "")[:8],
                            push_id[:8])
                await self._send_push_to_user(agent_id, push_id, content)
            elif self._ws_connections:
                # ③ 兜底：无 push_id 且无活跃会话，走 legacy（按 metadata 投递）
                logger.info("[XiaoyiChannel] _send_team → legacy fallback: agent_id=%s", (agent_id or "")[:8])
                await self._send_legacy(msg)
            else:
                logger.warning("[XiaoyiChannel] _send_team drop: no ws/push/legacy available agent_id=%s",
                               (agent_id or "")[:8])

    def _resolve_active_ws(
            self, agent_id: str, delivery: XiaoyiDeliveryTarget
    ) -> tuple[str, str, float]:
        """解析当前活跃的 (顶层 sessionId, task_id, 最后 inbound 时间戳)。

        优先用 _active_push_sessions[agent_id] 的最新值（inbound 实时维护），
        避免 /join 时冻结的旧 sessionId 导致 team 异步消息 ws 投递路由不到。
        delivery.xiaoyi_session_id 仅在无活跃映射时兜底（用户从未发消息的纯 /join 场景）。
        task_id 始终从活跃映射取（请求级临时值，不进 Subscription）。
        时间戳用于 team 投递 ws 活跃窗口判定：超窗说明手机端可能已关 ws，应降级 push。
        """
        active = self._active_push_sessions.get(agent_id) if agent_id else None
        if active:
            return active[0], active[1], active[3]
        return delivery.xiaoyi_session_id, "", 0.0

    def _extract_team_content(self, msg: Message) -> str:
        """从 Message.payload 抽取 team 消息文本内容.

        team.message 走 event.* 结构化字段（与飞书 _send_team_message 对齐），
        普通 chat 仍走 payload.content/delta。
        """
        if not isinstance(msg.payload, dict):
            return str(msg.payload) if msg.payload else ""
        event = msg.payload.get("event", {})
        if isinstance(event, dict) and str(event.get("type", "")).startswith("team.message"):
            msg_type = event.get("type", "")
            from_member = event.get("from_member", "") or "team"
            to_member = event.get("to_member", "")
            content = str(event.get("content", "") or "")
            if msg_type == "team.message.broadcast":
                recipient = "📢 全员"
            elif msg_type == "team.message.p2p":
                recipient = f"👾 {to_member}" if to_member else "👾 —"
            else:
                recipient = f"👾 {to_member}" if to_member else "👾 —"
            bar = "─" * 20
            return f"\n---\n╭{bar}╮\n│ 🤖 {from_member}   →   {recipient}\n╰{bar}╯\n{content}\n"
        content = msg.payload.get("content", "") or msg.payload.get("delta", "")
        if isinstance(content, dict):
            content = content.get("output", str(content))
        return str(content)

    async def _send_ws_to_user(
            self, session_id: str, task_id: str, msg: Message, content: str
    ) -> None:
        """活跃会话 ws 流式投递（复用 _send_text_response）。

        流式合并：delta chunk 用 16ms（≈60fps）短窗口累积合并发送，
        缓解高频小 chunk 导致的客户端卡顿，同时保持人眼连续的流式实时性。
        final 消息立即冲刷缓冲并发送，保证结尾不延迟。
        """
        last_chunk = msg.event_type == EventType.CHAT_FINAL
        is_final = False
        if isinstance(msg.payload, dict):
            is_final = bool(msg.payload.get("is_complete", False))
        if is_final:
            last_chunk = True

        # team.message 是完整的逻辑消息（monitor 转发全文，非流式 delta），
        if msg.event_type == EventType.TEAM_MESSAGE:
            last_chunk = True
            is_final = False

        # final 消息：冲刷缓冲 + 立即发送
        if is_final or last_chunk:
            buf = self._ws_flush_buffers.pop(task_id, "")
            merged = (buf + content) if buf else content
            # 末尾加换行，分隔多条 final 消息，避免客户端连在一起显示
            if merged and not merged.endswith("\n"):
                merged = merged + "\n"
            # 取消未决 flush 任务，避免重复发送
            t = self._ws_flush_tasks.pop(task_id, None)
            if t and not t.done():
                t.cancel()
            for url_key, ws in self._ws_connections.items():
                if ws:
                    try:
                        await self._send_text_response(
                            session_id, task_id, merged, url_key,
                            append=True, last_chunk=last_chunk, is_final=is_final,
                        )
                        logger.info(
                            "[XiaoyiChannel] team ws sent: event=%s sid=%s task_id=%s "
                            "append=True last=%s final=%s len=%d",
                            msg.event_type, (session_id or "")[:8], task_id,
                            last_chunk, is_final, len(merged),
                        )
                    except Exception as e:
                        logger.warning(f"XiaoyiChannel team ws 发送失败 ({url_key}): {e}")
            return

        # delta chunk：累积进缓冲。超过阈值（200 字符）立即同步冲刷，避免 ws.send
        # 阻塞时 buffer 持续积压导致延迟；否则调度 16ms 延迟 flush 合并高频小 chunk。
        self._ws_flush_buffers[task_id] = self._ws_flush_buffers.get(task_id, "") + content
        if len(self._ws_flush_buffers[task_id]) >= 200:
            buf = self._ws_flush_buffers.pop(task_id, "")
            t = self._ws_flush_tasks.pop(task_id, None)
            if t and not t.done():
                t.cancel()
            for url_key, ws in self._ws_connections.items():
                if ws:
                    try:
                        await self._send_text_response(
                            session_id, task_id, buf, url_key,
                            append=True, last_chunk=False, is_final=False,
                        )
                    except Exception as e:
                        logger.warning(f"XiaoyiChannel team ws 即时冲刷失败 ({url_key}): {e}")
            return
        if task_id not in self._ws_flush_tasks or self._ws_flush_tasks[task_id].done():
            self._ws_flush_tasks[task_id] = asyncio.create_task(
                self._flush_ws_buffer(session_id, task_id, delay=0.016)
            )

    async def _flush_ws_buffer(
            self, session_id: str, task_id: str, delay: float = 0.0
    ) -> None:
        """冲刷 ws 流式缓冲，合并发送累积的 delta 文本。"""
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            buf = self._ws_flush_buffers.pop(task_id, "")
            if not buf:
                return
            for url_key, ws in self._ws_connections.items():
                if ws:
                    try:
                        await self._send_text_response(
                            session_id, task_id, buf, url_key,
                            append=True, last_chunk=False, is_final=False,
                        )
                    except Exception as e:
                        logger.warning(f"XiaoyiChannel team ws flush 发送失败 ({url_key}): {e}")
        except asyncio.CancelledError:
            pass
        finally:
            self._ws_flush_tasks.pop(task_id, None)

    async def _send_push_to_user(self, agent_id: str, push_id: str, content: str) -> None:
        """后台 push webhook 投递全文 final，带 per-agent_id 合并窗口。

        同一 agent_id 在 message_merge_window_ms 内的多条消息累积进 buffer，
        第一条到达时调度延迟 flush 任务，窗口到期统一合并发送一次，
        避免短时多条 mention 轰炸。push_id 作为 webhook 寻址 token 随 flush 一并发出。
        """
        if not self.config.api_id:
            logger.debug("[PUSH] team push 跳过：api_id 未配置")
            return
        if not content.strip():
            return

        now = time.time()
        buf = self._push_merge_buffers.setdefault(agent_id, [])
        buf.append((now, content, content[:30], push_id))

        # 若已有 pending flush 任务，复用之（继续累积）；否则调度延迟 flush
        if agent_id not in self._push_flush_tasks or self._push_flush_tasks[agent_id].done():
            window_ms = getattr(self.config, "message_merge_window_ms", 15000) or 15000
            self._push_flush_tasks[agent_id] = asyncio.create_task(
                self._flush_push_buffer(agent_id, window_ms / 1000)
            )

    async def _flush_push_buffer(self, agent_id: str, window_s: float) -> None:
        """延迟 window 秒后合并发送 buffer 全部内容。"""
        try:
            await asyncio.sleep(window_s)
            buf = self._push_merge_buffers.pop(agent_id, [])
            if not buf:
                return
            # buffer 在 flush 后整体 pop 清空，不存在跨窗口残留，无需按时间过滤
            merged_content = "\n\n".join(b[1] for b in buf)
            summary = self._build_push_summary(buf[0][1]) or (
                merged_content[:30] + "..." if len(merged_content) > 30 else merged_content)
            # push_id 取 buffer 中最近一次的非空值（同一用户 push_id 稳定）
            push_id = ""
            for entry in reversed(buf):
                if entry[3]:
                    push_id = entry[3]
                    break
            if not push_id:
                logger.warning("[PUSH] team push flush 跳过：buffer 中无有效 push_id (agent_id=%s)", agent_id[:8])
                return
            push_config = PushConfig(
                mode=self.config.mode,
                api_id=self.config.api_id,
                push_id=push_id,
                push_url=self.config.push_url,
                ak=self.config.ak,
                sk=self.config.sk,
                uid=self.config.uid,
                api_key=self.config.api_key,
            )
            await XiaoYiPushService(push_config).send_push(summary, merged_content)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[PUSH] team push flush 失败 (agent_id=%s): %s", agent_id[:8], e)
        finally:
            self._push_flush_tasks.pop(agent_id, None)

    @staticmethod
    def _build_push_summary(content: str) -> str:
        """从 _extract_team_content 拼好的 team 消息里提取 summary。

        格式 @recipient content截断...，取第一条消息的 recipient + 正文前 30 字。
        content 结构：╭{bar}╮\\n│ 🤖 {from}   →   {recipient}\\n╰{bar}╯\\n{正文}\\n---\\n
        （recipient 可能带 👾 前缀，如 "👾 human-wolf"；广播为 "📢 全员"）
        提取失败回退空串（由调用方用截断 merged_content 兜底）。
        """
        if not content:
            return ""
        # recipient：│ ... →   {recipient} 行的 → 之后部分（可能含 👾 前缀）
        recipient = ""
        for line in content.split("\n"):
            if "→" in line and "🤖" in line:
                recipient = line.split("→", 1)[1].strip()
                break
        # 正文：╰...╯ 之后的行（排除尾部分隔符 ---）
        body = ""
        m = re.search(r"╯\s*\n(.*)", content, re.DOTALL)
        if m:
            tail = m.group(1)
            # 去掉结尾的 \n---\n
            tail = re.sub(r"\n---\s*\n?$", "", tail).strip()
            body = tail
        if not recipient and not body:
            return ""
        if not body:
            return f"@{recipient}" if recipient else ""
        snippet = body[:30] + "..." if len(body) > 30 else body
        return f"@{recipient} {snippet}" if recipient else snippet

    def get_metadata(self) -> ChannelMetadata:
        return ChannelMetadata(
            channel_id=self.channel_id,
            source="websocket",
            extra={
                "mode": "client",
                "ws_url1": self.config.ws_url1,
                "ws_url2": self.config.ws_url2,
                "agent_id": self.config.agent_id,
            },
        )

    async def _reconnect_loop(self, url_key: str, url: str) -> None:
        """自动重连循环（双通道）."""
        while self._running:
            try:
                await self._connect(url_key, url)
                if not self._running:
                    break
                # 连接被远端正常关闭时也做退避，避免瞬时重连刷屏。
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"XiaoyiChannel 连接失败 ({url}): {e}")
                await asyncio.sleep(5)

    async def _connect(self, url_key: str, url: str) -> None:
        """连接到小艺服务器（双通道）."""
        if is_named_pipe_url(url):
            # 桌面集成形态：本地中转已迁命名管道（np://claw-relay）
            await self._connect_pipe(url_key, url)
            return

        import websockets

        headers = _generate_auth_headers(self.config)
        parsed = urlparse(url)
        is_ip = bool(parsed.hostname and parsed.hostname.replace(".", "").isdigit())

        ssl_context = ssl.create_default_context()
        if is_ip:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        async with websockets.connect(
                url,
                additional_headers=headers,
                # ssl=ssl_context,
                ping_interval=15,
                ping_timeout=15,
                close_timeout=5,
        ) as ws:
            self._ws_connections[url_key] = ws
            self._send_locks[url_key] = asyncio.Lock()
            logger.info(f"XiaoyiChannel 已连接 {url_key}: {url}")

            # clawd_bot_init / heartbeat 由应用层（桌面客户端 cloud-ws-relay）统一发送，
            # channel 层不再构造，只发 agent_response（本连接仅承担收发业务帧）。

            try:
                async for raw in ws:
                    await self._handle_raw_message(raw, url_key)
            except Exception as e:
                logger.warning(f"XiaoyiChannel 连接异常 ({url_key}): {e}")
            finally:
                self._ws_connections[url_key] = None
                self._send_locks.pop(url_key, None)
                close_code = getattr(ws, "close_code", None)
                close_reason = getattr(ws, "close_reason", None)
                logger.info(
                    f"XiaoyiChannel 连接关闭 {url_key}: {url} (code={close_code}, reason={close_reason})"
                )

    async def _connect_pipe(self, url_key: str, url: str) -> None:
        """命名管道形态的本地中转连接（桌面集成形态，替代 ws://127.0.0.1:19690）。

        协议（与 claw_desktop cloud-ws-relay 的管道 server 同一契约）：
        长度前缀帧（np_transport FrameCodec）；首帧必须是鉴权帧
        {"type":"auth","agentId":...,"ak":...,"ts":...,"sign":...}（WS 握手头下沉），
        之后每帧 = 一条原 WS 文本消息（JSON 对象）；下行同样逐帧下发。
        断连/异常返回后由 _reconnect_loop 统一 5s 退避重连。
        """
        pipe = await open_pipe(pipe_path_from_url(url))
        # PipeStream 为 overlapped 全双工（读驻留不阻塞写），直接承担本渠道
        # 「常驻 recv + 并发 send（心跳/业务帧）」形态，无需额外适配层
        self._ws_connections[url_key] = pipe
        self._send_locks[url_key] = asyncio.Lock()
        logger.info(f"XiaoyiChannel 已连接 {url_key}: {url}（命名管道）")
        try:
            # 首帧鉴权（必须在任何业务帧之前；验签失败对端直接断管）
            await pipe.send_frame(_generate_pipe_auth_frame(self.config))

            # clawd_bot_init / heartbeat 由应用层（桌面客户端 cloud-ws-relay）统一发送，
            # channel 层不再构造，只发 agent_response（本连接仅承担收发业务帧）。

            try:
                while True:
                    frame = await pipe.recv_frame()
                    if not isinstance(frame, (str, bytes)):
                        # 帧负载即解析好的 JSON 对象：还原为文本复用既有处理路径
                        frame = json.dumps(frame, ensure_ascii=False)
                    await self._handle_raw_message(frame, url_key)
            except Exception as e:
                logger.warning(f"XiaoyiChannel 管道连接异常 ({url_key}): {e}")
        finally:
            self._ws_connections[url_key] = None
            self._send_locks.pop(url_key, None)
            try:
                await self._close_transport(pipe)
            except Exception as close_error:
                logger.warning(f"XiaoyiChannel 关闭管道连接失败 ({url_key}): {close_error}")
            logger.info(f"XiaoyiChannel 管道连接关闭 {url_key}: {url}")

    async def _close_transport(self, ws: Any) -> None:
        """关闭一条中转连接（WebSocket 或命名管道 PipeStream）。

        管道关闭内需解除读驻留并收尾句柄（overlapped CancelIoEx），
        加 5s 兜底防关闭路径挂死 stop()/重连流程。
        """
        if isinstance(ws, PipeStream):
            await asyncio.wait_for(ws.close(), timeout=5)
            return
        await ws.close()

    async def _handle_raw_message(self, raw: str | bytes, url_key: str | None = None) -> None:
        """处理接收到的原始消息，转换为 JiuwenSwarm 内部格式."""
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            is_gui_response_frame = "InvokeJarvisGUIAgentResponse" in raw
            message = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"XiaoyiChannel JSON 解析失败: {e}")
            return
        msg_type = message.get("msgType")
        method = message.get("method")
        if is_gui_response_frame:
            logger.info(
                "[GUI_RPC_TRACE] phase=CHANNEL_RAW_GUI_FRAME "
                "msg_type=%s method=%s session_id=%s has_msg_detail=%s",
                msg_type,
                method,
                str(message.get("sessionId") or ""),
                isinstance(message.get("msgDetail"), str),
            )
            logger.info(
                "[GUI_AGENT_DIAG] phase=XIAOYI_RAW_GUI_FRAME raw=%s parsed=%r",
                raw,
                message,
            )

        # 添加详细日志用于诊断工具消息
        if method or (msg_type and msg_type != "heartbeat"):
            logger.info(f"[XiaoyiChannel] _handle_raw_message: msg_type={msg_type},"
                        f"method={method}, sessionId={message.get('sessionId', 'N/A')}")

        if msg_type == "heartbeat":
            return
        # ── 入站 A2A 诊断:扫描所有 AgentEvent header 打出,用于发现端侧下发的
        # 各类指令(含尚未处理的 MemoryQuery/SelfEvolution/CronQuery 及其他)。
        # 无 AgentEvent header 的普通消息也打一条 msg_type/method,便于全覆盖。
        try:
            from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.self_evolution import (
                iter_agent_event_headers,
            )
            _in_events = iter_agent_event_headers(message)
            if _in_events:
                logger.info(
                    "[XiaoyiChannel][A2A_IN] AgentEvent=%s msg_type=%s method=%s "
                    "session_id=%s task_id=%s msg_id=%s",
                    _in_events, msg_type, method,
                    str(message.get("sessionId") or ""),
                    str(message.get("taskId") or ""),
                    str(message.get("id") or ""),
                )
            else:
                logger.info(
                    "[XiaoyiChannel][A2A_IN] no_AgentEvent msg_type=%s method=%s "
                    "session_id=%s",
                    msg_type, method,
                    str(message.get("sessionId") or ""),
                )
        except Exception as _e:
            logger.debug("[XiaoyiChannel][A2A_IN] scan failed: %s", _e)
        # MemoryQuery is a command data event (direct or wrapped A2A), not a
        # normal user message. Consume it before the generic data-event parser.
        from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.memory_query import (
            extract_memory_query,
            handle_memory_query,
            memory_query_command,
            configured_runtime_state_path,
        )

        memory_query = extract_memory_query(message)
        if memory_query is not None:
            logger.info(
                "[XiaoyiChannel][MemoryQuery_IN] action=%s session=%s task=%s "
                "msg=%s params=%s",
                memory_query.action,
                memory_query.session_id,
                memory_query.task_id,
                memory_query.message_id,
                memory_query.params,
            )
            from jiuwenswarm.common.utils import get_agent_workspace_dir

            answer = await asyncio.to_thread(
                handle_memory_query,
                memory_query,
                workspace_dir=get_agent_workspace_dir(),
                runtime_state_path=configured_runtime_state_path(),
            )
            logger.info(
                "[XiaoyiChannel][MemoryQuery_DONE] action=%s answer=%s",
                memory_query.action,
                answer,
            )
            await self.send_xiaoyi_phone_tools_command(
                memory_query.session_id,
                memory_query.task_id,
                memory_query.message_id,
                memory_query_command(memory_query.action, answer),
                final=True,
            )
            return
        # ── SelfEvolution detection (AgentEvent.ClawSelfEvolutionState / .Get) ──
        # 1:1 with xy_channel self-evolution-handler.ts: persist device-reported
        # selfEvolutionState to .xiaoyiruntime (set, reply empty final ACK) or emit
        # a Common/Action intent back to the device (get). Consumed before the
        # generic data-event parser, same slot as MemoryQuery above.
        from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.self_evolution import (
            extract_self_evolution_set,
            extract_self_evolution_get,
            build_self_evolution_state_get_command,
        )
        from jiuwenswarm.agents.harness.common.memory.celia.runtime_state import (
            set_self_evolution_state,
            read_self_evolution_state,
        )

        self_evolution_set = extract_self_evolution_set(message)
        if self_evolution_set is not None:
            logger.info(
                "[XiaoyiChannel][SelfEvolution_IN] kind=ClawSelfEvolutionState "
                "state=%s session=%s task=%s msg=%s",
                self_evolution_set.state,
                self_evolution_set.session_id,
                self_evolution_set.task_id,
                self_evolution_set.message_id,
            )
            await asyncio.to_thread(
                set_self_evolution_state,
                self_evolution_set.state,
                configured_runtime_state_path(),
            )
            logger.info(
                "[XiaoyiChannel][SelfEvolution_SET_DONE] persisted state=%s "
                "session=%s task=%s msg=%s",
                self_evolution_set.state,
                self_evolution_set.session_id,
                self_evolution_set.task_id,
                self_evolution_set.message_id,
            )
            await self._send_empty_final_ack(
                self_evolution_set.session_id,
                self_evolution_set.task_id,
                self_evolution_set.message_id,
            )
            return

        self_evolution_get = extract_self_evolution_get(message)
        if self_evolution_get is not None:
            logger.info(
                "[XiaoyiChannel][SelfEvolution_IN] kind=ClawSelfEvolutionStateGet "
                "session=%s task=%s msg=%s",
                self_evolution_get.session_id,
                self_evolution_get.task_id,
                self_evolution_get.message_id,
            )
            state = await asyncio.to_thread(
                read_self_evolution_state,
                configured_runtime_state_path(),
            )
            logger.info(
                "[XiaoyiChannel][SelfEvolution_GET_DONE] read state=%s "
                "session=%s task=%s msg=%s",
                state,
                self_evolution_get.session_id,
                self_evolution_get.task_id,
                self_evolution_get.message_id,
            )
            await self.send_xiaoyi_phone_tools_command(
                self_evolution_get.session_id,
                self_evolution_get.task_id,
                self_evolution_get.message_id,
                build_self_evolution_state_get_command(state),
                final=True,
            )
            return
        # ── A2A CronQuery detection (AgentEvent.CronQuery) ──────────────
        # Detect CronQuery directives at root level or inside parts directives.
        # Handle and respond via WebSocket before falling through to normal flow.
        try:
            from jiuwenswarm.gateway.cron.cron_query_handler import (
                extract_cron_query_from_message,
                dispatch_cron_query,
                build_a2a_response_envelope,
            )
            from jiuwenswarm.gateway.cron.controller import CronController

            cron_payload = extract_cron_query_from_message(message)
            if cron_payload is not None:
                logger.info(
                    "[XiaoyiChannel] CronQuery action=%s detected",
                    cron_payload.get("action"),
                )
                try:
                    cc = CronController.get_instance()
                except RuntimeError:
                    cc = None
                # session_id_ctx: params.sessionId (conversationId, 带 _CronQuery 后缀)
                session_id_ctx = ""
                params_root = message.get("params")
                if isinstance(params_root, dict):
                    sid_val = params_root.get("sessionId")
                    if isinstance(sid_val, str) and sid_val.strip():
                        session_id_ctx = sid_val.strip()
                # rpc_id: JSON-RPC request id (用于响应关联)
                rpc_id = message.get("id") or ""
                task_id = ""
                if isinstance(params_root, dict):
                    task_id = params_root.get("id") or ""
                result = await dispatch_cron_query(
                    cron_payload,
                    cron_controller=cc,
                    session_id=session_id_ctx,
                )
                # dispatch returns {action, status:True, ans} on success,
                # or {action, ans:{error}} on error (no status field).
                # build_a2a_response_envelope: status=True → include payload.status;
                # status=False → omit payload.status (error per protocol doc).
                is_ok = bool(result.get("status", False))
                response_envelope = build_a2a_response_envelope(
                    result.get("action", cron_payload.get("action", "")),
                    result.get("ans", {}),
                    status=is_ok,
                )
                if url_key:
                    try:
                        # CronQuery 信封包装在标准 JSON-RPC artifact-update 响应里，
                        # 与正常 agent_response 路径一致（jsonrpc/id/result/artifact/parts）。
                        # 下行响应用 data.commands（与正常消息流一致），元素为 {header, payload} 信封。
                        cron_rpc_response = {
                            "jsonrpc": "2.0",
                            "id": rpc_id,
                            "result": {
                                "taskId": task_id,
                                "kind": "artifact-update",
                                "append": False,
                                "lastChunk": True,
                                "final": True,
                                "artifact": {
                                    "artifactId": str(uuid.uuid4()),
                                    "parts": [
                                        {
                                            "kind": "data",
                                            "data": {"commands": [response_envelope]},
                                        }
                                    ],
                                },
                            },
                        }
                        cron_wrapper = {
                            "msgType": "agent_response",
                            "agentId": self.config.agent_id,
                            "sessionId": session_id_ctx,
                            "taskId": task_id,
                            "msgDetail": json.dumps(cron_rpc_response, ensure_ascii=False),
                        }
                        await self._safe_ws_send(url_key, cron_wrapper)
                    except Exception as send_err:
                        logger.warning(
                            "[XiaoyiChannel] CronQuery response send failed: %s",
                            send_err,
                        )
                return  # CronQuery handled; do not fall through to message dispatch
        except ImportError:
            logger.debug("[XiaoyiChannel] cron_query_handler not available, skipping CronQuery detection")
        except Exception as exc:
            logger.warning("[XiaoyiChannel] CronQuery handling error: %s", exc)

        # ── Cron Trigger detection (Common/Trigger, triggerType=event) ──
        # 设备端 cron 到时间后，通过 WS 发来报文1（含 Common/Trigger 事件，
        # payload.dataMap 含 cronId/cronTitle/pushDataId）。我们识别后触发
        # 对应 cron job 执行，等结果产出，再通过 WS 回发正常 a2a 文本消息
        # （kind: "text" 的 artifact-update），与普通消息流一致。
        # 与 CronQuery 同层，在 _handle_message_stream 之前拦截。
        try:
            from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.cron_trigger_handler import (
                extract_cron_trigger,
            )
            from jiuwenswarm.gateway.cron.controller import CronController

            trigger_ctx = extract_cron_trigger(message)
            if trigger_ctx is not None:
                logger.info(
                    "[XiaoyiChannel][CronTrigger_IN] cronId=%s cronTitle=%s "
                    "pushDataId=%s session=%s conversation=%s agent=%s "
                    "rpc_id=%s task_id=%s",
                    trigger_ctx.cron_id,
                    trigger_ctx.cron_title,
                    trigger_ctx.push_data_id,
                    trigger_ctx.session_id,
                    trigger_ctx.conversation_id,
                    trigger_ctx.agent_id,
                    trigger_ctx.rpc_id,
                    trigger_ctx.task_id,
                )
                try:
                    cc = CronController.get_instance()
                except RuntimeError:
                    cc = None

                result_text = ""
                run_id = ""
                if cc is not None:
                    try:
                        run_id, result_text = await cc.run_now_and_wait(
                            trigger_ctx.cron_id
                        )
                        logger.info(
                            "[XiaoyiChannel][CronTrigger_DONE] cronId=%s run_id=%s "
                            "result_len=%d",
                            trigger_ctx.cron_id,
                            run_id,
                            len(result_text or ""),
                        )
                    except Exception as run_exc:
                        logger.warning(
                            "[XiaoyiChannel][CronTrigger_RUN_ERROR] cronId=%s "
                            "error=%s",
                            trigger_ctx.cron_id,
                            run_exc,
                        )
                        result_text = f"[cron] 任务执行失败: {run_exc}"

                if not result_text:
                    result_text = "[cron] 任务完成，但未返回可展示文本"

                # 用正常 a2a 文本消息回发（kind: "text"），与 _send_text_response 一致
                if url_key:
                    try:
                        await self._send_text_response(
                            trigger_ctx.session_id,
                            trigger_ctx.task_id,
                            result_text,
                            url_key,
                        )
                        logger.info(
                            "[XiaoyiChannel][CronTrigger_SENT] cronId=%s "
                            "session=%s result_len=%d",
                            trigger_ctx.cron_id,
                            trigger_ctx.session_id,
                            len(result_text),
                        )
                    except Exception as send_err:
                        logger.warning(
                            "[XiaoyiChannel][CronTrigger_SEND_ERROR] cronId=%s "
                            "error=%s",
                            trigger_ctx.cron_id,
                            send_err,
                        )
                return  # CronTrigger handled; do not fall through to message dispatch
        except ImportError:
            logger.debug("[XiaoyiChannel] cron_trigger_handler not available, skipping CronTrigger detection")
        except Exception as exc:
            logger.warning("[XiaoyiChannel] CronTrigger handling error: %s", exc)

        # ── Common/Trigger detection (用户点击推送消息触发) ──────────
        # 设备端用户点击定时任务的推送消息时，通过 WS 发来含 Common/Trigger
        # 事件的报文（payload.dataMap 含 pushDataId）。识别后直接读取预存的
        # pushData 内容，构造标准 a2a 文本消息（kind: "text" 的 artifact-update）
        # 通过 WS 回发，不走正常 agent 流程。与 xy_channel bot.ts Trigger 分支
        # + formatter.ts sendA2AResponse 对齐。与上方 CronTrigger（System/UnfinishedTask，
        # 设备 cron 到点自动触发并执行 job）的区别：本分支是用户主动点击、
        # 直接返回缓存内容、不执行 cron job。
        try:
            from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools.trigger_handler import (
                handle_trigger_event,
            )

            async def _send_trigger_wrapper(wrapper: dict[str, Any]) -> None:
                logger.info("url_key %s wrapper %s", url_key, json.dumps(wrapper))
                if url_key:
                    await self._safe_ws_send(url_key, wrapper)

            handled = await handle_trigger_event(
                message,
                _send_trigger_wrapper,
                agent_id=self.config.agent_id,
            )
            if handled:
                return  # Common/Trigger handled; do not fall through to message dispatch
        except ImportError:
            logger.debug("[XiaoyiChannel] trigger_handler not available, skipping Common/Trigger detection")
        except Exception as exc:
            logger.warning("[XiaoyiChannel] Common/Trigger handling error: %s", exc)

        # MemoryQuery is a command data event (direct or wrapped A2A), not a
        # normal user message. Consume it before the generic data-event parser.
        from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.memory_query import (
            extract_memory_query,
            handle_memory_query,
            memory_query_command,
            configured_runtime_state_path,
        )

        memory_query = extract_memory_query(message)
        if memory_query is not None:
            from jiuwenswarm.common.utils import get_agent_workspace_dir

            answer = await asyncio.to_thread(
                handle_memory_query,
                memory_query,
                workspace_dir=get_agent_workspace_dir(),
                runtime_state_path=configured_runtime_state_path(),
            )
            await self.send_xiaoyi_phone_tools_command(
                memory_query.session_id,
                memory_query.task_id,
                memory_query.message_id,
                memory_query_command(memory_query.action, answer),
                final=True,
            )
            return

        # Direct message streams require a stable params.sessionId. Other
        # JSON-RPC methods (for example tasks/cancel) have their own schema.
        if message.get("jsonrpc") == "2.0" and method == "message/stream":
            params_root = message.get("params")
            if not isinstance(params_root, dict):
                params_root = {}
            sid = params_root.get("sessionId")
            if sid is None or (isinstance(sid, str) and not sid.strip()):
                logger.warning(
                    "XiaoyiChannel 直连 A2A 缺少有效 params.sessionId，跳过本帧（与 xy_channel 一致）"
                )
                return

        await self._dispatch_gui_agent_events(message)

        # 检查是否是 data-only 消息（工具执行结果）
        data_event = self._extract_data_event(message)
        if data_event:
            logger.info(f"XiaoyiChannel 收到 data-event: {data_event.intent_name}, status={data_event.status}")
            await self._handle_data_event(data_event)
            return

        # Direct A2A GUI responses also use method=message/stream. They have
        # already been consumed by the GUI handler and must not become a new
        # empty user request, which would cancel the active Agent stream.
        if is_gui_response_frame:
            logger.info(
                "[GUI_AGENT_DIAG] phase=XIAOYI_GUI_FRAME_CONSUMED "
                "session_id=%s method=%s reason=handled_by_gui_rpc",
                str(message.get("sessionId") or ""),
                method,
            )
            return

        # GUI / UploadExeResult 等已在 _dispatch_gui_agent_events 与 _extract_data_event 中处理，勿再落 unknown method。
        if msg_type == "data":
            return

        method = message.get("method")
        if method == "message/stream":
            await self._handle_message_stream(message)
        elif method == "clearContext":
            await self._handle_clear_context(message)
        elif method == "tasks/cancel":
            await self._handle_tasks_cancel(message)
        else:
            # 服务端 JSON-RPC 仅含 data parts 的工具回包（如纯 GUI 响应）无 method 字段
            if not method and not msg_type and message.get("jsonrpc") == "2.0":
                parts = self._get_a2a_parts(message)
                if parts and all(p.get("kind") == "data" for p in parts):
                    return
            logger.warning(f"XiaoyiChannel 未知方法: {method}")

    async def _handle_message_stream(self, message: dict[str, Any]) -> None:
        """处理 message/stream 消息，转换为 JiuwenSwarm Message."""
        # V2: 区分两层 sessionId —— 顶层 sessionId 是物理回发地址（临时），
        # conversationId / params.sessionId 是逻辑会话（跨请求稳定）。
        top_session_id = message.get("sessionId", "") or ""
        conversation_id = (
                message.get("conversationId")
                or message.get("params", {}).get("sessionId", "")
                or ""
        )
        # 兼容旧逻辑：inbound 的 session_id 取顶层（物理），逻辑会话独立存 metadata
        session_id = top_session_id or conversation_id
        task_id = message.get("params", {}).get("id", "") or message.get("id", "") or ""
        agent_id = message.get("agentId", "") or self.config.agent_id
        device_id = message.get("deviceId", "") or ""
        # 优先用 params.sessionId（= conversationId，真·对话 id），跨多轮稳定、与 task 解耦；
        # 缺失时回退到顶层 sessionId。
        session_id = message.get("params", {}).get("sessionId") or message.get("sessionId", "")
        task_id = message.get("params", {}).get("id", ) or ""
        root_session_id = message.get("sessionId", "")
        user_message = message.get("params", {}).get("message", {})
        parts = user_message.get("parts", [])

        # ==================== PROCESS PARTS (TEXT & FILES) ====================
        text = ""
        push_id = ""  # V2: 从 data part 的 systemVariables 提取，webhook 推送寻址 token
        client_variables: dict[str, Any] = {}  # data part 的 variables.clientVariables（workspace/permission 等）
        permission_reply: dict[str, Any] | None = None  # AgentEvent/PermissionReply 审批回执事件
        file_attachments: list[str] = []
        media_files: list[dict[str, Any]] = []

        for part in parts:
            kind = part.get("kind")
            if kind == "text" and part.get("text"):
                text += part.get("text", "")
            elif kind == "file" and part.get("file"):
                file_info = part["file"]
                uri = file_info.get("uri")
                mime_type = file_info.get("mimeType", "")
                name = file_info.get("name", "")

                if not uri:
                    logger.warning(f"XiaoYi: File part without URI, skipping: {name}")
                    continue

                try:
                    media_files.append({"uri": uri, "mime_type": mime_type, "name": name})

                    # For text-based files, extract content inline
                    from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.media import \
                        is_text_mime_type, extract_text_from_url
                    if is_text_mime_type(mime_type):
                        try:
                            file_attachments.append(f"[文件: {name}]")
                            logger.info(f"XiaoYi: Successfully extracted text from: {name}")
                        except Exception:
                            logger.warning(f"XiaoYi: Text extraction failed for {name}, will download as binary")
                            file_attachments.append(f"[文件: {name}]")
                    else:
                        file_attachments.append(f"[文件: {name}]")
                except Exception as e:
                    logger.error(f"XiaoYi: Failed to process file {name}: {e}")
                    file_attachments.append(f"[文件处理失败: {name}]")
            elif kind == "data":
                data = part.get("data", {})
                if isinstance(data, dict):
                    push_id = data.get("variables", {}).get("systemVariables", {}).get("push_id", "")
                    self.config.push_id = push_id if push_id else self.config.push_id
                    # clientVariables：手机端下发的工作空间/权限模式等客户端上下文
                    variables = data.get("variables")
                    if isinstance(variables, dict):
                        cv = variables.get("clientVariables")
                        if isinstance(cv, dict):
                            client_variables.update(cv)
                    # 审批回执事件（AgentEvent/PermissionReply）：用户点选/结构化回复授权决定
                    events = data.get("events")
                    if isinstance(events, list):
                        for ev in events:
                            if not isinstance(ev, dict):
                                continue
                            header = ev.get("header")
                            if (
                                isinstance(header, dict)
                                and header.get("namespace") == "AgentEvent"
                                and header.get("name") == "PermissionReply"
                            ):
                                payload = ev.get("payload")
                                permission_reply = payload if isinstance(payload, dict) else {}
                    # 持久化 pushId 到本地 pushIdList.json（异步，不阻塞主流程），
                    # 供 pushBroadcast 向所有已注册 pushId 广播。对应 xy_channel
                    # 的 utils/pushid-manager.ts addPushId 调用。
                    if push_id:
                        try:
                            from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.pushid_manager import (
                                add_push_id,
                            )
                            asyncio.ensure_future(
                                asyncio.to_thread(add_push_id, push_id)
                            )
                            logger.info("XiaoYi: Extracted push_id from user message")
                        except Exception as push_id_error:
                            logger.warning(
                                f"XiaoYi: Failed to persist pushId: {push_id_error}"
                            )
        # =================================================================

        # V2: 维护 agent_id → (顶层 sessionId, task_id, push_id, ts) 活跃映射，出站判定 ws vs push 用
        # ts 用于超时清理，避免异常断开的 stale 条目累积（多用户长期运行）
        if agent_id:
            self._active_push_sessions[agent_id] = (top_session_id, task_id, push_id, time.time())
            if push_id:
                self.config.push_id = push_id  # 内存态保持最新，供 cron 推送兜底

        # Log summary of processed attachments
        if file_attachments:
            logger.info(f"XiaoYi: Processed {len(file_attachments)} file(s): {', '.join(file_attachments)}")

        # ==================== DOWNLOAD AND SAVE MEDIA FILES ====================
        media_payload: dict[str, Any] = {}
        if media_files:
            logger.info(f"XiaoYi: Downloading {len(media_files)} media file(s)...")
            from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.media import (
                MediaFile,
                MediaDownloadOptions,
                download_and_save_media_list,
                build_xiaoyi_media_payload,
            )
            files_to_download = [
                MediaFile(uri=f["uri"], mime_type=f["mime_type"], name=f["name"])
                for f in media_files
            ]
            options = MediaDownloadOptions(max_bytes=30_000_000, timeout_ms=60_000)
            downloaded_media = await download_and_save_media_list(files_to_download, options)
            logger.info(f"XiaoYi: Successfully downloaded {len(downloaded_media)}/{len(media_files)} file(s)")
            media_payload = build_xiaoyi_media_payload(downloaded_media)
        # =================================================================

        # 将最近一次可回发的小艺身份写入 config.yaml，供 cron 推送时使用
        try:
            from jiuwenswarm.common.config import update_xiaoyi_runtime_in_config

            rpc_id = message.get("id")
            conf_update: dict[str, Any] = {
                "last_session_id": session_id or "",
                "last_task_id": task_id or "",
                "last_message_id": str(rpc_id) if rpc_id is not None else "",
            }
            # V2: 收到非空 push_id 时同时写入顶层与 apps[].push_id（webhook 推送 token）
            if push_id:
                conf_update["push_id"] = push_id
            update_xiaoyi_runtime_in_config(
                conf_update,
                api_id=self.config.api_id,
                agent_id=self.config.agent_id,
            )
        except Exception as config_error:
            logger.warning(f"XiaoyiChannel 更新配置失败: {config_error}")

        # ==================== BUILD MESSAGE AND ROUTE ====================
        # V2 Stream Routing: 填充 5 维字段 ——
        #   user_id = agentId（per-user agent 标识，RoutingKey.user_id 维度）
        #   bot_id = config.agent_id（让 MessageHandler._resolve_app_id 兜底拿到 app_id）
        #   session_id = 逻辑会话（conversationId，非 team 兜底更稳定）
        #   chat_id = 顶层 sessionId（物理回发兜底）
        # 平台身份写入 metadata，供回发时使用（与 session_id 解耦，\new_session 后仍可正确回发）
        user_id = agent_id
        logical_session = conversation_id or session_id
        metadata = {
            "method": "message/stream",
            "xiaoyi_session_id": top_session_id,  # 顶层 sessionId（物理回发）
            "xiaoyi_task_id": task_id,
            "xiaoyi_conversation_id": conversation_id,  # 逻辑会话
            "xiaoyi_push_id": push_id,  # webhook 推送 token
            "xiaoyi_device_id": device_id,  # 设备标识（备用）
            "im_sender_user_id": user_id,  # MessageHandler whoami 用
            "xiaoyi_session_id": session_id,
            "xiaoyi_root_session_id": root_session_id or session_id,
            "xiaoyi_params_session_id": message.get("params", {}).get("sessionId", ""),
            "xiaoyi_task_id": task_id,
            "xiaoyi_rpc_id": str(message.get("id") or ""),
            "xiaoyi_push_id": str(self.config.push_id or ""),
            "xiaoyi_user_id": str(self.config.uid or ""),
            "celia_user_id": str(self.config.uid or ""),
            "conversation_id": session_id,
        }
        try:
            from jiuwenswarm.agents.harness.common.memory.celia.runtime_state import update_runtime_info
            from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.memory_query import (
                configured_runtime_state_path,
            )

            await asyncio.to_thread(
                update_runtime_info,
                root_session_id or session_id,
                session_id,
                task_id,
                configured_runtime_state_path(),
            )
        except OSError:
            logger.warning("XiaoyiChannel failed to update .xiaoyiruntime", exc_info=True)

        raw_msg_id = message.get("id")
        # ==================== PERMISSION APPROVAL BRIDGE ====================
        # 本会话存在待答复的权限/确认审批时，把用户回复（文本约定或 PermissionReply
        # data event）转换为 interrupt resume（chat.send + request_id/answers/source）
        # 恢复被挂起的运行，而不是作为新任务下发。
        # 必须在空帧拦截之前：仅携带 PermissionReply 事件的帧无 text/file，
        # 放后面会被当成空帧丢弃，审批回执永远到不了。
        approval = self._resolve_approval_reply(logical_session, text.strip(), permission_reply)
        if approval is not None:
            answer, pending = approval
            logger.info(
                f"XiaoYi: 识别到审批回复 session={logical_session} "
                f"request_id={pending.get('request_id')} answer={answer}"
            )
            await self._route_approval_reply(
                message,
                answer,
                pending,
                session_id=session_id,
                logical_session=logical_session,
                agent_id=agent_id,
                device_id=device_id,
                push_id=push_id,
            )
            return
        # =================================================================
        # ── 空 message/stream 帧拦截 ───────────────────────────────────
        # 端侧会通过同一条 WS 推一些 method=message/stream 的事件帧（切换到下一条
        # 指令、文件更新通知 files_updated_by_user、push_id 回写等）：它们带一个
        # data part 但没有真实用户文本/文件，顶层 id 与 task_id 却是有效真值。若
        # 仅按 id 是否占位符判定（旧版 guard），这类帧因 id 有效而被放过，空内容
        # 被当成空 user 消息路由进对话流，凭空起新 session 让模型对空 input 回无关
        # 寒暄（如"看起来你发了一条空消息过来~是不是手滑了"/"内容好像有点空空的"）。
        # 根治判定：无文本且无文件附件且无 media —— 直接 return，不构造 Message、
        # 不路由。push_id 回写在上方 for 循环内已完成，此处 return 不影响它；正常
        # 用户消息必有 text 或 file/media，不会被误拦。
        if not text and not file_attachments and not media_files:
            logger.info(
                "[XiaoyiChannel] 跳过空 message/stream 事件帧（无文本/文件/media）"
                " session_id=%s method=message/stream msg_type=%s task_id=%r raw_msg_id=%r",
                str(message.get("sessionId") or ""),
                str(message.get("msgType")),
                task_id,
                raw_msg_id,
            )
            return
        # ────────────────────────────────────────────────────────────────

        # Only real user requests become active platform tasks. Empty
        # message/stream event frames must not leave behind phantom tasks.
        previous_task_id = self._latest_platform_tasks.get(session_id)
        if (
            session_id in self._team_sessions
            and previous_task_id
            and previous_task_id != task_id
        ):
            await self._retire_superseded_team_task(session_id, previous_task_id)

        self._mark_session_active(session_id, task_id)
        self._remember_active_platform_task(session_id, task_id)
        if session_id in self._team_sessions:
            self._team_tasks.add((session_id, task_id))
        self._session_task_map[task_id] = session_id

        # ==================== clientVariables：工作空间 + 权限模式 ====================
        # 手机端在 data part 的 variables.clientVariables 里携带：
        #   workspace  —— 工作空间路径（WorkspaceQuery 应答条目回传）
        #   permission —— 权限模式（default / full_access，兼容中文名）
        client_workspace = resolve_client_workspace(client_variables.get("workspace"))
        if client_variables.get("workspace") and not client_workspace:
            logger.warning(
                f"XiaoYi: clientVariables.workspace 无效或目录不存在，按未携带处理: "
                f"{client_variables.get('workspace')}"
            )
        permission_profile = normalize_permission_profile(client_variables.get("permission"))
        if client_variables.get("permission") and not permission_profile:
            logger.warning(
                f"XiaoYi: clientVariables.permission 未识别，保持当前权限配置: "
                f"{client_variables.get('permission')}"
            )
        # 权限档位先落 config.yaml + 热重载（best-effort），再路由本消息，保证当轮生效
        if permission_profile:
            await self._apply_permission_profile(permission_profile)
        # 工作空间按请求下发 project_dir/cwd（AgentServer 首回合锁定到会话）；受限档
        # 附带 trusted_dirs 与工作空间约束指令（与桌面端本地对话行为完全一致）
        if client_workspace:
            text = with_workspace_directive(text, client_workspace, permission_profile)
        # =================================================================

        # Add media payload to metadata
        params = {"query": text, "task_id": task_id}
        if client_workspace:
            params["project_dir"] = client_workspace
            params["cwd"] = client_workspace
            trusted_dirs = resolve_trusted_dirs(permission_profile, client_workspace)
            if trusted_dirs:
                params["trusted_dirs"] = trusted_dirs
        if media_payload:
            params["files"] = media_payload
        # Per-request model (same key as Web chat.send); agent last-mile overrides model=
        model_name = extract_model_name(parts)
        if model_name:
            params["model_name"] = model_name
            logger.info("[Xiaoyi] Found modelName: %s", model_name)

        user_message = Message(
            id=message.get("id", ""),
            type="req",
            channel_id=self.channel_id,
            session_id=logical_session,  # 逻辑会话（非 team 兜底）
            user_id=user_id,  # agentId
            bot_id=self.config.agent_id,  # ← _resolve_app_id 兜底拿 app_id
            app_id=self.app_id,
            params=params,
            timestamp=time.time(),
            is_stream=self.config.enable_streaming,
            ok=True,
            req_method=ReqMethod.CHAT_SEND,
            chat_id=session_id,  # 顶层 sessionId（物理回发兜底）
            metadata=metadata,
        )

        # ==================== START TASK TIMEOUT PROTECTION ====================
        # Start 1-hour task timeout timer
        task_timeout_ms = self.config.task_timeout_ms
        logger.info(f"[TASK TIMEOUT] Starting {task_timeout_ms}ms task timeout protection for session {session_id}")

        async def task_timeout_handler():
            """1-hour task timeout handler."""
            try:
                await asyncio.sleep(task_timeout_ms / 1000)
                if (session_id, task_id) not in self._active_tasks:
                    return
                logger.info(f"[TASK TIMEOUT] 1-hour timeout triggered for session {session_id}")
                # Send default message with is_final=true
                for url_key in list(self._ws_connections.keys()):
                    await self._send_text_response(session_id, task_id, "任务还在处理中~", url_key, is_final=True)
                # Mark session as waiting for push state
                self._mark_session_waiting_for_push(session_id, task_id)
            except asyncio.CancelledError:
                pass

        task_key = (session_id, task_id)
        self._clear_task_timeout(session_id, task_id)
        self._task_timeout_tasks[task_key] = asyncio.create_task(task_timeout_handler())

        # Start 60-second periodic timeout for status updates
        async def periodic_timeout_handler():
            """60-second periodic timeout for status updates."""
            try:
                while (session_id, task_id) in self._active_tasks:
                    await asyncio.sleep(60)
                    if (session_id, task_id) not in self._active_tasks:
                        break
                    # Skip if already waiting for push (1-hour timeout triggered)
                    if self._is_session_waiting_for_push(session_id, task_id):
                        break
                    # Send status update
                    await self._send_status_update(task_id, session_id, "任务正在处理中，请稍后~")
            except asyncio.CancelledError:
                pass

        self._clear_session_timeout(session_id, task_id)
        self._session_timeout_tasks[task_key] = asyncio.create_task(periodic_timeout_handler())
        # =================================================================

        handled = False
        if self._on_message_cb is not None:
            result = self._on_message_cb(user_message)
            if inspect.isawaitable(result):
                result = await result
            handled = bool(result)

        if not handled:
            await self.bus.route_user_message(user_message)

        # Start session heartbeat to prevent xiaoyi client timeout
        if not self.config.enable_streaming and session_id:
            await self._start_session_heartbeat(session_id, task_id)

    # ==================== clientVariables 权限档位 + 审批桥接 ====================

    def set_reload_permissions_handler(self, cb: Callable[[], Any] | None) -> None:
        """注入权限热重载回调（gateway 侧持有 AgentServer E2A 连接，由 app_gateway 装配）。"""
        self._reload_permissions_cb = cb

    async def _apply_permission_profile(self, profile: str) -> None:
        """把 clientVariables.permission 指定的权限档位落 config.yaml 并热重载（best-effort）。

        配置无变更时跳过 reload；reload 失败不阻塞消息（配置已写盘，下一轮对话生效）。
        """
        try:
            from jiuwenswarm.common.config import update_permission_profile_in_config

            changed = await asyncio.to_thread(update_permission_profile_in_config, profile)
        except Exception as e:
            logger.warning(f"XiaoYi: 权限档位 {profile} 写配置失败: {e}")
            return
        if not changed:
            return
        cb = self._reload_permissions_cb
        if cb is None:
            logger.info(f"XiaoYi: 权限档位 {profile} 已写盘（未注入热重载回调，AgentServer 下次重载生效）")
            return
        try:
            result = cb()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=15)
            logger.info(f"XiaoYi: 权限档位已切换为 {profile} 并完成热重载")
        except Exception as e:
            logger.warning(f"XiaoYi: 权限热重载失败（配置已写盘，下一轮对话生效）: {e}")

    def _resolve_approval_reply(
        self,
        logical_session: str,
        text: str,
        permission_reply: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """待答复审批 + 用户回复 → (answer, pending)；无待答复或不构成回复返回 None。

        识别顺序：PermissionReply data event（结构化回执）→ 文本约定词表 →
        其余非空文本视为「其他意见」（拒绝 + 原文反馈，对齐 Web 端 custom_input 语义）。
        """
        pending = self._pending_approvals.get(logical_session)
        if not pending:
            return None
        if time.time() - float(pending.get("created_at", 0)) > _APPROVAL_EXPIRE_S:
            self._pending_approvals.pop(logical_session, None)
            logger.info(f"XiaoYi: 待答复审批已过期，按普通消息处理 session={logical_session}")
            return None

        if permission_reply is not None:
            action = str(permission_reply.get("action") or "").strip().lower()
            decision = _APPROVAL_EVENT_ACTIONS.get(action)
            if decision is None:
                logger.warning(f"XiaoYi: 未识别的 PermissionReply.action={action}，不消费待答复审批")
                return None
            feedback = str(permission_reply.get("feedback") or "")
            self._pending_approvals.pop(logical_session, None)
            return self._build_approval_answer(decision, feedback), pending

        normalized = _normalize_approval_text(text)
        if not normalized:
            return None
        if normalized in _APPROVAL_APPROVE_WORDS:
            decision = "approve"
        elif normalized in _APPROVAL_SESSION_WORDS:
            decision = "session"
        elif normalized in _APPROVAL_ALWAYS_WORDS:
            decision = "always"
        elif normalized in _APPROVAL_REJECT_WORDS:
            decision = "reject"
        else:
            decision = "feedback"
        self._pending_approvals.pop(logical_session, None)
        return self._build_approval_answer(decision, text.strip()), pending

    @staticmethod
    def _build_approval_answer(decision: str, feedback: str = "") -> dict[str, Any]:
        """审批决定 → interrupt resume answers 条目（selected_options 取框架识别的标签）。"""
        if decision == "approve":
            return {"selected_options": ["本次允许"], "custom_input": ""}
        if decision == "session":
            return {"selected_options": ["会话内记住"], "custom_input": ""}
        if decision == "always":
            return {"selected_options": ["永久记住"], "custom_input": ""}
        if decision == "reject":
            return {"selected_options": ["拒绝"], "custom_input": feedback}
        # 其他意见：拒绝并把原文作为反馈（对齐 Web 端 custom_input 语义）
        return {"selected_options": [], "custom_input": feedback}

    async def _route_approval_reply(
        self,
        message: dict[str, Any],
        answer: dict[str, Any],
        pending: dict[str, Any],
        *,
        session_id: str,
        logical_session: str,
        agent_id: str,
        device_id: str,
        push_id: str,
    ) -> None:
        """把审批回复转成 chat.send interrupt-resume 路由给 AgentServer（恢复挂起的运行）。

        resume 的 xiaoyi_task_id 沿用原任务 id：续跑流式输出回到手机端同一任务气泡。
        """
        task_id = str(pending.get("task_id") or "")
        params = {
            "query": "",
            "task_id": task_id,
            "session_id": logical_session,  # 对齐 Web 端 resume 载荷（useWebSocket chat.send）
            "request_id": pending.get("request_id") or "",
            "answers": [answer],
            "source": pending.get("source") or "permission_interrupt",
            "mode": "agent",
        }
        metadata = {
            "method": "message/stream",
            "xiaoyi_session_id": session_id,  # 顶层 sessionId（物理回发）
            "xiaoyi_task_id": task_id,  # 续跑流回原始任务气泡
            "xiaoyi_conversation_id": logical_session,  # 逻辑会话
            "xiaoyi_push_id": push_id,
            "xiaoyi_device_id": device_id,
            "im_sender_user_id": agent_id,
        }
        resume_message = Message(
            id=message.get("id", ""),
            type="req",
            channel_id=self.channel_id,
            session_id=logical_session,
            user_id=agent_id,
            bot_id=self.config.agent_id,
            app_id=self.app_id,
            params=params,
            timestamp=time.time(),
            is_stream=self.config.enable_streaming,
            ok=True,
            req_method=ReqMethod.CHAT_SEND,
            chat_id=session_id,
            metadata=metadata,
        )
        handled = False
        if self._on_message_cb is not None:
            result = self._on_message_cb(resume_message)
            if inspect.isawaitable(result):
                result = await result
            handled = bool(result)
        if not handled:
            await self.bus.route_user_message(resume_message)
        # 回执：状态条提示已收到回复（append 到任务流，不关闭气泡）
        for url_key in list(self._ws_connections.keys()):
            await self._send_status_update(task_id, session_id, "已收到您的回复，继续执行…")

    async def _send_ask_user_question_prompt(self, msg: Message, session_id: str, task_id: str) -> None:
        """权限/确认审批提示：渲染问题与选项，并登记待答复审批（等用户下一条消息回复）。

        回复约定：文本（同意 / 会话内允许 / 永久允许 / 拒绝，详见模块级词表）或
        data event {"header":{"namespace":"AgentEvent","name":"PermissionReply"},
        "payload":{"action":"approve|session_allow|always_allow|reject","feedback":?}}。
        """
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        questions = payload.get("questions") or []
        request_id = str(payload.get("request_id") or "").strip()
        source = str(payload.get("source") or "").strip() or "permission_interrupt"
        meta = getattr(msg, "metadata", None) or {}
        logical_session = (
            str(meta.get("xiaoyi_conversation_id") or "").strip()
            or (msg.session_id or "")
            or session_id
        )
        if request_id and logical_session:
            self._pending_approvals[logical_session] = {
                "request_id": request_id,
                "source": source,
                "task_id": task_id,
                "created_at": time.time(),
            }
        else:
            logger.warning("XiaoYi: ask_user_question 缺少 request_id/会话标识，无法登记待答复审批")

        q0: dict[str, Any] = questions[0] if questions and isinstance(questions[0], dict) else {}
        header = str(q0.get("header") or "").strip()
        question = str(q0.get("question") or "").strip()
        options = q0.get("options") or []
        labels = [
            str(opt.get("label") or "").strip()
            for opt in options
            if isinstance(opt, dict) and str(opt.get("label") or "").strip()
        ]
        lines = ["🔐 需要您的确认"]
        if header:
            lines.append(f"【{header}】")
        if question:
            lines.append(question)
        if labels:
            lines.append("可选项：" + " / ".join(labels))
        lines.append(_APPROVAL_REPLY_HINT)
        prompt_text = "\n".join(lines)
        # 完整文本块但非 final：提示可见且不关闭任务气泡，续跑结果继续落到同一任务
        for url_key in list(self._ws_connections.keys()):
            await self._send_text_response(
                session_id,
                task_id,
                prompt_text,
                url_key,
                append=True,
                last_chunk=True,
                is_final=False,
            )

    async def _start_session_heartbeat(self, session_id: str, task_id: str) -> None:
        """启动会话心跳任务，每隔5秒发送空消息直到final消息发出."""
        await self._stop_session_heartbeat(session_id)

        async def heartbeat_loop():
            try:
                while self._running:
                    await asyncio.sleep(5)
                    # Send empty heartbeat message (non-final)
                    for url_key, ws in self._ws_connections.items():
                        if ws:
                            try:
                                await self._send_text_response(
                                    session_id,
                                    task_id,
                                    "",
                                    url_key,
                                    append=True,
                                    is_final=False,
                                )
                            except Exception as e:
                                logger.warning(f"XiaoyiChannel 发送心跳消息失败 ({url_key}): {e}")
            except asyncio.CancelledError:
                logger.info(f"XiaoyiChannel 会话心跳已停止: {session_id}")
            except Exception as e:
                logger.warning(f"XiaoyiChannel 会话心跳异常 ({session_id}): {e}")

        self._session_heartbeat_tasks[session_id] = asyncio.create_task(heartbeat_loop())
        logger.info(f"XiaoyiChannel 会话心跳已启动: {session_id}")

    async def _stop_session_heartbeat(self, session_id: str) -> None:
        """停止会话心跳任务."""
        if session_id in self._session_heartbeat_tasks:
            task = self._session_heartbeat_tasks[session_id]
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._session_heartbeat_tasks.pop(session_id, None)
            logger.info(f"XiaoyiChannel 会话心跳已停止: {session_id}")

    async def _send_status_update(self, task_id: str, session_id: str, message: str) -> None:
        """发送状态更新消息（A2A 格式）."""
        response = {
            "jsonrpc": "2.0",
            "id": f"msg_{int(time.time() * 1000)}",
            "result": {
                "taskId": task_id,
                "kind": "status-update",
                "final": False,
                "status": {
                    "message": {
                        "role": "agent",
                        "parts": [{"kind": "text", "text": message}],
                    },
                    "state": "working",
                },
            },
        }
        # Send to all active connections
        for url_key in list(self._ws_connections.keys()):
            await self._send_agent_response(session_id, task_id, response, url_key)

    async def _send_status_update_with_state(
            self, task_id: str, session_id: str, message: str, state: str, url_key: str
    ) -> None:
        """发送状态更新消息（A2A 格式），支持自定义状态."""
        is_final = state in {"completed", "failed", "canceled"}
        response = {
            "jsonrpc": "2.0",
            "id": f"msg_{int(time.time() * 1000)}",
            "result": {
                "taskId": task_id,
                "kind": "status-update",
                "final": is_final,
                "status": {
                    "message": {
                        "role": "agent",
                        "parts": [{"kind": "text", "text": message}],
                    },
                    "state": state,
                },
            },
        }
        logger.info(
            "[GUI_AGENT_DIAG] phase=XIAOYI_STATUS_RESPONSE_BUILT "
            "session_id=%s task_id=%s connection=%s state=%s "
            "final=%s text=%r response=%r",
            session_id,
            task_id,
            url_key,
            state,
            is_final,
            message,
            response,
        )
        await self._send_agent_response(session_id, task_id, response, url_key)

    def _is_session_active(self, session_id: str, task_id: str | None = None) -> bool:
        """检查会话是否有活跃任务."""
        if task_id:
            session_tasks = {key for key in self._active_tasks if key[0] == session_id}
            if session_tasks:
                return (session_id, task_id) in session_tasks
        return session_id in self._session_active

    def _remember_active_platform_task(self, session_id: str, task_id: str) -> None:
        """Remember the latest inbound A2A task for a stable Xiaoyi session."""
        if session_id and task_id:
            self._latest_platform_tasks[session_id] = task_id

    async def _retire_superseded_team_task(
        self, session_id: str, task_id: str
    ) -> None:
        """Finish one delivery task while retaining its Agent Team session."""
        if self._is_session_active(session_id, task_id):
            for url_key in list(self._ws_connections.keys()):
                await self._send_status_update_with_state(
                    task_id,
                    session_id,
                    "已切换到下一条指令，团队继续处理中~",
                    "completed",
                    url_key,
                )
        self._clear_session_waiting_for_push(session_id, task_id)
        await self._finalize_session(
            session_id,
            task_id,
            preserve_team_session=True,
        )
        self._session_task_map.pop(task_id, None)

    def _mark_session_active(self, session_id: str, task_id: str | None = None) -> None:
        """标记会话为活跃状态."""
        self._session_active.add(session_id)
        if task_id:
            self._active_tasks.add((session_id, task_id))

    def _mark_session_completed(self, session_id: str, task_id: str | None = None) -> None:
        """标记会话已完成."""
        if task_id:
            self._active_tasks.discard((session_id, task_id))
            if any(key[0] == session_id for key in self._active_tasks):
                return
        else:
            self._active_tasks = {
                key for key in self._active_tasks if key[0] != session_id
            }
        self._session_active.discard(session_id)

    def _clear_team_task(self, session_id: str, task_id: str) -> None:
        """Clear one Team A2A round without clearing later tasks."""
        task_key = (session_id, task_id)
        self._team_tasks.discard(task_key)
        self._team_last_leader_finals.pop(task_key, None)
        if not any(key[0] == session_id for key in self._team_tasks):
            self._team_sessions.discard(session_id)

    def _clear_team_session(self, session_id: str) -> None:
        """Clear all Team task markers for a platform session."""
        self._team_tasks = {key for key in self._team_tasks if key[0] != session_id}
        self._team_last_leader_finals = {
            key: value
            for key, value in self._team_last_leader_finals.items()
            if key[0] != session_id
        }
        self._team_sessions.discard(session_id)

    def _is_session_waiting_for_push(self, session_id: str, task_id: str) -> bool:
        """检查会话是否正在等待推送."""
        return self._sessions_waiting_for_push.get(session_id) == task_id

    def _mark_session_waiting_for_push(self, session_id: str, task_id: str) -> None:
        """标记会话正在等待推送."""
        self._sessions_waiting_for_push[session_id] = task_id

    def _clear_session_waiting_for_push(self, session_id: str, task_id: str) -> None:
        """清除会话的推送等待状态."""
        if self._sessions_waiting_for_push.get(session_id) == task_id:
            self._sessions_waiting_for_push.pop(session_id, None)

    async def _finalize_session(
        self,
        session_id: str,
        task_id: str,
        accumulated_text: str | None = None,
        *,
        preserve_team_session: bool = False,
    ) -> None:
        """Finish one A2A task and emit a deferred push exactly once."""
        self._clear_task_timeout(session_id, task_id)
        self._clear_session_timeout(session_id, task_id)
        self._mark_session_completed(session_id, task_id)
        if not self._is_session_active(session_id):
            await self._stop_session_heartbeat(session_id)
            self._clear_task_timeout(session_id)
            self._clear_session_timeout(session_id)

        text = accumulated_text
        task_key = (session_id, task_id)
        if text is None:
            text = self._accumulated_texts.get(task_key, "")
        if self._is_session_waiting_for_push(session_id, task_id):
            if not text:
                text = "团队任务已完成" if session_id in self._team_sessions else "任务已完成"
            summary = text[:30] + "..." if len(text) > 30 else text
            await self._send_push_notification(summary, "后台任务已完成：" + summary)
            self._clear_session_waiting_for_push(session_id, task_id)

        self._accumulated_texts.pop(task_key, None)
        if preserve_team_session:
            self._team_tasks.discard((session_id, task_id))
            self._team_last_leader_finals.pop((session_id, task_id), None)
        else:
            self._clear_team_task(session_id, task_id)

    def _is_session_pending_cleanup(self, session_id: str) -> bool:
        """检查会话是否待清理."""
        return session_id in self._sessions_marked_for_cleanup

    def _mark_session_for_cleanup(self, session_id: str, reason: str = "unknown") -> None:
        """标记会话待清理."""
        self._sessions_marked_for_cleanup[session_id] = {
            "reason": reason,
            "marked_at": time.time(),
        }

    def _force_cleanup_session(self, session_id: str) -> None:
        """强制清理会话."""
        self._sessions_marked_for_cleanup.pop(session_id, None)
        self._session_task_map.pop(session_id, None)
        self._mark_session_completed(session_id)
        self._latest_platform_tasks.pop(session_id, None)
        self._clear_team_session(session_id)
        self._clear_task_timeout(session_id)
        self._clear_session_timeout(session_id)
        self._sessions_waiting_for_push.pop(session_id, None)
        self._accumulated_texts = {
            key: value
            for key, value in self._accumulated_texts.items()
            if key[0] != session_id
        }

    async def _handle_clear_context(self, message: dict[str, Any]) -> None:
        """处理清空上下文请求."""
        session_id = message.get("sessionId", "")
        logger.info(f"XiaoyiChannel 清空上下文: {session_id}")

        # 清理待答复审批（两个会话键都兜底清理）
        self._pending_approvals.pop(session_id, None)
        conversation_id = message.get("conversationId") or message.get("params", {}).get("sessionId", "") or ""
        if conversation_id:
            self._pending_approvals.pop(conversation_id, None)

        # Check if there's an active task for this session
        if self._is_session_active(session_id):
            logger.info(f"[CLEAR] Active task exists for session {session_id}, will continue in background")
            # Mark session for cleanup (delayed cleanup)
            self._mark_session_for_cleanup(session_id, "user_cleared")
        else:
            logger.info(f"[CLEAR] No active task for session {session_id}, clean up immediately")
            self._force_cleanup_session(session_id)

        response = {
            "jsonrpc": "2.0",
            "id": message.get("id", ""),
            "result": {"status": {"state": "cleared"}},
        }
        # Send response to all active connections
        for url_key in list(self._ws_connections.keys()):
            await self._send_agent_response(session_id, session_id, response, url_key)

    async def _handle_tasks_cancel(self, message: dict[str, Any]) -> None:
        """处理取消任务请求."""
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        session_id = params.get("sessionId") or message.get("sessionId", "")
        task_id = params.get("id") or message.get("taskId", "")
        logger.info(f"XiaoyiChannel 取消任务: {session_id} {task_id}")
        # 取消即放弃待答复审批（两个会话键都兜底清理）
        self._pending_approvals.pop(session_id, None)
        cancel_conversation_id = message.get("conversationId") or message.get("params", {}).get("sessionId", "") or ""
        if cancel_conversation_id:
            self._pending_approvals.pop(cancel_conversation_id, None)
        response = {
            "jsonrpc": "2.0",
            "id": message.get("id", ""),
            "result": {"id": message.get("id", ""), "status": {"state": "canceled"}},
        }
        # Send response to all active connections
        for url_key in list(self._ws_connections.keys()):
            await self._send_agent_response(session_id, task_id, response, url_key)

        # 清理超时任务和推送状态
        self._clear_session_waiting_for_push(session_id, task_id)
        self._clear_task_timeout(session_id, task_id)
        self._clear_session_timeout(session_id, task_id)
        self._mark_session_completed(session_id, task_id)
        if not self._is_session_active(session_id):
            if session_id:
                await self._stop_session_heartbeat(session_id)
            self._clear_task_timeout(session_id)
            self._clear_session_timeout(session_id)
        # Cancelling one platform task must not stop a long-running Team
        # runtime. A later user turn will replace the latest task mapping.
        self._team_tasks.discard((session_id, task_id))
        self._team_last_leader_finals.pop((session_id, task_id), None)

    async def _send_text_response(
            self,
            session_id: str,
            task_id: str,
            text: str,
            url_key: str,
            *,
            append: bool = False,
            last_chunk: bool = True,
            is_final: bool = True,
            kind: str | None = None,
    ) -> None:
        """发送文本响应（A2A 格式）到指定通道.

        ``kind`` 显式指定 part 类型（``"text"`` 或 ``"reasoningText"``）。
        默认 ``None`` 时按 ``last_chunk`` 推导（``True``→text，``False``→reasoningText），
        向后兼容旧调用点。新调用点应按 event_type 显式传 ``kind``，避免
        ``last_chunk`` 与 part kind 的语义耦合。
        """
        if kind is None:
            kind = "text" if last_chunk else "reasoningText"
        if kind == "reasoningText":
            data = {"kind": "reasoningText", "reasoningText": text}
        else:
            data = {"kind": "text", "text": text}
        response = {
            "jsonrpc": "2.0",
            "id": f"msg_{int(time.time() * 1000)}",
            "result": {
                "taskId": task_id,
                "kind": "artifact-update",
                "append": append,
                "lastChunk": last_chunk,
                "final": is_final,
                "artifact": {
                    "artifactId": f"artifact_{int(time.time() * 1000)}",
                    "parts": [data],
                },
            },
        }
        logger.info(
            "[GUI_AGENT_DIAG] phase=XIAOYI_TEXT_RESPONSE_BUILT "
            "session_id=%s task_id=%s connection=%s append=%s "
            "last_chunk=%s final=%s text=%r response=%r",
            session_id,
            task_id,
            url_key,
            append,
            last_chunk,
            is_final,
            text,
            response,
        )
        await self._send_agent_response(session_id, task_id, response, url_key)

    async def _send_empty_final_ack(
            self, session_id: str, task_id: str, message_id: str
    ) -> None:
        """发送空正文 artifact-update 末帧作为事件 ACK.

        1:1 对应 xy_channel formatter.ts sendA2AResponse(text:"", append:false,
        final:true). 用于 SelfEvolution set 等只需确认"已处理、流结束"、无需
        实质内容的事件回复:parts 为空 text part,final=True,id 用入站 message_id.
        遍历所有活跃连接;ACK 失败只 warning 不 raise.
        """
        response = {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "taskId": task_id,
                "kind": "artifact-update",
                "append": False,
                "lastChunk": True,
                "final": True,
                "artifact": {
                    "artifactId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": ""}],
                },
            },
        }
        logger.info(
            "[GUI_AGENT_DIAG] phase=XIAOYI_EMPTY_FINAL_ACK "
            "session_id=%s task_id=%s message_id=%s",
            session_id,
            task_id,
            message_id,
        )
        for url_key, ws in self._ws_connections.items():
            if ws:
                try:
                    await self._send_agent_response(
                        session_id, task_id, response, url_key
                    )
                except Exception as e:
                    logger.warning(
                        "[XiaoyiChannel] _send_empty_final_ack 失败 (%s): %s",
                        url_key,
                        e,
                    )

    async def _send_agent_response(self, session_id: str, task_id: str, response: dict[str, Any], url_key: str) -> None:
        """发送 agent_response 包装的消息（A2A 格式）到指定通道."""
        wrapper = {
            "msgType": "agent_response",
            "agentId": self.config.agent_id,
            "sessionId": session_id,
            "taskId": task_id,
            "msgDetail": json.dumps(response),
        }
        result = response.get("result")
        is_status_response = (
            isinstance(result, dict) and result.get("kind") == "status-update"
        )
        artifact = result.get("artifact") if isinstance(result, dict) else None
        parts = artifact.get("parts") if isinstance(artifact, dict) else None
        is_text_response = bool(
            isinstance(parts, list)
            and any(
                isinstance(part, dict)
                and part.get("kind") in ("text", "reasoningText")
                for part in parts
            )
        )
        try:
            if is_text_response:
                logger.info(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_WS_TEXT_SEND_BEGIN "
                    "session_id=%s task_id=%s connection=%s wrapper=%r",
                    session_id,
                    task_id,
                    url_key,
                    wrapper,
                )
            elif is_status_response:
                logger.info(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_WS_STATUS_SEND_BEGIN "
                    "session_id=%s task_id=%s connection=%s wrapper=%r",
                    session_id,
                    task_id,
                    url_key,
                    wrapper,
                )
            await self._safe_ws_send(url_key, wrapper)
            if is_text_response:
                logger.info(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_WS_TEXT_SEND_DONE "
                    "session_id=%s task_id=%s connection=%s",
                    session_id,
                    task_id,
                    url_key,
                )
            elif is_status_response:
                logger.info(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_WS_STATUS_SEND_DONE "
                    "session_id=%s task_id=%s connection=%s",
                    session_id,
                    task_id,
                    url_key,
                )
        except Exception as e:
            if is_text_response:
                logger.exception(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_WS_TEXT_SEND_FAILED "
                    "session_id=%s task_id=%s connection=%s error_type=%s",
                    session_id,
                    task_id,
                    url_key,
                    type(e).__name__,
                )
            elif is_status_response:
                logger.exception(
                    "[GUI_AGENT_DIAG] phase=XIAOYI_WS_STATUS_SEND_FAILED "
                    "session_id=%s task_id=%s connection=%s error_type=%s",
                    session_id,
                    task_id,
                    url_key,
                    type(e).__name__,
                )
            logger.warning(f"XiaoyiChannel 发送响应失败 ({url_key}): {e}")

    async def _send_file_response_base64(self, session_id: str, task_id: str, file_info: dict, url_key: str) -> None:
        """发送文件响应（Base64 格式）到指定通道."""
        try:
            file_path = file_info.get("fullPath", "")
            if not file_path or not os.path.exists(file_path):
                logger.error(f"send file failed, caused by file not exist. file path: {file_path}")
                return
            file_name = os.path.basename(file_info.get("fileName", ""))
            file_name = file_name if file_name else os.path.basename(file_path)

            # Check file size (limit to 20MB for Base64)
            base_url = self.file_upload_config.get("baseUrl")
            api_key = self.file_upload_config.get("apiKey")
            uid = self.file_upload_config.get("uid")

            # api_key 可选：桌面客户端经本地代理注入 credential 鉴权时无需配置
            if not all([base_url, uid]):
                logger.error("XiaoyiChannel OSMS配置不完整，无法上传大文件")
                return

            object_id = ""
            mime_type = FILE_TYPE_TO_MIME_TYPE.get(file_name.split(".")[-1], "text/plain")
            async with XYFileUploadService(base_url, api_key, uid) as upload_service:
                object_id = await upload_service.upload_file(file_path)
                logger.info(f"file upload success: {object_id}")
                if object_id:
                    # Send file reference response
                    payload = {
                        "jsonrpc": "2.0",
                        "id": task_id,
                        "result": {
                            "kind": "artifact-update",
                            "append": True,
                            "lastChunk": False,
                            "isFinal": False,
                            "artifact": {
                                "artifactId": task_id,
                                "parts": [
                                    {
                                        "kind": "file",
                                        "file": {
                                            "fileId": object_id,
                                            "name": file_name,
                                            "mimeType": mime_type
                                        }
                                    }
                                ],
                            },
                        },
                        "error": {
                            "code": 0
                        }
                    }
                    response = {
                        "msgType": "agent_response",
                        "agentId": self.config.agent_id,
                        "sessionId": session_id,
                        "taskId": task_id,
                        "msgDetail": json.dumps(payload)
                    }
                    await self._safe_ws_send(url_key, response)
            return object_id
        except Exception as e:
            logger.error(f"XiaoyiChannel 发送文件响应失败: {e}")

    async def _send_file_response(self, session_id: str, task_id: str, file_info: dict, url_key: str) -> None:
        """发送文件响应到指定通道."""
        try:
            # If file is available locally, send as Base64
            if file_info.get("fullPath"):
                await self._send_file_response_base64(session_id, task_id, file_info, url_key)
                return
        except Exception as e:
            logger.error(f"XiaoyiChannel 发送文件响应失败: {e}")

    async def _send_html_card_response(
        self,
        session_id: str,
        task_id: str,
        message_id: str,
        cards_info: list[dict[str, Any]],
        url_key: str,
    ) -> None:
        """发送 HTML H5 卡片（对齐 openclaw sendCard / clawH5）."""
        try:
            rpc_id = message_id or task_id
            payload = {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "taskId": task_id,
                    "kind": "artifact-update",
                    "append": False,
                    "lastChunk": True,
                    "final": False,
                    "artifact": {
                        "artifactId": str(uuid.uuid4()),
                        "parts": [
                            {
                                "kind": "data",
                                "data": {"cardsInfo": cards_info},
                            }
                        ],
                    },
                },
            }
            response = {
                "msgType": "agent_response",
                "agentId": self.config.agent_id,
                "sessionId": session_id,
                "taskId": task_id,
                "msgDetail": json.dumps(payload, ensure_ascii=False),
            }
            logger.info(
                "[A2A_CARD] Sending html card session_id=%s task_id=%s cards=%s",
                session_id,
                task_id,
                len(cards_info),
            )
            await self._safe_ws_send(url_key, response)
        except Exception as e:
            logger.error("XiaoyiChannel 发送 HTML 卡片失败: %s", e)
            raise

    async def _safe_ws_send(self, url_key: str, payload: dict[str, Any]) -> None:
        ws = self._ws_connections.get(url_key)
        if not ws:
            raise RuntimeError(f"ws connection not available: {url_key}")
        lock = self._send_locks.get(url_key)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[url_key] = lock
        async with lock:
            if isinstance(ws, PipeStream):
                # 命名管道形态：帧负载即 JSON 对象（桌面侧按帧 JSON.parse 后透传）
                await ws.send_frame(payload)
            else:
                data = json.dumps(payload, ensure_ascii=False)
                await ws.send(data)

    async def send_agent_response_to_all(
            self, session_id: str, task_id: str, response: dict[str, Any]
    ) -> None:
        """向所有活跃 WebSocket 连接发送预构建的 agent_response 消息.

        Args:
            session_id: 会话 ID
            task_id: 任务 ID
            response: 已包含 msgType、agentId 等字段的完整消息体
        """
        sent = False
        for url_key in list(self._ws_connections.keys()):
            try:
                await self._safe_ws_send(url_key, response)
                sent = True
            except Exception as e:
                logger.warning(
                    "XiaoyiChannel send_agent_response_to_all 失败 (%s): %s",
                    url_key,
                    e,
                )
        if not sent:
            raise RuntimeError("发送文件消息失败，WebSocket 未连接")

    def _clear_task_timeout(self, session_id: str, task_id: str | None = None) -> None:
        """清除任务超时任务."""
        keys = [
            key
            for key in self._task_timeout_tasks
            if key[0] == session_id and (task_id is None or key[1] == task_id)
        ]
        for key in keys:
            task = self._task_timeout_tasks.pop(key, None)
            if task and not task.done():
                task.cancel()

    def _clear_session_timeout(self, session_id: str, task_id: str | None = None) -> None:
        """清除会话超时任务."""
        keys = [
            key
            for key in self._session_timeout_tasks
            if key[0] == session_id and (task_id is None or key[1] == task_id)
        ]
        for key in keys:
            task = self._session_timeout_tasks.pop(key, None)
            if task and not task.done():
                task.cancel()

    async def _send_push_notification(self, text: str, push_text: str) -> bool:
        """发送推送通知."""
        if not (self.config.api_id):
            logger.info("[PUSH] Push not configured, skipping")
            return False

        try:
            push_config = PushConfig(
                mode=self.config.mode,
                api_id=self.config.api_id,
                push_id=self.config.push_id,
                push_url=self.config.push_url,
                ak=self.config.ak,
                sk=self.config.sk,
                uid=self.config.uid,
                api_key=self.config.api_key
            )
            push_service = XiaoYiPushService(push_config)
            result = await push_service.send_push(text, push_text)
            logger.info(f"[PUSH] Push notification sent: {result}")
            return result
        except Exception as e:
            logger.error(f"[PUSH] Error sending push: {e}")
            return False

    async def send_xiaoyi_phone_tools_command(
            self,
            session_id: str,
            task_id: str,
            message_id: str,
            command: dict[str, Any],
            final: bool = False,
    ) -> bool:
        """发送 Command 指令到手机端（A2A artifact-update 格式）.

        Args:
            session_id: 会话 ID
            task_id: 任务 ID
            message_id: 消息 ID（用于 JSON-RPC id）
            command: Command 数据结构，包含 header 和 payload

        Returns:
            是否发送成功
        """
        response = {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "taskId": task_id,
                "kind": "artifact-update",
                "append": False,
                "lastChunk": True,
                "final": final,
                "artifact": {
                    "artifactId": str(uuid.uuid4()),
                    "parts": [{"kind": "data", "data": {"commands": [command]}}],
                },
            },
        }

        # OutboundWebSocketMessage：msgType/agentId/sessionId/taskId/msgDetail（msgDetail 为 JSON 字符串）
        wrapper = {
            "msgType": "agent_response",
            "agentId": self.config.agent_id,
            "sessionId": session_id,
            "taskId": task_id,
            "msgDetail": json.dumps(response, ensure_ascii=False),
        }

        # 发送到所有活跃连接
        privilege_intent = (
            command.get("payload", {})
            .get("executeParam", {})
            .get("intentName")
        )
        if privilege_intent == "CheckPlugInPrivilege":
            logger.info(
                "[CRON_DEVICE] phase=PRIVILEGE_WIRE_SEND "
                "agent_id=%s session_id=%s task_id=%s message_id=%s "
                "wrapper=%r",
                self.config.agent_id,
                session_id,
                task_id,
                message_id,
                wrapper,
            )

        sent = False
        is_gui_command = (
            command.get("header", {}).get("namespace") == "ClawAgent"
            and command.get("header", {}).get("name")
            == "InvokeJarvisGUIAgentRequest"
        )
        if is_gui_command:
            logger.info(
                "[GUI_RPC_TRACE] phase=CHANNEL_SEND_ENTER session_id=%s "
                "task_id=%s message_id=%s active_connection_count=%s",
                session_id,
                task_id,
                message_id,
                sum(bool(ws) for ws in self._ws_connections.values()),
            )
        for url_key, ws in self._ws_connections.items():
            if ws:
                try:
                    if is_gui_command:
                        logger.info(
                            "[GUI_RPC_TRACE] phase=CHANNEL_WS_SEND_BEGIN "
                            "session_id=%s task_id=%s message_id=%s "
                            "connection=%s",
                            session_id,
                            task_id,
                            message_id,
                            url_key,
                        )
                    await self._safe_ws_send(url_key, wrapper)
                    if is_gui_command:
                        logger.info(
                            "[GUI_RPC_TRACE] phase=CHANNEL_WS_SEND_DONE "
                            "session_id=%s task_id=%s message_id=%s "
                            "connection=%s success=true",
                            session_id,
                            task_id,
                            message_id,
                            url_key,
                        )
                    intent_name = command.get("payload", {}).get("executeParam", {}).get("intentName") or command.get(
                        "header", {}
                    ).get("name", "unknown")
                    logger.info(f"XiaoyiChannel 发送 command 成功 ({url_key}):intent={intent_name}")
                    sent = True
                except Exception as e:
                    if is_gui_command:
                        logger.warning(
                            "[GUI_RPC_TRACE] phase=CHANNEL_WS_SEND_DONE "
                            "session_id=%s task_id=%s message_id=%s "
                            "connection=%s success=false error_type=%s",
                            session_id,
                            task_id,
                            message_id,
                            url_key,
                            type(e).__name__,
                        )
                    logger.warning(f"XiaoyiChannel 发送 command 失败 ({url_key}): {e}")

        if is_gui_command:
            logger.info(
                "[GUI_RPC_TRACE] phase=CHANNEL_SEND_EXIT session_id=%s "
                "task_id=%s message_id=%s sent=%s",
                session_id,
                task_id,
                message_id,
                sent,
            )
        return sent

    async def send_login_token_artifact(
        self,
        *,
        session_id: str,
        task_id: str,
        message_id: str,
        client_id: str,
        skill_name: str,
        final: bool = False,
    ) -> bool:
        """下发 getLoginToken artifact 给端侧小艺 App（huawei_id_tool 方案1 wire）.

        1:1 复刻 xy_channel login-token-tool.ts 第 53-90 行：artifact-update 的
        parts 为单个自定义 part {kind: "getLoginToken", clientId, skillName}，
        append=False / lastChunk=True / final=False。端侧 App 收到该 part 后弹
        授权 UI，授权完成经 LoginTokenEvent.ClawAutoLogin 回写
        /home/sandbox/.openclaw/.xiaoyitoken.json（由 login-token-handler.ts 写入），
        工具侧轮询该文件取结果。

        与 send_xiaoyi_phone_tools_command 的差异：后者 parts 为
        [{kind:"data", data:{commands:[...]}}]（设备指令）；本方法 parts 为
        自定义 getLoginToken part（授权请求），不走 commands 包装，与 TS 原样一致。

        Args:
            session_id: 小艺会话 ID
            task_id: 小艺任务 ID
            message_id: JSON-RPC id（端侧用它关联授权回调）
            client_id: 账号服务唯一标识
            skill_name: skill 名称
            final: 是否为末帧（默认 False，授权请求不是末帧）

        Returns:
            是否至少成功发送到一个活跃连接
        """
        logger.info(
            "[SEND_LOGIN_TOKEN_ARTIFACT_ENTER] 方法被调用 client_id=%s skill=%s "
            "session=%s task=%s msg_id=%s",
            client_id, skill_name, session_id, task_id, message_id,
        )
        response = {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "taskId": task_id,
                "kind": "artifact-update",
                "append": False,
                "lastChunk": True,
                "final": final,
                "artifact": {
                    "artifactId": str(uuid.uuid4()),
                    "parts": [
                        {
                            "kind": "getLoginToken",
                            "clientId": client_id,
                            "skillName": skill_name,
                        }
                    ],
                },
            },
        }
        wrapper = {
            "msgType": "agent_response",
            "agentId": self.config.agent_id,
            "sessionId": session_id,
            "taskId": task_id,
            "msgDetail": json.dumps(response, ensure_ascii=False),
        }
        logger.info(
            "[LOGIN_TOKEN] phase=CHANNEL_ARTIFACT_SEND_BEGIN "
            "session_id=%s task_id=%s message_id=%s client_id=%s skill_name=%s "
            "connection_count=%s",
            session_id,
            task_id,
            message_id,
            client_id,
            skill_name,
            sum(bool(ws) for ws in self._ws_connections.values()),
        )
        sent = False
        for url_key, ws in self._ws_connections.items():
            if ws:
                try:
                    await self._safe_ws_send(url_key, wrapper)
                    logger.info(
                        "[LOGIN_TOKEN] phase=CHANNEL_ARTIFACT_SEND_DONE "
                        "session_id=%s connection=%s success=true",
                        session_id,
                        url_key,
                    )
                    sent = True
                except Exception as exc:
                    logger.warning(
                        "[LOGIN_TOKEN] phase=CHANNEL_ARTIFACT_SEND_DONE "
                        "session_id=%s connection=%s success=false error_type=%s",
                        session_id,
                        url_key,
                        type(exc).__name__,
                    )
        logger.info(
            "[LOGIN_TOKEN] phase=CHANNEL_ARTIFACT_SEND_EXIT "
            "session_id=%s message_id=%s sent=%s",
            session_id,
            message_id,
            sent,
        )
        return sent

    async def execute_phone_tool_command(
        self,
        request: DeviceCommandRequest,
    ) -> dict[str, Any]:
        context = request.context
        session_id = (
            context.xiaoyi_root_session_id
            or context.xiaoyi_params_session_id
            or context.jiuwen_session_id
            or ""
        )
        if not session_id:
            raise RuntimeError("Xiaoyi session_id is missing")
        task_id = context.xiaoyi_task_id or session_id
        message_id = (
            context.xiaoyi_rpc_id
            if request.intent_name == "CheckPlugInPrivilege"
            and context.xiaoyi_rpc_id
            else f"cmd_{request.operation_id}"
        )
        if request.intent_name == "CheckPlugInPrivilege":
            lock = self._privilege_check_lock
        else:
            lock_key = (session_id, request.intent_name)
            lock = self._device_command_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            return await self._execute_phone_tool_command_locked(
                request=request,
                session_id=session_id,
                task_id=task_id,
                message_id=message_id,
            )

    async def _execute_phone_tool_command_locked(
        self,
        *,
        request: DeviceCommandRequest,
        session_id: str,
        task_id: str,
        message_id: str,
    ) -> dict[str, Any]:
        result_event = asyncio.Event()
        result_data: dict[str, Any] | None = None
        error: Exception | None = None
        started_at = time.monotonic()

        def on_data_event(event: DataEvent) -> None:
            nonlocal result_data, error
            if event.intent_name != request.intent_name:
                return
            if request.intent_name == "CheckPlugInPrivilege":
                result_data = {} if event.outputs is None else event.outputs
            elif _is_data_event_status_success(event.status):
                result_data = {} if event.outputs is None else event.outputs
            else:
                error = RuntimeError(f"Device execution failed: {event.status}")
            result_event.set()

        self.register_data_event_handler(request.intent_name, on_data_event)
        try:
            logger.info(
                "[XiaoyiChannel] device command send: rpc_id=%s operation_id=%s source_request_id=%s "
                "session_id=%s task_id=%s intent_name=%s pid=%s",
                request.rpc_id,
                request.operation_id,
                request.context.source_request_id,
                session_id,
                task_id,
                request.intent_name,
                os.getpid(),
            )
            sent = await self.send_xiaoyi_phone_tools_command(
                session_id=session_id,
                task_id=task_id,
                message_id=message_id,
                command=request.command,
            )
            if not sent:
                raise RuntimeError("Xiaoyi WebSocket is not connected")

            await asyncio.wait_for(
                result_event.wait(),
                timeout=request.timeout_seconds,
            )
            if error is not None:
                raise error
            return {} if result_data is None else result_data
        finally:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            logger.info(
                "[XiaoyiChannel] device command finished: rpc_id=%s operation_id=%s source_request_id=%s "
                "session_id=%s task_id=%s intent_name=%s pid=%s elapsed_ms=%s",
                request.rpc_id,
                request.operation_id,
                request.context.source_request_id,
                session_id,
                task_id,
                request.intent_name,
                os.getpid(),
                elapsed_ms,
            )
            self.unregister_data_event_handler(request.intent_name, on_data_event)

    async def execute_scheduled_phone_tool_command(
        self,
        request: DeviceCommandRequest,
    ) -> dict[str, Any]:
        scheduled_device = request.context.metadata.get("scheduled_device")
        if not isinstance(scheduled_device, dict):
            logger.warning(
                "[CRON_DEVICE] phase=SCHEDULED_INTENT_REJECTED rpc_id=%s "
                "operation_id=%s intent_name=%s reason=missing_scheduled_device",
                request.rpc_id,
                request.operation_id,
                request.intent_name,
            )
            raise RuntimeError(
                "Scheduled Xiaoyi device permissions are missing; "
                "recreate the cron job"
            )
        push_id = str(scheduled_device.get("push_id") or "").strip()
        required_intents = scheduled_device.get("required_intents")
        allowed_intents = {
            str(item or "").strip()
            for item in required_intents
            if str(item or "").strip()
        } if isinstance(required_intents, list) else set()
        if not push_id:
            raise RuntimeError("Scheduled Xiaoyi push_id is missing")
        if not allowed_intents:
            logger.warning(
                "[CRON_DEVICE] phase=SCHEDULED_INTENT_REJECTED rpc_id=%s "
                "operation_id=%s intent_name=%s reason=empty_required_intents",
                request.rpc_id,
                request.operation_id,
                request.intent_name,
            )
            raise RuntimeError(
                "Scheduled Xiaoyi device intents are missing; "
                "recreate the cron job"
            )
        if request.intent_name not in allowed_intents:
            logger.warning(
                "[CRON_DEVICE] phase=SCHEDULED_INTENT_REJECTED rpc_id=%s "
                "operation_id=%s intent_name=%s reason=intent_not_allowed",
                request.rpc_id,
                request.operation_id,
                request.intent_name,
            )
            raise RuntimeError(
                f"Intent {request.intent_name} is not allowed by the scheduled device context"
            )

        async with self._scheduled_device_command_lock:
            result_event = asyncio.Event()
            result_data: dict[str, Any] | None = None
            error: Exception | None = None
            started_at = time.monotonic()

            def on_data_event(event: DataEvent) -> None:
                nonlocal result_data, error
                if event.intent_name != request.intent_name:
                    return
                if _is_data_event_status_success(event.status):
                    result_data = {} if event.outputs is None else event.outputs
                else:
                    error = RuntimeError(
                        f"Device execution failed: {event.status}"
                    )
                result_event.set()

            self.register_data_event_handler(request.intent_name, on_data_event)
            try:
                push_config = PushConfig(
                    mode=self.config.mode,
                    api_id=self.config.api_id,
                    push_id=push_id,
                    push_url=self.config.push_url,
                    ak=self.config.ak,
                    sk=self.config.sk,
                    uid=self.config.uid,
                    api_key=self.config.api_key,
                )
                push_service = XiaoYiPushService(push_config)
                logger.info(
                    "[CRON_DEVICE] phase=DIRECTIVE_SEND_BEGIN rpc_id=%s "
                    "operation_id=%s intent_name=%s",
                    request.rpc_id,
                    request.operation_id,
                    request.intent_name,
                )
                sent = await push_service.send_push_with_directives(
                    push_id=push_id,
                    session_id=str(uuid.uuid4()),
                    directives=[request.command],
                )
                if not sent:
                    raise RuntimeError("Failed to send Xiaoyi directive push")
                await asyncio.wait_for(
                    result_event.wait(),
                    timeout=request.timeout_seconds,
                )
                if error is not None:
                    raise error
                return {} if result_data is None else result_data
            finally:
                self.unregister_data_event_handler(
                    request.intent_name,
                    on_data_event,
                )
                logger.info(
                    "[CRON_DEVICE] phase=DIRECTIVE_SEND_DONE rpc_id=%s "
                    "operation_id=%s intent_name=%s elapsed_ms=%s",
                    request.rpc_id,
                    request.operation_id,
                    request.intent_name,
                    int((time.monotonic() - started_at) * 1000),
                )

    def _get_a2a_parts(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """从直连或 Wrapped A2A 消息中取出 message.parts."""
        msg_type = message.get("msgType")
        if msg_type == "data":
            try:
                a2a_request = json.loads(message.get("msgDetail", "{}"))
            except json.JSONDecodeError:
                return []
            params = a2a_request.get("params", {})
        else:
            params = message.get("params", {})
        msg = params.get("message", {})
        parts = msg.get("parts", [])
        return parts if isinstance(parts, list) else []

    async def _dispatch_gui_agent_events(self, message: dict[str, Any]) -> None:
        """分发 InvokeJarvisGUIAgentResponse（data.events 内）.

        各 handler 独立 try/except，单个工具回调异常不影响同帧其他 handler 及后续 data-event。
        """
        if len(self._gui_agent_handlers) > 1:
            logger.warning(
                "XiaoyiChannel GUI handler 数量=%s，可能存在并发未串行化",
                len(self._gui_agent_handlers),
            )
        for part in self._get_a2a_parts(message):
            if part.get("kind") != "data":
                continue
            events = part.get("data", {}).get("events", [])
            if not isinstance(events, list):
                continue
            for item in events:
                if (
                        item.get("header", {}).get("namespace") == "ClawAgent"
                        and item.get("header", {}).get("name") == "InvokeJarvisGUIAgentResponse"
                ):
                    dispatch_item = dict(item)
                    dispatch_item["_xiaoyi_session_id"] = _gui_response_session_id(message)
                    payload = item.get("payload")
                    payload = payload if isinstance(payload, dict) else {}
                    stream_info = payload.get("streamInfo")
                    stream_info = stream_info if isinstance(stream_info, dict) else {}
                    content = stream_info.get("streamContent")
                    logger.info(
                        "[GUI_RPC_TRACE] phase=CHANNEL_GUI_FRAME_DISPATCH "
                        "session_id=%s interaction_id=%s is_final=%s "
                        "content_len=%s handler_count=%s",
                        dispatch_item["_xiaoyi_session_id"],
                        str(payload.get("interactionId") or ""),
                        payload.get("isFinal"),
                        len(str(content)) if content is not None else 0,
                        len(self._gui_agent_handlers),
                    )
                    logger.info(
                        "[GUI_AGENT_DIAG] phase=XIAOYI_GUI_EVENT_DISPATCH "
                        "session_id=%s interaction_id=%s raw_is_final=%r "
                        "stream_content=%r payload=%r event=%r",
                        dispatch_item["_xiaoyi_session_id"],
                        str(payload.get("interactionId") or ""),
                        payload.get("isFinal"),
                        content,
                        payload,
                        item,
                    )
                    for h in list(self._gui_agent_handlers):
                        try:
                            if asyncio.iscoroutinefunction(h):
                                await h(dispatch_item)
                            else:
                                h(dispatch_item)
                        except Exception as e:
                            logger.warning(
                                "XiaoyiChannel GUI agent 处理器异常（已隔离）: %s",
                                e,
                                exc_info=True,
                            )

    def register_gui_agent_handler(self, handler: Callable[[dict[str, Any]], Any]) -> None:
        """注册 InvokeJarvisGUIAgentResponse 处理器."""
        if handler not in self._gui_agent_handlers:
            self._gui_agent_handlers.append(handler)
            logger.info("XiaoyiChannel 注册 GUI agent 处理器")

    def unregister_gui_agent_handler(self, handler: Callable[[dict[str, Any]], Any]) -> None:
        """注销 GUI agent 处理器."""
        try:
            self._gui_agent_handlers.remove(handler)
            logger.info("XiaoyiChannel 注销 GUI agent 处理器")
        except ValueError:
            pass

    def register_data_event_handler(
            self, intent_name: str, handler: Callable[[DataEvent], Any]
    ) -> None:
        """注册 data-event 处理器.

        Args:
            intent_name: 要监听的 intent 名称（如 "GetCurrentLocation"）
            handler: 处理函数，接收 DataEvent 参数
        """
        if intent_name not in self._data_event_handlers:
            self._data_event_handlers[intent_name] = []
        if handler not in self._data_event_handlers[intent_name]:
            self._data_event_handlers[intent_name].append(handler)
            logger.info(f"XiaoyiChannel 注册 data-event 处理器: {intent_name}")

    def unregister_data_event_handler(
            self, intent_name: str, handler: Callable[[DataEvent], Any]
    ) -> None:
        """注销 data-event 处理器.

        Args:
            intent_name: intent 名称
            handler: 要移除的处理函数
        """
        if intent_name in self._data_event_handlers:
            try:
                self._data_event_handlers[intent_name].remove(handler)
                logger.info(f"XiaoyiChannel 注销 data-event 处理器: {intent_name}")
            except ValueError:
                pass

    async def _handle_data_event(self, event: DataEvent) -> None:
        """分发 data-event 到注册的处理器."""
        logger.info(f"[XiaoyiChannel] 分发 data-event: intent={event.intent_name}, status={event.status}")
        logger.info(f"[XiaoyiChannel] 已注册处理器: {list(self._data_event_handlers.keys())}")

        handlers = self._data_event_handlers.get(event.intent_name, [])
        if not handlers:
            logger.warning(f"[XiaoyiChannel] 无处理器处理 data-event: {event.intent_name}")
            return

        logger.info(f"[XiaoyiChannel] 找到 {len(handlers)} 个处理器 for {event.intent_name}")

        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                logger.warning(f"XiaoyiChannel data-event 处理器异常 ({event.intent_name}): {e}")

    def _extract_data_event(self, message: dict[str, Any]) -> DataEvent | None:
        """从 A2A 消息中提取 data-event（如果是 data-only 消息）.

        支持三种消息格式：
        1. Direct A2A format: 直接包含 params.message.parts
        2. Wrapped format (msgType="data"): A2A 内容在 msgDetail 中
        3. UploadExeResult 格式: header.name="UploadExeResult" + payload.intentName + payload.outputs

        Args:
            message: 解析后的 A2A 消息

        Returns:
            DataEvent 或 None（如果不是 data-only 消息）
        """
        # Wrapped format：msgType="data"，msgDetail 为嵌套的 A2A JSON-RPC 字符串
        msg_type = message.get("msgType")
        method = message.get("method")
        if msg_type == "data":
            try:
                # 从 msgDetail 解析 A2A JSON-RPC 请求
                a2a_request = json.loads(message.get("msgDetail", "{}"))
                params = a2a_request.get("params", {})
                msg = params.get("message", {})
                parts = msg.get("parts", [])
                session_id = message.get("sessionId", "")
            except json.JSONDecodeError as e:
                logger.info(
                    f"[XiaoyiChannel] _extract_data_event: msgDetail JSON 解析失败: {e}"
                )
                return None
            except KeyError as e:
                logger.info(
                    f"[XiaoyiChannel] _extract_data_event: Wrapped A2A 缺少字段: {e}"
                )
                return None
        else:
            # Direct A2A format
            params = message.get("params", {})
            msg = params.get("message", {})
            parts = msg.get("parts", [])
            session_id = message.get("sessionId", "")

        if not parts:
            return None

        # 检查是否所有 parts 都是 data 类型
        data_parts = [p for p in parts if p.get("kind") == "data"]
        if not data_parts or len(data_parts) != len(parts):
            return None

        # 提取 data 内容
        for part in data_parts:
            data = part.get("data", {})
            events = data.get("events", [])
            if not isinstance(events, list):
                continue

            for event in events:
                intent_name = ""
                outputs = {}
                status = "success"  # 未显式给出时与直接格式默认一致

                # 格式 1: 直接格式 (events[].intentName)
                if event.get("intentName"):
                    intent_name = event.get("intentName", "")
                    outputs = event.get("outputs", {})
                    status = event.get("status", "success")

                # 格式 2: UploadExeResult 包装格式 (header.name + payload)
                elif event.get("header", {}).get("name") == "UploadExeResult":
                    payload = event.get("payload", {})
                    intent_name = payload.get("intentName", "")
                    outputs = payload.get("outputs", {})
                    # UploadExeResult 格式默认 status 为 success
                    status = payload.get("status", "success") or "success"

                # 格式 3: InvokeJarvisGUIAgentResponse（GUI 工具响应，跳过）
                elif event.get("header", {}).get("namespace") == "ClawAgent" and \
                        event.get("header", {}).get("name") == "InvokeJarvisGUIAgentResponse":
                    # GUI 响应不处理，继续检查下一个 event
                    continue

                if intent_name:
                    outputs_keys = list(outputs.keys())
                    logger.info(f"[XiaoyiChannel] Extracted data-event: intent={intent_name}, "
                                f"status={status}, outputs_keys={outputs_keys}")
                    return DataEvent(
                        intent_name=intent_name,
                        outputs=outputs,
                        status=status,
                        session_id=message.get("sessionId", ""),
                        task_id=params.get("id", ""),
                    )

        return None


