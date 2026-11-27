# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""IM 输入管道，负责处理收到的 IM 消息，包括解析、验证、路由等."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from openjiuwen.core.foundation.llm import Model
from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig

from jiuwenclaw.schema.message import Message, ReqMethod
from jiuwenclaw.gateway.slash_command import CONTROL_MESSAGE_TEXTS
from jiuwenclaw.utils import get_root_dir, logger
SYSTEM_PROMPT_TEMPLATE = """
你是{principal_name}的数字分身，活跃在即时通讯群聊中。当群里有其他用户发送与{principal_name}相关的消息时，你的任务是改写这条消息，使其更清晰、更完整，以便后续帮助{principal_name}生成恰当的回复。

## 消息格式说明

收到的消息格式为：`[时间戳] [发送者]: 消息内容`
例如：`[2026-03-20 14:44:34] [某人]: {bot_mention_hint} @{principal_name} 111`

## 判断是否需要处理（按优先级顺序）

### 必须回复的情况（不能输出[无需处理]）：

1. **消息中@了机器人**：如果消息内容中出现机器人 mention（例如 {bot_mention_hint}），无论发送者是谁，都必须回复
2. **其他人@了用户本人**：如果发送者不是{principal_name}本人，且消息中@了{principal_name}，必须回复

### 由模型判断的情况：

如果以上条件都不满足，则根据以下标准判断：
- 当前消息中提到了{principal_name}的名字
- 当前消息是对{principal_name}之前发言的回复或延续
- 当前消息是群聊中需要{principal_name}参与讨论的话题
- 当前消息是否与群里其他人所发历史消息有关

如果判断为无关，输出：[无需处理]

## 改写原则

1. 明确发送者意图：对方想表达什么？是提问、请求、讨论还是闲聊？
2. 补充上下文：如果消息涉及历史对话（如"那个文件"、"刚才说的"），补充具体信息
3. 明确回复期望：对方期望什么样的回应？是解答问题、确认信息、还是参与讨论？
4. 保留原意：不要改变消息的核心意图

## 输出要求

直接输出改写后的消息，不要添加任何解释或额外内容。
""".strip()


@dataclass
class IMHistoryMessage:
    user_id: str
    user_name: str
    content: str
    timestamp_ms: int


class IMPlatformAdapter(Protocol):
    """统一的平台适配器接口，同时服务入站和出站管线。

    每个 IM 平台（飞书 / 企微）实现一个适配器，注册后供
    IMInboundPipeline 和 IMOutboundPipeline 共享使用。
    """

    channel_id: str

    # --- 入站能力 ---

    def get_principal_user_id(self) -> str:
        ...

    def get_principal_display_name(self) -> str:
        ...

    def resolve_user_display_name(self, user_id: str) -> str:
        ...

    def get_bot_mention_tokens(self) -> list[str]:
        ...

    def load_recent_messages(
        self, thread_id: str, limit: int = 500
    ) -> list[IMHistoryMessage]:
        ...

    def build_relevance_metadata(
        self,
        metadata: dict[str, Any],
        *,
        sender_user_id: str,
        relevant: bool,
    ) -> dict[str, Any]:
        ...

    # --- 出站能力 ---

    @property
    def platform_name(self) -> str:
        """平台显示名，用于 LLM prompt（如 "飞书"、"企业微信"）。"""
        ...

    @property
    def reply_user_id_key(self) -> str:
        """metadata 中设置回复目标用户 ID 的 key。

        飞书: "reply_feishu_open_id"
        企微: "reply_wecom_user_id"
        """
        ...

    @property
    def use_keyword_override(self) -> bool:
        """LLM 判断为 CHAT 但关键词命中时，是否覆盖为 DM。"""
        ...

    def get_candidate_user_id(self, metadata: dict[str, Any]) -> str:
        """从 metadata 中提取候选私发目标用户 ID；无候选返回空字符串。"""
        ...


@dataclass
class InboundProcessResult:
    should_forward: bool = True
    rewritten_content: str | None = None
    metadata_patch: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


