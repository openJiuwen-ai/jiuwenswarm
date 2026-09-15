# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Xiaoyi append-reference toolkit.

对齐 OpenClaw xiaoyi_append_reference：把联网搜索等引用来源打给手机端
（A2A data.reference 卡片）。仅 xiaoyi 渠道注册。
"""

from __future__ import annotations

import logging
import time
from typing import Any, List

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.common.xiaoyi_reference import coerce_references

logger = logging.getLogger(__name__)


class XiaoyiAppendReferenceToolkit:
    """Push citation cards to the Xiaoyi phone client."""

    def __init__(
        self,
        request_id: str,
        session_id: str,
        channel_id: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.request_id = request_id
        self.session_id = session_id
        self.channel_id = channel_id
        self._request_metadata = dict(metadata) if metadata else None

    def update_runtime_context(
        self,
        *,
        request_id: str,
        session_id: str,
        channel_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.request_id = request_id
        self.session_id = session_id
        self.channel_id = channel_id
        self._request_metadata = dict(metadata) if metadata else None

    async def append_reference(self, references: Any = None, **_ignored: Any) -> str:
        items = coerce_references(references)
        if not items:
            return "发送引用来源失败：references 必须是非空数组，且每项含 title/url/source/name"

        try:
            from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
            from jiuwenswarm.server.runtime.session.session_history import (
                append_history_record,
            )

            append_history_record(
                session_id=self.session_id,
                request_id=self.request_id,
                channel_id=self.channel_id,
                role="assistant",
                event_type="chat.reference",
                content="",
                timestamp=time.time(),
                extra={"references": items},
            )

            msg: dict[str, Any] = {
                "request_id": self.request_id,
                "channel_id": self.channel_id,
                "session_id": self.session_id,
                "payload": {
                    "event_type": "chat.reference",
                    "references": items,
                },
                "is_complete": False,
            }
            if self._request_metadata:
                msg["metadata"] = dict(self._request_metadata)

            server = AgentWebSocketServer.get_instance()
            await server.send_push(msg)
            logger.info(
                "[XiaoyiAppendReference] send_push ok session_id=%s count=%s",
                self.session_id,
                len(items),
            )
            return f"成功发送 {len(items)} 条引用来源"
        except Exception as e:
            logger.exception(
                "[XiaoyiAppendReference] 失败 session_id=%s error=%s",
                self.session_id,
                e,
            )
            return f"提交引用来源失败: {e}"

    def get_tools(self) -> List[Tool]:
        def make_tool(
            name: str,
            description: str,
            input_params: dict,
            func,
        ) -> Tool:
            card = ToolCard(
                name=name,
                description=description,
                input_params=input_params,
            )
            return LocalFunction(card=card, func=func)

        return [
            make_tool(
                name="xiaoyi_append_reference",
                description=(
                    "【引用来源】将回答过程中搜索到的信息依赖引用返回给用户（手机端参考来源卡片）。"
                    "对话涉及联网搜索、或天气/金融/健康/百科等查询 skill 拿到的数据依赖时，"
                    "必须在最后融合答复时统一调用本工具一次，不要分别调用。"
                    "例如 xiaoyi-web-search 等联网搜索工具的结果必须下发引用来源。"
                    "调用不会中断当前流式输出。"
                    "title 为页面标题，name 为站点名称（如百度百科），"
                    "source 为来源类型（如 web_search），url 为可点击链接。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "references": {
                            "type": "array",
                            "description": "引用来源数组，每个元素为一个引用对象",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {
                                        "type": "string",
                                        "description": "引用页面的标题，用于卡片主标题展示",
                                    },
                                    "url": {
                                        "type": "string",
                                        "description": "引用页面的链接地址，用于点击跳转",
                                    },
                                    "source": {
                                        "type": "string",
                                        "description": (
                                            "来源类型标识，如 web_search、document、knowledge_base"
                                        ),
                                    },
                                    "name": {
                                        "type": "string",
                                        "description": "站点名称，如百度百科、维基百科",
                                    },
                                    "imageUrl": {
                                        "type": "string",
                                        "description": "页面标题 logo 小图标 URL，可选",
                                    },
                                },
                                "required": ["title", "url", "source", "name"],
                            },
                        },
                    },
                    "required": ["references"],
                },
                func=self.append_reference,
            ),
        ]
