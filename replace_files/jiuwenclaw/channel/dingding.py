# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field
import httpx


from jiuwenclaw.channel.base import RobotMessageRouter, BaseChannel
from jiuwenclaw.channel.dingtalk_file_service import DingTalkFileService
from jiuwenclaw.schema.message import Message, ReqMethod

logger = logging.getLogger(__name__)


class DingTalkConfig(BaseModel):
    """钉钉通道配置（使用Stream模式）"""
    enabled: bool = False
    client_id: str = ""  # 应用ID
    client_secret: str = ""  # 应用密钥
    allow_from: list[str] = Field(default_factory=list)  # 允许的员工ID
    # 文件处理配置
    max_download_size: int = 100 * 1024 * 1024  # 最大下载文件大小（默认 100MB）
    download_timeout: int = 60  # 下载超时时间（秒）
    send_file_allowed: bool = True  # 是否启用文件上传功能
    enable_file_download: bool = True  # 是否启用文件下载功能
    workspace_dir: str = ""  # 工作空间目录


@dataclass
class DingTalkInboundMessage:
    """钉钉入站消息载体，避免参数列表持续膨胀。"""

    content: str
    sender_id: str
    sender_name: str
    conversation_id: str
    conversation_type: str
    files: list[dict] | None = None


@dataclass
class DingTalkMessageSendRequest:
    """钉钉消息发送请求参数封装。"""

    token: str
    chat_id: str
    conversation_type: str
    open_conversation_id: str
    file_path: str
    file_name: str


@dataclass
class DingTalkFileNotificationRequest:
    """钉钉文件通知请求参数封装。"""

    token: str
    chat_id: str
    conversation_type: str
    open_conversation_id: str
    file_path: str
    file_name: str
    error_msg: str = ""


@dataclass
class DingTalkMediaMessageRequest:
    """钉钉媒体消息发送请求参数封装。"""

    token: str
    chat_id: str
    conversation_type: str
    open_conversation_id: str
    msg_key: str
    msg_param: str


@dataclass
class DingTalkImageSendRequest:
    """钉钉图片发送请求参数封装。"""

    token: str
    chat_id: str
    conversation_type: str
    open_conversation_id: str
    file_path: str


try:
    from dingtalk_stream import (
        DingTalkStreamClient,
        Credential,
        CallbackHandler,
        CallbackMessage,
        AckMessage,
    )
    from dingtalk_stream.chatbot import ChatbotMessage

    DINGTALK_AVAILABLE = True
except ImportError:
    DINGTALK_AVAILABLE = False
    CallbackHandler = object
    CallbackMessage = None
    AckMessage = None
    ChatbotMessage = None


