# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""
小艺 ws 客户端类实现：
    支持invoke/stream调用
    支持类定义体导入时自动注册
"""
import json
import logging
import asyncio
from typing import Optional, Union, List, AsyncIterator, Any

import websockets
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.common.logging import llm_logger, LogEventType
from openjiuwen.core.foundation.tool import ToolInfo

from openjiuwen.core.foundation.llm import (
    ModelClientConfig, ModelRequestConfig,
    BaseMessage, OpenAIModelClient, ToolCall,
    BaseOutputParser, AssistantMessage, AssistantMessageChunk, UsageMetadata
)
from openjiuwen.core.runner.callback import LLMCallEvents, trigger

logger = logging.getLogger(__name__)


class XiaoYiWSModelClient(OpenAIModelClient):
    __client_name__ = "XiaoYiWS"
    TOOL_CALL_START = "<tool_call>"
    TOOL_CALL_END = "</tool_call>"

    def __init__(self, model_config: ModelRequestConfig, model_client_config: ModelClientConfig):
        super().__init__(model_config, model_client_config)

    @staticmethod
    def get_request_body_template() -> dict:
        return {
            "session": {"sessionId": "session_skill_creator_test"},
            "body": {
                "modelName": "",  # AGENT-GLM5.1 AGENT-DEEPSEEK-V3
                "userInfo": {
                    "appPackageName": "com.huawei.hmos.vassistant",
                    "appVersion": "11.3.4.202"
                },
                "apiKey": "",
                "deviceInfo": {
                    "deviceId": "ODkwMDg2MDAwMDAwMDAxODQ4hwvdpwisoper"
                },
                "modelParam": {},  # 模型参数
                "messages": [],
                "tools": []
            }
        }

    @staticmethod
    def _parse_tool_calls(message):
        tool_calls = []
        tool_call = message.get("toolCalls", [])
        for idx, tc in enumerate(tool_call):
            tool_call = ToolCall(
                id=tc.get('id', ""),
                type="function",
                name=tc.get('function', {}).get("name", ""),
                arguments=tc.get("function", {}).get("arguments", ""),
                index=tc.get('index', idx)
            )
            tool_calls.append(tool_call)
        return tool_calls

    async def invoke(
            self,
            messages: Union[str, List[BaseMessage], List[dict]],
            *,
            tools: Union[List[ToolInfo], List[dict], None] = None,
            temperature: Optional[float] = None,
            top_p: Optional[float] = None,
            model: str = None,
            max_tokens: Optional[int] = None,
            stop: Union[Optional[str], None] = None,
            output_parser: Optional[BaseOutputParser] = None,
            timeout: float = None,
            **kwargs
    ) -> AssistantMessage:

        params = self._build_request_params(
            messages=messages,
            tools=tools,
            model=model,
            temperature=temperature,
            top_p=top_p,
            stop=stop,
            max_tokens=max_tokens,
            stream=False,
            **kwargs
        )

        request_body = self._build_request_body(
            params
        )
        session_id = request_body.get("session", {}).get("sessionId", "")
        # 请求
        logger.info(f"[XiaoYiWSModelClient] [{session_id}] Websockets connect!")
        recv_timeout = timeout if timeout else self.model_client_config.timeout
        async with websockets.connect(self.model_client_config.api_base) as websocket:
            await trigger(
                LLMCallEvents.LLM_INPUT,
                model_name=params.get("model"),
                model_provider=self.model_client_config.client_provider,
                messages=params.get("messages"),
                tools=params.get("tools"),
                temperature=params.get("temperature"),
                top_p=params.get("top_p"),
                max_tokens=params.get("max_tokens")
            )
            await websocket.send(json.dumps(request_body, ensure_ascii=False))
            message = None
            while True:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=recv_timeout)
                    data = json.loads(message)
                    code = data.get("code", 0)
                    if code != 0:  # 请求异常
                        raise Exception(f"Error chunk: {data}")
                except Exception as e:
                    await trigger(
                        LLMCallEvents.LLM_CALL_ERROR,
                        model_name=params.get("model"),
                        model_provider=self.model_client_config.client_provider,
                        is_stream=False,
                        error=e
                    )
                    llm_logger.error(
                        f"[XiaoYiWSModelClient] [{session_id}] XiaoYi ws API async invoke error. "
                        f"Response message: {message}",
                        event_type=LogEventType.LLM_CALL_ERROR,
                        model_provider=self.model_client_config.client_provider,
                        is_stream=False,
                        exception=str(e)
                    )
                    raise build_error(
                        StatusCode.MODEL_CALL_FAILED,
                        error_msg=f"[XiaoYiWSModelClient] [{session_id}] XiaoYi ws API async invoke "
                                  f"error: {str(e)}."
                    ) from e
                if data.get("finalTime") != -1:
                    break
        logger.info(f"[XiaoYiWSModelClient] [{session_id}] Websockets disconnect!")
        assistant_message = await self._parse_response(data, output_parser)
        await trigger(
            LLMCallEvents.LLM_OUTPUT,
            model_name=params.get("model"),
            model_provider=self.model_client_config.client_provider,
            response=assistant_message.content,
            usage=assistant_message.usage_metadata,
            tool_calls=assistant_message.tool_calls
        )
        return assistant_message

    async def stream(
            self,
            messages: Union[str, List[BaseMessage], List[dict]],
            *,
            tools: Union[List[ToolInfo], List[dict], None] = None,
            temperature: Optional[float] = None,
            top_p: Optional[float] = None,
            model: str = None,
            max_tokens: Optional[int] = None,
            stop: Union[Optional[str], None] = None,
            output_parser: Optional[BaseOutputParser] = None,
            timeout: float = None,
            **kwargs
    ) -> AsyncIterator[AssistantMessageChunk]:
        params = self._build_request_params(
            messages=messages,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            model=model,
            stop=stop,
            max_tokens=max_tokens,
            stream=True,
            **kwargs
        )
        request_body = self._build_request_body(
            params
        )
        session_id = request_body.get("session", {}).get("sessionId", "")
        recv_timeout = timeout if timeout else self.model_client_config.timeout
        logger.info(f"[XiaoYiWSModelClient] [{session_id}] Websockets connect!")
        async with websockets.connect(self.model_client_config.api_base) as websocket:
            await trigger(
                LLMCallEvents.LLM_INPUT,
                model_name=params.get("model"),
                model_provider=self.model_client_config.client_provider,
                messages=params.get("messages"),
                tools=params.get("tools"),
                temperature=params.get("temperature"),
                top_p=params.get("top_p"),
                max_tokens=params.get("max_tokens"),
                is_stream=True
            )
            await websocket.send(json.dumps(request_body, ensure_ascii=False))
            message = None
            while True:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=recv_timeout)
                    data = json.loads(message)
                    parsed_result = self._parse_stream_chunk(data)
                except Exception as e:
                    await trigger(
                        LLMCallEvents.LLM_CALL_ERROR,
                        model_name=params.get("model"),
                        model_provider=self.model_client_config.client_provider,
                        is_stream=True,
                        error=e
                    )
                    llm_logger.error(
                        f"[XiaoYiWSModelClient] [{session_id}] XiaoYi ws API async stream error. "
                        f"Response message: {message}",
                        event_type=LogEventType.LLM_CALL_ERROR,
                        model_provider=self.model_client_config.client_provider,
                        is_stream=True,
                        exception=str(e)
                    )
                    raise build_error(
                        StatusCode.MODEL_CALL_FAILED,
                        error_msg=f"[XiaoYiWSModelClient] [{session_id}] XiaoYi ws API async "
                                  f"stream error: {str(e)}."
                    ) from e
                # 推送流式思考和推理回复
                if parsed_result.content or parsed_result.reasoning_content:
                    yield parsed_result
                if data.get("finalTime") != -1:
                    await trigger(
                        LLMCallEvents.LLM_OUTPUT,
                        model_name=params.get("model"),
                        model_provider=self.model_client_config.client_provider,
                        is_stream=True
                    )
                    yield parsed_result
                    logger.info(f"[XiaoYiWSModelClient] [{session_id}] Websockets disconnect!")
                    break

    def _parse_valid_token(
            self, chunk: AssistantMessageChunk, is_tool_call_chunk: bool
    ) -> (AssistantMessageChunk, bool):
        # 收集token目标：去除特殊token
        # 1、保留 START 前的字符，即向前截断
        # 2、保留 END 后的字符，即向后截断
        # 真实场景
        # 一个chunk可能包含 s + START + s；s + END + s；s + START + s + END + s
        chunk_token = chunk.content
        cache_token = ""
        while True:
            if is_tool_call_chunk:  # 检测到 START，需要检测是否有 END
                tool_end_idx = chunk_token.find(self.TOOL_CALL_END)
                if tool_end_idx != -1:  # 检测到 END，中间内容不输出
                    chunk_token = chunk_token[tool_end_idx + len(self.TOOL_CALL_END):]
                    is_tool_call_chunk = False
                else:
                    break
            else:  # 还未检测到 START
                tool_start_idx = chunk_token.find(self.TOOL_CALL_START)
                if tool_start_idx != -1:  # 检测到 START
                    cache_token += chunk_token[:tool_start_idx]  # 缓存 START 之前内容
                    chunk_token = chunk_token[tool_start_idx + len(self.TOOL_CALL_START):]
                    is_tool_call_chunk = True
                else:
                    cache_token += chunk_token
                    break
        chunk.content = cache_token
        return chunk, is_tool_call_chunk

    async def _parse_response(
            self,
            response: Any,
            parser: Optional[BaseOutputParser] = None
    ) -> AssistantMessage:
        message = response.get("result", {})

        tool_calls = self._parse_tool_calls(message)
        metadata = self._get_usage_metadata(response)
        reasoning_content = message.get('reasoningText', None)
        content = message.get('text', "")

        parser_content = None
        llm_logger.info(
            "Before parse content with parser.",
            event_type=LogEventType.LLM_CALL_END,
            model_name=self.model_config.model_name,
            model_provider=self.model_client_config.client_provider,
            response_content=content,
            is_stream=False
        )
        llm_logger.info(
            "Before parse content with parser config.",
            event_type=LogEventType.LLM_CALL_END,
            model_name=self.model_config.model_name,
            model_provider=self.model_client_config.client_provider,
            is_stream=False,
            metadata={"parser": str(parser)}
        )
        if parser and content:
            try:
                parser_content = await parser.parse(content)
                llm_logger.info(
                    "Parser parse success.",
                    event_type=LogEventType.LLM_CALL_END,
                    model_name=self.model_config.model_name,
                    model_provider=self.model_client_config.client_provider,
                    is_stream=False,
                    metadata={"parser_content": parser_content}
                )
            except Exception as e:
                llm_logger.warning(
                    "Parser parse error.",
                    event_type=LogEventType.LLM_CALL_ERROR,
                    model_name=self.model_config.model_name,
                    model_provider=self.model_client_config.client_provider,
                    is_stream=False,
                    exception=str(e)
                )
                parser_content = None
        return AssistantMessage(
            content=content,
            tool_calls=tool_calls if tool_calls else None,
            finish_reason="tool_calls" if tool_calls else "stop",
            reasoning_content=reasoning_content,
            usage_metadata=metadata,
            parser_content=parser_content
        )

    def _build_request_body(
            self,
            params: dict
    ) -> dict:
        # 组装请求体
        body = self.get_request_body_template()
        model_extra = self.model_client_config.model_extra
        # 适配小艺 payload-sessionId
        session_raw = model_extra.get("session")
        if session_raw:
            if isinstance(session_raw, str):
                body["session"] = {"sessionId": session_raw}
            elif isinstance(session_raw, dict) and isinstance(session_raw.get("sessionId"), str):
                body["session"] = session_raw

        body["body"]["userInfo"] = model_extra.get("user_info", {})
        body["body"]["apiKey"] = self.model_client_config.api_key
        body["body"]["deviceInfo"] = model_extra.get("device_info", {})
        messages = params.get("messages", [])
        if messages:
            body["body"]["messages"] = messages
        model_name = params.get("model")
        if model_name is not None:
            body["body"]["modelName"] = model_name
        temperature = params.get("temperature")
        if temperature is not None:
            body["body"]["modelParam"]["temperature"] = temperature
        top_p = params.get("top_p")
        if top_p is not None:
            body["body"]["modelParam"]["top_p"] = top_p
        tools = params.get("tools", [])
        if tools:
            body["body"]["tools"] = tools
        return body

    def _parse_stream_chunk(self, chunk: Any) -> Optional[AssistantMessageChunk]:
        code = chunk.get("code", 0)
        if code != 0:  # 请求异常
            raise Exception(f"Error chunk: {chunk}")
        message = chunk.get('result', {})
        content = message.get('newDisplayToken')
        token = message.get('token')

        # Parse tool_calls
        tool_calls = self._parse_tool_calls(message)
        metadata = self._get_usage_metadata(chunk)
        finish_reason = "null"
        if tool_calls:
            finish_reason = "tool_calls"
        elif not token:
            finish_reason = "stop"

        return AssistantMessageChunk(
            content=content if content else "",
            reasoning_content=message.get("newReasoningToken", ""),
            tool_calls=tool_calls if tool_calls else None,
            usage_metadata=metadata,
            finish_reason=finish_reason
        )

    def _get_usage_metadata(self, chunk: Any) -> UsageMetadata:
        content_bean = chunk.get('modelRequestInfo', {}).get('contentBean', {})
        input_token_num = content_bean.get('inputTokenNum', 0)
        generate_token_num = content_bean.get("generateTokenNum", 0)
        return UsageMetadata(
            model_name=self.model_config.model_name,
            input_tokens=input_token_num,
            output_tokens=generate_token_num,
            total_tokens=input_token_num + generate_token_num,
        )
