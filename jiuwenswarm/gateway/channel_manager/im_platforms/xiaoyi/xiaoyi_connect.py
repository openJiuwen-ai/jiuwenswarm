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
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, List, Optional
from urllib.parse import urlparse

import aiohttp

from jiuwenswarm.gateway.channel_manager.base import BaseChannel, ChannelMetadata, RobotMessageRouter
from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.gateway.routing.keys import XiaoyiDeliveryTarget
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.push import XiaoYiPushService, PushConfig
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_utils.formatter import (
    get_status_state_for_event,
    get_status_text_for_event,
    should_send_as_status_update,
)

logger = logging.getLogger(__name__)

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
        self.api_key = api_key
        self.uid = uid
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.session.close()

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
                "x-api-key": self.api_key,
                "x-request-from": "openclaw",
            }

            async with self.session.post(prepare_url, json=prepare_data, headers=headers) as resp:
                if not resp.ok:
                    raise Exception(f"Prepare failed: HTTP {resp}")

                prepare_resp = await resp.json()
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

            async with self.session.request(
                    upload_method,
                    upload_url,
                    data=file_content,
                    headers=upload_headers
            ) as resp:
                if not resp.ok:
                    raise RuntimeError(f"Upload failed: HTTP {resp.status}")

            complete_url = f"{self.base_url}/osms/v1/file/manager/complete"
            complete_data = {
                "objectId": object_id,
                "draftId": draft_id,
            }

            async with self.session.post(complete_url, json=complete_data, headers=headers) as resp:
                if not resp.ok:
                    raise RuntimeError(f"Complete failed: HTTP {resp.status}")

                complete_resp = await resp.json()
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


