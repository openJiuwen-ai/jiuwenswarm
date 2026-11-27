# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tool that invokes external agents via agent-runtime-service."""

from __future__ import annotations

import logging
import os
from typing import Any, AsyncIterator, Dict, Optional

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.foundation.tool.base import Tool, ToolCard

from jiuwenclaw.agentserver.skilldev_agent.meta_tools.agent_runtime_client import (
    AgentRuntimeClient,
)

from jiuwenclaw.agentserver.skilldev_agent.meta_tools.skilldev_tool_context import (
    load_agent_output_schema,
    resolve_session_id,
)

from jiuwenclaw.agentserver.skilldev_agent.meta_tools.llm_call_client import call_llm

_MOCK_AGENT_RUNTIME_BASE_URL = "localhost"

logger = logging.getLogger(__name__)

_AGENT_RUNTIME_BASEURL_ENV = "AGENT_RUNTIME_BASEURL"

_DESCRIPTIONS = {
    "cn": (
        "调用外部 Agent 服务。传入目标 agentId、query 及可选的 filesInfo，"
        "通过 agent-runtime-service 的 A2A WebSocket 接口执行远程智能体。"
    ),
    "en": (
        "Invoke an external Agent service. Provide target agentId, query, "
        "and optional filesInfo; runs the remote agent via agent-runtime-service A2A WebSocket API."
    ),
}


def _build_input_params(language: str = "cn") -> Dict[str, Any]:
    is_cn = language in ("cn", "zh")
    return {
        "type": "object",
        "properties": {
            "agentId": {
                "type": "string",
                "description": "调用的目标 agentId" if is_cn else "Target agent ID to invoke",
            },
            "query": {
                "type": "string",
                "description": "输入目标 agent 的请求" if is_cn else "Request text for the target agent",
            },
            "filesInfo": {
                "type": "array",
                "description": "附件文件信息列表" if is_cn else "Optional attached file metadata",
                "items": {
                    "type": "object",
                    "properties": {
                        "fileType": {
                            "type": "string",
                            "enum": ["file", "image"],
                            "description": "文件类型" if is_cn else "File type: file or image",
                        },
                        "fileId": {
                            "type": "string",
                            "description": "文件唯一 ID" if is_cn else "Unique file ID",
                        },
                        "fileUrl": {
                            "type": "string",
                            "description": "文件下载地址" if is_cn else "File download URL",
                        },
                    },
                    "required": ["fileType", "fileId", "fileUrl"],
                },
            },
        },
        "required": ["agentId", "query"],
    }


def _build_agent_as_a_tool_card(
        *,
        agent_id: Optional[str] = None,
        language: str = "cn",
) -> ToolCard:
    lang = "cn" if language in ("cn", "zh") else "en"
    tool_id = f"agent_as_a_tool_{agent_id}" if agent_id else "agent_as_tool_default"
    return ToolCard(
        id=tool_id,
        name="agent_as_a_tool",
        description=_DESCRIPTIONS[lang],
        input_params=_build_input_params(language),
    )


class AgentAsATool(Tool):
    """Wraps external agent invocation as an openjiuwen Tool."""

    def __init__(
            self,
            *,
            language: str = "cn",
            card: Optional[ToolCard] = None,
    ) -> None:
        super().__init__(card or _build_agent_as_a_tool_card(language=language))
        base_url = (os.environ.get(_AGENT_RUNTIME_BASEURL_ENV) or "").strip()
        if not base_url:
            base_url = _MOCK_AGENT_RUNTIME_BASE_URL
        self._client = AgentRuntimeClient(base_url)

    def _validate_inputs(self, inputs: Any) -> dict[str, Any]:
        if not isinstance(inputs, dict):
            raise ValueError("inputs must be a dict")

        agent_id = (inputs.get("agentId") or "").strip()
        query = (inputs.get("query") or "").strip()
        if not agent_id:
            raise ValueError("agentId is required and cannot be empty")
        if not query:
            raise ValueError("query is required and cannot be empty")

        payload: dict[str, Any] = {
            "agentId": agent_id,
            "query": query,
        }
        files_info = inputs.get("filesInfo")
        if files_info is not None:
            if not isinstance(files_info, list):
                raise ValueError("filesInfo must be a list")
            payload["filesInfo"] = files_info
        return payload

    async def invoke(self, inputs: Dict[str, Any], **kwargs) -> Any:
        logger.info("Agent as a tool invoke inputs: %s", inputs)
        payload = self._validate_inputs(inputs)
        logger.info(
            "[AgentAsSkillTool] invoking external agent agentId=%s (card_id=%s)",
            payload["agentId"],
            self.card.id,
        )

        if os.getenv("SANDBOX_ENABLE", "").strip().lower() == "true":
            session_id = resolve_session_id(kwargs)
            if session_id:
                payload["sessionId"] = session_id
                output_schema = load_agent_output_schema(
                    session_id,
                    payload["agentId"],
                    log_prefix="AgentAsSkillTool",
                )
                if output_schema is not None:
                    inputs["outputSchema"] = output_schema
            result = await call_llm(inputs)
            return {
                "result": result.content,
            }

        last_frame: Optional[dict[str, Any]] = None
        async for frame in self._client.run_agent_stream(payload):
            if isinstance(frame, dict):
                last_frame = frame

        return {
            "result": self._extract_context_text(last_frame)
        }

    @staticmethod
    def _extract_context_text(frame: Optional[dict[str, Any]]) -> str:
        """从最后一帧 directives 中查找 header.name == "DisplayStreamingText" 的条目，
        取其 payload.contextText 作为文本内容。"""
        if not isinstance(frame, dict):
            return ""
        directives = frame.get("directives")
        if not isinstance(directives, list):
            return ""
        for directive in directives:
            if not isinstance(directive, dict):
                continue
            header = directive.get("header")
            if not isinstance(header, dict) or header.get("name") != "DisplayStreamingText":
                continue
            payload = directive.get("payload")
            if not isinstance(payload, dict):
                continue
            context_text = payload.get("contextText")
            if isinstance(context_text, str):
                return context_text
        return ""

    async def stream(self, inputs: Dict[str, Any], **kwargs) -> AsyncIterator[Any]:
        payload = self._validate_inputs(inputs)
        logger.info(
            "[AgentAsSkillTool] streaming external agent agentId=%s (card_id=%s)",
            payload["agentId"],
            self.card.id,
        )

        async for frame in self._client.run_agent_stream(payload):
            yield frame
        return


def get_agent_as_a_tool() -> AgentAsATool:
    """Factory: 返回默认配置的 AgentAsSkillTool 实例（card.id=agent_as_a_tool_default）.

    与 ``get_function_call_tool`` 一致：父 agent 用本工厂注册 Tool 实例，
    子 agent 引用 ``.card`` 共享同一 card.id 以避免重复注册冲突。
    """
    return AgentAsATool()


__all__ = [
    "AgentAsATool",
    "_build_agent_as_a_tool_card",
    "get_agent_as_a_tool",
]
