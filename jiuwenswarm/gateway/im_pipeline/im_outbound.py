# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""IMOutboundPipeline — 出站预处理管线：路由决策（群发 vs 私发）+ 追问前缀解析。

在 MessageHandler.publish_robot_messages() 入队前拦截，根据 LLM 分类和关键词匹配
决定是否将回复私发给目标用户。Channel.send() 仅读取 metadata 执行实际发送。

追问前缀约定：
  [群聊追问@张三] → 群聊中 @张三 追问
  [私聊追问]      → 私聊 principal 追问
"""

from __future__ import annotations

import logging
import re
import secrets
from typing import TYPE_CHECKING, Any

from jiuwenswarm.common.config import _parse_custom_headers
from jiuwenswarm.common.local_env_config import read_env
from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs
from jiuwenswarm.gateway.routing.interaction_context import PendingInteraction

if TYPE_CHECKING:
    from jiuwenswarm.gateway.im_pipeline.im_inbound import IMPlatformAdapter
    from jiuwenswarm.common.schema.message import Message

logger = logging.getLogger(__name__)

_CLASSIFIABLE_EVENT_TYPES = frozenset({"chat.final"})

_GROUP_ACK_KEYWORDS: tuple[str, ...] = (
    "待办",
    "提醒",
    "定时",
    "日程",
    "会议",
    "行程",
    "安排",
    "记下了",
    "记住了",
)

_RE_GROUP_FOLLOWUP = re.compile(r"^\[群聊追问(?:@(.+?))?\]\s*")
_RE_DM_FOLLOWUP = re.compile(r"^\[私聊追问\]\s*")


class IMOutboundPipeline:
    """出站预处理管线：根据回复内容决定是否私发给目标用户。"""

    def __init__(self) -> None:
        self._adapters: dict[str, "IMPlatformAdapter"] = {}
        self._llm = None          # openjiuwen Model instance (lazy)
        self._llm_model_name: str = ""

    def register_adapter(self, channel_id: str, adapter: "IMPlatformAdapter") -> None:
        self._adapters[channel_id] = adapter

    def unregister_adapter(self, channel_id: str) -> None:
        self._adapters.pop(channel_id, None)

    # ---- LLM 初始化（与 react agent 一致） ----

    def _ensure_llm(self) -> bool:
        """懒加载 openjiuwen Model 实例，配置读取与 react agent 保持一致。"""
        if self._llm is not None:
            return True

        try:
            from jiuwenswarm.common.config import get_config
            cfg = get_config() or {}
        except Exception:
            cfg = {}

        react = cfg.get("react") or {}
        mcc_raw = react.get("model_client_config") or {}
        model_config_obj = react.get("model_config_obj") or {}
        api_key = (mcc_raw.get("api_key") or read_env("API_KEY") or "").strip()
        api_base = (mcc_raw.get("api_base") or read_env("API_BASE") or "").strip()
        if api_base.endswith("/chat/completions"):
            api_base = api_base.rsplit("/chat/completions", 1)[0]
        model_name = (
            react.get("model_name")
            or mcc_raw.get("model_name")
            or read_env("MODEL_NAME")
            or "gpt-4o"
        ).strip()
        client_provider = mcc_raw.get("client_provider", "OpenAI")
        custom_headers = _parse_custom_headers(mcc_raw.get("custom_headers") or read_env("CUSTOM_HEADERS"))

        if not api_key or not api_base:
            logger.warning(
                "[IMOutboundPipeline] LLM 跳过：API_KEY=%s API_BASE=%s",
                "set" if api_key else "empty",
                "set" if api_base else "empty",
            )
            return False

        try:
            from openjiuwen.core.foundation.llm import Model
            from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig

            client_config = ModelClientConfig(
                client_provider=client_provider,
                api_key=api_key,
                api_base=api_base,
                verify_ssl=mcc_raw.get("verify_ssl", True),
                ssl_cert=mcc_raw.get("ssl_cert", None),
                custom_headers=custom_headers,
            )
            reasoning_mcc = {
                **dict(mcc_raw or {}),
                "client_provider": client_provider,
                "api_base": api_base,
            }
            runtime_model_config_obj = {
                **dict(model_config_obj or {}),
                "temperature": 0.1,
                "top_p": 0.1,
            }
            model_config = ModelRequestConfig(
                **build_reasoning_model_request_kwargs(
                    model_client_config=reasoning_mcc,
                    model_config_obj=runtime_model_config_obj,
                    model_name=model_name,
                )
            )
            self._llm = Model(model_client_config=client_config, model_config=model_config)
            self._llm_model_name = model_name
            logger.info(
                "[IMOutboundPipeline] LLM 初始化完成: model=%s provider=%s api_base=%s",
                model_name, client_provider,
                api_base[:30] + "..." if len(api_base) > 30 else api_base,
            )
            return True
        except Exception as exc:
            logger.warning("[IMOutboundPipeline] LLM 初始化失败: %s", exc)
            return False

    # ---- public entry ----

    async def apply(self, msg: "Message") -> None:
        """对出站消息执行路由决策，结果写入 msg.metadata（原地修改）。"""
        # 检查是否需要数字分身路由决策
        # 优先使用 msg.group_digital_avatar，其次检查 metadata 中的 avatar_mode
        if not msg.group_digital_avatar and not (msg.metadata or {}).get("avatar_mode"):
            return

        meta = dict(msg.metadata or {})

        payload = msg.payload if isinstance(msg.payload, dict) else {}
        message_event_type = getattr(msg, "event_type", None)
        event_type = str(
            payload.get("event_type")
            or getattr(message_event_type, "value", message_event_type)
            or ""
        )
        if event_type and event_type not in _CLASSIFIABLE_EVENT_TYPES:
            return

        chat_type = str(meta.get("chat_type") or "").strip()
        if chat_type != "group":
            return

        adapter = self._adapters.get(msg.channel_id)
        if adapter is None:
            return

        content = self._extract_content(msg)
        if not content:
            return

        # ---- 群聊追问的 pending 清除逻辑（必须在追问前缀解析之前） ----
        answered_user_id = str(meta.get("interaction_answered_user_id") or "").strip()
        if answered_user_id:
            session_id = str(msg.session_id or "").strip()
            if session_id:
                existing = PendingInteraction.find_group_pending(session_id, answered_user_id)
                if existing:
                    existing.remove()
                    logger.info(
                        "[IMOutboundPipeline] clear_pending: session=%s user=%s",
                        session_id, answered_user_id,
                    )

        # ---- 追问前缀解析（优先于原有路由逻辑） ----
        group_match = _RE_GROUP_FOLLOWUP.match(content)
        dm_match = _RE_DM_FOLLOWUP.match(content)

        if group_match:
            await self._handle_group_followup(msg, meta, adapter, content, group_match)
            return

        if dm_match:
            await self._handle_dm_followup(msg, meta, adapter, content)
            return

        # ---- 原有路由逻辑 ----
        if str(meta.get("reply_scope") or "").strip():
            return

        candidate_user_id = adapter.get_candidate_user_id(meta)
        if not candidate_user_id:
            return

        logger.info(
            "[IMOutboundPipeline] 继续处理: channel=%s candidate=%s content_len=%d",
            msg.channel_id, candidate_user_id, len(content)
        )

        keyword_hit = _is_personal_action_reply(content)

        is_personal, llm_raw = await self._classify_personal_action(
            adapter.platform_name, meta, content,
        )
        if is_personal is None:
            logger.info(
                "[IMOutboundPipeline] LLM 无结果，关键词兜底=%s: channel=%s request=%s llm_output=%r content_snippet=%r",
                keyword_hit, msg.channel_id, msg.id, llm_raw, content[:100],
            )
            is_personal = keyword_hit
        elif is_personal:
            logger.info(
                "[IMOutboundPipeline] LLM=DM: channel=%s request=%s keyword_hit=%s llm_output=%r content_snippet=%r",
                msg.channel_id, msg.id, keyword_hit, llm_raw, content[:100],
            )
        else:
            logger.info(
                "[IMOutboundPipeline] LLM=CHAT: channel=%s request=%s keyword_hit=%s llm_output=%r content_snippet=%r",
                msg.channel_id, msg.id, keyword_hit, llm_raw, content[:100],
            )

        if not is_personal:
            logger.info(
                "[IMOutboundPipeline] 判定为群聊，不升级 DM: channel=%s request=%s",
                msg.channel_id, msg.id,
            )
            return

        meta["reply_scope"] = "dm"
        meta[adapter.reply_user_id_key] = candidate_user_id
        meta["reply_reason"] = str(
            meta.get("reply_candidate_reason") or "processor_target_user"
        ).strip()
        meta["reply_personal_action"] = True
        msg.metadata = meta

        logger.info(
            "[IMOutboundPipeline] 升级为私发: channel=%s request=%s reply_user_id_key=%s",
            msg.channel_id, msg.id, adapter.reply_user_id_key,
        )

    # ---- 追问处理 ----

    async def _handle_group_followup(
        self,
        msg: "Message",
        meta: dict[str, Any],
        adapter: "IMPlatformAdapter",
        content: str,
        match: re.Match,
    ) -> None:
        target_name = (match.group(1) or "").strip()
        stripped = _RE_GROUP_FOLLOWUP.sub("", content).strip()

        target_user_id = ""
        if target_name and hasattr(adapter, "resolve_user_id_by_name"):
            target_user_id = adapter.resolve_user_id_by_name(target_name)

        if not target_user_id:
            sender_user_id = str(
                meta.get("im_sender_user_id")
                or meta.get("open_id")
                or meta.get("sender_id")
                or ""
            ).strip()
            if sender_user_id:
                target_user_id = sender_user_id
                if not target_name and hasattr(adapter, "resolve_user_display_name"):
                    target_name = adapter.resolve_user_display_name(sender_user_id)
            else:
                candidate_user_id = adapter.get_candidate_user_id(meta)
                if candidate_user_id:
                    target_user_id = candidate_user_id
                    if not target_name:
                        target_name = str(meta.get("reply_target_name") or "").strip()

        meta["interaction_mention_user"] = target_name
        meta["interaction_mention_user_id"] = target_user_id
        meta["interaction_question"] = True

        payload = msg.payload if isinstance(msg.payload, dict) else {}
        payload["content"] = content
        msg.payload = payload

        session_id = str(msg.session_id or "").strip()
        origin_content = str(meta.get("avatar_original_query") or "").strip()
        origin_sender_id = str(
            meta.get("im_sender_user_id")
            or meta.get("open_id")
            or meta.get("sender_id")
            or ""
        ).strip()
        origin_sender_name = target_name

        pi = PendingInteraction(
            interaction_id=f"gpq_{session_id}_{target_user_id}",
            mode="group",
            origin_channel_id=msg.channel_id,
            origin_session_id=session_id,
            origin_content=origin_content,
            origin_sender_name=origin_sender_name,
            origin_sender_id=origin_sender_id,
            question=stripped,
            target_user_id=target_user_id,
            target_user_name=target_name,
            origin_metadata=dict(meta),
        )
        pi.save()

        msg.metadata = meta
        logger.info(
            "[IMOutboundPipeline] 群聊追问: session=%s target=%s(%s) question=%s",
            session_id, target_name, target_user_id, stripped[:80],
        )

    async def _handle_dm_followup(
        self,
        msg: "Message",
        meta: dict[str, Any],
        adapter: "IMPlatformAdapter",
        content: str,
    ) -> None:
        stripped = _RE_DM_FOLLOWUP.sub("", content).strip()

        meta["reply_scope"] = "dm"
        meta["interaction_question"] = True

        candidate_user_id = adapter.get_candidate_user_id(meta)
        if candidate_user_id:
            meta[adapter.reply_user_id_key] = candidate_user_id

        payload = msg.payload if isinstance(msg.payload, dict) else {}
        payload["content"] = content
        msg.payload = payload

        session_id = str(msg.session_id or "").strip()
        origin_content = str(meta.get("avatar_original_query") or "").strip()
        origin_sender_name = str(meta.get("reply_target_name") or "").strip()
        origin_sender_id = str(
            meta.get("im_sender_user_id")
            or meta.get("open_id")
            or meta.get("sender_id")
            or ""
        ).strip()
        target_user_id = candidate_user_id or ""

        hex_suffix = secrets.token_hex(4)
        pi = PendingInteraction(
            interaction_id=f"iact_{msg.channel_id}_{hex_suffix}",
            mode="dm",
            origin_channel_id=msg.channel_id,
            origin_session_id=session_id,
            origin_content=origin_content,
            origin_sender_name=origin_sender_name,
            origin_sender_id=origin_sender_id,
            question=stripped,
            target_user_id=target_user_id,
            origin_metadata=dict(meta),
        )
        pi.save()

        msg.metadata = meta
        logger.info(
            "[IMOutboundPipeline] DM 追问: session=%s target=%s question=%s",
            session_id, target_user_id, stripped[:80],
        )

    # ---- helpers ----

    @staticmethod
    def _extract_content(msg: "Message") -> str:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        content = payload.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, dict):
            for key in ("output", "text", "message"):
                val = content.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return str(content or "").strip()

    async def _classify_personal_action(
        self,
        platform_name: str,
        metadata: dict[str, Any],
        content: str,
    ) -> tuple[bool | None, str]:
        """使用 openjiuwen Model.invoke 判断回复是否属于个人行动类内容。

        Returns:
            (decision, llm_raw_text) — decision 为 True(DM)/False(CHAT)/None(失败),
            llm_raw_text 为 LLM 原始返回文本（失败时为空字符串）。
        """
        if not self._ensure_llm():
            return None, ""

        target_name = str(metadata.get("reply_target_name") or "").strip() or "目标用户"
        original_query = str(metadata.get("avatar_original_query") or "").strip()

        query_section = ""
        if original_query:
            query_section = f"群聊中的原始提问：\n{original_query[:300]}\n\n"

        prompt = (
            "请判断下面这条{platform}机器人回复，是否应该私发给特定用户，而不是直接回到群里。\n\n"
            "判断标准：\n"
            "- 如果内容是在为该用户记录待办、设置提醒、安排日程、私下跟进、单独通知，输出 DM\n"
            "- 如果内容是在公开回复群讨论、解释问题、同步信息、继续群聊协作，输出 CHAT\n"
            "- 只输出 DM 或 CHAT，不要输出其他内容\n\n"
            "{query_section}"
            "目标用户：{name}\n"
            "机器人回复：\n{content}"
        ).format(
            platform=platform_name,
            query_section=query_section,
            name=target_name,
            content=content[:500],
        )

        logger.info(
            "[IMOutboundPipeline] LLM 请求: model=%r target=%s content_len=%d prompt:\n%s",
            self._llm_model_name, target_name, len(content), prompt,
        )

        try:
            from openjiuwen.core.foundation.llm import UserMessage

            result = await self._llm.invoke(
                messages=[UserMessage(content=prompt)],
                model=self._llm_model_name,
                temperature=0,
                timeout=60,
            )
            text = (result.content or "").strip().upper()
            logger.info("[IMOutboundPipeline] LLM 原始返回: %r", text)
            if "DM" in text:
                return True, text
            if "CHAT" in text:
                return False, text
            logger.warning("[IMOutboundPipeline] LLM 返回无法解析为 DM/CHAT: %r", text)
            return None, text
        except Exception as e:
            logger.warning("[IMOutboundPipeline] LLM 判断回复投递意图失败: %s", e)

        return None, ""


def _is_personal_action_reply(content: str) -> bool:
    """关键词兜底：粗略判断回复是否更适合私发给目标用户。"""
    normalized = re.sub(r"\s+", "", content or "")
    return bool(normalized) and any(kw in normalized for kw in _GROUP_ACK_KEYWORDS)
