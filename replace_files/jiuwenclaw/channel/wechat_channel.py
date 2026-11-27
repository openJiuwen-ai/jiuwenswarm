from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import json
import logging
import random
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import aiohttp
from aiohttp.client_exceptions import ContentTypeError
from pydantic import BaseModel, Field

from jiuwenclaw.schema.message import EventType, Message, ReqMethod
from .base import BaseChannel, ChannelMetadata, RobotMessageRouter

logger = logging.getLogger(__name__)
main_logger = logging.getLogger("__main__")

MAX_MESSAGES_SEND_TO_WECHAT = 10


class StreamDeltaAccumulator:
    """最小增量聚合器：按 session key 缓存并在 flush 时输出。"""

    def __init__(self) -> None:
        self._buffers: dict[str, list[str]] = {}
        self._index_pending: dict[str, dict[int, str]] = {}
        self._index_cursor: dict[str, int] = {}

    def add_chunk(self, key: str, chunk: str, *, index: int | None = None) -> str:
        if not key or not chunk:
            return ""
        if index is None:
            self._buffers.setdefault(key, []).append(chunk)
            return chunk

        pending = self._index_pending.setdefault(key, {})
        pending[index] = chunk
        cursor = self._index_cursor.setdefault(key, 0)
        out: list[str] = []
        while cursor in pending:
            out.append(pending.pop(cursor))
            cursor += 1
        self._index_cursor[key] = cursor
        merged = "".join(out)
        if merged:
            self._buffers.setdefault(key, []).append(merged)
        return merged

    def flush(self, key: str) -> str:
        parts = self._buffers.pop(key, [])
        self._index_pending.pop(key, None)
        self._index_cursor.pop(key, None)
        return "".join(parts)

    def clear(self, key: str) -> None:
        self._buffers.pop(key, None)
        self._index_pending.pop(key, None)
        self._index_cursor.pop(key, None)


def format_tool_call_message(payload: dict[str, Any]) -> str:
    tool_name = str(payload.get("tool_call", {}).get("name") or payload.get("name") or "工具调用")
    args = payload.get("tool_call", {}).get("arguments")
    args_str = str(args) if args is not None else ""
    return f"[{tool_name}] 调用中...\n{args_str}".strip()


def format_tool_result_message(payload: dict[str, Any]) -> str:
    tool_name = str(payload.get("tool_name") or payload.get("name") or "工具结果")
    result = payload.get("result")
    if isinstance(result, (dict, list)):
        result_str = json.dumps(result, ensure_ascii=False)
    else:
        result_str = str(result or "")
    return f"[{tool_name}] 结果\n{result_str}".strip()


class WechatConfig(BaseModel):
    """个人微信（ClawBot iLink Bot API）通道配置。"""

    enabled: bool = False
    base_url: str = "https://ilinkai.weixin.qq.com"
    bot_token: str = ""
    ilink_bot_id: str = ""
    ilink_user_id: str = ""
    allow_from: list[str] = Field(default_factory=list)

    # 登录与轮询
    auto_login: bool = True
    qrcode_poll_interval_sec: float = 2.0
    long_poll_timeout_sec: int = 45
    backoff_base_sec: float = 1.0
    backoff_max_sec: float = 30.0

    # 可选：本地凭据持久化
    credential_file: str = "~/.wx-ai-bridge/credentials.json"

    # 是否下发过程消息（工具调用/结果、delta 在工具边界的冲刷）；False 时仅在下发 chat.final（及 interrupt 等完结类事件）时合并发送
    enable_streaming: bool = True


# 供前端轮询展示扫码登录进度（与 Logger 输出互补）
_login_ui_lock = asyncio.Lock()
_login_ui_state: dict[str, Any] = {
    "phase": "idle",
    "message": "",
    "qr": None,
    "credentials": None,
    "credentials_source": None,
    "error": None,
    "updated_at": 0.0,
}


def _guess_image_mime_from_bytes(head: bytes) -> str | None:
    if len(head) < 12:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_plausible_raster_image(mime: str, raw: bytes) -> bool:
    """过滤「魔数碰巧命中但并非有效位图」的 base64 载荷，避免浏览器裂图。"""
    if mime == "image/png":
        if len(raw) < 64 or not (raw.startswith(b"\x89PNG\r\n\x1a\n") and raw[12:16] == b"IHDR"):
            return False
        width = int.from_bytes(raw[16:20], "big")
        height = int.from_bytes(raw[20:24], "big")
        if not (1 <= width <= 4096 and 1 <= height <= 4096):
            return False
        return True
    if mime == "image/jpeg":
        return len(raw) >= 100
    if mime == "image/gif":
        return len(raw) >= 80
    if mime == "image/webp":
        return len(raw) >= 64
    return False


