# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""IMOutboundPipeline — 出站预处理管线：路由决策（群发 vs 私发）。

在 MessageHandler.publish_robot_messages() 入队前拦截，根据 LLM 分类和关键词匹配
决定是否将回复私发给目标用户。Channel.send() 仅读取 metadata 执行实际发送。
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jiuwenclaw.gateway.im_inbound import IMPlatformAdapter
    from jiuwenclaw.schema.message import Message

logger = logging.getLogger(__name__)

_SKIP_EVENT_TYPES = frozenset({
    "chat.delta",
    "chat.tool_call",
    "chat.tool_calls.delta",
    "chat.tool_result",
    "todo.updated",
})

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
            from jiuwenclaw.config import get_config
            cfg = get_config() or {}
        except Exception:
            cfg = {}

        react = cfg.get("react") or {}
        mcc_raw = react.get("model_client_config") or {}
        api_key = (mcc_raw.get("api_key") or os.getenv("API_KEY") or "").strip()
        api_base = (mcc_raw.get("api_base") or os.getenv("API_BASE") or "").strip()
        if api_base.endswith("/chat/completions"):
            api_base = api_base.rsplit("/chat/completions", 1)[0]
        model_name = (react.get("model_name") or os.getenv("MODEL_NAME") or "gpt-4o").strip()
        client_provider = mcc_raw.get("client_provider", "OpenAI")

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
                verify_ssl=False,
            )
            model_config = ModelRequestConfig(
                model_name=model_name,
                temperature=0.1,
                top_p=0.1,
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
        logger.debug(
            "[IMOutboundPipeline] apply 入口: channel=%s msg.id=%s group_digital_avatar=%s",
            msg.channel_id, msg.id, msg.group_digital_avatar
        )

        # 检查是否需要数字分身路由决策
        # 优先使用 msg.group_digital_avatar，其次检查 metadata 中的 avatar_mode
        meta = dict(msg.metadata or {})
        is_digital_avatar = msg.group_digital_avatar or bool(meta.get("avatar_mode"))
        if not is_digital_avatar:
            return

        # 幂等：已经有 reply_scope 则跳过
        if str(meta.get("reply_scope") or "").strip():
            return

        # 跳过不需要路由决策的中间事件
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        event_type = str(payload.get("event_type") or "")
        if event_type in _SKIP_EVENT_TYPES:
            return

        # 仅群聊需要路由决策
        chat_type = str(meta.get("chat_type") or "").strip()
        if chat_type != "group":
            return

        adapter = self._adapters.get(msg.channel_id)
        if adapter is None:
            return

        candidate_user_id = adapter.get_candidate_user_id(meta)
        if not candidate_user_id:
            return

        content = self._extract_content(msg)
        if not content:
            return

        logger.info(
            "[IMOutboundPipeline] 继续处理: channel=%s candidate=%s content_len=%d",
            msg.channel_id, candidate_user_id, len(content)
        )

        # 先进行关键词判断，只有包含个人行动关键词时才需要进一步判断
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

    # ---- helpers ----

    @staticmethod
    def _extract_content(msg: "Message") -> str:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        return str(payload.get("content") or "").strip()

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
