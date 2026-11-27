# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import logging
import asyncio
import concurrent.futures
import os
import types
import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

import requests
from pydantic import BaseModel, Field

from jiuwenclaw.channel.base import RobotMessageRouter, BaseChannel
from jiuwenclaw.channel.platform_adapter.message import MessageStore
from jiuwenclaw.schema.message import Message, ReqMethod, EventType
from jiuwenclaw.channel.feishu_file_service import (
    FeishuFileService,
    is_image_file,
    is_audio_file,
    is_video_file,
)



logger = logging.getLogger(__name__)


class FeishuConfig(BaseModel):
    """飞书通道配置模型，使用WebSocket长连接接收消息。"""

    enabled: bool = False  # 是否启用飞书通道
    app_id: str = ""  # 飞书开放平台的应用ID
    app_secret: str = ""  # 飞书开放平台的应用密钥
    encrypt_key: str = ""  # 事件订阅的加密密钥（可选）
    verification_token: str = ""  # 事件订阅的验证令牌（可选）
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户的open_id列表
    enable_streaming: bool = True  # 是否开启流式/过程消息下发
    chat_id: str = ""  # 可选：固定推送目标 chat_id（群聊 oc_xxx 或个人 open_id）
    channel_id: str = "feishu"  # ChannelManager 路由键，支持多实例
    bot_key: str = ""  # 企业飞书多 bot 配置键（仅 feishu_enterprise 使用）
    # 收消息时写入 config.yaml，用于无 metadata 时的回发兜底（与 session_id 解耦）
    last_chat_id: str = ""
    last_open_id: str = ""

    # 文件处理配置
    max_download_size: int = 100 * 1024 * 1024  # 最大下载文件大小（默认100MB）
    download_timeout: int = 60  # 下载超时时间（秒）
    enable_file_upload: bool = True  # 是否启用文件上传功能
    temp_file_dir: str = ""  # 临时文件存储目录，默认使用工作空间

    # 数字分身配置
    my_user_id: str = ""  # 可选：当前数字分身对应的用户open_id
    bot_name: str = ""  # 可选：机器人在群聊中的名称，用于@识别
    group_digital_avatar: bool = False  # 是否启用群聊数字分身功能
    enable_memory: bool = False  # 是否启用群聊记忆功能
    message_merge_window_ms: int = 15000  # 连续消息合并窗口（毫秒）


try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateImageRequest,
        CreateImageRequestBody,
        CreateFileRequest,
        CreateFileRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        Emoji,
        P2ImMessageReceiveV1,
    )
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
        P2CardActionTriggerResponse,
    )

    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None
    Emoji = None

# 非文本消息类型的显示占位符映射
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


class _ThreadLocalLoopProxy:
    """Provide a thread-local event loop proxy for lark_oapi ws client."""

    @staticmethod
    def _get_loop() -> asyncio.AbstractEventLoop:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            pass

        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("event loop is closed")
            return loop
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

    def run_until_complete(self, coro):
        return self._get_loop().run_until_complete(coro)

    def create_task(self, coro):
        return self._get_loop().create_task(coro)


@dataclass
class FeishuInboundMessage:
    """Feishu 入站消息载体，避免参数列表持续膨胀。"""

    message_id: str
    chat_id: str
    content: str
    user_id: str | None = None
    bot_id: str | None = None
    metadata: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


@dataclass
class FeishuMessageSendRequest:
    """飞书消息发送请求参数封装。"""

    receive_id: str
    id_type: str
    msg_type: str
    content: str
    log_label: str = ""
    max_retries: int = 3