def _strip_base64_payload(s: str) -> str:
    return "".join(s.split())


def _payload_from_possible_base64_text(s: str) -> str | None:
    """若 API 将扫码 URL 等文本再做了一层 base64，解出 UTF-8 文本供前端画码。"""
    payload = _strip_base64_payload(s)
    if len(payload) < 8:
        return None
    try:
        raw = base64.b64decode(payload, validate=False)
    except ValueError:
        return None
    if not raw or len(raw) > 8192:
        return None
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if text.startswith(("http://", "https://", "weixin://")):
        return text
    return None


def _coerce_base64_image_to_data_url(s: str) -> str | None:
    """
    将 API 返回的二维码图内容转为浏览器可用的 data URL。
    上游常为 raw base64 或带 data: 头但 MIME 与实际字节不一致（例如JPEG被标成PNG），会导致裂图。
    """
    s = (s or "").strip()
    if not s or s.startswith("http://") or s.startswith("https://"):
        return None
    if s.lower().startswith("data:image"):
        parts = s.split(",", 1)
        if len(parts) != 2:
            return None
        b64_payload = _strip_base64_payload(parts[1])
    else:
        b64_payload = _strip_base64_payload(s)
    if not b64_payload:
        return None
    try:
        raw = base64.b64decode(b64_payload, validate=False)
    except ValueError:
        return None
    if len(raw) < 12:
        return None
    mime = _guess_image_mime_from_bytes(raw[:16])
    if not mime or not _is_plausible_raster_image(mime, raw):
        return None
    b64_out = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64_out}"


def _as_qr_img_content_string(v: Any) -> str:
    """统一 API 里 qrcode_img_content 可能出现的 str / bytes / int 列表等形式。"""
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, (bytes, bytearray)):
        return base64.b64encode(bytes(v)).decode("ascii")
    if isinstance(v, list) and v:
        try:
            return base64.b64encode(bytes(int(x) & 0xFF for x in v)).decode("ascii")
        except (TypeError, ValueError):
            return ""
    return ""


def build_wechat_qr_display(qr_data: dict[str, Any]) -> dict[str, Any] | None:
    """将 get_bot_qrcode 响应整理为前端可展示结构。

    注意：微信 iLink 的 ``qrcode_img_content`` 在多数实现里是「编入二维码的文本载荷」
    （参见 weixin-ai-bridge 对 qrcode-terminal 的用法），而非 PNG/JPEG base64。
    因此仅在通过严格位图校验时才使用 data_url，否则用前端根据文本生成二维码（encode）。

    ``qrcode`` 字段为轮询用语 UUID/令牌，绝不是图片，不得当作 base64 图解析（否则易误判导致裂图）。
    """
    img = ""
    for key in ("qrcode_img_content", "qrcodeImgContent", "QRCodeImgContent"):
        candidate = _as_qr_img_content_string(qr_data.get(key))
        if candidate:
            img = candidate
            break

    qc = ""
    for key in ("qrcode", "QRCode"):
        v = qr_data.get(key)
        if isinstance(v, str) and v.strip():
            qc = str(v).strip()
            break

    if img:
        if img.startswith("http://") or img.startswith("https://"):
            return {"kind": "url", "value": img}
        data_url = _coerce_base64_image_to_data_url(img)
        if data_url:
            return {"kind": "data_url", "value": data_url}
        nested = _payload_from_possible_base64_text(img)
        to_encode = nested if nested else img
        return {"kind": "encode", "value": to_encode}

    if qc.startswith("http://") or qc.startswith("https://"):
        return {"kind": "url", "value": qc}
    if qc:
        return {"kind": "text", "value": qc}
    return None


async def push_wechat_login_ui(**kwargs: Any) -> None:
    async with _login_ui_lock:
        for key, value in kwargs.items():
            if key == "credentials_source":
                continue
            if key in _login_ui_state:
                _login_ui_state[key] = value
        if "credentials" in kwargs:
            if kwargs["credentials"] is None:
                _login_ui_state["credentials_source"] = None
            elif kwargs.get("credentials_source") in ("scan", "local_file"):
                _login_ui_state["credentials_source"] = kwargs["credentials_source"]
            else:
                _login_ui_state["credentials_source"] = "scan"
        _login_ui_state["updated_at"] = time.time()