class XiaoyiChannel(BaseChannel):
    """小艺通道：作为客户端连接到小艺服务器，实现 A2A 协议."""

    name = "xiaoyi"

    def __init__(self, config: XiaoyiChannelConfig, router: RobotMessageRouter):
        super().__init__(config, router)
        self.config: XiaoyiChannelConfig = config
        self._ws_connections: dict[str, Any] = {}  # Dual channel connections
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._running = False
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}  # Heartbeat tasks for each channel
        self._connect_tasks: dict[str, asyncio.Task] = {}  # Connection tasks for each channel
        self._session_task_map: dict[str, str] = {}
        self._session_heartbeat_tasks: dict[str, asyncio.Task] = {}  # Response heartbeat tasks for each session
        self._stream_text_buffers: dict[str, str] = {}
        self._task_last_activity: dict[str, float] = {}
        # V2: team ws 流式合并缓冲（task_id → 累积 delta 文本 + 延迟 flush 任务）
        self._ws_flush_buffers: dict[str, str] = {}
        self._ws_flush_tasks: dict[str, asyncio.Task] = {}
        self._on_message_cb: Callable[[Message], Any] | None = None
        # Task timeout management
        self._session_active: set[str] = set()  # Active sessions (concurrent request detection)
        self._task_timeout_tasks: dict[str, asyncio.Task] = {}  # 1-hour task timeout tasks
        self._session_timeout_tasks: dict[str, asyncio.Task] = {}  # 60-second periodic timeout tasks
        self._sessions_waiting_for_push: dict[str, str] = {}  # {session: task} waiting for push
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
        # Cancel all heartbeat tasks
        for url_key in list(self._heartbeat_tasks.keys()):
            if self._heartbeat_tasks[url_key]:
                self._heartbeat_tasks[url_key].cancel()
                self._heartbeat_tasks[url_key] = None
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
        for session_id in list(self._task_timeout_tasks.keys()):
            if self._task_timeout_tasks[session_id]:
                self._task_timeout_tasks[session_id].cancel()
                self._task_timeout_tasks[session_id] = None
        # Cancel all session timeout tasks
        for session_id in list(self._session_timeout_tasks.keys()):
            if self._session_timeout_tasks[session_id]:
                self._session_timeout_tasks[session_id].cancel()
                self._session_timeout_tasks[session_id] = None
        # Close all websocket connections
        for url_key, ws in list(self._ws_connections.items()):
            if ws:
                try:
                    await ws.close()
                except Exception as e:
                    logger.warning(f"关闭 WebSocket 连接失败 ({url_key}): {e}")
                self._ws_connections[url_key] = None
        self._heartbeat_tasks.clear()
        self._connect_tasks.clear()
        self._session_heartbeat_tasks.clear()
        self._task_timeout_tasks.clear()
        self._session_timeout_tasks.clear()
        self._ws_connections.clear()
        self._session_active.clear()
        self._sessions_waiting_for_push.clear()
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
            return
        await self._send_legacy(msg)

    async def _send_legacy(self, msg: Message) -> None:
        """非 team 模式原有单值投递路径（保留兼容）."""
        logger.info(f"XiaoyiChannel 发送消息: {msg}")
        session_id, task_id = self._extract_platform_receive_info(msg)
        # Handle chat.file event
        if self.config.mode == "xiaoyi_claw" and msg.event_type == EventType.CHAT_FILE:
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

        if should_send_as_status_update(msg.event_type):
            status_text = get_status_text_for_event(msg.event_type, msg.payload)
            status_state = get_status_state_for_event(msg.event_type, msg.payload)
            for url_key in list(self._ws_connections.keys()):
                await self._send_status_update_with_state(
                    task_id, session_id, status_text, status_state, url_key
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

        content = ""
        cron_job_name = ""
        if isinstance(msg.payload, dict):
            content = msg.payload.get("content", "\n")
            if isinstance(content, dict):
                content = content.get("output", str(content))
            content = str(content)
            cron_job_name = msg.payload.get("cron", {}).get("job_name", "")
        elif msg.payload:
            content = str(msg.payload)

        # 推送消息发送
        if msg.id.startswith("cron-push"):
            await self._send_push_notification(cron_job_name, content)
            return

        # 如果禁用流式，总是作为完整消息发送
        if not self.config.enable_streaming:
            append = False
            last_chunk = True
            final = True
        else:
            # 流式模式：按事件类型计算增量与是否结束
            is_delta = msg.event_type == EventType.CHAT_DELTA
            last_chunk = msg.event_type == EventType.CHAT_FINAL
            is_final = msg.payload.get("is_complete", False)
            last_chunk = True if is_final else last_chunk

            # 获取之前发送的文本
            previous_text = self._accumulated_texts.get(session_id, "")

            # 累积当前文本
            self._accumulated_texts[session_id] = content

            # 计算增量文本
            if is_delta:
                incremental_text = content[len(previous_text):]
            else:
                incremental_text = content

            # 在消息流中，总是使用 append=true, isFinal=false
            append = True
            final = False
            last_chunk = last_chunk
            final = is_final

        # Get accumulated text for this session (for push notification)
        accumulated_text = self._accumulated_texts.get(session_id, "")
        self._accumulated_texts[session_id] = content

        # Send to all active connections
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
                        is_final=final
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
                ssl=ssl_context,
                ping_interval=15,
                ping_timeout=15,
                close_timeout=5,
        ) as ws:
            self._ws_connections[url_key] = ws
            self._send_locks[url_key] = asyncio.Lock()
            logger.info(f"XiaoyiChannel 已连接 {url_key}: {url}")

            # 发送初始化消息（必须在 heartbeat 之前）
            await self._send_init_message(url_key)

            # 启动心跳
            self._heartbeat_tasks[url_key] = asyncio.create_task(self._heartbeat_loop(url_key))

            try:
                async for raw in ws:
                    await self._handle_raw_message(raw)
            except Exception as e:
                logger.warning(f"XiaoyiChannel 连接异常 ({url_key}): {e}")
            finally:
                if self._heartbeat_tasks.get(url_key):
                    self._heartbeat_tasks[url_key].cancel()
                    self._heartbeat_tasks[url_key] = None
                self._ws_connections[url_key] = None
                self._send_locks.pop(url_key, None)
                close_code = getattr(ws, "close_code", None)
                close_reason = getattr(ws, "close_reason", None)
                logger.info(
                    f"XiaoyiChannel 连接关闭 {url_key}: {url} (code={close_code}, reason={close_reason})"
                )

    async def _send_init_message(self, url_key: str) -> None:
        """发送初始化消息 (clawd_bot_init) 到指定通道."""
        ws = self._ws_connections.get(url_key)
        if not ws:
            return
        init_message = {
            "msgType": "clawd_bot_init",
            "agentId": self.config.agent_id,
        }
        try:
            await self._safe_ws_send(url_key, init_message)
            logger.info(f"XiaoyiChannel 已发送初始化消息 ({url_key})")
        except Exception as e:
            logger.warning(f"XiaoyiChannel 发送初始化消息失败 ({url_key}): {e}")
            raise

    async def _heartbeat_loop(self, url_key: str) -> None:
        """应用层心跳循环（20秒间隔）."""
        while self._running and self._ws_connections.get(url_key):
            try:
                heartbeat = {"msgType": "heartbeat", "agentId": self.config.agent_id}
                await self._safe_ws_send(url_key, heartbeat)
            except Exception as e:
                logger.warning(f"XiaoyiChannel 心跳发送失败 ({url_key}): {e}")
                ws = self._ws_connections.get(url_key)
                if ws:
                    try:
                        await ws.close()
                    except Exception as close_error:
                        logger.warning(f"XiaoyiChannel 关闭连接失败 ({url_key}): {close_error}")
                break
            await asyncio.sleep(20)

    async def _handle_raw_message(self, raw: str | bytes) -> None:
        """处理接收到的原始消息，转换为 JiuwenSwarm 内部格式."""
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            message = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"XiaoyiChannel JSON 解析失败: {e}")
            return

        msg_type = message.get("msgType")
        method = message.get("method")

        # 添加详细日志用于诊断工具消息
        if method or (msg_type and msg_type != "heartbeat"):
            logger.info(f"[XiaoyiChannel] _handle_raw_message: msg_type={msg_type},"
                        f"method={method}, sessionId={message.get('sessionId', 'N/A')}")

        if msg_type == "heartbeat":
            return

        # 根级直连 A2A（jsonrpc 2.0）须含 params.sessionId，否则整帧丢弃
        if message.get("jsonrpc") == "2.0":
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
        user_message = message.get("params", {}).get("message", {})
        parts = user_message.get("parts", [])

        # Mark session as active
        self._mark_session_active(session_id)
        self._session_task_map[task_id] = session_id

        # ==================== PROCESS PARTS (TEXT & FILES) ====================
        text = ""
        push_id = ""  # V2: 从 data part 的 systemVariables 提取，webhook 推送寻址 token
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
                            text_content = await extract_text_from_url(uri, 5_000_000, 30_000)
                            text += f"\n\n[文件内容: {name}]\n{text_content}"
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
                    pid = data.get("variables", {}).get("systemVariables", {}).get("push_id", "")
                    if pid:
                        push_id = pid
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

        # ==================== INTERCEPT TEAM MODE COMMANDS ====================
        # xiaoyi channel 不支持 team 模式，拦截并直接返回提示
        text_stripped = text.strip()
        if text_stripped in ("/mode team", "/mode code.team"):
            logger.info(f"XiaoYi: Intercepted team mode command: {text_stripped}")
            response_text = "小艺：暂不支持team 模式。请使用web或者飞书频道试用"
            for url_key in list(self._ws_connections.keys()):
                await self._send_text_response(session_id, task_id, response_text, url_key, is_final=True)
            return
        # =================================================================

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
        }
        # Add media payload to metadata
        params = {"query": text, "task_id": task_id}
        if media_payload:
            params["files"] = media_payload

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
                logger.info(f"[TASK TIMEOUT] 1-hour timeout triggered for session {session_id}")
                # Send default message with is_final=true
                for url_key in list(self._ws_connections.keys()):
                    await self._send_text_response(session_id, task_id, "任务还在处理中~", url_key, is_final=True)
                # Mark session as waiting for push state
                self._mark_session_waiting_for_push(session_id, task_id)
            except asyncio.CancelledError:
                pass

        self._task_timeout_tasks[session_id] = asyncio.create_task(task_timeout_handler())

        # Start 60-second periodic timeout for status updates
        async def periodic_timeout_handler():
            """60-second periodic timeout for status updates."""
            try:
                while session_id in self._session_active:
                    await asyncio.sleep(60)
                    # Skip if already waiting for push (1-hour timeout triggered)
                    if self._is_session_waiting_for_push(session_id, task_id):
                        break
                    # Send status update
                    await self._send_status_update(task_id, session_id, "任务正在处理中，请稍后~")
            except asyncio.CancelledError:
                pass

        self._session_timeout_tasks[session_id] = asyncio.create_task(periodic_timeout_handler())
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
                    "state": state,
                },
            },
        }
        await self._send_agent_response(session_id, task_id, response, url_key)

    def _is_session_active(self, session_id: str) -> bool:
        """检查会话是否有活跃任务."""
        return session_id in self._session_active

    def _mark_session_active(self, session_id: str) -> None:
        """标记会话为活跃状态."""
        self._session_active.add(session_id)

    def _mark_session_completed(self, session_id: str) -> None:
        """标记会话已完成."""
        self._session_active.discard(session_id)

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

    async def _handle_clear_context(self, message: dict[str, Any]) -> None:
        """处理清空上下文请求."""
        session_id = message.get("sessionId", "")
        logger.info(f"XiaoyiChannel 清空上下文: {session_id}")

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
        session_id = message.get("sessionId", "")
        task_id = message.get("params", {}).get("id") or message.get("taskId", "")
        logger.info(f"XiaoyiChannel 取消任务: {session_id} {task_id}")
        if session_id:
            await self._stop_session_heartbeat(session_id)

        response = {
            "jsonrpc": "2.0",
            "id": message.get("id", ""),
            "result": {"id": message.get("id", ""), "status": {"state": "canceled"}},
        }
        # Send response to all active connections
        for url_key in list(self._ws_connections.keys()):
            await self._send_agent_response(session_id, task_id, response, url_key)

        # 清理超时任务和推送状态
        self._clear_task_timeout(session_id)
        self._clear_session_timeout(session_id)
        self._clear_session_waiting_for_push(session_id, task_id)
        self._mark_session_completed(session_id)

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
    ) -> None:
        """发送文本响应（A2A 格式）到指定通道."""
        if last_chunk:
            data = {"kind": "text", "text": text}
        else:
            data = {"kind": "reasoningText", "reasoningText": text}
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
        await self._send_agent_response(session_id, task_id, response, url_key)

    async def _send_agent_response(self, session_id: str, task_id: str, response: dict[str, Any], url_key: str) -> None:
        """发送 agent_response 包装的消息（A2A 格式）到指定通道."""
        wrapper = {
            "msgType": "agent_response",
            "agentId": self.config.agent_id,
            "sessionId": session_id,
            "taskId": task_id,
            "msgDetail": json.dumps(response),
        }
        try:
            await self._safe_ws_send(url_key, wrapper)
        except Exception as e:
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

            if not all([base_url, api_key, uid]):
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

    async def _safe_ws_send(self, url_key: str, payload: dict[str, Any]) -> None:
        ws = self._ws_connections.get(url_key)
        if not ws:
            raise RuntimeError(f"ws connection not available: {url_key}")
        lock = self._send_locks.get(url_key)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[url_key] = lock
        data = json.dumps(payload, ensure_ascii=False)
        async with lock:
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

    def _clear_task_timeout(self, session_id: str) -> None:
        """清除任务超时任务."""
        if session_id in self._task_timeout_tasks:
            task = self._task_timeout_tasks[session_id]
            if task and not task.done():
                task.cancel()
            self._task_timeout_tasks.pop(session_id, None)

    def _clear_session_timeout(self, session_id: str) -> None:
        """清除会话超时任务."""
        if session_id in self._session_timeout_tasks:
            task = self._session_timeout_tasks[session_id]
            if task and not task.done():
                task.cancel()
            self._session_timeout_tasks.pop(session_id, None)

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
                "final": False,
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
        sent = False
        for url_key, ws in self._ws_connections.items():
            if ws:
                try:
                    await self._safe_ws_send(url_key, wrapper)
                    intent_name = command.get("payload", {}).get("executeParam", {}).get("intentName") or command.get(
                        "header", {}
                    ).get("name", "unknown")
                    logger.info(f"XiaoyiChannel 发送 command 成功 ({url_key}):intent={intent_name}")
                    sent = True
                except Exception as e:
                    logger.warning(f"XiaoyiChannel 发送 command 失败 ({url_key}): {e}")

        return sent

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
                    for h in list(self._gui_agent_handlers):
                        try:
                            if asyncio.iscoroutinefunction(h):
                                await h(item)
                            else:
                                h(item)
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
