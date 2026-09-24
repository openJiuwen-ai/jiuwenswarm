# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""面向 agent 的主机模型注册工具封装.

skill 部署/下线本机模型服务后,通过这两个工具把模型接入信息登记到
ModelRegistry;模型管理页(models.list)据此展示模型卡片。
"""

from __future__ import annotations

from typing import Any, Callable

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.server.runtime.model_registry import ModelRegistry
from jiuwenswarm.server.runtime.model_registry.model_registry import get_default_registry

# 注册工具的可选描述字段,与 ModelRegistry.validate_entry 的字段集保持一致。
_ENTRY_PROPERTY_SCHEMAS: dict[str, dict[str, Any]] = {
    "provider": {
        "type": "string",
        "description": "Inference engine / provider, e.g. 'vllm', 'ollama', 'sglang'.",
    },
    "description": {"type": "string", "description": "Short human-readable description."},
    "model_type": {
        "type": "string",
        "description": "Model category. One of: llm, vision, audio, video, embedding, rerank. Defaults to 'llm'.",
    },
    "protocol": {
        "type": "string",
        "description": "Serving protocol. Defaults to 'openai' (OpenAI-compatible).",
    },
    "api_key": {
        "type": "string",
        "description": "API key of the endpoint, if required. Stored locally; never returned in plaintext by RPCs.",
    },
    "param_size": {"type": "string", "description": "Parameter size, e.g. '7B'."},
    "quantization": {"type": "string", "description": "Quantization, e.g. 'BF16', 'W8A8'."},
    "context_length": {"type": "integer", "description": "Context length in tokens, e.g. 32768."},
    "capabilities": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Capability tags, e.g. ['tool_calling', 'multimodal'].",
    },
}


class ModelRegistryToolkit:
    """把 ModelRegistry 暴露成模型友好的 tool 集合。"""

    def __init__(self, registry: ModelRegistry | None = None) -> None:
        self._registry = registry or get_default_registry()

    async def register_model(self, name: str, api_base: str, **kwargs: Any) -> dict[str, Any]:
        return await self._registry.register({"name": name, "api_base": api_base, **kwargs})

    async def unregister_model(self, name: str) -> dict[str, Any]:
        return await self._registry.unregister(name)

    def get_tools(self) -> list[Tool]:
        """Return model-registry tools for agent registration."""

        def make_tool(name: str, description: str, input_params: dict, func: Callable[..., Any]) -> Tool:
            # 统一用 LocalFunction 包装,保持与现有 toolkit 注册方式一致。
            card = ToolCard(
                id=name,
                name=name,
                description=description,
                input_params=input_params,
            )
            return LocalFunction(card=card, func=func)

        return [
            make_tool(
                name="register_model",
                description=(
                    "Register a model service running on this host (e.g. a vLLM-served "
                    "'Qwen2.5-7B-Instruct') into the model registry shown on the model "
                    "management page. Call this after a skill deploys/starts a model "
                    "service. Re-registering the same `name` updates the entry."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Model name, e.g. 'Qwen2.5-7B-Instruct'. Used as the unique key.",
                        },
                        "api_base": {
                            "type": "string",
                            "description": "Serving endpoint base URL, e.g. 'http://127.0.0.1:8000/v1'.",
                        },
                        **_ENTRY_PROPERTY_SCHEMAS,
                    },
                    "required": ["name", "api_base"],
                },
                func=self.register_model,
            ),
            make_tool(
                name="unregister_model",
                description=(
                    "Remove a model from the model registry, e.g. after its service "
                    "is stopped/torn down. Idempotent: unknown names succeed."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Registered model name, e.g. 'Qwen2.5-7B-Instruct'.",
                        },
                    },
                    "required": ["name"],
                },
                func=self.unregister_model,
            ),
        ]


def get_model_registry_tools() -> list[Tool]:
    """构建模型注册工具集(共享默认注册表,可直接注册到任意 adapter)。"""
    return ModelRegistryToolkit().get_tools()


__all__ = [
    "ModelRegistryToolkit",
    "get_model_registry_tools",
]
