# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Smallest tool an AgentGroup member can carry: echo the ticket it was called with."""
from __future__ import annotations

from typing import Any

from openjiuwen.core.foundation.tool import Tool, ToolCard


class EchoTool(Tool):
    """Echo one ticket id back, so a test can assert on a value it chose itself."""

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="echo_ticket",
                name="echo_ticket",
                description=(
                    "Echo a ticket id back. Call it whenever the task names a ticket "
                    "and the caller needs that exact value returned."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "ticket": {
                            "type": "string",
                            "description": "Ticket id to echo back.",
                        },
                    },
                    "required": ["ticket"],
                },
            )
        )

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict:
        ticket = str((inputs or {}).get("ticket") or "").strip()
        if not ticket:
            return {"success": False, "error": "ticket required"}
        return {"success": True, "echo": ticket}

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)
