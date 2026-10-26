# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
import threading
import json
from typing import Any
from pathlib import Path
from jiuwenswarm.common.utils import logger
from jiuwenswarm.gateway.tenant_paths import resolve_channel_group_chat_memory_dir

# lark_oapi (飞书 SDK) 体积巨大 (21175 文件 / 49MB), 启动期 import 耗时 ~5-8s/进程.
# 历史在模块顶层 try-import, 导致只要 import MessageStore 就强制加载 lark_oapi
# (本模块是所有 im_platforms 的共享基类, 经 wecom_connect -> channel_manager.__init__
# 进入启动链路). 实际 lark_oapi 仅在 load_feishu_history 里用到, 改为惰性:
# 首次调用 _load_feishu_symbols() 时才 import, 未启用飞书时永不加载.
lark = None
ListMessageRequest = None
_FEISHU_SYMBOLS_LOADED = False


def _load_feishu_symbols() -> bool:
    """惰性加载 lark_oapi 符号. 首次调用时 import, 之后从缓存读. 返回是否可用."""
    global lark, ListMessageRequest, _FEISHU_SYMBOLS_LOADED
    if _FEISHU_SYMBOLS_LOADED:
        return lark is not None
    _FEISHU_SYMBOLS_LOADED = True
    try:
        import lark_oapi as _lark  # noqa: F401  (绑定到模块名 lark 供历史代码使用)
        from lark_oapi.api.im.v1 import ListMessageRequest as _LMR

        lark = _lark
        ListMessageRequest = _LMR
        return True
    except ImportError:
        return False


MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