class DingTalkHandler(CallbackHandler):
    """
    钉钉Stream SDK标准回调处理器。
    解析传入消息并转发到通道。
    """

    def __init__(self, channel: "DingTalkChannel"):
        super().__init__()
        self.channel = channel

    def _extract_text_content(self, chatbot_msg: ChatbotMessage, raw_data: dict) -> str:
        """从消息对象中提取文本内容"""
        content = ""
        if chatbot_msg.text:
            content = chatbot_msg.text.content.strip()
        if not content:
            content = raw_data.get("text", {}).get("content", "").strip()
        return content

    def _extract_sender_info(self, chatbot_msg: ChatbotMessage) -> tuple[str, str]:
        """提取发送者信息"""
        sender_id = chatbot_msg.sender_staff_id or chatbot_msg.sender_id
        sender_name = chatbot_msg.sender_nick or "Unknown"
        return sender_id, sender_name

    def _extract_conversation_info(self, chatbot_msg: ChatbotMessage) -> tuple[str, str]:
        """提取会话信息"""
        conversation_id = chatbot_msg.conversation_id or ""
        conversation_type = chatbot_msg.conversation_type or "1"  # 1: 单聊；2：群聊
        return conversation_id, conversation_type

    def _create_message_task(self, message: DingTalkInboundMessage) -> None:
        """创建异步任务处理消息"""
        task = asyncio.create_task(
            self.channel.handle_incoming_message(message)
        )
        self.channel._background_tasks.add(task)
        task.add_done_callback(self.channel._background_tasks.discard)

    async def process(self, message: CallbackMessage):
        """处理传入的流消息"""
        try:
            # 使用SDK的ChatbotMessage进行健壮解析
            chatbot_msg = ChatbotMessage.from_dict(message.data)
            raw_data = message.data
            msg_type = raw_data.get("msgtype", "text")

            # 提取发送者信息
            sender_id, sender_name = self._extract_sender_info(chatbot_msg)

            # 提取会话信息
            conversation_id, conversation_type = self._extract_conversation_info(chatbot_msg)

            # 权限检查（所有消息类型）
            if not self.channel.is_allowed(sender_id):
                logger.warning(f"发送者 {sender_id} 未被允许使用此机器人")
                return AckMessage.STATUS_OK, "OK"

            # 根据消息类型处理
            content = ""
            files = None

            if msg_type == "text":
                content = self._extract_text_content(chatbot_msg, raw_data)
            elif msg_type == "picture":
                content, files = await self.channel.handle_picture_message(
                    raw_data, sender_id, conversation_id, conversation_type
                )
            elif msg_type == "file":
                content, files = await self.channel.handle_file_message(
                    raw_data, sender_id, conversation_id, conversation_type
                )
            elif msg_type == "audio" or msg_type == "voice":
                content, files = await self.channel.handle_audio_message(
                    raw_data, sender_id, conversation_id, conversation_type
                )
            elif msg_type == "video":
                content, files = await self.channel.handle_video_message(
                    raw_data, sender_id, conversation_id, conversation_type
                )
            else:
                content = f"[不支持的消息类型: {msg_type}]"
                logger.warning(f"收到不支持的消息类型: {msg_type}")

            if not content and not files:
                logger.warning(f"收到空消息: {msg_type}")
                return AckMessage.STATUS_OK, "OK"

            logger.info(
                f"收到来自 {sender_name} ({sender_id}) 的钉钉消息: {content[:50]}... (会话ID: {conversation_id})"
            )

            # 转发到通道（非阻塞）
            self._create_message_task(
                DingTalkInboundMessage(
                    content=content,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    conversation_id=conversation_id,
                    conversation_type=conversation_type,
                    files=files,
                )
            )

            return AckMessage.STATUS_OK, "OK"

        except Exception as e:
            logger.error(f"处理钉钉消息时出错: {e}")
            # 返回OK以避免钉钉服务器重试循环
            return AckMessage.STATUS_OK, "Error"