class IMConversationProcessor:
    def __init__(
        self,
        *,
        user_profile_path: Path | None = None,
        model_name: str | None = None,
    ) -> None:
        self._user_profile_path = (
            user_profile_path
            if user_profile_path is not None
            else get_root_dir() / "workspace" / "agent" / "memory" / "USER.md"
        )
        self._model_name, self._model_client_raw = self._load_model_config(model_name)
        self._llm: Model | None = None

    @staticmethod
    def _load_model_config(model_name_override: str | None = None) -> tuple[str, dict]:
        """与 react agent 一致的模型配置读取：config.yaml → 环境变量 → 默认值。"""
        try:
            from jiuwenclaw.config import get_config
            cfg = get_config() or {}
        except Exception:
            cfg = {}
        react = cfg.get("react") or {}
        mcc = react.get("model_client_config") or {}
        name = (
            (model_name_override or "").strip()
            or react.get("model_name", "")
            or os.getenv("MODEL_NAME", "").strip()
            or "gpt-4o"
        )
        return name, mcc

    async def process(
        self,
        msg: Message,
        adapter: IMPlatformAdapter,
    ) -> InboundProcessResult:
        if msg.req_method != ReqMethod.CHAT_SEND:
            return InboundProcessResult(reason="non-chat-send")

        text = self._extract_text(msg)
        if not text:
            return InboundProcessResult(reason="empty-content")
        if text.strip() in CONTROL_MESSAGE_TEXTS:
            return InboundProcessResult(reason="control-message")

        metadata = dict(msg.metadata or {})
        chat_type = str(
            metadata.get("im_chat_type") or metadata.get("chat_type") or ""
        ).strip().lower()
        if chat_type != "group":
            return InboundProcessResult(reason="non-group-chat")

        sender_user_id = str(
            metadata.get("im_sender_user_id")
            or metadata.get("open_id")
            or metadata.get("sender_id")
            or ""
        ).strip()
        thread_id = str(
            metadata.get("im_thread_id")
            or metadata.get("feishu_chat_id")
            or msg.session_id
            or ""
        ).strip()

        principal_user_id = adapter.get_principal_user_id().strip()
        principal_name = adapter.get_principal_display_name().strip() or "用户"
        if not principal_user_id:
            return InboundProcessResult(reason="missing-principal-user")

        if self._should_always_reply(
            text=text,
            metadata=metadata,
            sender_user_id=sender_user_id,
            principal_user_id=principal_user_id,
            principal_name=principal_name,
            bot_mentions=adapter.get_bot_mention_tokens(),
        ):
            return InboundProcessResult(
                metadata_patch=adapter.build_relevance_metadata(
                    metadata,
                    sender_user_id=sender_user_id,
                    relevant=True,
                ),
                reason="always-reply",
            )

        prompt = self._build_prompt(
            thread_id=thread_id,
            sender_user_id=sender_user_id,
            text=text,
            timestamp_ms=self._resolve_timestamp_ms(msg.timestamp, metadata),
            principal_name=principal_name,
            adapter=adapter,
        )
        rewritten_content = await self._rewrite_query(prompt, principal_name, adapter)
        if not rewritten_content:
            return InboundProcessResult(reason="rewrite-failed")

        normalized_content = rewritten_content.strip()
        if normalized_content == "[无需处理]":
            if self._has_image_context(metadata):
                return InboundProcessResult(reason="image-fallback")
            return InboundProcessResult(should_forward=False, reason="irrelevant")

        return InboundProcessResult(
            rewritten_content=normalized_content,
            metadata_patch=adapter.build_relevance_metadata(
                metadata,
                sender_user_id=sender_user_id,
                relevant=True,
            ),
            reason="rewritten",
        )

    @staticmethod
    def _extract_text(msg: Message) -> str:
        if not isinstance(msg.params, dict):
            return ""
        query = msg.params.get("query")
        if isinstance(query, str) and query.strip():
            return query
        content = msg.params.get("content")
        if isinstance(content, str):
            return content
        return ""

    @staticmethod
    def _resolve_timestamp_ms(timestamp: float, metadata: dict[str, Any]) -> int:
        raw_ts = metadata.get("timestamp_ms")
        if isinstance(raw_ts, int):
            return raw_ts
        if isinstance(raw_ts, str) and raw_ts.strip().isdigit():
            return int(raw_ts.strip())
        return int(timestamp * 1000)

    @staticmethod
    def _has_image_context(metadata: dict[str, Any]) -> bool:
        if str(metadata.get("msg_type") or "").strip() == "image":
            return True
        merged_types = metadata.get("merged_msg_types") or []
        if isinstance(merged_types, list):
            return any(str(item).strip() == "image" for item in merged_types)
        return False

    @staticmethod
    def _should_always_reply(
        *,
        text: str,
        metadata: dict[str, Any],
        sender_user_id: str,
        principal_user_id: str,
        principal_name: str,
        bot_mentions: list[str],
    ) -> bool:
        mentioned_user_ids = metadata.get("im_mentioned_user_ids") or metadata.get(
            "mentioned_open_ids"
        ) or []
        if isinstance(mentioned_user_ids, list):
            if principal_user_id in [str(item).strip() for item in mentioned_user_ids]:
                return True

        for token in bot_mentions:
            if token and token in text:
                return True
            # 同时检查机器人名称（从配置读取）是否在文本中
            try:
                from jiuwenclaw.config import get_config
                cfg = get_config()
                bot_name = str(
                    cfg.get("bot_name") or
                    cfg.get("channels", {}).get("wecom", {}).get("bot_name") or
                    ""
                ).strip()
                if bot_name and bot_name in text:
                    return True
            except Exception as e:
                logger.debug(f"Failed to get bot_name from config: {e}")

        is_not_me = sender_user_id and sender_user_id != principal_user_id
        if is_not_me and principal_name and f"@{principal_name}" in text:
            return True
        return False

    def _build_prompt(
        self,
        *,
        thread_id: str,
        sender_user_id: str,
        text: str,
        timestamp_ms: int,
        principal_name: str,
        adapter: IMPlatformAdapter,
    ) -> str:
        prompt_parts: list[str] = []
        prompt_parts.append("=== 群聊历史消息 ===")
        history = adapter.load_recent_messages(thread_id, limit=500)
        if history:
            prompt_parts.append(f"最近 {len(history)} 条消息：\n")
            for msg in history:
                dt = datetime.fromtimestamp(int(msg.timestamp_ms) / 1000)
                prompt_parts.append(
                    f"[{dt.strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"[{msg.user_name or '未知用户'}]: {msg.content}"
                )
            prompt_parts.append("")
        else:
            prompt_parts.append("暂无历史消息\n")

        prompt_parts.append("=== 用户画像 ===")
        user_profile = self._load_user_profile()
        prompt_parts.append(user_profile if user_profile else "暂无用户画像信息")
        prompt_parts.append("")

        prompt_parts.append("=== 当前消息 ===")
        sender_name = adapter.resolve_user_display_name(sender_user_id) or "未知用户"
        dt = datetime.fromtimestamp(int(timestamp_ms) / 1000)
        prompt_parts.append(
            f"[{dt.strftime('%Y-%m-%d %H:%M:%S')}] [{sender_name}]: {text}"
        )
        prompt_parts.append("")

        return "\n".join(prompt_parts)

    def _load_user_profile(self) -> str:
        try:
            if not self._user_profile_path.exists():
                return ""
            return self._user_profile_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("[IMConversationProcessor] 读取 USER.md 失败: %s", exc)
            return ""

    def _ensure_llm(self) -> Model | None:
        if self._llm is not None:
            return self._llm
        try:
            model_cfg = ModelRequestConfig(
                model=self._model_name,
                temperature=0.2,
                top_p=0.7,
            )
            mcc = self._model_client_raw
            api_key = (mcc.get("api_key") or os.getenv("API_KEY") or "").strip()
            api_base = (mcc.get("api_base") or os.getenv("API_BASE") or "").strip()
            if api_base.endswith("/chat/completions"):
                api_base = api_base.rsplit("/chat/completions", 1)[0]
            client_provider = mcc.get("client_provider") or os.getenv("MODEL_PROVIDER", "OpenAI")
            model_client_cfg = ModelClientConfig(
                client_id="im_conversation_processor_client",
                client_provider=client_provider,
                api_key=api_key,
                api_base=api_base,
                verify_ssl=False,
                timeout=180.0,
            )
            self._llm = Model(
                model_config=model_cfg,
                model_client_config=model_client_cfg,
            )
        except Exception as exc:
            logger.warning(
                "[IMConversationProcessor] 初始化 LLM 失败，将回退原始消息: %s",
                exc,
            )
            self._llm = None
        return self._llm

    async def _rewrite_query(
        self,
        prompt: str,
        principal_name: str,
        adapter: IMPlatformAdapter,
    ) -> str | None:
        llm = self._ensure_llm()
        if llm is None:
            return None

        bot_mentions = adapter.get_bot_mention_tokens()
        bot_mention_hint = " / ".join(bot_mentions) if bot_mentions else "@机器人"
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            principal_name=principal_name,
            bot_mention_hint=bot_mention_hint,
        )
        try:
            response = await llm.invoke(
                model=self._model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
            )
            if response and isinstance(response.content, str):
                return response.content.strip() or None
            
        except Exception as exc:
            logger.warning(
                "[IMConversationProcessor] 调用 LLM 改写失败，将回退原始消息: %s",
                exc,
            )
        return None