class MessageStore:
    def __init__(self, api_client: Any = None, platform_adapter: Any = None):
        # Paths are resolved lazily per (service_id, agent_id); do not mkdir at init.
        self._api_client = api_client  # 飞书API客户端
        self._platform_adapter = platform_adapter  # 平台适配器，用于获取用户信息等
        self._memory_lock = threading.Lock()  # 记忆文件读写锁

    @staticmethod
    def _memory_dir(
        service_id: str | None = None,
        agent_id: str | None = None,
    ) -> Path:
        from jiuwenswarm.gateway.tenant_paths import workspace_key_from_channel_ids

        return resolve_channel_group_chat_memory_dir(
            workspace_key_from_channel_ids(service_id, agent_id)
        )

    def _get_memory_file_path(
        self,
        chat_id: str,
        *,
        service_id: str | None = None,
        agent_id: str | None = None,
    ) -> Path:
        """获取指定群聊的记忆文件路径。"""
        return self._memory_dir(service_id, agent_id) / f"{chat_id}.json"

    def _legacy_memory_file(
        self,
        *,
        service_id: str | None = None,
        agent_id: str | None = None,
    ) -> Path:
        return self._memory_dir(service_id, agent_id) / "feishu_memory.json"

    def set_api_client(self, api_client: Any) -> None:
        """设置飞书API客户端。"""
        self._api_client = api_client
        logger.info("飞书API客户端已设置")

    def set_platform_adapter(self, platform_adapter: Any) -> None:
        """设置平台适配器。"""
        self._platform_adapter = platform_adapter
        logger.info("平台适配器已设置")

    def get_user_name_by_open_id(self, open_id: str) -> str:
        """获取用户名称，优先使用平台适配器。"""
        if self._platform_adapter and hasattr(self._platform_adapter, "get_user_name_by_open_id"):
            return self._platform_adapter.get_user_name_by_open_id(open_id)
        return ""

    def load_memory(
        self,
        chat_id: str | None = None,
        *,
        service_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, list] | list:
        """加载飞书记忆文件（按租户路径隔离）。"""
        with self._memory_lock:
            if chat_id:
                memory_file = self._get_memory_file_path(
                    chat_id, service_id=service_id, agent_id=agent_id
                )
                if not memory_file.exists():
                    return []
                try:
                    with open(memory_file, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception as e:
                    logger.warning("加载群聊记忆文件失败: %s, chat_id=%s", e, chat_id)
                    return []

            memory_file = self._legacy_memory_file(service_id=service_id, agent_id=agent_id)
            if not memory_file.exists():
                return {}
            try:
                with open(memory_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("加载飞书记忆文件失败: %s", e)
                return {}

    def _save_memory(
        self,
        memory: dict[str, list] | list,
        chat_id: str | None = None,
        *,
        service_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """保存飞书记忆文件（按租户路径隔离）。"""
        with self._memory_lock:
            try:
                memory_dir = self._memory_dir(service_id, agent_id)
                memory_dir.mkdir(parents=True, exist_ok=True)
                if chat_id:
                    memory_file = self._get_memory_file_path(
                        chat_id, service_id=service_id, agent_id=agent_id
                    )
                    with open(memory_file, "w", encoding="utf-8") as f:
                        json.dump(memory, f, ensure_ascii=False, indent=2)
                else:
                    memory_file = self._legacy_memory_file(
                        service_id=service_id, agent_id=agent_id
                    )
                    with open(memory_file, "w", encoding="utf-8") as f:
                        json.dump(memory, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning("保存飞书记忆文件失败: %s", e)

    @staticmethod
    def _parse_history_message_content(item: Any) -> str:
        """
        解析历史消息内容（从API返回的消息对象）。

        Args:
            item: 飞书API返回的消息对象

        Returns:
            str: 解析后的消息内容
        """
        msg_type = getattr(item, "msg_type", "")

        if msg_type == "text":
            try:
                body = getattr(item, "body", None)
                if body and hasattr(body, "content"):
                    content_str = body.content
                    content_data = json.loads(content_str)
                    return content_data.get("text", "")
            except (json.JSONDecodeError, AttributeError):
                pass

            return getattr(item, "content", "") or ""
        elif msg_type == "interactive":
            try:
                body = getattr(item, "body", None)
                if body and hasattr(body, "content"):
                    content_str = body.content
                    content_data = json.loads(content_str)
                    if isinstance(content_data, dict):
                        elements = content_data.get("elements", [])
                        texts = []

                        def extract_text_from_elem(elem):
                            if isinstance(elem, dict):
                                tag = elem.get("tag", "")
                                if tag == "text":
                                    text_content = elem.get("text", "")
                                    if text_content:
                                        texts.append(text_content)
                                elif tag == "div":
                                    text_obj = elem.get("text", {})
                                    if isinstance(text_obj, dict):
                                        md_content = text_obj.get("content", "")
                                        if md_content:
                                            texts.append(md_content)
                            elif isinstance(elem, list):
                                for sub_elem in elem:
                                    extract_text_from_elem(sub_elem)

                        for elem in elements:
                            extract_text_from_elem(elem)

                        return "\n".join(texts) if texts else "[interactive card]"
            except (json.JSONDecodeError, AttributeError):
                pass

            return "[interactive]"
        else:
            return MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")
    
    def _fetch_history_from_feishu(
        self, chat_id: str, start_time: int = 0
    ) -> list[dict]:
        """
        从飞书API拉取历史消息。

        Args:
            chat_id: 聊天ID
            start_time: 开始时间戳（毫秒），0表示拉取所有历史

        Returns:
            list: 历史消息列表
        """
        if not self._api_client or not _load_feishu_symbols():
            logger.warning("飞书API客户端未初始化，无法拉取历史消息")
            return []

        try:
            builder = (
                ListMessageRequest.builder()
                .container_id_type("chat")
                .container_id(chat_id)
                .sort_type("ByCreateTimeAsc")
                .page_size(50)
            )

            if start_time > 0:
                builder.start_time(str(start_time))

            request = builder.build()
            response = self._api_client.im.v1.message.list(request)

            if not response.success():
                logger.warning(
                    f"拉取飞书历史消息失败: code={response.code}, msg={response.msg}"
                )
                return []
            messages = []
            if response.data and response.data.items:
                for item in response.data.items:
                    msg_content = self._parse_history_message_content(item)
                    if msg_content:
                        open_id = ""
                        user_name = ""

                        sender = getattr(item, "sender", None)
                        if sender:
                            sender_id = getattr(sender, "id", None)
                            sender_id_type = getattr(sender, "id_type", None)

                            if sender_id and sender_id_type:
                                if sender_id_type == "open_id":
                                    open_id = sender_id
                                    user_name = self.get_user_name_by_open_id(
                                        sender_id
                                    )
                                elif sender_id_type == "app_id":
                                    user_name = f"bot_{sender_id}"

                        messages.append(
                            {
                                "message_id": getattr(item, "message_id", ""),
                                "content": msg_content,
                                "timestamp": getattr(item, "create_time", 0),
                                "msg_type": getattr(item, "msg_type", ""),
                                "open_id": open_id,
                                "user_name": user_name,
                            }
                        )

            logger.info(f"从飞书拉取了 {len(messages)} 条历史消息: chat_id={chat_id}")
            return messages

        except Exception as e:
            logger.warning(f"拉取飞书历史消息时发生异常: {e}")
            return []

    def _get_or_fetch_history(
        self,
        chat_id: str,
        *,
        service_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[dict]:
        """获取或拉取会话历史消息（按租户懒解析本地文件）。"""
        from datetime import datetime, timedelta, timezone

        memory = self.load_memory(chat_id, service_id=service_id, agent_id=agent_id)
        memory_file = self._get_memory_file_path(
            chat_id, service_id=service_id, agent_id=agent_id
        )

        if not memory_file.exists():
            logger.info(f"[调试] 本地记忆文件不存在，首次拉取过去7天历史: chat_id={chat_id}")
            now = datetime.now(timezone.utc)
            start_time = int((now - timedelta(days=7)).timestamp() * 1000)
            history = self._fetch_history_from_feishu(chat_id, start_time=start_time)
            self._save_memory(history, chat_id, service_id=service_id, agent_id=agent_id)
            logger.info(f"[调试] 首次拉取历史消息完成: chat_id={chat_id}, 消息数={len(history)}")
            return history

        logger.info(f"[调试] 本地记忆文件存在，进行增量更新: chat_id={chat_id}")
        if memory and len(memory) > 0:
            last_timestamp = memory[-1].get("timestamp", 0)
            if last_timestamp:
                logger.info(f"[调试] 从最后一条消息时间开始拉取: last_timestamp={last_timestamp}")
                new_messages = self._fetch_history_from_feishu(
                    chat_id, start_time=last_timestamp
                )
                existing_ids = {msg.get("message_id") for msg in memory}
                added_count = 0
                for msg in new_messages:
                    if msg.get("message_id") not in existing_ids:
                        memory.append(msg)
                        added_count += 1
                if added_count > 0:
                    self._save_memory(
                        memory, chat_id, service_id=service_id, agent_id=agent_id
                    )
                    logger.info(
                        f"[调试] 增量更新完成: chat_id={chat_id}, 新增消息数={added_count}, 总消息数={len(memory)}"
                    )
                else:
                    logger.info(f"[调试] 没有新消息需要添加: chat_id={chat_id}")
                return memory
        return memory if memory else []

    def add_message_to_memory(
        self,
        chat_id: str,
        message: dict,
        *,
        service_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """将消息添加到本地记忆（按租户懒解析路径）。"""
        history = self.load_memory(chat_id, service_id=service_id, agent_id=agent_id)
        open_id = message.get("open_id", "")
        message["user_name"] = self.get_user_name_by_open_id(open_id)
        history.append(message)
        self._save_memory(history, chat_id, service_id=service_id, agent_id=agent_id)
        logger.info(
            f"[调试] 新消息已添加到群聊记忆: chat_id={chat_id}, 总消息数={len(history)}"
        )