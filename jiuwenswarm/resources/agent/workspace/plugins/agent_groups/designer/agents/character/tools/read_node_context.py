# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, AsyncIterator

from openjiuwen.core.foundation.tool.base import Tool, ToolCard

from support import args_of, ticket_of, node_tools


class ReadNodeContext(Tool):
    """Return the context fields for one opaque shot ticket."""

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="read_node_context",
                name="read_node_context",
                description=(
                    "用整段票据读取这个节点本次要看的上下文：节点设定、上游产物、分镜行、锁定项、"
                    "连续性、参考图绑定、提示词上限和用户画布编辑。不要拆开票据。"
                ),
                input_params={
                    "type": "object",
                    "properties": {"shot_ticket": {"type": "string"}},
                    "required": ["shot_ticket"],
                    "additionalProperties": False,
                },
            )
        )

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict:
        del kwargs
        try:
            ticket = ticket_of(args_of(inputs))
            if ticket is None:
                return {"error": "missing_shot_ticket"}
            return await node_tools().read_node_context(ticket)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    async def stream(self, inputs: Any, **kwargs: Any) -> AsyncIterator[dict]:
        yield await self.invoke(inputs, **kwargs)