class FeishuChannel(BaseChannel):
    """
    飞书/飞书IM通道实现，基于WebSocket长连接。

    特性：
    - 使用WebSocket接收事件，无需公网IP或webhook
    - 支持群聊和私聊消息
    - 自动添加"已读"反应表情
    - 支持Markdown表格渲染为飞书表格元素

    依赖：
    - 飞书开放平台的应用ID和应用密钥
    - 机器人功能已启用
    - 事件订阅已启用（im.message.receive_v1）
    """

    name = "feishu"
    _ws_loop_proxy_lock = threading.Lock()
    _ws_loop_proxy_installed = False

    def __init__(
        self,
        config: FeishuConfig,
        router: RobotMessageRouter,
        im_platform_adapter: Any | None = None,
    ):
        """
        初始化飞书通道实例。

        Args:
            config: 飞书配置对象
            router: 消息路由器实例
            im_platform_adapter: 平台适配器实例（可选，用于数字分身功能）
        """
        super().__init__(config, router)
        self.config: FeishuConfig = config
        self._channel_id = str(getattr(config, "channel_id", "") or self.name).strip() or self.name
        self._api_client: Any = None  # 飞书API客户端（用于发送消息）
        self._websocket_client: Any = None  # WebSocket客户端（用于接收消息）
        self._websocket_thread: threading.Thread | None = None  # WebSocket运行线程
        self._message_dedup_cache: OrderedDict[str, None] = OrderedDict()  # 消息去重缓存
        self._main_loop: asyncio.AbstractEventLoop | None = None  # 主线程事件循环
        self._ws_thread_loop: asyncio.AbstractEventLoop | None = None  # WebSocket线程事件循环
        self._message_callback: Callable[[Message], None] | None = None  # 网关模式回调
        self._im_platform_adapter = im_platform_adapter
        self._message_storage = MessageStore(api_client=None, platform_adapter=self._im_platform_adapter)
        self._pending_message_batches: dict[tuple[str, str], dict[str, Any]] = {}
        self._pending_message_lock = asyncio.Lock()
        self._pending_group_progress_tasks: dict[str, asyncio.Task[None]] = {}
        self._sent_group_progress_requests: set[str] = set()
        self._stopping = False
        # 按 request_id 聚合 chat.delta，避免同一任务被拆分成多条消息发送到飞书。
        self._stream_text_buffers: dict[str, str] = {}
        # 文件服务（延迟初始化）
        self._file_service: FeishuFileService | None = None
        # 按 request_id 记录已通过 chat.file 发送的文件路径，用于兜底去重
        # key=request_id, value=set of absolute file paths
        self._sent_file_paths_by_req: dict[str, set[str]] = {}
        # 自演进用户确认卡片, key=request_id, value=query card
        self._user_question_card: dict[str, dict] = {}

    @property
    def channel_id(self) -> str:
        """返回通道唯一标识符，用于ChannelManager注册与消息派发。"""
        return self._channel_id

    def on_message(self, callback: Callable[[Message], None]) -> None:
        """
        注册消息回调函数，用于Gateway模式。

        当收到消息时调用此回调函数，而非通过router路由。

        Args:
            callback: 消息回调函数
        """
        self._message_callback = callback

    async def _handle_message(
        self,
        inbound: FeishuInboundMessage,
    ) -> None:
        """
        处理接收到的消息并分发。

        若已通过on_message注册网关回调，则直接回调；否则通过router路由消息。

        Args:
            inbound: Feishu 入站消息
        """
        # Message 的 id 必须全局唯一；多 bot 同群时飞书消息 message_id 相同，需加上 channel_id 防止冲突。
        msg_id = f"{self.channel_id}:{inbound.message_id}"
        base_params = {
            "content": inbound.content,
            "query": inbound.content,
            "interactive_ask": True,
        }
        params = base_params if inbound.params is None else {**base_params, **inbound.params}
        params["interactive_ask"] = True
        _meta = inbound.metadata or {}
        _chat_type = _meta.get("chat_type", "")
        _is_group = _chat_type == "group"
        _msg_enable_streaming = self.config.enable_streaming
        _is_stream = self.config.enable_streaming
        if self.config.group_digital_avatar and _is_group:
            _msg_enable_streaming = False
            _is_stream = False
            _meta = dict(_meta)
            _meta["avatar_mode"] = True
            _meta["principal_user_id"] = self._get_target_user_open_id()
            _meta["triggering_user_id"] = str(inbound.user_id or "")
        _effective_group_digital_avatar = self.config.group_digital_avatar and _is_group
        msg = Message(
            id=msg_id,
            type="req",
            channel_id=self.channel_id,
            session_id=str(inbound.chat_id),
            params=params,
            timestamp=time.time(), ok=True,
            provider=self.name,
            chat_id=str(inbound.chat_id or ""),
            user_id=str(inbound.user_id or ""),
            bot_id=str(inbound.bot_id or self.config.app_id or ""),
            req_method=ReqMethod.CHAT_SEND,
            is_stream=_is_stream,
            metadata=_meta,
            group_digital_avatar=_effective_group_digital_avatar,
            enable_memory=self.config.enable_memory,
            enable_streaming=_msg_enable_streaming,
        )
        if self._message_callback:
            self._message_callback(msg)
        else:
            await self.bus.route_user_message(msg)

    async def start(self) -> None:
        """启动飞书机器人，使用WebSocket长连接接收消息。"""
        if not self._validate_start_conditions():
            return

        self._running = True
        self._main_loop = asyncio.get_running_loop()
        self._initialize_api_client()
        self._start_websocket_in_thread()

        logger.info("飞书机器人已启动，使用WebSocket长连接接收消息")
        logger.info("无需公网IP - 通过WebSocket接收事件")

        # 持续运行直到停止
        while self._running:
            await asyncio.sleep(1)

    def _validate_start_conditions(self) -> bool:
        """验证启动所需的条件是否满足。"""
        if not FEISHU_AVAILABLE:
            logger.error("飞书SDK未安装，请先安装 lark_oapi")
            return False

        if not self.config.app_id or not self.config.app_secret:
            logger.error("飞书应用ID或应用密钥未配置")
            return False

        return True

    def _initialize_api_client(self) -> None:
        """初始化飞书API客户端，用于发送消息。"""
        self._api_client = (
            lark.Client.builder()
            .app_id(self.config.app_id)
            .app_secret(self.config.app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        self._message_storage.set_api_client(self._api_client)
        if self._im_platform_adapter and hasattr(self._im_platform_adapter, "set_api_client"):
            self._im_platform_adapter.set_api_client(self._api_client)
        if hasattr(self._message_storage, 'set_platform_adapter'):
            self._message_storage.set_platform_adapter(self._im_platform_adapter)
        # 初始化文件服务
        from jiuwenclaw.utils import get_agent_workspace_dir
        workspace_dir = self.config.temp_file_dir or str(get_agent_workspace_dir())
        self._file_service = FeishuFileService(
            api_client=self._api_client,
            config=self.config,
            workspace_dir=workspace_dir,
        )

    def _start_websocket_in_thread(self) -> None:
        """在独立线程中启动WebSocket客户端，避免事件循环冲突。"""
        self._websocket_client = None
        self._ws_thread_loop = None
        config = {
            "app_id": self.config.app_id,
            "app_secret": self.config.app_secret,
            "encrypt_key": self.config.encrypt_key or "",
            "verification_token": self.config.verification_token or "",
        }

        self._websocket_thread = threading.Thread(
            target=self._run_websocket_client,
            args=(config,),
            daemon=True,
        )
        self._websocket_thread.start()

        # 等待WebSocket客户端创建完成
        self._wait_for_websocket_client_ready()

    def _run_websocket_client(self, config: dict) -> None:
        """
        在子线程中运行WebSocket客户端。

        Args:
            config: WebSocket配置参数
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._ws_thread_loop = loop

        # 将 SDK 的模块级 loop 替换为线程内代理，避免多实例时相互覆盖。
        self._ensure_thread_local_ws_loop_proxy()

        ws_client = None
        try:
            event_handler = (
                lark.EventDispatcherHandler.builder(
                    config["encrypt_key"],
                    config["verification_token"],
                )
                .register_p2_im_message_receive_v1(self._on_message_sync)
                .register_p2_im_message_message_read_v1(self._on_message_read_event)
                .register_p2_card_action_trigger(self._on_card_action_trigger_sync)
                .build()
            )

            ws_client = lark.ws.Client(
                config["app_id"],
                config["app_secret"],
                event_handler=event_handler,
                log_level=lark.LogLevel.INFO,
            )
            self._patch_ws_client_shutdown(ws_client)
            self._websocket_client = ws_client
            ws_client.start()
        except Exception as e:
            if self._stopping or not self._running:
                logger.info("飞书WebSocket线程退出: %s", e)
            else:
                logger.error("飞书WebSocket连接建立失败: %s", e)
        finally:
            self._cleanup_websocket_thread(ws_client, loop)

    @classmethod
    def _ensure_thread_local_ws_loop_proxy(cls) -> None:
        with cls._ws_loop_proxy_lock:
            if cls._ws_loop_proxy_installed:
                return
            import lark_oapi.ws.client as _ws_client_mod
            _ws_client_mod.loop = _ThreadLocalLoopProxy()
            cls._ws_loop_proxy_installed = True

    def _cleanup_websocket_thread(self, ws_client: Any, loop: asyncio.AbstractEventLoop) -> None:
        """清理WebSocket线程资源。"""

        if self._websocket_client is ws_client:
            self._websocket_client = None

        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(asyncio.sleep(0))
        except Exception:
            pass

        try:
            loop.close()
        except Exception:
            pass

        if self._ws_thread_loop is loop:
            self._ws_thread_loop = None

    def _wait_for_websocket_client_ready(self) -> None:
        """等待WebSocket客户端创建完成。"""
        for _ in range(50):
            if self._websocket_client is not None:
                break
            time.sleep(0.1)

    async def stop(self) -> None:
        """停止飞书机器人。"""
        self._running = False
        self._stopping = True
        self._stream_text_buffers.clear()

        if self._websocket_client and self._ws_thread_loop and self._ws_thread_loop.is_running():
            try:
                await self._shutdown_ws_client()
            except Exception as e:
                logger.warning("停止WebSocket客户端时发生异常: {}", e)

        if self._ws_thread_loop and self._ws_thread_loop.is_running():
            self._ws_thread_loop.call_soon_threadsafe(self._ws_thread_loop.stop)

        if self._websocket_thread and self._websocket_thread.is_alive():
            self._websocket_thread.join(timeout=2.0)

        self._websocket_client = None
        self._ws_thread_loop = None
        logger.info("飞书机器人已停止")
        self._stopping = False

    async def _shutdown_ws_client(self) -> None:
        """在飞书 websocket 线程中执行断连与任务清理."""
        loop = self._ws_thread_loop
        ws_client = self._websocket_client
        if loop is None or ws_client is None or not loop.is_running():
            return

        async def _shutdown() -> None:
            try:
                setattr(ws_client, "_auto_reconnect", False)
            except Exception:
                pass

            conn = getattr(ws_client, "_conn", None)
            if conn is not None:
                try:
                    await conn.close(code=1000, reason="bye")
                except Exception as e:
                    logger.debug("飞书连接关闭时出现异常: {}", e)

            await asyncio.sleep(0.05)

        fut = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
        try:
            await asyncio.wait_for(asyncio.wrap_future(fut), timeout=2.0)
        except concurrent.futures.CancelledError:
            pass
        except asyncio.TimeoutError:
            logger.debug("飞书客户端清理超时，继续停止事件循环")
        except Exception as e:
            logger.debug("飞书客户端清理任务异常: {}", e)

    @staticmethod
    def _patch_ws_client_shutdown(ws_client: Any) -> None:
        """修复 lark_oapi 在并发关闭时可能触发的 Lock release 异常."""
        original_disconnect = getattr(ws_client, "_disconnect", None)
        if not callable(original_disconnect):
            return
        if getattr(ws_client, "_disconnect_patched", False):
            return

        async def _safe_disconnect(self):
            try:
                return await original_disconnect()
            except RuntimeError as e:
                if "Lock is not acquired" in str(e):
                    logger.debug("忽略 lark_oapi 断连并发异常: {}", e)
                    return None
                raise

        ws_client._disconnect = types.MethodType(_safe_disconnect, ws_client)
        ws_client._disconnect_patched = True

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        """
        添加消息反应的同步方法（在线程池中运行）。

        Args:
            message_id: 消息ID
            emoji_type: 表情类型
        """
        try:
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                )
                .build()
            )

            response = self._api_client.im.v1.message_reaction.create(request)

            if not response.success():
                logger.warning(
                    f"添加消息反应失败: 错误码={response.code}, 消息={response.msg}"
                )
            else:
                logger.debug("已为消息 %s 添加 %s 表情", message_id, emoji_type)
        except Exception as e:
            logger.warning(f"添加消息反应时发生异常: {e}")

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """
        为消息添加反应表情符号（非阻塞）。

        常见表情符号类型：
        - THUMBSUP: 点赞
        - OK: 确认
        - EYES: 查看
        - DONE: 完成
        - OnIt: 处理中
        - HEART: 爱心

        Args:
            message_id: 消息ID
            emoji_type: 表情类型
        """
        if not self._api_client or not Emoji:
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)

    def _get_target_user_open_id(self) -> str:
        """返回当前数字分身对应的用户 open_id。"""
        return str(
            (self.config.my_user_id or "").strip()
            or os.getenv("MY_USER_ID", "").strip()
        )

    @staticmethod
    def _extract_mentioned_users(message: Any) -> list[dict[str, str]]:
        """从飞书消息事件中提取被 @ 用户的 open_id 和姓名。"""
        mentions = getattr(message, "mentions", None) or []
        users: list[dict[str, str]] = []
        for mention in mentions:
            open_id = ""
            name = str(getattr(mention, "name", "") or "").strip()
            mention_id = getattr(mention, "id", None)
            if isinstance(mention_id, dict):
                open_id = str(mention_id.get("open_id") or mention_id.get("id") or "").strip()
            elif isinstance(mention_id, str):
                open_id = mention_id.strip()
            elif mention_id is not None:
                nested_open_id = getattr(mention_id, "open_id", None)
                if nested_open_id:
                    open_id = str(nested_open_id).strip()
            else:
                open_id = str(getattr(mention, "open_id", "") or "").strip()
            if open_id and all(user["open_id"] != open_id for user in users):
                users.append({"open_id": open_id, "name": name})
        return users

    def _resolve_user_display_name(self, open_id: str, fallback_name: str = "") -> str:
        """优先使用已知姓名，必要时再查询飞书联系人信息。"""
        if fallback_name.strip():
            return fallback_name.strip()
        if self._im_platform_adapter and hasattr(self._im_platform_adapter, 'get_user_name_by_open_id'):
            return self._im_platform_adapter.get_user_name_by_open_id(open_id).strip()
        else:
            return ""

    def _replace_mentions_with_names(self, message: Any, text: str) -> str:
        """
        将消息中的 @mentions 占位符（如 @_user_1）替换为真实用户名。
        当 @all 时，替换为数字分身用户本人的名字。

        Args:
            message: 飞书消息对象
            text: 原始文本内容

        Returns:
            str: 替换后的文本内容
        """
        mentions = getattr(message, "mentions", None) or []

        result = text
        target_user_open_id = self._get_target_user_open_id()
        target_user_name = ""
        if target_user_open_id:
            target_user_name = self._resolve_user_display_name(target_user_open_id, "")

        if "@_all" in result and target_user_name:
            result = result.replace("@_all", f"@{target_user_name}")

        if not mentions:
            return result

        for mention in mentions:
            mention_key = getattr(mention, "key", None)

            if not mention_key:
                mention_id = getattr(mention, "id", None)
                if isinstance(mention_id, dict):
                    mention_key = mention_id.get("key", "")
                elif isinstance(mention_id, str):
                    if "_" in mention_id:
                        mention_key = mention_id.split("_")[-1]

            if not mention_key:
                continue

            name = str(getattr(mention, "name", "") or "").strip()
            if not name:
                bot_name = getattr(message, "bot_name", "") or ""
                name = bot_name or mention_key

            if mention_key.startswith("@"):
                old_pattern = mention_key
            else:
                old_pattern = f"@{mention_key}"
            new_pattern = f"@{name}"
            result = result.replace(old_pattern, new_pattern)

        return result

    _GROUP_PROGRESS_HINT_DELAY_SECONDS = 6.0
    _GROUP_PROGRESS_HINT_TEXTS: tuple[str, ...] = (
        "收到，我先看一下。",
        "我先确认下，马上回复。",
        "这个我在处理，稍等我一下。",
    )

    @staticmethod
    def _should_send_group_ack(metadata: dict[str, Any]) -> bool:
        """仅在待办/提醒类私发场景下，才补发群内短确认。"""
        # 定时任务消息不发送群确认
        if bool(metadata.get("is_cron_job")):
            return False
        if str(metadata.get("reply_scope") or "").strip().lower() != "dm":
            return False
        if str(metadata.get("reply_reason") or "").strip() not in {
            "mentioned_target_user",
            "processor_target_user",
        }:
            return False
        if not str(metadata.get("feishu_chat_id") or "").strip():
            return False
        return bool(metadata.get("reply_personal_action"))

    @staticmethod
    def _fallback_group_ack() -> str:
        """群内短回复兜底文案，保持第一人称口吻。"""
        return "好的，我知道了，会跟进处理。"

    @classmethod
    def _normalize_group_ack_text(cls, target_name: str, text: str) -> str:
        """过滤掉机器人旁白式短回复，尽量保留像用户本人说的话。"""
        normalized = re.split(r"[\r\n]+", (text or "").strip(), maxsplit=1)[0]
        normalized = re.sub(r"\s+", " ", normalized).strip(' "\'""''「」')
        if not normalized:
            return ""

        forbidden_phrases = (
            "已提醒",
            "已通知",
            "私下通知",
            "私聊",
            "转告",
            "提醒了",
            "通知了",
            "告诉了",
            "帮你",
            "帮您",
            "代为",
        )
        if target_name and target_name in normalized:
            return ""
        if any(phrase in normalized for phrase in forbidden_phrases):
            return ""
        return normalized

    def _generate_group_ack_sync(self, target_name: str, content: str) -> str:
        """调用轻量 LLM 生成群内简短确认文案。"""
        api_key = os.getenv("API_KEY", "").strip()
        api_base = os.getenv("API_BASE", "").strip()
        model_name = os.getenv("MODEL_NAME", "").strip() or "GLM-4.7"
        if not api_key or not api_base:
            return self._fallback_group_ack()

        prompt = (
            "你是一个飞书群聊机器人。群里有一条需要{name}关注的消息,"
            "你已经把详细回复私发给了{name}。"
            "现在群里需要补一句很短的话，但这句话必须像{name}本人在群里的直接回复,"
            "而不是机器人旁白或转述。\n\n"
            "要求：\n"
            "- 一句话，不超过30个字\n"
            "- 用第一人称口吻，像当事人本人在说话\n"
            "- 不要提到{name}的名字\n"
            "- 不要写成'我已经提醒了{name}''已通知{name}''我帮{name}处理了'这类机器人/第三人称表达\n"
            "- 更像'好的，我知道了，会准时参加会议''收到，我会跟进这件事'\n"
            "- 不要照搬原文，保留核心动作即可\n\n"
            "你私发给{name}的内容是：\n{content}"
        ).format(   
            name=target_name,
            content=content[:500],
        )
        try:
            resp = requests.post(
                f"{api_base.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_name,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                    "max_tokens": 80,
                },
                timeout=30,
            )
            resp.raise_for_status()
            choices = resp.json().get("choices") or []
            if choices:
                text = (choices[0].get("message") or {}).get("content", "").strip()
                text = self._normalize_group_ack_text(target_name, text)
                if text:
                    return text
        except Exception as e:
            logger.warning("[FeishuChannel] 生成群确认文案失败，使用回退: %s", e)

        return self._fallback_group_ack()

    async def _send_group_ack(self, metadata: dict[str, Any], content: str) -> None:
        """后台生成并发送群内简短确认，不阻塞主回复。"""
        try:
            target_name = str(metadata.get("reply_target_name") or "").strip() or "对方"
            group_chat_id = str(metadata.get("feishu_chat_id") or "").strip()
            if not group_chat_id:
                return

            ack_text = await asyncio.to_thread(
                self._generate_group_ack_sync, target_name, content
            )
            if ack_text:
                card = self._build_card_content(ack_text)
                await self._send_feishu_message(group_chat_id, "chat_id", card, "group_ack")
                logger.info("[FeishuChannel] 群确认已发送: chat_id=%s text=%s", group_chat_id, ack_text[:50])
        except Exception as e:
            logger.warning("[FeishuChannel] 后台群确认发送失败: %s", e)

    @staticmethod
    def _should_send_group_progress_hint(metadata: dict[str, Any]) -> bool:
        """仅对群聊消息展示轻量处理进度。"""
        return (
            str(metadata.get("chat_type") or "").strip() == "group"
            and bool(str(metadata.get("feishu_chat_id") or "").strip())
        )

    def _build_group_progress_hint_text(self, metadata: dict[str, Any]) -> str:
        """根据场景挑选一条尽量自然的群内处理提示。"""
        try:
            merged_count = int(metadata.get("merged_count", 1) or 1)
        except (TypeError, ValueError):
            merged_count = 1

        if str(metadata.get("reply_scope") or "").strip().lower() == "dm":
            return self._GROUP_PROGRESS_HINT_TEXTS[1]
        if merged_count > 1:
            return self._GROUP_PROGRESS_HINT_TEXTS[2]
        return self._GROUP_PROGRESS_HINT_TEXTS[0]

    def _clear_group_progress_state(self, request_id: str) -> None:
        """清理指定请求的延迟提示任务和已发送标记。"""
        pending_task = self._pending_group_progress_tasks.pop(request_id, None)
        if pending_task and not pending_task.done():
            pending_task.cancel()
        self._sent_group_progress_requests.discard(request_id)

    def _should_skip_group_progress_scheduling(self, request_id: str, metadata: dict[str, Any]) -> bool:
        """判断是否应该跳过群内处理提示的调度。"""
        if not request_id:
            return True
        if request_id in self._pending_group_progress_tasks:
            return True
        if request_id in self._sent_group_progress_requests:
            return True
        if not self._should_send_group_progress_hint(metadata):
            return True
        return False

    def _schedule_group_progress_hint(self, request_id: str, metadata: dict[str, Any]) -> None:
        """为慢请求安排一条延迟发送的群内处理提示。"""
        if self._should_skip_group_progress_scheduling(request_id, metadata):
            return

        task = asyncio.create_task(
            self._send_group_progress_hint_after_delay(request_id, dict(metadata)),
            name=f"feishu-progress-{request_id}",
        )
        self._pending_group_progress_tasks[request_id] = task

    async def _send_group_progress_hint_after_delay(
        self, request_id: str, metadata: dict[str, Any]
    ) -> None:
        """仅在请求持续较久时，补发一条极短群内处理提示。"""
        try:
            await asyncio.sleep(self._GROUP_PROGRESS_HINT_DELAY_SECONDS)
            if self._pending_group_progress_tasks.get(request_id) is not asyncio.current_task():
                return
            if request_id in self._sent_group_progress_requests:
                return
            if not self._should_send_group_progress_hint(metadata):
                return

            group_chat_id = str(metadata.get("feishu_chat_id") or "").strip()
            hint_text = self._build_group_progress_hint_text(metadata)
            if not group_chat_id or not hint_text:
                return

            card = self._build_card_content(hint_text)
            await self._send_feishu_message(
                group_chat_id,
                "chat_id",
                card,
                f"group_progress:{request_id}",
            )
            self._sent_group_progress_requests.add(request_id)
            logger.info(
                "[FeishuChannel] 群进度提示已发送: request_id=%s chat_id=%s text=%s",
                request_id,
                group_chat_id,
                hint_text,
            )
        except Exception as e:
            logger.warning("[FeishuChannel] 群进度提示发送失败: %s", e)
        finally:
            if self._pending_group_progress_tasks.get(request_id) is asyncio.current_task():
                self._pending_group_progress_tasks.pop(request_id, None)

    async def _handle_processing_status_event(
        self, msg: Message, metadata: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        """处理网关发出的 processing_status 事件。"""
        request_id = str(msg.id or "").strip()
        if not request_id or not self._should_send_group_progress_hint(metadata):
            return

        if bool(payload.get("is_processing")):
            self._schedule_group_progress_hint(request_id, metadata)
            return

        self._clear_group_progress_state(request_id)

    def _build_reply_metadata(
        self, *, message: Any, sender_open_id: str, base_metadata: dict[str, Any]
    ) -> dict[str, Any]:
        """根据群聊上下文补充默认的回复投递意图。"""
        if not self.config.group_digital_avatar:
            return dict(base_metadata)
        metadata = dict(base_metadata)
        target_user_open_id = self._get_target_user_open_id()
        mentioned_users = self._extract_mentioned_users(message)
        mentioned_open_ids = [user["open_id"] for user in mentioned_users]

        message_content = ""
        try:
            content_obj = json.loads(message.content or "{}")
            message_content = content_obj.get("text", "") or ""
        except Exception:
            message_content = str(message.content or "")

        mention_all = "@_all" in message_content
        if mention_all and target_user_open_id and target_user_open_id not in mentioned_open_ids:
            mentioned_open_ids.append(target_user_open_id)

        if mentioned_open_ids:
            metadata["mentioned_open_ids"] = mentioned_open_ids
            metadata["im_mentioned_user_ids"] = mentioned_open_ids

        is_group_chat = str(getattr(message, "chat_type", "") or "").strip() == "group"
        if not is_group_chat or not target_user_open_id:
            return metadata

        if target_user_open_id in mentioned_open_ids:
            target_user_name = ""
            for user in mentioned_users:
                if user["open_id"] == target_user_open_id:
                    target_user_name = self._resolve_user_display_name(
                        target_user_open_id, user.get("name", "")
                    )
                    break
            metadata["reply_candidate_feishu_open_id"] = target_user_open_id
            metadata["reply_candidate_reason"] = "mentioned_target_user"
            if target_user_name:
                metadata["reply_target_name"] = target_user_name
            return metadata

        if sender_open_id == target_user_open_id:
            metadata["reply_scope"] = "dm"
            metadata["reply_feishu_open_id"] = target_user_open_id
            metadata["reply_reason"] = "sender_is_target_user"
            return metadata

        return metadata

    @staticmethod
    def _is_control_message(content: str) -> bool:
        from jiuwenclaw.gateway.slash_command import is_control_like_for_im_batching

        return is_control_like_for_im_batching(content)

    async def _enqueue_message_batch(
        self,
        *,
        chat_id: str,
        open_id: str,
        content: str,
        timestamp_ms: int,
        metadata: dict[str, Any],
        params: dict[str, Any] | None = None,
    ) -> None:
        """将同一用户的连续消息做短暂聚合，再统一交给 gateway 入站链路。"""
        if self._is_control_message(content):
            if content == "/mode team":
                await self._process_batched_message(
                chat_id=chat_id,
                open_id=open_id,
                merged_content="/new_session",
                timestamp_ms=timestamp_ms,
                metadata=metadata,
                params=params,
                )
            await self._process_batched_message(
                chat_id=chat_id,
                open_id=open_id,
                merged_content=content,
                timestamp_ms=timestamp_ms,
                metadata=metadata,
                params=params,
            )
            return

        merge_window_ms = max(int(self.config.message_merge_window_ms or 0), 0)
        if merge_window_ms <= 0:
            await self._process_batched_message(
                chat_id=chat_id,
                open_id=open_id,
                merged_content=content,
                timestamp_ms=timestamp_ms,
                metadata=metadata,
                params=params,
            )
            return

        batch_key = (chat_id, open_id or "")
        async with self._pending_message_lock:
            batch = self._pending_message_batches.get(batch_key)
            if batch is None:
                batch = {
                    "chat_id": chat_id,
                    "open_id": open_id,
                    "items": [],
                    "flush_task": None,
                }
                self._pending_message_batches[batch_key] = batch

            batch["items"].append(
                {
                    "content": content,
                    "timestamp_ms": timestamp_ms,
                    "metadata": dict(metadata),
                    "params": params,
                }
            )

            old_task = batch.get("flush_task")
            if old_task and not old_task.done():
                old_task.cancel()

            delay_seconds = merge_window_ms / 1000
            batch["flush_task"] = asyncio.create_task(
                self._flush_message_batch_after_delay(batch_key, delay_seconds)
            )

    async def _flush_message_batch_after_delay(
        self, batch_key: tuple[str, str], delay_seconds: float
    ) -> None:
        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            return

        async with self._pending_message_lock:
            batch = self._pending_message_batches.get(batch_key)
            if not batch or batch.get("flush_task") is not asyncio.current_task():
                return
            self._pending_message_batches.pop(batch_key, None)

        items = batch.get("items") or []
        if not items:
            return

        merged_content = "\n".join(
            item["content"].strip() for item in items if item.get("content", "").strip()
        ).strip()
        if not merged_content:
            return

        last_item = items[-1]
        merged_metadata = dict(last_item["metadata"])
        merged_metadata["merged_message_ids"] = [
            item["metadata"].get("message_id", "") for item in items
        ]
        merged_metadata["merged_msg_types"] = [
            item["metadata"].get("msg_type", "") for item in items
        ]
        merged_metadata["merged_count"] = len(items)
        for key in (
            "reply_scope",
            "reply_feishu_open_id",
            "reply_feishu_chat_id",
            "reply_reason",
            "reply_target_name",
        ):
            for item in reversed(items):
                value = item["metadata"].get(key)
                if isinstance(value, str) and value.strip():
                    merged_metadata[key] = value
                    break

        mentioned_open_ids: list[str] = []
        for item in items:
            for open_id in item["metadata"].get("mentioned_open_ids", []) or []:
                if open_id and open_id not in mentioned_open_ids:
                    mentioned_open_ids.append(open_id)
        if mentioned_open_ids:
            merged_metadata["mentioned_open_ids"] = mentioned_open_ids

        # 取最后一个消息的 params（文件通常单独发送）
        last_params = last_item.get("params")
        # 如果最后一个消息没有 params，从 items 中找第一个有 files 的
        if not last_params:
            for item in reversed(items):
                if item.get("params") and item["params"].get("files"):
                    last_params = item["params"]
                    break

        if len(items) > 1:
            logger.info(
                "[FeishuChannel] 合并连续消息: chat_id=%s open_id=%s count=%s",
                batch.get("chat_id", ""),
                batch.get("open_id", ""),
                len(items),
            )

        await self._process_batched_message(
            chat_id=batch["chat_id"],
            open_id=batch["open_id"],
            merged_content=merged_content,
            timestamp_ms=int(last_item["timestamp_ms"]),
            metadata=merged_metadata,
            params=last_params,
        )

    async def _process_batched_message(
        self,
        *,
        chat_id: str,
        open_id: str,
        merged_content: str,
        timestamp_ms: int,
        metadata: dict[str, Any],
        params: dict[str, Any] | None = None,
    ) -> None:
        """对单条或合并后的消息做平台整理后转发。"""
        chat_type = metadata.get("chat_type", "")
        is_group_chat = chat_type == "group"

        enriched_metadata = dict(metadata)
        enriched_metadata["timestamp_ms"] = timestamp_ms
        enriched_metadata["im_platform"] = "feishu"
        enriched_metadata["im_chat_type"] = "group" if is_group_chat else "direct"
        enriched_metadata["im_sender_user_id"] = open_id
        enriched_metadata["im_thread_id"] = chat_id

        inbound = FeishuInboundMessage(
            message_id=enriched_metadata.get("message_id", ""),
            chat_id=chat_id,
            content=merged_content,
            user_id=open_id,
            bot_id=self.config.app_id or "",
            metadata=enriched_metadata,
            params=params,
        )
        await self._handle_message(inbound)

    # Markdown表格正则表达式（标题行+分隔符行+数据行）
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    @staticmethod
    def _parse_markdown_table(table_text: str) -> dict | None:
        """
        将Markdown表格解析为飞书表格元素。

        Args:
            table_text: Markdown表格文本

        Returns:
            dict: 飞书表格元素，解析失败返回None
        """
        lines = [
            line.strip() for line in table_text.strip().split("\n") if line.strip()
        ]
        if len(lines) < 3:
            return None

        def split_line(line):
            return [c.strip() for c in line.strip("|").split("|")]

        headers = split_line(lines[0])
        rows = [split_line(line) for line in lines[2:]]

        columns = [
            {"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
            for i, h in enumerate(headers)
        ]

        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [
                {f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))}
                for r in rows
            ],
        }

    def _build_feishu_card_elements(self, content: str) -> list[dict]:
        """
        将内容分割为Markdown和表格元素，用于构建飞书卡片。

        Args:
            content: 要处理的内容

        Returns:
            list[dict]: 飞书卡片元素列表
        """
        elements, last_end = [], 0

        for m in self._TABLE_RE.finditer(content):
            before = content[last_end : m.start()].strip()
            if before:
                # 转换非表格内容为富文本 div 元素
                elements.extend(self._markdown_to_feishu_elements(before))

            elements.append(
                self._parse_markdown_table(m.group(1))
                or self._markdown_to_feishu_elements(m.group(1))
            )
            last_end = m.end()

        remaining = content[last_end:].strip()
        if remaining:
            elements.extend(self._markdown_to_feishu_elements(remaining))

        return elements or self._markdown_to_feishu_elements(content)

    def _markdown_to_feishu_elements(self, md_content: str) -> list[dict]:
        """
        将 Markdown 内容转换为飞书卡片元素列表。

        Args:
            md_content: Markdown 内容

        Returns:
            list[dict]: 飞书卡片元素列表
        """
        elements = []
        lines = md_content.split('\n')
        current_text = []

        for line in lines:
            stripped = line.strip()

            # 处理标题
            if stripped.startswith('## '):
                if current_text:
                    elements.append(self._create_div_element('\n'.join(current_text)))
                    current_text = []
                elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{stripped[3:]}**"
                    }
                })
            elif stripped.startswith('### '):
                if current_text:
                    elements.append(self._create_div_element('\n'.join(current_text)))
                    current_text = []
                elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{stripped[4:]}**"
                    }
                })
            elif stripped.startswith('# '):
                if current_text:
                    elements.append(self._create_div_element('\n'.join(current_text)))
                    current_text = []
                elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{stripped[2:]}**"
                    }
                })
            # 处理分隔线
            elif stripped == '---':
                if current_text:
                    elements.append(self._create_div_element('\n'.join(current_text)))
                    current_text = []
                elements.append({"tag": "hr"})
            # 处理引用块
            elif stripped.startswith('> '):
                current_text.append(stripped[2:])
            # 处理列表项
            elif stripped.startswith('- ') or stripped.startswith('* '):
                current_text.append(f"• {stripped[2:]}")
            elif re.match(r'^\d+\. ', stripped):
                current_text.append(stripped)
            else:
                current_text.append(line)

        if current_text:
            elements.append(self._create_div_element('\n'.join(current_text)))

        return elements if elements else [{"tag": "div", "text": {"tag": "lark_md", "content": md_content}}]

    def _create_div_element(self, content: str) -> dict:
        """
        创建飞书 div 元素。

        Args:
            content: 文本内容

        Returns:
            dict: 飞书 div 元素
        """
        # 处理内联格式：粗体、斜体、代码等
        formatted = content
        # 保留粗体和斜体
        # 处理行内代码
        formatted = re.sub(r'`([^`]+)`', r'`\1`', formatted)
        # 处理链接
        formatted = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'[\1](\2)', formatted)

        return {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": formatted
            }
        }

    async def send(self, msg: Message) -> None:
        """
        通过飞书发送消息。

        Args:
            msg: 要发送的消息对象
        """
        if not self._api_client:
            logger.warning("飞书客户端未初始化")
            return

        try:
            payload = msg.payload if isinstance(msg.payload, dict) else {}
            event_name = getattr(msg.event_type, "value", None) or payload.get("event_type") or ""
            stream_key = str(getattr(msg, "id", "") or "")
            _msg_streaming = getattr(msg, "enable_streaming", None)
            streaming_enabled = bool(_msg_streaming if _msg_streaming is not None else self.config.enable_streaming)

            meta = dict(getattr(msg, "metadata", None) or {})

            # 处理文件消息
            if msg.event_type == EventType.CHAT_FILE or msg.event_type == EventType.CHAT_REASONING:
                if self.config.enable_file_upload and self._file_service:
                    await self._send_file_message(msg)
                return

            # 处理媒体消息
            if msg.event_type == EventType.CHAT_MEDIA:
                if self.config.enable_file_upload and self._file_service:
                    await self._send_media_message(msg)
                return

            # 处理team message
            if msg.event_type == EventType.TEAM_MESSAGE:
                await self._send_team_message(msg)
                return

            # 处理用户询问消息（发送确认卡片）
            if msg.event_type == EventType.CHAT_ASK_USER_QUESTION:
                await self._send_ask_user_question_card(msg)
                return

            # 流式增量：先缓存；若开启流式则实时发送，否则仅缓存不发送。
            if event_name == "chat.delta":
                delta = self._extract_message_content(msg)
                if delta and stream_key:
                    self._stream_text_buffers[stream_key] = (
                        self._stream_text_buffers.get(stream_key, "") + delta
                    )
                return

            # 非 streaming 模式下仅下发最终结果，屏蔽执行过程类事件。
            if (not streaming_enabled) and event_name in {
                "chat.tool_call",
                "chat.tool_calls.delta",
                "chat.tool_result",
                "todo.updated",
            }:
                return

            # 流式结束兜底：有些场景不会携带非空 chat.final，使用 processing_status=false 冲刷缓存。
            if event_name == "chat.processing_status":
                is_processing = payload.get("is_processing")
                if is_processing is not False:
                    if not streaming_enabled:
                        await self._handle_processing_status_event(msg, meta, payload)
                        return
                    content_str = self._extract_message_content(msg)
                    if not content_str.strip():
                        return
                else:
                    content_str = self._stream_text_buffers.pop(stream_key, "")
                    if not content_str.strip():
                        if not streaming_enabled:
                            await self._handle_processing_status_event(msg, meta, payload)
                            return
                        content_str = self._extract_message_content(msg)
                        if not content_str.strip():
                            return
            else:
                buffered_text = ""
                if event_name == "chat.final":
                    buffered_text = self._stream_text_buffers.pop(stream_key, "")
                elif event_name in {"chat.error", "chat.interrupt_result"}:
                    self._stream_text_buffers.pop(stream_key, None)
                content_str = self._extract_message_content(msg)
                is_complete = msg.payload.get("is_complete", False)
                # chat.final 常带空 content 且未必含 is_complete=True；但必须与上方 pop 出的流式缓冲合并，
                # 否则缓冲已被弹出却未并入 content_str，会导致正文丢失（仅依赖 processing_status 冲刷时更明显）。
                if event_name == "chat.final" or is_complete:
                    content_str = self._merge_stream_and_final_content(
                        buffered_text,
                        content_str,
                    )

            receive_id, id_type = self._extract_receive_info(msg)
            payload = getattr(msg, "payload", None) or {}
            skills_card_content = self._build_skills_list_card_content(payload, event_name)
            if skills_card_content:
                request_id = str(msg.id or "").strip()
                if request_id and msg.event_type != EventType.HEARTBEAT_RELAY:
                    self._clear_group_progress_state(request_id)
                await self._send_feishu_message(receive_id, id_type, skills_card_content, msg.id)
                return
            if (
                msg.event_type == EventType.HEARTBEAT_RELAY
                and isinstance(payload, dict)
                and payload.get("heartbeat")
            ):
                content_str = str(payload.get("heartbeat"))

            if not content_str.strip():
                logger.warning("飞书发送：消息内容为空，跳过发送")
                return

            request_id = str(msg.id or "").strip()
            if request_id and msg.event_type != EventType.HEARTBEAT_RELAY:
                self._clear_group_progress_state(request_id)

            # 过滤群聊消息中的用户敏感信息
            content_str = self._filter_user_info_for_group(content_str, meta)

            # 兜底：chat.final 中提到了 workspace 文件但 LLM 未调用 send_file_to_user 时，
            # 自动提取文件路径并发送，避免用户收不到文件。
            if (
                event_name == "chat.final"
                and self.config.enable_file_upload
                and self._file_service
            ):
                req_id = str(getattr(msg, "id", "") or "")
                already_sent = self._sent_file_paths_by_req.get(req_id, set())
                fs_sid = str(getattr(msg, "session_id", None) or "").strip() or "default"
                detected_files = [
                    fp for fp in self._detect_workspace_files(content_str, fs_sid)
                    if os.path.abspath(fp) not in already_sent
                ]
                if detected_files:
                    logger.info(
                        "飞书兜底文件发送：从 chat.final 中检测到 %d 个未发送文件: %s",
                        len(detected_files),
                        detected_files,
                    )
                    for fp in detected_files:
                        try:
                            if is_image_file(fp):
                                await self._send_image_message(receive_id, id_type, fp)
                            elif is_audio_file(fp):
                                await self._send_audio_message(receive_id, id_type, fp, os.path.basename(fp))
                            elif is_video_file(fp):
                                await self._send_video_message(receive_id, id_type, fp, os.path.basename(fp))
                            else:
                                await self._send_file_card(receive_id, id_type, fp, os.path.basename(fp))
                        except Exception as file_err:
                            logger.error("飞书兜底文件发送失败: %s %s", fp, file_err)
            
            # 群聊数字分身回复到群聊时，@发送人
            if msg.group_digital_avatar and id_type == "chat_id":
                sender_open_id = str(
                    meta.get("im_sender_user_id")
                    or meta.get("open_id")
                    or ""
                ).strip()
                if sender_open_id and not sender_open_id.startswith("bot"):
                    content_str = f"<at id={sender_open_id}></at>\n{content_str}"

            card_content = self._build_card_content(content_str)
            await self._send_feishu_message(receive_id, id_type, card_content, msg.id)

            # 用LLM在群内补发简短回复
            if msg.group_digital_avatar and self._should_send_group_ack(meta):
                group_chat_id = str(meta.get("feishu_chat_id") or "").strip()
                if group_chat_id and group_chat_id != receive_id:
                    asyncio.create_task(self._send_group_ack(meta, content_str))

            try:
                chat_id = ""

                if not chat_id:
                    meta = getattr(msg, "metadata", None) or {}
                    reply_scope = str(meta.get("reply_scope") or "").strip().lower()

                    if reply_scope == "dm" and id_type == "open_id":
                        chat_id = receive_id
                    elif id_type == "chat_id":
                        chat_id = receive_id
                    else:
                        chat_id = meta.get("feishu_chat_id") or ""

                # 记录机器人回复消息到群聊历史中去（仅数字分身模式）
                if chat_id and self.config.group_digital_avatar:
                    self._message_storage.add_message_to_memory(
                            chat_id=chat_id,
                            message={
                            "message_id": f"bot_{int(time.time() * 1000)}_{msg.id}",
                            "content": content_str,
                            "timestamp": int(time.time() * 1000),
                            "msg_type": "interactive",
                            "open_id": f"bot_{self.config.app_id}",
                            "chat_type": "bot_reply",
                        }
                    )
            except Exception as e:
                logger.warning(f"记录机器人回复消息失败: {e}")

        except Exception as e:
            logger.error(f"发送飞书消息时发生异常: {e}")

    def _detect_workspace_files(self, text: str, session_id: str | None = None) -> list[str]:
        """从文本中提取 workspace 下实际存在的文件路径。

        用于兜底检测 LLM 提到但未通过 send_file_to_user 发送的文件。
        支持两种模式：
        1. 完整绝对路径：/home/xxx/.jiuwenclaw/agent/workspace/xxx.docx
        2. 仅文件名：'xxx.docx' 或 "xxx.docx"——在 workspace 目录下查找
        """
        from jiuwenclaw.agentserver.session_metadata import get_resolved_project_dir
        from jiuwenclaw.utils import get_agent_sessions_dir

        sid = (session_id or "").strip() or "default"
        workspace_dir = get_resolved_project_dir(sid, str(get_agent_sessions_dir()))

        seen: set[str] = set()
        result: list[str] = []

        # 模式1：完整路径
        abs_pattern = r"(/home/[^\s\[\]\"']+\.jiuwenclaw/agent/workspace/[^\s\[\]\"']+\.\w+)"
        for m in re.findall(abs_pattern, text):
            m = m.rstrip(".,;:!?)")
            if m not in seen and os.path.isfile(m):
                seen.add(m)
                result.append(m)

        # 模式2：从引号或书名号中提取文件名，在 workspace 下查找
        # 匹配 '文件名.ext'、"文件名.ext"、《文件名.ext》
        name_pattern = (
            r"""['"'《]([^'"'》\n]+\."""
            r"""(?:docx?|xlsx?|pptx?|pdf|csv|txt|md|zip|tar|gz|"""
            r"""png|jpg|jpeg|gif|mp3|mp4|wav|opus))['"'》]"""
        )  # noqa: E501
        for fname in re.findall(name_pattern, text, re.IGNORECASE):
            fpath = os.path.join(workspace_dir, fname)
            if fpath not in seen and os.path.isfile(fpath):
                seen.add(fpath)
                result.append(fpath)

        return result

    @staticmethod
    def _merge_stream_and_final_content(stream_text: str, final_text: str) -> str:
        """合并流式累积文本和 final 文本，优先保留信息更完整的一侧。"""
        stream_text = stream_text or ""
        final_text = final_text or ""
        if not stream_text.strip():
            return final_text
        if not final_text.strip():
            return stream_text
        if stream_text == final_text:
            return final_text
        if final_text.startswith(stream_text):
            return final_text
        if stream_text.startswith(final_text):
            return stream_text
        return final_text if len(final_text) >= len(stream_text) else stream_text

    def _extract_receive_info(self, msg: Message) -> tuple[str, str]:
        """
        从消息对象中提取接收者ID和ID类型。
        优先使用 metadata 中的平台身份（feishu_chat_id / feishu_open_id），
        避免 \new_session 覆盖 session_id 后导致 Invalid ids。

        Args:
            msg: 消息对象

        Returns:
            tuple: (接收者ID, ID类型)
        """
        meta = getattr(msg, "metadata", None) or {}
        receive_id = ""
        id_type = "open_id"

        # 0) 群聊数字分身场景：仅当 outbound pipeline 确认 reply_scope=dm 时，
        #    才用 reply_candidate_feishu_open_id 作为私发目标；
        #    否则 candidate 仅作为候选，不影响实际发送路由。
        reply_scope = str(meta.get("reply_scope") or "").strip().lower()
        if reply_scope == "dm":
            # 优先使用 reply_candidate_feishu_open_id（由 outbound pipeline 设置）
            # 兼容 reply_feishu_open_id（由入站阶段 sender_is_target_user 场景设置）
            reply_candidate_open_id = (
                meta.get("reply_candidate_feishu_open_id")
                or meta.get("reply_feishu_open_id")
                or ""
            ).strip()
            if reply_candidate_open_id:
                receive_id = reply_candidate_open_id
                id_type = "open_id"

        # 1) 优先用 metadata 中的平台身份
        if not receive_id:
            feishu_chat_id = (meta.get("feishu_chat_id") or "").strip()
            feishu_open_id = (meta.get("feishu_open_id") or "").strip()

            if feishu_chat_id:
                receive_id = feishu_chat_id
                id_type = "chat_id" if feishu_chat_id.startswith("oc_") else "open_id"
            elif feishu_open_id:
                receive_id = feishu_open_id
                id_type = "open_id"

        # 2) 若 metadata 中没有平台身份，则使用配置中的 chat_id 作为固定推送目标
        if not receive_id:
            cfg_chat_id = getattr(self.config, "chat_id", "") or ""
            cfg_chat_id = cfg_chat_id.strip()
            if cfg_chat_id:
                receive_id = cfg_chat_id
                id_type = "chat_id" if cfg_chat_id.startswith("oc_") else "open_id"

        # 2b) 最近一次会话（收消息时写入 config / 内存），避免 supplement 等路径丢 metadata 后误用 session_id
        if not receive_id:
            last_cid = str(getattr(self.config, "last_chat_id", "") or "").strip()
            last_oid = str(getattr(self.config, "last_open_id", "") or "").strip()
            if last_cid:
                receive_id = last_cid
                id_type = "chat_id" if last_cid.startswith("oc_") else "open_id"
            elif last_oid:
                receive_id = last_oid
                id_type = "open_id"

        # 3) 仍然没有，则回退到 session_id / id（兼容旧逻辑）
        if not receive_id:
            receive_id = getattr(msg, "session_id", None) or msg.id or ""
            if receive_id.startswith("oc_"):
                id_type = "chat_id"
            else:
                id_type = "open_id"
            logger.warning(f"飞书回发未找到有效平台身份，使用回退值: {receive_id}")

        return receive_id, id_type

    def _extract_message_content(self, msg: Message) -> str:
        """
        从消息对象中提取内容字符串。

        Args:
            msg: 消息对象

        Returns:
            str: 消息内容字符串
        """
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        event_name = getattr(msg.event_type, "value", None) or payload.get("event_type") or ""

        if event_name == "chat.tool_call":
            tool_info = payload.get("tool_call", payload)
            if isinstance(tool_info, dict):
                tool_name = tool_info.get("tool_name") or tool_info.get("name") or "unknown_tool"
                args = (
                    tool_info.get("arguments")
                    or tool_info.get("args")
                    or tool_info.get("input")
                    or tool_info.get("params")
                )
                args_text = self._truncate_text(self._extract_preferred_text(args), max_len=240)
                return f"[工具调用] {tool_name}" if not args_text else f"[工具调用] {tool_name}\n参数: {args_text}"
            tool_text = self._extract_preferred_text(tool_info)
            tool_text = self._truncate_text(tool_text, max_len=160)
            return f"[工具调用] {tool_text}" if tool_text else "[工具调用]"

        if event_name == "chat.tool_result":
            tool_name = payload.get("tool_name") or "unknown_tool"
            result_text = self._extract_tool_result_text(payload.get("result"))
            return f"[工具结果] {tool_name}" if not result_text else f"[工具结果] {tool_name}\n{result_text}"

        if event_name == "todo.updated":
            todos = payload.get("todos")
            if not isinstance(todos, list) or not todos:
                return "[待办更新]"
            total = len(todos)
            completed = 0
            running = 0
            pending = 0
            cancelled = 0
            for item in todos:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status", "")).strip().lower()
                if status == "completed":
                    completed += 1
                elif status == "running":
                    running += 1
                elif status in ("cancelled", "canceled"):
                    cancelled += 1
                else:
                    # waiting/pending/unknown 统一归为待处理
                    pending += 1
            return (
                f"[待办更新] 已完成 {completed}/{total}"
                f"｜进行中 {running}"
                f"｜待处理 {pending}"
                f"｜已取消 {cancelled}"
            )

        if event_name == "chat.error":
            error_text = self._extract_preferred_text(payload.get("error"))
            return f"[错误] {error_text}" if error_text else "[错误] 未知错误"

        if event_name == "chat.processing_status":
            is_processing = payload.get("is_processing")
            if is_processing is True:
                return "[状态] 处理中"
            if is_processing is False:
                return "[状态] 已完成"
            return ""

        if event_name == "chat.interrupt_result":
            return self._extract_preferred_text(payload.get("message")) or "[状态] 任务已中断"

        if event_name == "heartbeat.relay":
            return self._extract_preferred_text(payload.get("heartbeat"))

        # Gateway/Agent 响应在 payload.content，直接发送可能在 params.content
        content_str = (msg.params or {}).get("content") or payload.get("content") or ""
        if isinstance(content_str, dict):
            content_str = content_str.get("output", content_str)
        text = self._truncate_text(self._extract_preferred_text(content_str), max_len=4000)
        if text:
            return text

        # 最后仅尝试提取可读字段，不再整包透传 JSON，避免渠道侧出现原始结构化噪音。
        return self._extract_preferred_text(payload if payload else msg.payload)

    def _extract_tool_result_text(self, value: Any) -> str:
        """提取工具结果可读摘要，限制长度，避免飞书消息过载。"""
        if isinstance(value, dict):
            for key in ("summary", "message", "output", "result", "content", "text", "error"):
                if key in value:
                    text = self._extract_preferred_text(value.get(key))
                    if text:
                        return self._truncate_text(text, max_len=600)
        text = self._extract_preferred_text(value)
        return self._truncate_text(text, max_len=600)

    @staticmethod
    def _extract_preferred_text(value: Any) -> str:
        """从结构化数据中提取可读文本，避免直接发送 JSON."""
        if value is None:
            return ""
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return ""
            if (
                (text.startswith("{") and text.endswith("}"))
                or (text.startswith("[") and text.endswith("]"))
            ):
                try:
                    parsed = json.loads(text)
                except Exception:
                    # 兼容 Python dict 字符串
                    match = re.search(
                        r"['\"](output|content|text|message|result|error|summary)['\"]\s*:\s*['\"](.+?)['\"]",
                        text,
                        flags=re.DOTALL,
                    )
                    return match.group(2).strip() if match else ""
                extracted = FeishuChannel._extract_preferred_text(parsed)
                return extracted or ""
            return text

        if isinstance(value, dict):
            for key in ("output", "content", "text", "message", "result", "error", "summary"):
                if key in value:
                    extracted = FeishuChannel._extract_preferred_text(value.get(key))
                    if extracted:
                        return extracted
            return ""

        if isinstance(value, list):
            parts: list[str] = []
            for item in value[:3]:
                extracted = FeishuChannel._extract_preferred_text(item)
                if extracted:
                    parts.append(extracted)
            return "\n".join(parts).strip()

        return str(value).strip()

    @staticmethod
    def _truncate_text(text: str, max_len: int = 240) -> str:
        text = (text or "").strip()
        if len(text) <= max_len:
            return text
        return text[:max_len].rstrip() + "..."

    @staticmethod
    def _stringify_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            try:
                return json.dumps(value, ensure_ascii=False)
            except Exception:
                return str(value)
        return str(value)

    _IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".bmp"})

    @staticmethod
    def _is_image_file(path: str) -> bool:
        """Check if file path has an image extension supported by Feishu."""
        ext = os.path.splitext(path)[1].lower()
        return ext in FeishuChannel._IMAGE_EXTENSIONS

    def _upload_image(self, abs_path: str) -> str | None:
        """Upload an image to Feishu and return the image_key, or None on failure."""
        if not self._api_client:
            return None
        try:
            with open(abs_path, "rb") as f:
                request = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(f)
                        .build()
                    )
                    .build()
                )
                response = self._api_client.im.v1.image.create(request)

            if not response.success():
                logger.warning(
                    "飞书图片上传失败: code=%s msg=%s path=%s",
                    response.code, response.msg, abs_path,
                )
                return None

            image_key = getattr(response.data, "image_key", None)
            if image_key:
                logger.info("飞书图片上传成功: image_key=%s", image_key)
            return image_key
        except FileNotFoundError:
            logger.warning("飞书图片上传失败: 文件不存在 path=%s", abs_path)
            return None
        except Exception as e:
            logger.error("飞书图片上传异常: %s path=%s", e, abs_path)
            return None

    def _upload_file(self, abs_path: str) -> str | None:
        """Upload a file to Feishu and return the file_key, or None on failure."""
        if not self._api_client:
            return None
        try:
            file_name = os.path.basename(abs_path)
            with open(abs_path, "rb") as f:
                request = (
                    CreateFileRequest.builder()
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type("stream")
                        .file_name(file_name)
                        .file(f)
                        .build()
                    )
                    .build()
                )
                response = self._api_client.im.v1.file.create(request)

            if not response.success():
                logger.warning(
                    "飞书文件上传失败: code=%s msg=%s path=%s",
                    response.code, response.msg, abs_path,
                )
                return None

            file_key = getattr(response.data, "file_key", None)
            if file_key:
                logger.info("飞书文件上传成功: file_key=%s", file_key)
            return file_key
        except FileNotFoundError:
            logger.warning("飞书文件上传失败: 文件不存在 path=%s", abs_path)
            return None
        except Exception as e:
            logger.error("飞书文件上传异常: %s path=%s", e, abs_path)
            return None

    def _build_card_content(self, content_str: str) -> str:
        """
        构建飞书卡片内容。

        Args:
            content_str: 消息内容字符串

        Returns:
            str: JSON格式的卡片内容
        """
        elements = self._build_feishu_card_elements(content_str)
        card = {
            "config": {"wide_screen_mode": True},
            "elements": elements,
        }
        return json.dumps(card, ensure_ascii=False)

    def _build_skills_list_card_content(self, payload: Any, event_name: str) -> str | None:
        """将 skills.list 返回渲染为飞书卡片；非 skills.list 数据返回 None。"""
        if event_name != "chat.final":
            return None
        if not isinstance(payload, dict):
            return None
        if "skills" not in payload and not self._looks_like_skills_error_payload(payload):
            return None

        skills = payload.get("skills")
        error_text = str(payload.get("error") or "").strip()
        header_title = "技能列表"
        elements: list[dict[str, Any]] = []

        if error_text:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"获取技能列表失败：{error_text}"},
            })
        elif not isinstance(skills, list) or not skills:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "当前无可用技能。"},
            })
        else:
            source_counter: dict[str, int] = {}
            for item in skills:
                if not isinstance(item, dict):
                    continue
                src = str(item.get("source") or "unknown").strip() or "unknown"
                source_counter[src] = source_counter.get(src, 0) + 1

            source_parts = [f"{k}: {v}" for k, v in sorted(source_counter.items(), key=lambda kv: kv[0])]
            if source_parts:
                source_summary = " | ".join(source_parts[:4])
                if len(source_parts) > 4:
                    source_summary += " | ..."
                elements.append({
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": f"总数: {len(skills)}"},
                        {"tag": "plain_text", "content": f"来源: {source_summary}"},
                    ],
                })
                elements.append({"tag": "hr"})

            limit = 20
            for i, item in enumerate(skills[:limit], 1):
                if isinstance(item, dict):
                    name = str(item.get("name") or item.get("title") or "?").strip()
                    desc = str(item.get("description") or "").strip()
                    source = str(item.get("source") or "").strip()
                    path = str(item.get("path") or "").strip()
                    title = f"{i}. {name}" + (f" ({source})" if source else "")
                    body = title
                    if desc:
                        short_desc = desc if len(desc) <= 180 else f"{desc[:180]}..."
                        body = f"{title}\n{short_desc}"
                    if path:
                        short_path = path if len(path) <= 120 else f"...{path[-117:]}"
                        body = f"{body}\n`{short_path}`"
                    elements.append({
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": body},
                    })
                else:
                    elements.append({
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": f"{i}. {item}"},
                    })
            if len(skills) > limit:
                elements.append({
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": f"共 {len(skills)} 项，仅显示前 {limit} 项"}],
                })

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": header_title},
            },
            "elements": elements,
        }
        return json.dumps(card, ensure_ascii=False)

    @staticmethod
    def _looks_like_skills_error_payload(payload: dict[str, Any]) -> bool:
        """判定是否为 skills.list 的错误透传载荷。"""
        if "error" not in payload:
            return False
        # 避免误判 chat.error/chat.final 的普通错误消息
        noisy_keys = {"content", "output", "result", "text", "event_type"}
        return not any(k in payload for k in noisy_keys)

    async def _send_ask_user_question_card(self, msg: Message) -> None:
        """
        发送用户询问卡片（带选项按钮的确认卡片）。

        Args:
            msg: 包含 ask_user_question payload 的消息对象
        """
        try:
            payload = msg.payload if isinstance(msg.payload, dict) else {}
            questions = payload.get("questions", [])
            request_id = payload.get("request_id", "")

            if not questions:
                logger.warning("发送用户询问卡片：没有问题数据")
                return

            # 构建 card 元素，每次只显示一个问题
            elements = []

            # 添加问题文本
            question = questions[0]
            question_text = question.get("question", "")
            if question_text:
                elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": question_text
                    }
                })

            # 添加分隔线
            elements.append({"tag": "hr"})

            # 添加按钮
            options = question.get("options", [])
            if options:
                actions = []
                for option in options:
                    label = option.get("label", "")
                    # value = json.dumps({"label": label}, ensure_ascii=False)
                    button_element = {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": label
                        },
                        "type": "primary",
                        "value": {"label": label, "request_id": request_id}
                    }
                    actions.append(button_element)

                elements.append({
                    "tag": "action",
                    "actions": actions
                })

            # 构建卡片
            card = {
                "config": {"wide_screen_mode": True},
                "elements": elements,
            }

            # 添加 header 用于存储 request_id
            header_text = question.get("header", "")
            if header_text:
                card["header"] = {
                    "title": {
                        "tag": "plain_text",
                        "content": header_text
                    },
                    "template": request_id  # 使用 template 字段存储 request_id
                }

            self._user_question_card[request_id] = card
            # 发送卡片
            receive_id, id_type = self._extract_receive_info(msg)
            card_json = json.dumps(card, ensure_ascii=False)

            await self._create_and_send_message(
                FeishuMessageSendRequest(
                    receive_id=receive_id,
                    id_type=id_type,
                    msg_type="interactive",
                    content=card_json,
                    log_label=f"ask_user_question:{request_id}",
                )
            )

            logger.info(
                "[FeishuChannel] 发送用户询问卡片: request_id=%s, chat_id=%s",
                request_id,
                receive_id,
            )

        except Exception as e:
            logger.error(f"发送用户询问卡片时发生异常: {e}", exc_info=True)

    async def _send_feishu_message(
        self, receive_id: str, id_type: str, content: str, msg_id: str,
    ) -> Any:
        """
        发送飞书卡片消息（异步，在线程池中执行同步 SDK 调用）。

        Args:
            receive_id: 接收者ID
            id_type: ID类型
            content: 消息内容（JSON字符串）
            msg_id: 发送消息ID（用于日志）
        """
        await self._create_and_send_message(
            FeishuMessageSendRequest(
                receive_id=receive_id,
                id_type=id_type,
                msg_type="interactive",
                content=content,
                log_label=msg_id,
            )
        )

    def _on_message_sync(self, data: "P2ImMessageReceiveV1") -> None:
        """
        传入消息的同步处理器（从WebSocket线程调用）。

        在主事件循环中调度异步处理。

        Args:
            data: 飞书消息事件数据
        """
        if self._main_loop and self._main_loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._main_loop)

    async def _on_message(self, data: "P2ImMessageReceiveV1") -> None:
        """
        处理来自飞书的传入消息。

        Args:
            data: 飞书消息事件数据
        """
        try:
            event = data.event
            message = event.message
            sender = event.sender

            # 消息去重检查
            if self._is_duplicate_message(message.message_id):
                return

            # 跳过机器人发送的消息
            if sender.sender_type == "bot":
                return

            # 群聊数字分身模式下不自动点赞
            if not self.config.group_digital_avatar:
                asyncio.create_task(self._add_reaction(message.message_id, "THUMBSUP"))

            # 解析消息内容（支持文件类型）
            content, file_info = await self._parse_message_content_with_file(message)
            if content == "/mode team" and self.config.enable_streaming == False:
                # 非流式情况下不支持team模式，向用户发送提示
                try:
                    # 提取发送者open_id
                    open_id = (
                        getattr(getattr(sender, "sender_id", None), "open_id", None) or ""
                    )
                    # 获取chat_id和判断ID类型
                    chat_id = getattr(message, "chat_id", None) or ""
                    if chat_id.startswith("oc_"):
                        receive_id = chat_id
                        id_type = "chat_id"
                    else:
                        # 私聊场景使用open_id
                        receive_id = open_id
                        id_type = "open_id"
                    hint_text = "⚠️ 非流式模式下不支持 Team 模式，已保持原有模式。\n\n" \
                        "如需使用 Team 模式，请在配置中开启流式输出 (enable_streaming: true)。"
                    card = self._build_card_content(hint_text)
                    await self._send_feishu_message(receive_id, id_type, card, message.message_id)
                except Exception as e:
                    logger.warning(f"[FeishuChannel] 发送Team模式提示失败: {e}")
                return
            if not content and not file_info:
                return

            # 提取发送者open_id
            open_id = (
                getattr(getattr(sender, "sender_id", None), "open_id", None) or ""
            )

            # 将最近一次可回发的飞书身份写入 config.yaml，供 cron 推送时使用
            if self.channel_id == self.name:
                try:
                    from jiuwenclaw.config import update_channel_in_config

                    update_channel_in_config(
                        "feishu",
                        {
                            "last_chat_id": getattr(message, "chat_id", None) or "",
                            "last_open_id": open_id or "",
                            "last_message_id": getattr(message, "message_id", None) or "",
                        },
                    )
                except Exception:
                    # 不影响正常收消息
                    pass
            elif self.channel_id.startswith("feishu_enterprise:") and self.config.bot_key:
                try:
                    from jiuwenclaw.config import update_channel_subsection_in_config

                    update_channel_subsection_in_config(
                        "feishu_enterprise",
                        self.config.bot_key,
                        {
                            "last_chat_id": getattr(message, "chat_id", None) or "",
                            "last_open_id": open_id or "",
                            "last_message_id": getattr(message, "message_id", None) or "",
                        },
                    )
                except Exception:
                    # 不影响正常收消息
                    pass

            # 内存同步 last_*：无 metadata 的回发兜底立即生效，不必等通道重启从 yaml 加载
            lc = str(getattr(message, "chat_id", None) or "").strip()
            lo = str(open_id or "").strip()
            try:
                self.config = self.config.model_copy(
                    update={"last_chat_id": lc, "last_open_id": lo},
                )
            except Exception:
                pass

            # 构建消息参数
            params = {
                "content": content,
                "query": content,
                "interactive_ask": True,
            }
            if file_info:
                params["files"] = [file_info]

            # 构建基础 metadata
            base_metadata = {
                "message_id": message.message_id,
                "chat_type": message.chat_type,
                "msg_type": message.message_type,
                "open_id": open_id,
                "feishu_open_id": open_id,
                "feishu_chat_id": getattr(message, "chat_id", None) or "",
                **({"file_info": file_info} if file_info else {}),
            }

            # 记录消息到本地存储（仅数字分身模式）
            if self.config.group_digital_avatar:
                try:
                    self._message_storage.add_message_to_memory(
                        chat_id=message.chat_id,
                        message={
                            "message_id": message.message_id,
                            "content": content,
                            "timestamp": getattr(message, "create_time", int(time.time() * 1000)),
                            "msg_type": message.message_type,
                            "open_id": open_id,
                            "chat_type": message.chat_type,
                        }
                    )
                except Exception as e:
                    logger.warning(f"记录消息到本地存储失败: {e}")

            # 数字分身模式：走消息批次合并；否则直接处理
            _ts = int(getattr(message, "create_time", int(time.time() * 1000)))
            _meta = self._build_reply_metadata(
                message=message,
                sender_open_id=open_id,
                base_metadata=base_metadata,
            )
            if self.config.group_digital_avatar:
                await self._enqueue_message_batch(
                    chat_id=message.chat_id,
                    open_id=open_id or "",
                    content=content,
                    timestamp_ms=_ts,
                    metadata=_meta,
                    params=params,
                )
            else:
                await self._process_batched_message(
                    chat_id=message.chat_id,
                    open_id=open_id or "",
                    merged_content=content,
                    timestamp_ms=_ts,
                    metadata=_meta,
                    params=params,
                )

        except Exception as e:
            logger.error(f"处理飞书消息时发生异常: {e}")

    def _on_message_read_event(self, data: Any) -> None:
        """
        处理飞书消息已读事件（空处理器，仅用于消除 SDK 日志警告）。

        飞书会在用户阅读消息后推送 im.message.message_read_v1 事件，
        但当前场景不需要处理此事件，注册空处理器避免 SDK 打印 ERROR 日志。

        Args:
            data: 飞书消息已读事件数据（忽略）
        """
        pass  # 不处理，仅用于消除日志警告

    def _on_card_action_trigger_sync(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        """
        处理飞书卡片按钮点击事件的同步处理器（从WebSocket线程调用）。

        根据飞书SDK规范，需要返回 P2CardActionTriggerResponse 对象。

        Args:
            data: 飞书卡片回调事件数据 (P2CardActionTrigger)

        Returns:
            Any: P2CardActionTriggerResponse 响应对象
        """
        try:
            event = data.event
            action = event.action

            # 提取回调信息
            token = event.token or ""
            action_value = action.value or ""

            # 返回成功的响应：更新卡片去除按钮，显示已选择的内容
            selected_label = action_value.get("label", "已选择")
            request_id = action_value.get("request_id", "")
            if not request_id or request_id not in self._user_question_card:
                card_data = {
                        "elements": [
                            {
                                "tag": "div",
                                "text": {
                                    "tag": "lark_md",
                                    "content": f"您已选择：**{selected_label}**"
                                }
                            }
                        ]
                    }
                header = {}
            else:
                card_data = self._user_question_card[request_id]
                content = card_data["elements"][0].get("text", {}).get("content", "") \
                    if "elements" in card_data and len(card_data["elements"]) > 0 else ""
                header = card_data.get("header", {})
                card_data = {
                    "elements": [{
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"{content} <br> <br> 您已选择：**{selected_label}**"
                            }
                        }]
                }
                self._user_question_card.pop(request_id)
            # 获取用户和上下文信息
            operator_id = event.operator.open_id if event.operator and event.operator.open_id else ""

            logger.info(
                "[FeishuChannel] 收到卡片回调: token=%s, action=%s, user=%s",
                token,
                action_value,
                operator_id,
            )

            # 在主事件循环中调度异步处理
            if self._main_loop and self._main_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._handle_card_callback_async(token, action_value, operator_id),
                    self._main_loop
                )

            # 返回成功的响应：更新卡片去除按钮，提示已接受
            response = {
                "toast": {
                    "type": "info",
                    "content": "已收到您的选择"
                },
                "card": {
                    "type": "raw",
                    "data": {
                         "schema": "2.0",
                         "config": {
                             "update_multi": True,
                         },
                         "body": card_data,
                         "header": header
                    }
                }
            }
            return P2CardActionTriggerResponse(response)
        except Exception as e:
            logger.error(f"处理飞书卡片回调时发生异常: {e}", exc_info=True)
            # 返回错误响应
            error_response = {
                "toast": {
                    "type": "error",
                    "content": "处理失败"
                }
            }
            if P2CardActionTriggerResponse:
                return P2CardActionTriggerResponse(error_response)
            return error_response

    async def _handle_card_callback_async(
        self,
        token: str,
        action_value: str,
        operator_id: str,
    ) -> None:
        """
        异步处理卡片回调，将回调转换为 chat.user_answer 事件。

        Args:
            token: 卡片 token
            action_value: 用户选择的值
            operator_id: 操作者 ID
        """
        try:
            request_id = action_value.pop("request_id", "")
            source = str(action_value.pop("source", "") or "").strip()
            if not request_id:
                raise ValueError("missing request_id")
            # 构建用户响应（符合 evolution service 期望的格式）
            answers = [{"selected_options": list(action_value.values())}] if action_value else []

            logger.info(
                "[FeishuChannel] 发送用户答案: token=%s, answer=%s",
                token,
                action_value,
            )
            params = {"answers": answers, "request_id": request_id}
            if source:
                params["source"] = source

            msg = Message(
                    id=token,
                    type="event",
                    channel_id=self.channel_id,
                    session_id=operator_id,
                    params=params,
                    timestamp=time.time(),
                    ok=True,
                    req_method=ReqMethod.CHAT_ANSWER,
                    provider=self.name,
                    chat_id="",  # 卡片回调不直接关联特定chat_id
                    user_id=operator_id,
                    bot_id=self.config.app_id,
                    event_type=None,  # 不使用特定事件类型，通过 params 传递答案
                    metadata={
                        "request_id": token,
                        "answers": answers,
                    },
                )
            # 发送 chat.user_answer 事件到 gateway
            if self._message_callback:
                # 构建 Message 对象
                self._message_callback(msg)
            else:
                # 通过 bus 路由
                await self.bus.publish_user_message(msg)

        except Exception as e:
            logger.error(f"异步处理卡片回调时发生异常: {e}", exc_info=True)

    def _is_duplicate_message(self, message_id: str) -> bool:
        """
        检查消息是否重复。

        Args:
            message_id: 消息ID

        Returns:
            bool: True表示消息重复，False表示新消息
        """
        if message_id in self._message_dedup_cache:
            return True

        self._message_dedup_cache[message_id] = None

        # 修剪缓存：当超过1000时保留最近的500条
        while len(self._message_dedup_cache) > 1000:
            self._message_dedup_cache.popitem(last=False)

        return False

    def _parse_message_content(self, message: Any) -> str:
        """
        解析消息内容。

        Args:
            message: 飞书消息对象

        Returns:
            str: 解析后的消息内容
        """
        msg_type = message.message_type

        if msg_type == "text":
            try:
                text = json.loads(message.content).get("text", "")
                # 替换 @mentions 占位符为真实用户名
                text = self._replace_mentions_with_names(message, text)
                return text
            except json.JSONDecodeError:
                return message.content or ""
        else:
            return MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")

    async def _parse_message_content_with_file(
        self,
        message: Any,
    ) -> tuple[str, dict | None]:
        """
        解析消息内容，支持文件类型（异步版本）。

        Args:
            message: 飞书消息对象

        Returns:
            tuple: (文本内容, 文件信息字典或None)
        """
        if not self._file_service:
            return self._parse_message_content(message), None

        msg_type = message.message_type

        # 文本消息
        if msg_type == "text":
            try:
                text = json.loads(message.content).get("text", "")
                # 替换 @mentions 占位符为真实用户名
                text = self._replace_mentions_with_names(message, text)
                return text, None
            except json.JSONDecodeError:
                return message.content or "", None

        # 图片消息
        if msg_type == "image":
            return await self._handle_image_message(message)

        # 文件消息
        if msg_type == "file":
            return await self._handle_file_message(message)

        # 音频消息
        if msg_type == "audio":
            return await self._handle_audio_message(message)

        # 视频/媒体消息
        if msg_type == "media":
            return await self._handle_media_message(message)

        # 贴纸消息
        if msg_type == "sticker":
            return "[贴纸]", None

        # 其他类型
        return MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"), None

    async def _handle_image_message(self, message: Any) -> tuple[str, dict | None]:
        """处理图片消息。"""
        try:
            content = json.loads(message.content)
            image_key = content.get("image_key")

            if not image_key:
                return "[图片]", None

            # 下载图片
            file_info = await self._file_service.download_image(
                file_key=image_key,
                message_id=message.message_id,
            )

            if file_info:
                return "[图片]", file_info

            return "[图片下载失败]", None

        except Exception as e:
            logger.error(f"处理图片消息失败: {e}")
            return "[图片]", None

    async def _handle_file_message(self, message: Any) -> tuple[str, dict | None]:
        """处理文件消息。"""
        try:
            content = json.loads(message.content)
            file_key = content.get("file_key")
            file_name = content.get("file_name", "unknown")
            file_size = content.get("file_size", 0)

            logger.info(f"飞书文件消息: file_name={file_name}, file_size={file_size}, file_key={file_key}")

            if not file_key:
                return f"[文件: {file_name}]", None

            # 检查文件大小限制
            max_size = self.config.max_download_size
            if file_size > max_size:
                logger.warning(f"文件过大，跳过下载: {file_name} ({file_size} > {max_size})")
                return f"[文件过大: {file_name}]", None

            # 下载文件
            logger.info(f"开始下载飞书文件: {file_name}")
            file_info = await self._file_service.download_file_resource(
                file_key=file_key,
                message_id=message.message_id,
                extra_info={"file_name": file_name, "file_size": file_size},
            )

            if file_info:
                # 关键修复：name 必须与实际存储的文件名一致，确保 Agent 能精确定位文件
                # file_name 是飞书原始名，file_info["path"] 包含实际存储的文件名
                local_filename = os.path.basename(file_info.get("path", ""))
                file_info["name"] = local_filename if local_filename else file_name
                file_info["_original_name"] = file_name  # 保留飞书原始文件名（用于用户展示）
                file_info["size"] = file_size
                logger.info(f"飞书文件下载成功: 原始名={file_name}, 存储名={local_filename}, path={file_info.get('path')}")
                return f"[文件: {file_name}]", file_info

            logger.warning(f"飞书文件下载失败: {file_name}")
            return f"[文件下载失败: {file_name}]", None

        except Exception as e:
            logger.error(f"处理文件消息失败: {e}")
            return "[文件]", None

    async def _handle_audio_message(self, message: Any) -> tuple[str, dict | None]:
        """处理音频消息。"""
        try:
            content = json.loads(message.content)
            file_key = content.get("file_key")
            duration = content.get("duration", 0)

            if not file_key:
                return "[音频]", None

            # 下载音频
            file_info = await self._file_service.download_audio(
                file_key=file_key,
                message_id=message.message_id,
            )

            if file_info:
                duration_sec = duration / 1000
                return f"[音频: {duration_sec:.1f}秒]", file_info

            return "[音频下载失败]", None

        except Exception as e:
            logger.error(f"处理音频消息失败: {e}")
            return "[音频]", None

    async def _handle_media_message(self, message: Any) -> tuple[str, dict | None]:
        """处理视频/媒体消息。"""
        try:
            content = json.loads(message.content)
            file_key = content.get("file_key")
            duration = content.get("duration", 0)

            if not file_key:
                return "[视频]", None

            # 下载视频
            file_info = await self._file_service.download_media(
                file_key=file_key,
                message_id=message.message_id,
            )

            if file_info:
                duration_sec = duration / 1000
                return f"[视频: {duration_sec:.1f}秒]", file_info

            return "[视频下载失败]", None

        except Exception as e:
            logger.error(f"处理视频消息失败: {e}")
            return "[视频]", None

    def _extract_text_from_file(self, file_path: str, max_len: int = 50000) -> str | None:
        """从文本文件中提取内容。"""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read(max_len + 1)
            if len(content) > max_len:
                content = content[:max_len] + "\n... [内容已截断]"
            return content
        except UnicodeDecodeError:
            # 尝试其他编码
            try:
                with open(file_path, "r", encoding="gbk") as f:
                    content = f.read(max_len + 1)
                if len(content) > max_len:
                    content = content[:max_len] + "\n... [内容已截断]"
                return content
            except Exception:
                return None
        except Exception as e:
            logger.warning(f"提取文件内容失败: {e}")
            return None

    # ==================== 文件消息发送 ====================

    async def _send_file_message(self, msg: Message) -> None:
        """
        发送文件消息（支持图片/音频/视频/普通文件）。

        Args:
            msg: 包含文件信息的消息对象
        """
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        files = payload.get("files", [])

        if not files:
            logger.warning("飞书发送文件消息：无文件信息")
            return

        receive_id, id_type = self._extract_receive_info(msg)
        logger.info(f"飞书发送文件消息: receive_id={receive_id}, id_type={id_type}")

        for file_info in files:
            # 支持两种格式：路径字符串 或 包含 path 字段的字典
            if isinstance(file_info, dict):
                file_path = file_info.get("path", "")
                file_name = file_info.get("name", os.path.basename(file_path))
            else:
                file_path = str(file_info)
                file_name = os.path.basename(file_path)

            if not file_path or not os.path.exists(file_path):
                logger.warning(f"飞书发送文件：文件不存在 {file_path}")
                continue

            req_id = str(getattr(msg, "id", "") or "")
            if req_id:
                self._sent_file_paths_by_req.setdefault(req_id, set()).add(
                    os.path.abspath(file_path)
                )

            if is_image_file(file_path):
                await self._send_image_message(receive_id, id_type, file_path)
            elif is_audio_file(file_path):
                await self._send_audio_message(receive_id, id_type, file_path, file_name)
            elif is_video_file(file_path):
                await self._send_video_message(receive_id, id_type, file_path, file_name)
            else:
                await self._send_file_card(receive_id, id_type, file_path, file_name)

    async def _create_and_send_message(
        self,
        request: FeishuMessageSendRequest,
    ) -> None:
        """构建并发送飞书消息（在线程池中执行同步 SDK 调用，支持重试）。"""
        loop = asyncio.get_running_loop()
        
        for attempt in range(request.max_retries):
            try:
                def _do_send():
                    req = (
                        CreateMessageRequest.builder()
                        .receive_id_type(request.id_type)
                        .request_body(
                            CreateMessageRequestBody.builder()
                            .receive_id(request.receive_id)
                            .msg_type(request.msg_type)
                            .content(request.content)
                            .build()
                        )
                        .build()
                    )
                    return self._api_client.im.v1.message.create(req)

                response = await loop.run_in_executor(None, _do_send)

                if not response.success():
                    logger.warning(
                        "飞书发送消息失败 (尝试 %d/%d, msg_type=%s%s): code=%s msg=%s",
                        attempt + 1, request.max_retries,
                        request.msg_type,
                        f" {request.log_label}" if request.log_label else "",
                        response.code,
                        response.msg,
                    )
                    if attempt < request.max_retries - 1:
                        await asyncio.sleep(1 * (attempt + 1))
                        continue
                else:
                    logger.info("飞书消息发送成功: msg_type=%s%s", request.msg_type,
                                f" {request.log_label}" if request.log_label else "")
                    return
                    
            except Exception as e:
                logger.warning(
                    "飞书发送消息异常 (尝试 %d/%d, msg_type=%s%s): %s",
                    attempt + 1, request.max_retries,
                    request.msg_type,
                    f" {request.log_label}" if request.log_label else "",
                    e,
                )
                if attempt < request.max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))
                    continue
                else:
                    logger.error("发送飞书消息异常: %s", e)

    async def _send_image_message(
        self,
        receive_id: str,
        id_type: str,
        file_path: str,
    ) -> None:
        """
        发送图片消息。

        优先使用 image.create API（返回 image_key，发送 image 消息），
        失败时回退到 file.create API（发送 file 消息）。
        """
        try:
            upload_result = await self._file_service.upload_image(file_path)
            if not upload_result:
                logger.error(f"飞书上传图片失败: {file_path}")
                return

            file_type = upload_result.get("file_type", "image")
            image_key = upload_result.get("image_key")
            file_key = upload_result.get("file_key")

            if file_type == "image" and image_key:
                content = json.dumps({"image_key": image_key}, ensure_ascii=False)
                msg_type = "image"
            elif file_key:
                content = json.dumps({"file_key": file_key}, ensure_ascii=False)
                msg_type = "file"
            else:
                logger.error(f"飞书上传图片返回无效: {upload_result}")
                return

            await self._create_and_send_message(
                FeishuMessageSendRequest(
                    receive_id=receive_id,
                    id_type=id_type,
                    msg_type=msg_type,
                    content=content,
                    log_label=os.path.basename(file_path),
                )
            )

        except Exception as e:
            logger.error(f"发送飞书图片消息异常: {e}")

    async def _send_audio_message(
        self,
        receive_id: str,
        id_type: str,
        file_path: str,
        file_name: str,
    ) -> None:
        """
        发送音频消息。

        若文件为 .opus 格式（飞书原生），使用 msg_type=audio 发送（可在线播放）；
        其他格式降级为普通文件消息。
        """
        try:
            upload_result = await self._file_service.upload_file_resource(file_path)
            if not upload_result:
                logger.error(f"飞书上传音频失败: {file_path}")
                return

            file_key = upload_result.get("file_key")
            if not file_key:
                logger.error(f"飞书上传音频返回无效: {file_path}")
                return

            feishu_file_type = upload_result.get("file_type", "stream")

            if feishu_file_type == "opus":
                # opus 格式支持在线播放
                content = json.dumps({"file_key": file_key}, ensure_ascii=False)
                msg_type = "audio"
            else:
                # 其他音频格式作为普通文件发送
                content = json.dumps({"file_key": file_key}, ensure_ascii=False)
                msg_type = "file"

            await self._create_and_send_message(
                FeishuMessageSendRequest(
                    receive_id=receive_id,
                    id_type=id_type,
                    msg_type=msg_type,
                    content=content,
                    log_label=file_name,
                )
            )

        except Exception as e:
            logger.error(f"发送飞书音频消息异常: {e}")

    async def _send_video_message(
        self,
        receive_id: str,
        id_type: str,
        file_path: str,
        file_name: str,
    ) -> None:
        """
        发送视频消息（msg_type=media，支持在线播放）。

        飞书 media 消息内容：{"file_key": "..."}（thumbnail 可选，此处省略）
        """
        try:
            upload_result = await self._file_service.upload_file_resource(file_path)
            if not upload_result:
                logger.error(f"飞书上传视频失败: {file_path}")
                return

            file_key = upload_result.get("file_key")
            if not file_key:
                logger.error(f"飞书上传视频返回无效: {file_path}")
                return

            content = json.dumps({"file_key": file_key}, ensure_ascii=False)

            await self._create_and_send_message(
                FeishuMessageSendRequest(
                    receive_id=receive_id,
                    id_type=id_type,
                    msg_type="media",
                    content=content,
                    log_label=file_name,
                )
            )

        except Exception as e:
            logger.error(f"发送飞书视频消息异常: {e}")

    async def _send_file_card(
        self,
        receive_id: str,
        id_type: str,
        file_path: str,
        file_name: str,
    ) -> None:
        """发送普通文件消息（msg_type=file）。"""
        try:
            upload_result = await self._file_service.upload_file_resource(file_path)
            if not upload_result:
                logger.error(f"飞书上传文件失败: {file_path}")
                return

            file_key = upload_result.get("file_key")
            if not file_key:
                logger.error(f"飞书上传文件返回无效: {file_path}")
                return

            content = json.dumps({"file_key": file_key}, ensure_ascii=False)

            await self._create_and_send_message(
                FeishuMessageSendRequest(
                    receive_id=receive_id,
                    id_type=id_type,
                    msg_type="file",
                    content=content,
                    log_label=file_name,
                )
            )

        except Exception as e:
            logger.error(f"发送飞书文件消息异常: {e}")

    async def _send_team_message(self, msg: Message) -> None:
        """
        发送Agent Team消息卡片。

        Args:
            msg: 包含 team.message payload 的消息对象
        """
        try:
            payload = msg.payload if isinstance(msg.payload, dict) else {}
            event = payload.get("event", {})
            message_type = event.get("type", "")
            from_member = event.get("from_member", "")
            to_member = event.get("to_member", "")
            content = event.get("content", "")

            if not from_member or not content:
                logger.warning("发送Team消息卡片：缺少必要字段 from_member 或 content")
                return

            # 构建 card 元素
            elements = []

            # 接收者信息
            if message_type == "team.message.broadcast":
                receiver_text = "📢 **广播消息**"
            elif message_type == "team.message.p2p":
                receiver_text = f"👤 **发给: {to_member}**"
            else:
                receiver_text = f"📋 **类型: {message_type}**"

            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": receiver_text
                }
            })

            # 添加分隔线
            elements.append({"tag": "hr"})

            # 消息内容
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": content
                }
            })

            # 构建卡片
            card = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"🤖 {from_member}"
                    },
                    "template": "teal"
                },
                "elements": elements,
            }

            # 发送卡片
            receive_id, id_type = self._extract_receive_info(msg)
            card_json = json.dumps(card, ensure_ascii=False)

            await self._create_and_send_message(
                FeishuMessageSendRequest(
                    receive_id=receive_id,
                    id_type=id_type,
                    msg_type="interactive",
                    content=card_json,
                    log_label=f"team_message:{message_type}",
                )
            )

            logger.info(
                "[FeishuChannel] 发送Team消息卡片: type=%s, from=%s, to=%s",
                message_type,
                from_member,
                to_member if message_type == "team.message.p2p" else "broadcast",
            )

        except Exception as e:
            logger.error(f"发送Team消息卡片时发生异常: {e}", exc_info=True)

    async def _send_media_message(self, msg: Message) -> None:
        """
        发送媒体消息（video/audio）入口，与文件消息统一处理。
        """
        await self._send_file_message(msg)

    def _filter_user_info_for_group(self, content: str, metadata: dict[str, Any]) -> str:
        """
        过滤群聊消息中的用户实际信息。

        在群聊中回复时，移除可能包含用户隐私信息的内容。
        私聊时不过滤。

        Args:
            content: 原始消息内容
            metadata: 消息元数据

        Returns:
            str: 过滤后的消息内容
        """
        chat_type = str(metadata.get("chat_type") or "").strip()
        im_chat_type = str(metadata.get("im_chat_type") or "").strip()
        is_group = chat_type == "group" or im_chat_type == "group"

        if not is_group:
            return content

        filtered_content = content

        user_sensitive_patterns = [
            r'\b[\w\.-]+@[\w\.-]+\.\w+\b',
            r'\b(?:电话|手机|联系方式|手机号|电话号码)[:：]?\s*[\d\s-]{7,15}\b',
            r'\b1[3-9]\d{9}\b',
            r'\b(?:身份证|身份证号|ID)[:：]?\s*\d{15,18}\b',
            r'\b\d{15,18}\b',
            r'ou_[a-zA-Z0-9]{20,}',
            r'on_[a-zA-Z0-9]{20,}',
            r'\b(?:密码|password|pwd)[:：]?\s*\S+\b',
            r'\b(?:地址|住址|家庭住址)[:：]?\s*[^\n]{10,50}\b',
        ]

        for pattern in user_sensitive_patterns:
            filtered_content = re.sub(pattern, '[已过滤]', filtered_content, flags=re.IGNORECASE)

        return filtered_content