async def snapshot_wechat_login_ui_state() -> dict[str, Any]:
    async with _login_ui_lock:
        return copy.deepcopy(_login_ui_state)


async def reset_wechat_login_ui_state() -> None:
    async with _login_ui_lock:
        _login_ui_state.update(
            {
                "phase": "idle",
                "message": "",
                "qr": None,
                "credentials": None,
                "credentials_source": None,
                "error": None,
                "updated_at": time.time(),
            }
        )


def clear_wechat_bound_session(conf: dict[str, Any]) -> dict[str, Any]:
    """删除 credential_file 指向的本地 JSON（若存在），并返回去掉 bot_token / ilink 绑定字段后的配置副本，用于写回 ChannelManager 与 config.yaml。"""
    out = dict(conf)
    cred_default = "~/.wx-ai-bridge/credentials.json"
    cred_path = str(out.get("credential_file") or "").strip() or cred_default
    path = Path(cred_path).expanduser()
    if path.is_file():
        try:
            path.unlink()
            logger.info("WechatChannel 已删除本地凭据文件: %s", path)
            main_logger.info("WechatChannel 已删除本地凭据文件: %s", path)
        except OSError as e:
            logger.warning("WechatChannel 删除凭据文件失败: %s: %s", path, e)
            main_logger.warning("WechatChannel 删除凭据文件失败: %s: %s", path, e)
    out["bot_token"] = ""
    out["ilink_bot_id"] = ""
    out["ilink_user_id"] = ""
    return out


