# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""飞书平台的 IMPlatformAdapter 实现，处理飞书特定的用户信息获取、历史消息加载和元数据构建."""

from __future__ import annotations

import json
from typing import Any

from jiuwenswarm.gateway.channel_manager.im_platforms.platform_adapter.message import MessageStore
from jiuwenswarm.gateway.im_pipeline.im_inbound import IMHistoryMessage

try:
    import lark_oapi as lark
    from lark_oapi.api.contact.v3 import GetUserRequest
    from lark_oapi.api.application.v6 import GetApplicationRequest
    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None
    GetUserRequest = None
    GetApplicationRequest = None

from jiuwenswarm.common.utils import logger


class FeishuIMPlatformAdapter:
    channel_id = "feishu"

    def __init__(self, *, api_client: Any = None, my_open_id: str = "", bot_name: str = "") -> None:
        self._my_open_id = my_open_id
        self._bot_name = bot_name
        self._api_client = api_client
        self._message_store = MessageStore(api_client=api_client)
        self._target_user_name_cache: str | None = None

    def set_api_client(self, api_client: Any) -> None:
        self._api_client = api_client
        self._message_store.set_api_client(api_client)
        self._target_user_name_cache = None

    def get_principal_user_id(self) -> str:
        return (self._my_open_id or "").strip()

    def get_user_name_by_open_id(self, open_id: str) -> str:
        """
        根据 open_id 获取用户名。

        Args:
            open_id: 用户 open_id

        Returns:
            str: 用户名，bot开头的返回"bot"，获取失败返回空字符串
        """
        if not open_id:
            return ""
        
        if open_id.startswith("bot"):
            return "bot"
        
        if not self._api_client or not FEISHU_AVAILABLE:
            return ""
        try:
            request = (
                GetUserRequest.builder()
                .user_id(open_id)
                .user_id_type("open_id")
                .department_id_type("open_department_id")
                .build()
            )

            response = self._api_client.contact.v3.user.get(request)
            if not response.success():
                logger.warning(
                    f"获取用户信息失败: open_id={open_id}, code={response.code}, msg={response.msg}"
                )
                return ""
            user = response.data.user
            return getattr(user, "name", "") or ""

        except Exception as e:
            logger.warning(f"获取用户名时发生异常: open_id={open_id}, error={e}")
            return ""

    def resolve_user_display_name(self, user_id: str) -> str:
        return self.get_user_name_by_open_id(user_id).strip()

    def get_principal_display_name(self) -> str:
        if self._target_user_name_cache is not None:
            return self._target_user_name_cache
        principal_user_id = self.get_principal_user_id()
        self._target_user_name_cache = (
            self.resolve_user_display_name(principal_user_id)
            if principal_user_id
            else ""
        )
        return self._target_user_name_cache

    def get_bot_name(self) -> str:
        """获取机器人名称。优先使用飞书 API 动态获取，配置的 bot_name 作为兜底。"""
        if (self._bot_name or "").strip():
            return self._bot_name.strip()

        # 尝试通过飞书 API 获取当前应用名称
        if self._api_client and FEISHU_AVAILABLE and GetApplicationRequest:
            try:
                request = (
                    GetApplicationRequest.builder()
                    .app_id("me")
                    .lang("zh_cn")
                    .build()
                )
                response = self._api_client.application.v6.application.get(request)
                if response.success() and response.data and response.data.app:
                    app_name = getattr(response.data.app, "app_name", "") or ""
                    if app_name.strip():
                        self._bot_name = app_name.strip()
                        logger.info("[FeishuIMAdapter] 从飞书 API 获取机器人名称: %s", self._bot_name)
                        return self._bot_name
            except Exception as e:
                logger.debug("[FeishuIMAdapter] 从飞书 API 获取机器人名称失败: %s", e)

        return ""

    def get_bot_mention_tokens(self) -> list[str]:
        bot_name = self.get_bot_name()
        return [f"@{bot_name}"] if bot_name else []

    def load_recent_messages(
        self,
        thread_id: str,
        limit: int = 500,
    ) -> list[IMHistoryMessage]:
        history = self._message_store.load_memory(thread_id)
        if not isinstance(history, list):
            return []

        recent_history = history[-limit:] if len(history) > limit else history
        normalized_history: list[IMHistoryMessage] = []
        for item in recent_history:
            if not isinstance(item, dict):
                continue
            normalized_history.append(
                IMHistoryMessage(
                    user_id=str(item.get("open_id") or "").strip(),
                    user_name=str(
                        item.get("user_name")
                        or self.resolve_user_display_name(
                            str(item.get("open_id") or "").strip()
                        )
                    ).strip(),
                    content=str(item.get("content") or "").strip(),
                    timestamp_ms=int(item.get("timestamp") or 0),
                )
            )
        return normalized_history

    def build_relevance_metadata(
        self,
        metadata: dict[str, Any],
        *,
        sender_user_id: str,
        relevant: bool,
    ) -> dict[str, Any]:
        if not relevant:
            return {}

        if str(metadata.get("reply_scope") or "").strip():
            return {}
        if str(metadata.get("chat_type") or "").strip() != "group":
            return {}

        principal_user_id = self.get_principal_user_id()
        if not principal_user_id or sender_user_id == principal_user_id:
            return {}

        patch: dict[str, Any] = {
            "reply_candidate_feishu_open_id": principal_user_id,
            "reply_candidate_reason": "processor_target_user",
            "reply_candidate_user_id": principal_user_id,
        }
        principal_name = self.get_principal_display_name()
        if principal_name:
            patch["reply_target_name"] = principal_name
        return patch

    # --- 出站能力 ---

    @property
    def platform_name(self) -> str:
        return "飞书"

    @property
    def reply_user_id_key(self) -> str:
        return "reply_candidate_feishu_open_id"

    @property
    def use_keyword_override(self) -> bool:
        return True

    @staticmethod
    def get_candidate_user_id(metadata: dict[str, Any]) -> str:
        return str(metadata.get("reply_candidate_feishu_open_id") or "").strip()

    def resolve_user_id_by_name(self, name: str) -> str:
        """根据用户名在群聊历史中查找对应的 open_id。"""
        if not name or not self._api_client:
            return ""
        try:
            from jiuwenswarm.common.utils import get_interactions_dir

            interactions_dir = get_interactions_dir()
            for fp in interactions_dir.glob("*.json"):
                try:
                    data = json.loads(fp.read_text(encoding="utf-8"))
                    if data.get("target_user_name") == name:
                        uid = str(data.get("target_user_id") or "").strip()
                        if uid:
                            return uid
                except Exception as e:
                    logger.debug("[FeishuIMAdapter] 解析交互文件 %s 失败: %s", fp, e)
                    continue
        except Exception as e:
            logger.warning("[FeishuIMAdapter] resolve_user_id_by_name 失败: %s", e)
        return ""