class DingTalkChannel(BaseChannel):
    """
    使用Stream模式的钉钉通道。

    通过 `dingtalk-stream` SDK 使用 WebSocket 接收事件。
    使用直接 HTTP API 发送消息（SDK主要用于接收）。
    """

    name = "dingtalk"

    def __init__(self, config: DingTalkConfig, router: RobotMessageRouter):
        super().__init__(config, router)
        self.config: DingTalkConfig = config
        self._client: Any = None
        self._http: httpx.AsyncClient | None = None

        self._access_token: str | None = None
        self._token_expiry: float = 0
        self._background_tasks: set[asyncio.Task] = set()

        self._gateway_callback: Callable[[Message], None] | None = None
        self._stream_task: asyncio.Task | None = None  # 用于跟踪 SDK start() 任务

        # 文件服务
        self._file_service: DingTalkFileService | None = None
        # 按 request_id 记录已发送文件路径，避免重复发送
        self._sent_file_paths_by_req: dict[str, set[str]] = {}

    @property
    def channel_id(self) -> str:
        """返回通道的唯一标识"""
        return self.name

    def on_message(self, callback: Callable[[Message], None]) -> None:
        """注册钉钉通道的回调函数"""
        self._gateway_callback = callback

    async def _handle_message(
            self,
            chat_id: str,
            content: str,
            metadata: dict[str, Any] | None = None
    ) -> None:
        """处理来自钉钉通道的传入消息（符合基类接口）"""
        # 检查发送者权限
        if not self.is_allowed(chat_id):
            logger.warning(f"发送者 {chat_id} 未被允许使用此机器人")
            return

        # 调用内部处理方法
        await self._process_incoming_message(
            chat_id=chat_id,
            sender_id=chat_id,
            content=content,
            conversation_id="",
            conversation_type="1",
            metadata=metadata,
        )

    def _build_user_message(self, chat_id: str, sender_id: str, content: str,
                            conversation_id: str, conversation_type: str,
                            metadata: dict[str, Any] | None = None,
                            files: list[dict] | None = None) -> Message:
        """构建用户消息对象"""
        metadata = metadata or {}
        metadata.update({"conversation_id": conversation_id, "conversation_type": conversation_type})
        params = {"content": content, "query": content}
        if files:
            params["files"] = files
        return Message(
            id=chat_id,
            type="req",
            channel_id=self.name,
            session_id=str(chat_id),
            params=params,
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.CHAT_SEND,
            chat_id=conversation_id,
            metadata=metadata,
        )

    async def _process_incoming_message(self, chat_id: str, sender_id: str, content: str, conversation_id: str,
                              conversation_type: str, metadata: dict[str, Any] | None = None,
                              files: list[dict] | None = None) -> None:
        """处理来自钉钉通道的传入消息"""
        msg = self._build_user_message(chat_id, sender_id, content, conversation_id, conversation_type, metadata, files)


        if self._gateway_callback:
            self._gateway_callback(msg)
        else:
            await self.bus.route_user_message(msg)

    def _validate_config(self) -> bool:
        """验证配置是否有效"""
        if not DINGTALK_AVAILABLE:
            logger.error(
                "钉钉Stream SDK未安装。请运行: pip install dingtalk-stream"
            )
            return False

        if not self.config.client_id or not self.config.client_secret:
            logger.error("钉钉 client_id 和 client_secret 未配置")
            return False

        return True

    def _initialize_stream_client(self) -> None:
        """初始化钉钉Stream客户端"""
        logger.info("正在初始化钉钉Stream客户端")
        credential = Credential(self.config.client_id, self.config.client_secret)
        self._client = DingTalkStreamClient(credential)

        # 注册标准处理器
        handler = DingTalkHandler(self)
        self._client.register_callback_handler(ChatbotMessage.TOPIC, handler)

        logger.info("钉钉机器人已启动（Stream模式）")

    async def start(self) -> None:
        """启动钉钉机器人（Stream模式）"""
        try:
            if not self._validate_config():
                return

            self._running = True
            self._http = httpx.AsyncClient()

            # 初始化文件服务
            workspace_dir = self.config.workspace_dir or os.path.expanduser("~/.jiuwenclaw/agent/workspace")
            self._file_service = DingTalkFileService(
                client_id=self.config.client_id,
                get_token_func=self._get_access_token,
                http_client=self._http,
                max_download_size=self.config.max_download_size,
                download_timeout=self.config.download_timeout,
                workspace_dir=workspace_dir,
            )

            self._initialize_stream_client()

            # 将 SDK start() 作为独立任务运行，便于在 stop() 时取消
            self._stream_task = asyncio.create_task(self._client.start(), name="dingtalk-sdk-start")

            # 等待任务完成（当 _running=False 时，任务会被取消）
            try:
                await self._stream_task
            except asyncio.CancelledError:
                logger.info("钉钉 Stream 任务已被取消")
            except Exception as e:
                logger.warning(f"钉钉 Stream 任务异常退出: {e}")

        except Exception as e:
            logger.exception(f"启动钉钉通道失败: {e}")

    async def stop(self) -> None:
        """停止钉钉机器人"""
        self._running = False

        # 取消 SDK start() 任务
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await asyncio.wait_for(self._stream_task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("等待钉钉 Stream 任务取消超时")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"等待钉钉 Stream 任务取消时出错: {e}")
        self._stream_task = None

        # 关闭 WebSocket 连接
        if self._client and hasattr(self._client, 'websocket') and self._client.websocket:
            try:
                await self._client.websocket.close()
            except Exception as e:
                logger.warning(f"关闭 WebSocket 连接时出错: {e}")

        # 清理客户端
        if self._client:
            try:
                # 检查 SDK 是否提供 stop 方法
                if hasattr(self._client, 'stop'):
                    await self._client.stop()
                # 检查 SDK 是否提供 close 方法
                elif hasattr(self._client, 'close'):
                    await self._client.close()
                # 检查 SDK 是否提供 shutdown 方法
                elif hasattr(self._client, 'shutdown'):
                    await self._client.shutdown()
            except Exception as e:
                logger.warning(f"停止 DingTalkStreamClient 时出错: {e}")
            finally:
                self._client = None

        # 关闭共享HTTP客户端
        if self._http:
            await self._http.aclose()
            self._http = None

        # 取消未完成的后台任务
        for task in self._background_tasks:
            task.cancel()
        self._background_tasks.clear()

    def _is_token_valid(self) -> bool:
        """检查当前令牌是否有效"""
        return self._access_token is not None and time.time() < self._token_expiry

    def _build_token_request_data(self) -> dict:
        """构建令牌请求数据"""
        return {
            "appKey": self.config.client_id,
            "appSecret": self.config.client_secret,
        }

    def _parse_token_response(self, res_data: dict) -> None:
        """解析令牌响应"""
        self._access_token = res_data.get("accessToken")
        # 提前60秒过期以确保安全
        self._token_expiry = time.time() + int(res_data.get("expireIn", 7200)) - 60

    async def _request_new_token(self) -> str | None:
        """请求新的访问令牌"""
        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        data = self._build_token_request_data()

        if not self._http:
            logger.warning("钉钉HTTP客户端未初始化，无法刷新令牌")
            return None

        try:
            resp = await self._http.post(url, json=data)
            resp.raise_for_status()
            res_data = resp.json()
            self._parse_token_response(res_data)
            return self._access_token
        except Exception as e:
            logger.error(f"获取钉钉访问令牌失败: {e}")
            return None

    async def _get_access_token(self) -> str | None:
        """获取或刷新访问令牌"""
        if self._is_token_valid():
            return self._access_token

        return await self._request_new_token()

    def _extract_message_content(self, msg: Message) -> str | None:
        """从消息对象中提取内容"""
        if msg.params and "content" in msg.params:
            return str(msg.params["content"])
        elif msg.payload and "content" in msg.payload:
            content_ = msg.payload["content"]
            if isinstance(content_, dict) and "output" in content_:
                return str(content_["output"])
            return str(content_)
        elif msg.payload and "text" in msg.payload:
            return str(msg.payload["text"])
        return None

    def _extract_chat_id(self, msg: Message) -> str | None:
        """从消息对象中提取聊天ID"""
        chat_id = msg.id if msg.id else None
        if not chat_id:
            chat_id = msg.session_id
        return chat_id

    def _build_group_message_payload(self, content: str, open_conversation_id: str) -> dict:
        """构建群聊消息负载"""
        return {
            "robotCode": self.config.client_id,
            "openConversationId": open_conversation_id,
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({
                "text": content,
                "title": "JiuClaw Reply",
            }),
        }

    def _build_private_message_payload(self, chat_id: str, content: str) -> dict:
        """构建私聊消息负载"""
        return {
            "robotCode": self.config.client_id,
            "userIds": [chat_id],
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({
                "text": content,
                "title": "JiuClaw Reply",
            }),
        }

    def _get_send_api_url(self, conversation_type: str) -> str:
        """根据会话类型获取发送API URL"""
        if conversation_type == "2":
            return "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
        else:
            return "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"

    def _build_send_request(self, chat_id: str, content: str, conversation_type: str, open_conversation_id: str) -> tuple[str, dict]:
        """构建发送请求"""
        url = self._get_send_api_url(conversation_type)

        if conversation_type == "2":
            data = self._build_group_message_payload(content, open_conversation_id)
        else:
            data = self._build_private_message_payload(chat_id, content)

        return url, data

    async def _send_http_request(self, url: str, data: dict, token: str, chat_id: str) -> None:
        """发送HTTP请求"""
        headers = {"x-acs-dingtalk-access-token": token}

        if not self._http:
            logger.warning("钉钉HTTP客户端未初始化，无法发送消息")
            return

        try:
            resp = await self._http.post(url, json=data, headers=headers)
            if resp.status_code != 200:
                logger.error(f"钉钉消息发送失败: {resp.text}")
            else:
                logger.debug("钉钉消息已发送至 %s", chat_id)
        except Exception as e:
            logger.error(f"发送钉钉消息时出错: {e}")

    async def send(self, msg: Message) -> None:
        """通过钉钉发送消息"""
        token = await self._get_access_token()
        if not token:
            return

        # 提取事件类型
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        event_type = getattr(msg.event_type, "value", None) or payload.get("event_type") or ""

        # 处理文件发送事件（chat.media 与 chat.file 统一走文件发送路径）
        if event_type in ("chat.file", "chat.media"):
            await self._send_file_message(msg)
            return

        # 提取内容
        content = self._extract_message_content(msg)
        if not content:
            logger.warning("钉钉发送: 在 msg.params 或 msg.payload 中未找到内容")
            return

        # 提取聊天ID
        chat_id = self._extract_chat_id(msg)
        if not chat_id:
            logger.warning("钉钉发送: 在消息中未找到 chat_id 或 session_id")
            return

        # 构建请求
        metadata = msg.metadata or {}
        conversation_type = metadata.get("conversation_type", "")
        open_conversation_id = metadata.get("conversation_id", "")
        url, data = self._build_send_request(chat_id, content, conversation_type, open_conversation_id)

        # 发送HTTP请求
        await self._send_http_request(url, data, token, chat_id)

        # chat.final 兜底文件发送
        if event_type == "chat.final":
            await self._send_fallback_files(msg, content)

    # ==================== 文件下载处理方法 ====================

    async def handle_picture_message(
        self, raw_data: dict, sender_id: str, conversation_id: str, conversation_type: str
    ) -> tuple[str, list[dict] | None]:
        """处理图片消息"""
        if not self.config.enable_file_download:
            return "[图片: 文件下载功能已禁用]", None

        if not self._file_service:
            return "[图片: 文件服务未初始化]", None

        content = raw_data.get("content", {})
        download_code = content.get("downloadCode", "")
        message_id = raw_data.get("msgId", sender_id)

        if not download_code:
            return "[图片: 缺少下载码]", None

        file_info = await self._file_service.download_image(download_code, message_id)
        if not file_info:
            return "[图片: 下载失败]", None

        return "[图片]", [file_info]

    async def handle_file_message(
        self, raw_data: dict, sender_id: str, conversation_id: str, conversation_type: str
    ) -> tuple[str, list[dict] | None]:
        """处理文件消息"""
        if not self.config.enable_file_download:
            return "[文件: 文件下载功能已禁用]", None

        if not self._file_service:
            return "[文件: 文件服务未初始化]", None

        content = raw_data.get("content", {})
        download_code = content.get("downloadCode", "")
        file_name = content.get("fileName", "unknown_file")
        file_size = content.get("fileSize", 0)
        message_id = raw_data.get("msgId", sender_id)

        if not download_code:
            return "[文件: 缺少下载码]", None

        # 检查文件大小（仅当文件大小已知且过大时跳过）
        # 注意：钉钉消息可能不包含 fileSize 字段，所以只在已知大小时检查
        if file_size > 0 and file_size > self.config.max_download_size:
            return f"[文件过大: {file_name}]", None

        file_info = await self._file_service.download_file(download_code, message_id, file_name)
        if not file_info:
            return f"[文件: {file_name} 下载失败]", None

        # 下载后检查实际文件大小
        if file_info.get("size", 0) == 0:
            logger.warning(f"下载的文件为空: {file_name}")
            return "[空文件]", None

        return f"[文件: {file_name}]", [file_info]

    async def handle_audio_message(
        self, raw_data: dict, sender_id: str, conversation_id: str, conversation_type: str
    ) -> tuple[str, list[dict] | None]:
        """处理音频消息"""
        if not self.config.enable_file_download:
            return "[音频: 文件下载功能已禁用]", None

        if not self._file_service:
            return "[音频: 文件服务未初始化]", None

        content = raw_data.get("content", {})
        download_code = content.get("downloadCode", "")
        duration = content.get("duration", 0)
        message_id = raw_data.get("msgId", sender_id)

        if not download_code:
            return "[音频: 缺少下载码]", None

        file_info = await self._file_service.download_audio(download_code, message_id)
        if not file_info:
            return "[音频: 下载失败]", None

        duration_str = f" {duration / 1000:.1f}s" if duration else ""
        return f"[音频{duration_str}]", [file_info]

    async def handle_video_message(
        self, raw_data: dict, sender_id: str, conversation_id: str, conversation_type: str
    ) -> tuple[str, list[dict] | None]:
        """处理视频消息"""
        if not self.config.enable_file_download:
            return "[视频: 文件下载功能已禁用]", None

        if not self._file_service:
            return "[视频: 文件服务未初始化]", None

        content = raw_data.get("content", {})
        download_code = content.get("downloadCode", "")
        duration = content.get("duration", 0)
        message_id = raw_data.get("msgId", sender_id)

        if not download_code:
            return "[视频: 缺少下载码]", None

        file_info = await self._file_service.download_video(download_code, message_id)
        if not file_info:
            return "[视频: 下载失败]", None

        duration_str = f" {duration / 1000:.1f}s" if duration else ""
        return f"[视频{duration_str}]", [file_info]

    async def handle_incoming_message(self, message: DingTalkInboundMessage) -> None:
        """处理传入消息（由DingTalkHandler调用）

        委托给 _process_incoming_message()，该方法在发布到总线之前执行 allow_from
        权限检查。
        """
        try:
            logger.info(f"钉钉入站消息: {message.content} 来自 {message.sender_name}")
            await self._process_incoming_message(
                chat_id=message.sender_id,
                sender_id=message.sender_id,
                content=str(message.content),
                conversation_id=message.conversation_id,
                conversation_type=message.conversation_type,
                metadata={
                    "sender_name": message.sender_name,
                    "platform": "dingtalk",
                    "dingtalk_chat_id": message.conversation_id,
                    "dingtalk_sender_id": message.sender_id,
                },
                files=message.files,
            )
        except Exception as e:
            logger.error(f"发布钉钉消息时出错: {e}")

    # ==================== 文件发送方法 ====================

    def _extract_receive_info(self, msg: Message) -> tuple[str, str, str]:
        """从消息中提取接收者信息。

        Returns:
            (chat_id, conversation_type, open_conversation_id)
        """
        metadata = msg.metadata or {}
        conversation_type = metadata.get("conversation_type", "1")
        open_conversation_id = metadata.get("conversation_id", "")

        # 根据会话类型选择正确的 chat_id：
        # - 私聊 (type=1): Robot API userIds 需要员工ID (sender_staff_id)
        # - 群聊 (type=2): Robot API openConversationId 需要 conversation_id
        if conversation_type == "2":
            # 群聊：使用 conversation_id
            chat_id = metadata.get("dingtalk_chat_id") or metadata.get("dingtalk_sender_id") or ""
        else:
            # 私聊：使用 sender_staff_id（员工ID）
            chat_id = metadata.get("dingtalk_sender_id") or metadata.get("dingtalk_chat_id") or ""

        # 回退到 session_id
        if not chat_id:
            chat_id = getattr(msg, "session_id", "") or msg.id or ""

        return chat_id, conversation_type, open_conversation_id

    async def _send_file_message(self, msg: Message) -> None:
        """发送文件消息"""
        if not self._file_service or not self.config.send_file_allowed:
            logger.warning("钉钉文件发送功能未启用")
            return

        payload = msg.payload if isinstance(msg.payload, dict) else {}
        files = payload.get("files", [])
        if not files:
            return

        chat_id, conversation_type, open_conversation_id = self._extract_receive_info(msg)
        if not chat_id:
            logger.warning("钉钉文件发送: 未找到接收者")
            return

        # 获取当前 request_id 用于去重
        request_id = getattr(msg, "id", "") or ""
        if request_id not in self._sent_file_paths_by_req:
            self._sent_file_paths_by_req[request_id] = set()

        token = await self._get_access_token()
        if not token:
            logger.error("钉钉文件发送: 无法获取 access_token")
            return

        for file_info in files:
            file_path = file_info.get("path", "")
            if not file_path or not os.path.isfile(file_path):
                logger.warning(f"钉钉文件发送: 文件不存在 {file_path}")
                continue

            # 检查是否已发送
            if file_path in self._sent_file_paths_by_req[request_id]:
                continue

            # 根据文件类型选择发送方法
            ext = os.path.splitext(file_path)[1].lower()
            file_name = os.path.basename(file_path)

            try:
                if ext in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}:
                    await self._send_image(
                        DingTalkImageSendRequest(
                            token=token,
                            chat_id=chat_id,
                            conversation_type=conversation_type,
                            open_conversation_id=open_conversation_id,
                            file_path=file_path,
                        )
                    )
                elif ext in {'.mp3', '.wav', '.aac', '.ogg', '.flac', '.m4a'}:
                    await self._send_audio(
                        DingTalkMessageSendRequest(
                            token=token,
                            chat_id=chat_id,
                            conversation_type=conversation_type,
                            open_conversation_id=open_conversation_id,
                            file_path=file_path,
                            file_name=file_name,
                        )
                    )
                elif ext in {'.mp4', '.mov', '.avi', '.mkv', '.flv', '.webm'}:
                    await self._send_video(
                        DingTalkMessageSendRequest(
                            token=token,
                            chat_id=chat_id,
                            conversation_type=conversation_type,
                            open_conversation_id=open_conversation_id,
                            file_path=file_path,
                            file_name=file_name,
                        )
                    )
                else:
                    await self._send_file(
                        DingTalkMessageSendRequest(
                            token=token,
                            chat_id=chat_id,
                            conversation_type=conversation_type,
                            open_conversation_id=open_conversation_id,
                            file_path=file_path,
                            file_name=file_name,
                        )
                    )

                self._sent_file_paths_by_req[request_id].add(file_path)
                logger.info(f"钉钉文件已发送: {file_name}")

            except Exception as e:
                logger.error(f"钉钉文件发送失败: {file_name} - {e}")
                # 发送失败时通知用户
                await self._send_file_notification(
                    DingTalkFileNotificationRequest(
                        token=token,
                        chat_id=chat_id,
                        conversation_type=conversation_type,
                        open_conversation_id=open_conversation_id,
                        file_path=file_path,
                        file_name=file_name,
                        error_msg=str(e),
                    )
                )

        # 清理过期的发送记录
        if len(self._sent_file_paths_by_req) > 100:
            keys_to_remove = list(self._sent_file_paths_by_req.keys())[:50]
            for key in keys_to_remove:
                del self._sent_file_paths_by_req[key]

    async def _send_image(self, request: DingTalkImageSendRequest) -> None:
        """发送图片消息"""
        media_id = await self._file_service.upload_media(request.file_path, "image")
        if not media_id:
            raise Exception("图片上传失败")

        msg_param = json.dumps({"photoURL": media_id})
        await self._send_media_message(
            DingTalkMediaMessageRequest(
                token=request.token,
                chat_id=request.chat_id,
                conversation_type=request.conversation_type,
                open_conversation_id=request.open_conversation_id,
                msg_key="sampleImageMsg",
                msg_param=msg_param,
            )
        )

    async def _send_audio(self, request: DingTalkMessageSendRequest) -> None:
        """发送音频消息"""
        media_id = await self._file_service.upload_media(request.file_path, "voice")
        if not media_id:
            raise Exception("音频上传失败")

        msg_param = json.dumps({"mediaId": media_id})
        await self._send_media_message(
            DingTalkMediaMessageRequest(
                token=request.token,
                chat_id=request.chat_id,
                conversation_type=request.conversation_type,
                open_conversation_id=request.open_conversation_id,
                msg_key="sampleAudio",
                msg_param=msg_param,
            )
        )

    async def _send_video(self, request: DingTalkMessageSendRequest) -> None:
        """发送视频消息"""
        media_id = await self._file_service.upload_media(request.file_path, "video")
        if not media_id:
            raise Exception("视频上传失败")

        msg_param = json.dumps({"mediaId": media_id})
        await self._send_media_message(
            DingTalkMediaMessageRequest(
                token=request.token,
                chat_id=request.chat_id,
                conversation_type=request.conversation_type,
                open_conversation_id=request.open_conversation_id,
                msg_key="sampleVideo",
                msg_param=msg_param,
            )
        )

    async def _send_file(self, request: DingTalkMessageSendRequest) -> None:
        """发送文件消息

        使用机器人消息 API 发送文件消息，msgKey 为 sampleFile。
        支持各种文件类型：音频、视频、PPT、Excel、PDF、压缩包等。

        参考：https://open.dingtalk.com/document/orgapp/chatbots-send-one-on-one-chat-messages-in-batches
        """
        # 上传文件获取 mediaId
        media_id = await self._file_service.upload_media(request.file_path, "file")
        if not media_id:
            raise Exception("文件上传失败")

        # 获取文件扩展名用于 fileType
        ext = os.path.splitext(request.file_name)[1].lower().lstrip('.')
        if not ext:
            ext = "stream"

        # 使用机器人 API 发送文件消息
        # msgKey: sampleFile
        # msgParam: {"mediaId": "xxx", "fileName": "xxx", "fileType": "xxx"}
        msg_param = json.dumps({
            "mediaId": media_id,
            "fileName": request.file_name,
            "fileType": ext,
        })

        await self._send_media_message(
            DingTalkMediaMessageRequest(
                token=request.token,
                chat_id=request.chat_id,
                conversation_type=request.conversation_type,
                open_conversation_id=request.open_conversation_id,
                msg_key="sampleFile",
                msg_param=msg_param,
            )
        )
        logger.info(f"[DingTalk] 文件发送成功: {request.file_name} -> {request.chat_id}")

    async def _send_file_notification(self, request: DingTalkFileNotificationRequest) -> None:
        """发送文件通知（Markdown 消息），用于文件发送失败时的备用通知。"""
        # 获取文件大小
        try:
            file_size = os.path.getsize(request.file_path)
            if file_size < 1024:
                size_str = f"{file_size} B"
            elif file_size < 1024 * 1024:
                size_str = f"{file_size / 1024:.1f} KB"
            else:
                size_str = f"{file_size / (1024 * 1024):.1f} MB"
        except Exception:
            size_str = "未知大小"

        if request.error_msg:
            markdown_content = (
                f"### 文件发送失败\n\n"
                f"**文件名**: {request.file_name}\n\n"
                f"**大小**: {size_str}\n\n"
                f"**错误**: {request.error_msg}\n\n"
                f"**路径**: `{request.file_path}`"
            )
        else:
            markdown_content = (
                f"### 文件已生成\n\n"
                f"**文件名**: {request.file_name}\n\n"
                f"**大小**: {size_str}\n\n"
                f"**路径**: `{request.file_path}`"
            )

        msg_param = json.dumps({
            "title": "文件通知",
            "text": markdown_content,
        })
        await self._send_media_message(
            DingTalkMediaMessageRequest(
                token=request.token,
                chat_id=request.chat_id,
                conversation_type=request.conversation_type,
                open_conversation_id=request.open_conversation_id,
                msg_key="sampleMarkdown",
                msg_param=msg_param,
            )
        )

    async def _send_media_message(self, request: DingTalkMediaMessageRequest) -> None:
        """发送媒体消息"""
        if request.conversation_type == "2":
            # 群聊
            url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            data = {
                "robotCode": self.config.client_id,
                "openConversationId": request.open_conversation_id,
                "msgKey": request.msg_key,
                "msgParam": request.msg_param,
            }
        else:
            # 私聊
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            data = {
                "robotCode": self.config.client_id,
                "userIds": [request.chat_id],
                "msgKey": request.msg_key,
                "msgParam": request.msg_param,
            }

        headers = {"x-acs-dingtalk-access-token": request.token}

        if not self._http:
            raise Exception("HTTP 客户端未初始化")

        response = await self._http.post(url, json=data, headers=headers)
        if response.status_code != 200:
            raise Exception(f"发送失败: {response.text}")

    # ==================== 兜底文件发送 ====================

    def _detect_workspace_files(self, text: str, workspace_dir: str) -> list[str]:
        """检测文本中提到的 workspace 文件路径。

        支持两种模式：
        1. 完整绝对路径匹配
        2. 从引号/书名号中提取文件名，在 workspace 下查找

        Args:
            text: 消息文本
            workspace_dir: 工作空间目录

        Returns:
            存在的文件路径列表
        """
        if not workspace_dir or not text:
            return []

        found_files = []

        # 模式1：完整绝对路径匹配
        # 匹配类似 /home/xxx/.jiuwenclaw/agent/workspace/xxx.ext 的路径
        path_pattern = re.compile(
            r'(?:^|["\'「「【《\s])(' + re.escape(workspace_dir) + r'[^\s"\'」」】》]+\.\w{1,10})(?:$|["\'」」】》\s])',
            re.MULTILINE
        )
        for match in path_pattern.finditer(text):
            path = match.group(1).strip()
            if os.path.isfile(path):
                found_files.append(path)

        # 模式2：从引号/书名号中提取文件名
        # 匹配 "filename.ext"、'filename.ext'、「filename.ext」、《filename.ext》
        filename_pattern = re.compile(
            r'["\'「「【《]([^"\'」」】》]+\.\w{1,10})["\'」」】》]'
        )
        for match in filename_pattern.finditer(text):
            filename = match.group(1).strip()
            # 在 workspace 下查找同名文件
            potential_path = os.path.join(workspace_dir, filename)
            if os.path.isfile(potential_path) and potential_path not in found_files:
                found_files.append(potential_path)

        return found_files

    async def _send_fallback_files(self, msg: Message, content: str) -> None:
        """chat.final 兜底文件发送。

        当 LLM 未调用 send_file_to_user 但在回复中提到了文件时，
        自动检测并发送这些文件。
        """
        if not self._file_service or not self.config.send_file_allowed:
            return

        workspace_dir = self.config.workspace_dir or os.path.expanduser("~/.jiuwenclaw/agent/workspace")
        if not os.path.isdir(workspace_dir):
            return

        # 检测文件路径
        file_paths = self._detect_workspace_files(content, workspace_dir)
        if not file_paths:
            return

        # 获取当前 request_id 用于去重
        request_id = getattr(msg, "id", "") or ""
        sent_paths = self._sent_file_paths_by_req.get(request_id, set())

        # 过滤已发送的文件
        new_files = [p for p in file_paths if p not in sent_paths]
        if not new_files:
            return

        logger.info(f"钉钉兜底文件发送: 检测到 {len(new_files)} 个未发送文件")

        # 构造文件消息并发送
        files_payload = [{"path": p, "name": os.path.basename(p)} for p in new_files]
        fallback_msg = Message(
            id=msg.id,
            type=msg.type,
            channel_id=self.name,
            session_id=msg.session_id,
            payload={"event_type": "chat.file", "files": files_payload},
            metadata=msg.metadata,
            chat_id=getattr(msg, "chat_id", None),
        )
        await self._send_file_message(fallback_msg)