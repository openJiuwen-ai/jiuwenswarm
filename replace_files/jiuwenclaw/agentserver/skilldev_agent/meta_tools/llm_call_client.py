from typing import Any
import os
import logging
import json

from openjiuwen.core.foundation.llm import (
    ModelClientConfig, ModelRequestConfig, Model,
)

from jiuwenclaw.sandbox import sandbox_routing_enabled
from jiuwenclaw.agentserver.open_ability_utils import get_oa_auth_headers

logger = logging.getLogger(__name__)

MODEL = None

_AGENT_FUNC_NAME = "agent_as_a_tool"

MOCK_AGENT_PROMPT = """你是一个万能的AI助手，你正在模拟智能体执行任务，允许通过合理推断来生成结果。

## 智能体信息
agentId: {agent_id}
name: {name}
description: {description}

## 输入参数说明（parameters.properties）
{parameters_schema}

## 输出格式要求
严格按照以下 outputSchema 输出纯 JSON，不要有任何额外符号、注释或解释文字。
{output_schema}

## 用户输入参数值
{query_params}

请根据用户输入参数，生成符合 outputSchema 格式的 JSON 输出。"""

MOCK_PLUGIN_PROMPT = """你是一个万能的AI助手，你正在模拟插件执行任务，允许通过合理推断来生成结果。

## 插件信息
bundleName: {bundle_name}
pluginType: {plugin_type}
toolType: {tool_type}
toolName: {tool_name}
description: {description}

## 输入参数说明（arguments.properties）
{arguments_schema}

## 输出格式要求
严格按照以下 outputSchema 输出纯 JSON，不要有任何额外符号、注释或解释文字。
{output_schema}

## 用户输入参数值
{query_params}

请根据用户输入参数，生成符合 outputSchema 格式的 JSON 输出。"""


async def call_llm(
        *,
        func_name: str,
        params: Any,
        output_schema: Any | None = None,
        session_id: str | None = None,
):
    global MODEL
    if is_empty_string_or_dict(params):
        query_params = "根据system prompt回答"
    else:
        query_params = params
    output_schema_str = json.dumps(output_schema.get("outputSchema", ""), ensure_ascii=False)

    if func_name == _AGENT_FUNC_NAME:
        parameters_schema = (output_schema.get("parameters") or {}).get("properties", {})
        content = MOCK_AGENT_PROMPT.format(
            agent_id=output_schema.get("agentId", ""),
            name=output_schema.get("name", ""),
            description=output_schema.get("description", ""),
            parameters_schema=json.dumps(parameters_schema, ensure_ascii=False),
            output_schema=output_schema_str,
            query_params=json.dumps(query_params, ensure_ascii=False),
        )
    else:
        arguments_schema = (output_schema.get("arguments") or {}).get("properties", {})
        content = MOCK_PLUGIN_PROMPT.format(
            bundle_name=output_schema.get("bundleName", ""),
            plugin_type=output_schema.get("pluginType", ""),
            tool_type=output_schema.get("toolType", ""),
            tool_name=output_schema.get("toolName", ""),
            description=output_schema.get("description", ""),
            arguments_schema=json.dumps(arguments_schema, ensure_ascii=False),
            output_schema=output_schema_str,
            query_params=json.dumps(query_params, ensure_ascii=False),
        )

    if MODEL is None:
        custom_headers = {
            "Accept": "text/event-stream", "x-request-from": "jiuwenclaw", "x-sandbox-id": "rytest",
            "x-api-key": "apikey", "x-hag-trace-id": "rytest001"
        }

        if sandbox_routing_enabled():
            custom_headers = get_oa_auth_headers()
            custom_headers["Accept"] = "text/event-stream"

        client_config = ModelClientConfig(
            client_provider=os.getenv("LLM_MODEL_PROVIDER", ""),  # 指定注册的名字
            api_key="AgentRuntimeService",
            api_base=os.getenv("LLM_API_BASE", ""),
            custom_headers=custom_headers,
            verify_ssl=False,
        )

        # 模型名称+超参配置
        request_config = ModelRequestConfig(
            model=os.getenv("LLM_MODEL_NAME", "")
        )

        logger.info("Init MODEL start client config:%s", client_config)

        MODEL = Model(
            model_client_config=client_config,
            model_config=request_config
        )

    logger.info("Llm client invoke system_prompt: %s, user_prompt: %s", content,
                json.dumps(query_params, ensure_ascii=False))

    chunks: list[str] = []
    async for chunk in MODEL.stream(
            messages=[
                {"role": "system", "content": content},
                {"role": "user", "content": json.dumps(query_params, ensure_ascii=False)}
            ]
    ):
        logger.info("Llm stream chunks: %s", chunk)
        if chunk.content:
            chunks.append(chunk.content)
    raw_name = "".join(chunks)

    return raw_name


def is_empty_string_or_dict(var: Any) -> bool:
    """判断变量是否为空串或空字典"""
    if isinstance(var, str):
        return var == ""  # 或 return not var
    elif isinstance(var, dict):
        return var == {}  # 或 return not var
    else:
        return False