class IMInboundPipeline:
    def __init__(
        self,
        *,
        processor: IMConversationProcessor | None = None,
        adapters: dict[str, IMPlatformAdapter] | None = None,
    ) -> None:
        self._processor = processor or IMConversationProcessor()
        self._adapters: dict[str, IMPlatformAdapter] = dict(adapters or {})

    def register_adapter(self, channel_id: str, adapter: IMPlatformAdapter) -> None:
        self._adapters[channel_id] = adapter

    def unregister_adapter(self, channel_id: str) -> None:
        self._adapters.pop(channel_id, None)

    async def apply(self, msg: Message) -> bool:
        if not msg.group_digital_avatar:
            return True

        adapter = self._adapters.get(msg.channel_id)
        if adapter is None:
            return True

        original_query = ""
        if isinstance(msg.params, dict):
            query = msg.params.get("query")
            content = msg.params.get("content")
            if isinstance(query, str) and query.strip():
                original_query = query
            elif isinstance(content, str):
                original_query = content

        result = await self._processor.process(msg, adapter)

        if result.metadata_patch:
            merged_metadata = dict(msg.metadata or {})
            merged_metadata.update(result.metadata_patch)
            msg.metadata = merged_metadata

        if result.rewritten_content is not None:
            if not isinstance(msg.params, dict):
                msg.params = {}
            msg.params["query"] = result.rewritten_content
            if "content" in msg.params:
                msg.params["content"] = result.rewritten_content
        
        # 将原始 query 存入 metadata，供 outbound pipeline 路由决策使用
        if original_query:
            merged = dict(msg.metadata or {})
            merged["avatar_original_query"] = original_query
            msg.metadata = merged

        # 补全 avatar 详情字段（avatar_mode 已在 channel 层源头注入）
        # 只在群聊模式下补全 avatar 详情字段
        if result.should_forward and result.reason != "non-group-chat":
            principal_name = adapter.get_principal_display_name().strip()
            avatar_detail: dict[str, Any] = {
                "avatar_mode": True,
                "avatar_channel_type": adapter.platform_name,
            }
            if principal_name:
                avatar_detail["avatar_principal_name"] = principal_name
            principal_id = adapter.get_principal_user_id().strip()
            if principal_id:
                # 统一用 principal_user_id，供权限方案和 prompt 方案共享
                avatar_detail["principal_user_id"] = principal_id
            merged = dict(msg.metadata or {})
            merged.update(avatar_detail)
            msg.metadata = merged

        rewritten_preview = (result.rewritten_content or "").replace("\n", "\\n")[:120]
        original_preview = (original_query or "").replace("\n", "\\n")[:120]
        metadata_keys = sorted(list((result.metadata_patch or {}).keys()))

        logger.info(
            "[IMInboundPipeline] channel=%s request=%s should_forward=%s reason=%s rewritten=%s "
            "original_preview=%r rewritten_preview=%r metadata_keys=%s",
            msg.channel_id,
            msg.id,
            result.should_forward,
            result.reason,
            result.rewritten_content is not None,
            original_preview,
            rewritten_preview,
            metadata_keys,
        )
        return result.should_forward