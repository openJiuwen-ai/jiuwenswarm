# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, AsyncIterator

from openjiuwen.core.foundation.tool.base import Tool, ToolCard

from support import args_of, ticket_of, node_tools


class GenerateScene(Tool):
    """One generation call for this member. A second call returns an error dict."""

    def __init__(self) -> None:
        timeout_s = float(node_tools().image_generate_timeout_seconds())
        super().__init__(
            ToolCard(
                id="generate_scene",
                name="generate_scene",
                description=(
                    "按票据和提示生成一次产物。服务端按节点调用对应的生成后端，"
                    "参考图、锁定项和画布媒体设置由服务端绑定。失败时返回字典，不抛异常。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "shot_ticket": {"type": "string"},
                        "prompt": {"type": "string"},
                        "setting": {
                            "type": "string",
                            "description": "这一张场景图对应的场景",
                        },
                    },
                    "required": ["shot_ticket", "prompt"],
                    "additionalProperties": False,
                },
                properties={"resilience": {"timeout_s": timeout_s}},
                parallel_safe=False,
            )
        )
        self._used = False

    def _failure(self, prompt: str, error: str) -> dict:
        return {
            "success": False,
            "image_uri": None,
            "sent_prompt": prompt,
            "prompt_source": "argument",
            "error": error,
        }

    def _reject(self, data: dict, prompt: str, error: str) -> dict:
        node_tools().note_rejection(data.get("shot_ticket"), "scene", "generate_scene", error)
        return self._failure(prompt, error)

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict:
        del kwargs
        prompt = ""
        try:
            data = args_of(inputs)
            prompt = data.get("prompt") if isinstance(data.get("prompt"), str) else ""
            if data.get("__node_guard_reject__") == "generate_already_called" or self._used:
                self._used = True
                return self._reject(data, prompt, "generate_already_called")
            ticket = ticket_of(data)
            if ticket is None:
                return self._reject(data, prompt, "missing_shot_ticket")
            if not prompt.strip():
                return self._reject(data, prompt, "missing_prompt")
            self._used = True
            return await node_tools().generate_node_output(
                ticket,
                member="scene",
                prompt=prompt,
                setting=data.get("setting") if isinstance(data.get("setting"), str) else "",
            )
        except Exception as exc:  # noqa: BLE001
            return self._failure(prompt, str(exc))

    async def stream(self, inputs: Any, **kwargs: Any) -> AsyncIterator[dict]:
        yield await self.invoke(inputs, **kwargs)