class WechatChannel(BaseChannel):
    """
    个人微信通道（基于 iLink Bot API）。

    特性：
    - 首次可扫码登录获取 bot_token
    - 长轮询 getupdates 接收消息
    - sendmessage 回发文本
    - 自动缓存 context_token（用户会话上下文）
    """

    name = "wechat"

    def __init__(self, config: WechatConfig, bus: RobotMessageRouter):
        super().__init__(config, bus)
        self.config: WechatConfig = config
        self._http: aiohttp.ClientSession | None = None
        self._message_callback: Callable[[Message], Any] | None = None
        self._poll_task: asyncio.Task | None = None
        self._poll_cursor: str = ""
        self._context_tokens: dict[str, str] = {}
        self._backoff_sec: float = self.config.backoff_base_sec
        self._delta_accumulator = StreamDeltaAccumulator()
        self._delta_leading: dict[str, str] = {}
        self.current_round: int = 0

    @property
    def channel_id(self) -> str:
        return self.name

    def on_message(self, callback: Callable[[Message], Any]) -> None:
        self._message_callback = callback

    async def start(self) -> None:
        if self._running:
            logger.warning("WechatChannel 已在运行")
            return

        self._running = True
        timeout = aiohttp.ClientTimeout(total=self.config.long_poll_timeout_sec + 5)
        self._http = aiohttp.ClientSession(timeout=timeout)
        logger.info(
            "WechatChannel 启动中: base_url=%s auto_login=%s token_present=%s",
            self.config.base_url,
            self.config.auto_login,
            bool(self.config.bot_token),
        )
        main_logger.info(
            "WechatChannel 启动中: base_url=%s auto_login=%s token_present=%s",
            self.config.base_url,
            self.config.auto_login,
            bool(self.config.bot_token),
        )

        try:
            await self._load_or_login_credentials()
        except Exception as e:
            logger.exception("WechatChannel 登录阶段失败: %s", e)
            main_logger.warning("WechatChannel 登录阶段失败: %s", e)
            raise

        self._poll_task = asyncio.create_task(self._poll_loop(), name="wechat-channel-poll")
        logger.info("WechatChannel 已启动（iLink 长轮询）")
        main_logger.info("WechatChannel 已启动（iLink 长轮询）")

        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        self._running = False

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        if self._http:
            await self._http.close()
            self._http = None

        logger.info("WechatChannel 已停止")

    def _require_http(self) -> aiohttp.ClientSession:
        http = self._http
        if http is None:
            raise RuntimeError("WechatChannel HTTP client is not initialized")
        return http

    async def send(self, msg: Message) -> None:
        """delta 聚合；enable_streaming 时与原先一致：工具调用/结果会即时下发并在边界冲刷 delta；关闭时仅 chat.final / interrupt 等完结事件合并下发（对齐飞书非流式）。"""
        if not self._http or not self.config.bot_token:
            logger.warning("WechatChannel 未就绪，跳过发送")
            return

        streaming = bool(self.config.enable_streaming)

        if msg.event_type == EventType.CHAT_PROCESSING_STATUS:
            return

        if msg.event_type == EventType.HEARTBEAT_RELAY:
            user_id = self._extract_platform_user_id(msg)
            if not user_id:
                logger.warning(
                    "WechatChannel 心跳未发送：无有效用户 ID（需先发消息或携带 wechat_user_id/reply_to_user_id）"
                )
                return
            content = self._extract_content(msg)
            if content:
                if self.current_round == MAX_MESSAGES_SEND_TO_WECHAT - 1:
                    logger.info(f"WechatChannel sendmessage: Message exceeds 10 messages, ignoring.")
                    return
                await self._send_text_chunks_to_user(msg, content)
            return

        if msg.event_type == EventType.CHAT_TOOL_CALL:
            if not streaming:
                return
            if self.current_round == MAX_MESSAGES_SEND_TO_WECHAT - 1:
                logger.info(f"WechatChannel sendmessage: Message exceeds 10 messages, ignoring.")
                return
            flushed = self._take_accumulated_delta(msg)
            payload = msg.payload if isinstance(msg.payload, dict) else {}
            tool_call_str = format_tool_call_message(payload)
            content = f"{flushed}\n\n{tool_call_str}" if flushed else f"\n{tool_call_str}"
            await self._send_text_chunks_to_user(msg, content)
            return

        if msg.event_type == EventType.CHAT_TOOL_RESULT:
            return

        if msg.event_type == EventType.CHAT_ERROR:
            if self.current_round == MAX_MESSAGES_SEND_TO_WECHAT - 1:
                logger.info(f"WechatChannel sendmessage: Message exceeds 10 messages, ignoring.")
                return
            self._clear_delta_session(msg)
            err_line = self._extract_content(msg)
            if err_line:
                await self._send_text_chunks_to_user(msg, err_line)
            return

        if msg.event_type == EventType.CHAT_DELTA:
            if self._is_reasoning_chunk(msg):
                return
            payload = msg.payload if isinstance(msg.payload, dict) else {}
            chunk = str(payload.get("content", "") or "")
            raw_index = payload.get("index")
            index = raw_index if isinstance(raw_index, int) else None
            sk = self._delta_session_key(msg)
            if not sk:
                return
            flushed = self._delta_accumulator.add_chunk(sk, chunk, index=index)
            if flushed:
                self._delta_leading[sk] = (self._delta_leading.get(sk) or "") + flushed
            return

        allow_plain_res = (
            msg.type == "res"
            and msg.event_type is None
            and not self._is_stream_accept_ack_only(msg)
        )
        final_like_events = (
            EventType.CHAT_FINAL,
            EventType.CHAT_INTERRUPT_RESULT,
        )
        if msg.event_type not in final_like_events and not allow_plain_res:
            return

        flushed_for_dedup: str | None = None

        user_id = self._extract_platform_user_id(msg)
        if not user_id:
            logger.warning("WechatChannel 无法确定回发目标用户")
            return
        if self._is_reasoning_chunk(msg):
            return

        content_str = self._strip_think_tags(self._extract_content(msg)).strip()
        if (
            msg.event_type == EventType.CHAT_FINAL
            and flushed_for_dedup
            and content_str == self._strip_think_tags(flushed_for_dedup).strip()
        ):
            return
        if not content_str or self._is_thinking_only_content(content_str):
            logger.debug("WechatChannel 消息内容为空或仅为思考占位，跳过发送")
            return
        await self._send_text_chunks_to_user(msg, content_str)
        self._clear_delta_session(msg)
        self.current_round = 0

    def _delta_session_key(self, msg: Message) -> str:
        return str(msg.session_id or msg.id or "")

    def _take_accumulated_delta(self, msg: Message) -> str | None:
        sk = self._delta_session_key(msg)
        if not sk:
            return None
        tail = self._delta_accumulator.flush(sk) or ""
        head = self._delta_leading.pop(sk, "") or ""
        combined = head + tail if head != tail else head
        return combined if combined.strip() else None

    def _clear_delta_session(self, msg: Message) -> None:
        sk = self._delta_session_key(msg)
        if sk:
            self._delta_accumulator.clear(sk)
            self._delta_leading.pop(sk, None)

    async def _send_text_chunks_to_user(self, msg: Message, text: str) -> None:
        cleaned = self._strip_think_tags(str(text or "")).strip()
        if not cleaned or self._is_thinking_only_content(cleaned):
            return
        user_id = self._extract_platform_user_id(msg)
        if not user_id:
            logger.warning("WechatChannel 无法确定回发目标用户")
            return
        context_token = self._extract_context_token(msg, user_id)
        if not context_token:
            logger.warning(
                "WechatChannel 缺少 context_token，用户需先发一条消息 user_id=%s",
                user_id,
            )
            return
        for part in self._chunk_text(cleaned, 2000):
            await self._send_message(user_id, context_token, part)
            await asyncio.sleep(0.25)
        self.current_round += 1

    @staticmethod
    def _strip_think_tags(text: str) -> str:
        if not text or not isinstance(text, str):
            return text or ""
        text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<think>[\s\S]*$", "", text, flags=re.IGNORECASE | re.DOTALL)
        return text

    @staticmethod
    def _is_thinking_only_content(text: str) -> bool:
        if not text or not isinstance(text, str):
            return True
        t = text.strip()
        if not t:
            return True
        if re.match(r"^[.．。…\s]+$", t):
            return True
        return False

    @staticmethod
    def _is_reasoning_chunk(msg: Message) -> bool:
        payload = getattr(msg, "payload", None) or {}
        if not isinstance(payload, dict):
            return False
        source_chunk_type = str(payload.get("source_chunk_type") or "").strip().lower()
        return source_chunk_type == "llm_reasoning"

    @staticmethod
    def _is_stream_accept_ack_only(msg: Message) -> bool:
        if msg.type != "res":
            return False
        pl = getattr(msg, "payload", None) or {}
        if not isinstance(pl, dict) or pl.get("accepted") is not True:
            return False
        if pl.get("content"):
            return False
        pr = getattr(msg, "params", None) or {}
        if isinstance(pr, dict) and pr.get("content"):
            return False
        return True

    def _extract_platform_user_id(self, msg: Message) -> str:
        meta: dict[str, Any] = msg.metadata if isinstance(msg.metadata, dict) else {}
        for key in ("wechat_user_id", "reply_to_user_id", "from_user_id"):
            s = str(meta.get(key) or "").strip()
            if s:
                return s
        for candidate in (getattr(msg, "user_id", None), getattr(msg, "chat_id", None)):
            s = str(candidate or "").strip()
            if s:
                return s
        sid = str(getattr(msg, "session_id", "") or "").strip()
        return sid

    def _extract_context_token(self, msg: Message, user_id: str) -> str:
        meta: dict[str, Any] = msg.metadata if isinstance(msg.metadata, dict) else {}
        for key in ("wechat_context_token", "context_token"):
            token = str(meta.get(key) or "").strip()
            if token:
                self._context_tokens[user_id] = token
                return token
        return str(self._context_tokens.get(user_id) or "").strip()

    def get_metadata(self) -> ChannelMetadata:
        token_preview = (
            self.config.bot_token[:8] + "..." if len(self.config.bot_token) > 8 else self.config.bot_token
        )
        return ChannelMetadata(
            channel_id=self.channel_id,
            source="http-long-polling",
            extra={
                "base_url": self.config.base_url,
                "bot_token": token_preview,
            },
        )

    async def _load_or_login_credentials(self) -> None:
        # 优先使用配置中的 bot_token
        if self.config.bot_token:
            logger.info("WechatChannel 使用配置中的 bot_token，跳过扫码登录")
            main_logger.info("WechatChannel 使用配置中的 bot_token，跳过扫码登录")
            await push_wechat_login_ui(
                phase="idle",
                message="已使用配置中的 bot_token",
                qr=None,
                credentials=None,
                error=None,
            )
            return

        # 再尝试读取本地凭据文件
        cred_path = Path(self.config.credential_file).expanduser()
        logger.info("WechatChannel 检查本地凭据: %s", cred_path)
        main_logger.info("WechatChannel 检查本地凭据: %s", cred_path)
        if cred_path.exists():
            try:
                data = json.loads(cred_path.read_text(encoding="utf-8"))
                token = str(data.get("botToken") or data.get("bot_token") or "").strip()
                if token:
                    self.config.bot_token = token
                    self.config.base_url = str(data.get("baseUrl") or data.get("base_url") or self.config.base_url)
                    self.config.ilink_bot_id = str(data.get("ilinkBotId") or data.get("ilink_bot_id") or "")
                    self.config.ilink_user_id = str(data.get("ilinkUserId") or data.get("ilink_user_id") or "")
                    logger.info("WechatChannel 已加载本地凭据: %s", cred_path)
                    main_logger.info("WechatChannel 已加载本地凭据: %s", cred_path)
                    # 与扫码成功一致：告知前端凭据来源，便于填回表单并「保存」写入 config（而非画二维码）
                    await push_wechat_login_ui(
                        phase="success",
                        message="已从本地凭据文件加载 token（未走扫码）。若需重新扫码，请删除或清空该凭据文件后保存配置。",
                        qr=None,
                        credentials={
                            "bot_token": self.config.bot_token,
                            "base_url": self.config.base_url,
                            "ilink_bot_id": self.config.ilink_bot_id,
                            "ilink_user_id": self.config.ilink_user_id,
                        },
                        credentials_source="local_file",
                        error=None,
                    )
                    return
            except Exception as e:
                logger.warning("WechatChannel 读取凭据失败: %s", e)
                main_logger.warning("WechatChannel 读取凭据失败: %s", e)

        if not self.config.auto_login:
            await push_wechat_login_ui(
                phase="error",
                message="",
                qr=None,
                credentials=None,
                error="未配置 bot_token 且 auto_login=false",
            )
            raise RuntimeError("WechatChannel 未配置 bot_token，且 auto_login=false")

        logger.info("WechatChannel 将进入扫码登录流程（bot_token 为空）")
        main_logger.info("WechatChannel 将进入扫码登录流程（bot_token 为空）")
        await self._login_by_qrcode()

    async def _login_by_qrcode(self) -> None:
        self._require_http()
        await push_wechat_login_ui(
            phase="fetching_qr",
            message="正在获取二维码…",
            qr=None,
            credentials=None,
            error=None,
        )
        try:
            logger.info("WechatChannel 开始获取登录二维码")
            main_logger.info("WechatChannel 开始获取登录二维码")
            qr_data = await self._get_qrcode()
            qrcode = str(qr_data.get("qrcode") or "").strip()
            qr_content = str(qr_data.get("qrcode_img_content") or qrcode).strip()
            if not qrcode:
                raise RuntimeError("获取二维码失败：响应缺少 qrcode")

            qr_display = build_wechat_qr_display(qr_data)
            await push_wechat_login_ui(
                phase="awaiting_scan",
                message="请使用微信扫描下方二维码登录",
                qr=qr_display,
                credentials=None,
                error=None,
            )

            logger.info("请使用微信扫描二维码登录：%s", qr_content)
            main_logger.info("请使用微信扫描二维码登录：%s", qr_content)

            deadline = time.time() + 5 * 60
            while time.time() < deadline:
                await asyncio.sleep(self.config.qrcode_poll_interval_sec)
                status = await self._get_qrcode_status(qrcode)
                st = str(status.get("status") or "").strip().lower()
                if st == "scaned":
                    logger.info("已扫码，请在手机上确认")
                    main_logger.info("已扫码，请在手机上确认")
                    await push_wechat_login_ui(
                        phase="scanned",
                        message="已扫码，请在手机上确认登录",
                        qr=qr_display,
                        credentials=None,
                        error=None,
                    )
                elif st == "confirmed":
                    token = str(status.get("bot_token") or "").strip()
                    if not token:
                        raise RuntimeError("登录成功但缺少 bot_token")
                    self.config.bot_token = token
                    self.config.base_url = str(status.get("baseurl") or self.config.base_url)
                    self.config.ilink_bot_id = str(status.get("ilink_bot_id") or "")
                    self.config.ilink_user_id = str(status.get("ilink_user_id") or "")
                    logger.info("WechatChannel 登录成功")
                    main_logger.info("WechatChannel 登录成功")
                    self._save_credentials()
                    await push_wechat_login_ui(
                        phase="success",
                        message="登录成功",
                        qr=None,
                        credentials={
                            "bot_token": self.config.bot_token,
                            "base_url": self.config.base_url,
                            "ilink_bot_id": self.config.ilink_bot_id,
                            "ilink_user_id": self.config.ilink_user_id,
                        },
                        error=None,
                    )
                    return
                elif st == "expired":
                    raise RuntimeError("二维码已过期，请重试")

            raise RuntimeError("登录超时，请重试")
        except Exception as e:
            await push_wechat_login_ui(phase="error", message="", qr=None, credentials=None, error=str(e))
            raise

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                msgs = await self._get_updates()
                self._backoff_sec = self.config.backoff_base_sec
                for wx_msg in msgs:
                    await self._handle_wechat_message(wx_msg)
            except Exception as e:
                logger.error("WechatChannel 轮询错误: %s", e)
                await asyncio.sleep(self._backoff_sec)
                self._backoff_sec = min(self._backoff_sec * 2, self.config.backoff_max_sec)

    async def _get_qrcode(self) -> dict[str, Any]:
        http = self._require_http()
        url = f"{self.config.base_url}/ilink/bot/get_bot_qrcode?bot_type=3"
        logger.info("WechatChannel 请求二维码: %s", url)
        async with http.get(url) as resp:
            if resp.status != 200:
                body_text = await resp.text()
                raise RuntimeError(f"获取二维码失败: HTTP {resp.status} {body_text}")
            return await self._read_json_response(resp, operation="get_bot_qrcode")

    async def _get_qrcode_status(self, qrcode: str) -> dict[str, Any]:
        http = self._require_http()
        url = f"{self.config.base_url}/ilink/bot/get_qrcode_status"
        params = {"qrcode": qrcode}
        headers = {"iLink-App-ClientVersion": "1"}
        async with http.get(url, params=params, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"轮询二维码状态失败: HTTP {resp.status}")
            return await self._read_json_response(resp, operation="get_qrcode_status")

    async def _get_updates(self) -> list[dict[str, Any]]:
        http = self._require_http()
        url = f"{self.config.base_url}/ilink/bot/getupdates"
        body = {
            "get_updates_buf": self._poll_cursor,
            "base_info": {"channel_version": "2.0.0"},
        }
        async with http.post(url, json=body, headers=self._headers()) as resp:
            if resp.status != 200:
                raise RuntimeError(f"getupdates 失败: HTTP {resp.status}")
            data = await self._read_json_response(resp, operation="getupdates")
            ret = data.get("ret")
            if ret is not None and ret != 0:
                errcode = data.get("errcode")
                errmsg = data.get("errmsg") or f"ret={ret}"
                raise RuntimeError(f"getupdates 错误: errcode={errcode} errmsg={errmsg}")

            cursor = data.get("get_updates_buf")
            if cursor:
                self._poll_cursor = str(cursor)
            return data.get("msgs") or []

    async def _send_message(self, user_id: str, context_token: str, text: str) -> None:
        http = self._require_http()
        url = f"{self.config.base_url}/ilink/bot/sendmessage"
        body = {
            "msg": {
                "to_user_id": user_id,
                "client_id": str(uuid.uuid4()),
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [
                    {
                        "type": 1,
                        "text_item": {"text": text},
                    }
                ],
            },
            "base_info": {"channel_version": "2.0.0"},
        }
        async with http.post(url, json=body, headers=self._headers()) as resp:
            if resp.status != 200:
                body_text = await resp.text()
                raise RuntimeError(f"sendmessage 失败: HTTP {resp.status} {body_text}")
            data = await self._read_json_response(resp, operation="sendmessage")
            ret = data.get("ret")
            if ret is not None and ret != 0:
                errcode = data.get("errcode")
                errmsg = data.get("errmsg") or f"ret={ret}"
                logger.warning(
                    "WechatChannel sendmessage 失败: ret=%s errcode=%s errmsg=%s response=%s to_user_id=%s",
                    ret,
                    errcode,
                    errmsg,
                    data,
                    user_id,
                )
                raise RuntimeError(f"sendmessage 错误: errcode={errcode} errmsg={errmsg} ret={ret}")

    async def _read_json_response(self, resp: aiohttp.ClientResponse, *, operation: str) -> dict[str, Any]:
        """Read JSON response even when upstream returns a non-JSON content-type."""
        try:
            data = await resp.json(content_type=None)
        except ContentTypeError:
            text = await resp.text()
            logger.warning(
                "WechatChannel %s 返回非标准 JSON Content-Type=%s，尝试文本解析",
                operation,
                resp.headers.get("Content-Type", ""),
            )
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                snippet = text[:300] if text else ""
                raise RuntimeError(
                    f"{operation} 响应不是可解析 JSON: content_type={resp.headers.get('Content-Type', '')} body={snippet}"
                ) from e
        if not isinstance(data, dict):
            raise RuntimeError(f"{operation} 响应类型异常: {type(data).__name__}")
        return data

    async def _handle_wechat_message(self, wx_msg: dict[str, Any]) -> None:
        # 仅处理用户消息（1），跳过机器人回显（2）
        message_type = int(wx_msg.get("message_type") or 0)
        if message_type != 1:
            return

        user_id = str(wx_msg.get("from_user_id") or "").strip()
        if not user_id:
            return

        if not self.is_allowed(user_id):
            logger.warning("WechatChannel 发送者 %s 未被允许", user_id)
            return

        context_token = str(wx_msg.get("context_token") or "").strip()
        if context_token:
            self._context_tokens[user_id] = context_token

        # 持久化 last_user_id 和 context_token 供 cron/心跳推送使用
        try:
            from jiuwenclaw.config import update_channel_in_config
            updates: dict[str, str] = {"last_user_id": user_id}
            if context_token:
                updates["last_context_token"] = context_token
            update_channel_in_config("wechat", updates)
        except Exception as exc:
            logger.warning("persist channel config failed: %s", exc)

        text, ref_text = self._parse_item_list(wx_msg.get("item_list") or [])
        if not text and not ref_text:
            return

        content = text.strip()
        if ref_text.strip():
            content = f"{content}\n\n{ref_text.strip()}" if content else ref_text.strip()

        req_id = str(wx_msg.get("message_id") or f"wechat_{int(time.time() * 1000)}")
        msg = Message(
            id=req_id,
            type="req",
            channel_id=self.name,
            session_id=user_id,
            params={"content": content, "query": content},
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.CHAT_SEND,
            is_stream=self.config.enable_streaming,
            metadata={
                "wechat_user_id": user_id,
                "reply_to_user_id": user_id,
                "wechat_context_token": context_token,
                "context_token": context_token,
                "raw_message": wx_msg,
            },
        )

        if self._message_callback:
            result = self._message_callback(msg)
            if asyncio.iscoroutine(result):
                await result
        else:
            await self.bus.route_user_message(msg)

    def _headers(self) -> dict[str, str]:
        if not self.config.bot_token:
            raise RuntimeError("缺少 bot_token")
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self.config.bot_token}",
            "X-WECHAT-UIN": self._generate_wechat_uin(),
        }

    @staticmethod
    def _generate_wechat_uin() -> str:
        val = str(random.randint(0, 0xFFFFFFFF))
        return base64.b64encode(val.encode("utf-8")).decode("utf-8")

    @staticmethod
    def _parse_item_list(item_list: list[dict[str, Any]]) -> tuple[str, str]:
        parts: list[str] = []
        ref_text = ""

        for item in item_list:
            t = int(item.get("type") or 0)
            if t == 1:
                txt = (item.get("text_item") or {}).get("text")
                if txt:
                    parts.append(str(txt))
            elif t == 3:
                vtxt = (item.get("voice_item") or {}).get("text")
                if vtxt:
                    parts.append(str(vtxt))

            ref_msg = item.get("ref_msg") or {}
            if ref_msg:
                msg_item = ref_msg.get("message_item") or {}
                ref_text = (
                    (msg_item.get("text_item") or {}).get("text")
                    or (msg_item.get("voice_item") or {}).get("text")
                    or ref_msg.get("title")
                    or ref_text
                )

        text = "\n".join(parts).strip()
        if text.startswith("[引用]:"):
            text = text.replace("[引用]:", "", 1).strip()
        return text, str(ref_text or "")

    @staticmethod
    def _chunk_text(text: str, max_len: int) -> list[str]:
        text = str(text or "")
        if len(text) <= max_len:
            return [text]

        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= max_len:
                chunks.append(remaining)
                break

            cut = remaining.rfind("\n\n", 0, max_len)
            if cut < int(max_len * 0.3):
                cut = remaining.rfind("\n", 0, max_len)
            if cut < int(max_len * 0.3):
                cut = remaining.rfind(" ", 0, max_len)
            if cut < int(max_len * 0.3):
                cut = max_len

            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip()

        return chunks

    @staticmethod
    def _extract_content(msg: Message) -> str:
        if msg.event_type == EventType.CHAT_ERROR:
            payload = msg.payload if isinstance(msg.payload, dict) else {}
            err = payload.get("error", "处理出错")
            return f"⚠️ {err}"

        if msg.event_type == EventType.HEARTBEAT_RELAY:
            payload = msg.payload if isinstance(msg.payload, dict) else {}
            hb = payload.get("heartbeat")
            return str(hb or "").strip()

        params = msg.params if isinstance(msg.params, dict) else {}
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        content = params.get("content") or payload.get("content") or ""
        if isinstance(content, dict):
            content = content.get("output", str(content))
        return str(content or "").strip()

    def _save_credentials(self) -> None:
        path = Path(self.config.credential_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "botToken": self.config.bot_token,
            "baseUrl": self.config.base_url,
            "ilinkBotId": self.config.ilink_bot_id,
            "ilinkUserId": self.config.ilink_user_id,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
