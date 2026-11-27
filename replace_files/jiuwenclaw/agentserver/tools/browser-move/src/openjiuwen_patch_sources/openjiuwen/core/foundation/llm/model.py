# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from typing import Union, List, Optional, AsyncIterator, Type, Dict

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.foundation.llm.model_clients.dashscope_model_client import DashScopeModelClient
from openjiuwen.core.foundation.llm.schema.message import BaseMessage, AssistantMessage, UserMessage
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.tool import ToolInfo
from openjiuwen.core.foundation.llm.schema.config import ModelRequestConfig, ModelClientConfig
from openjiuwen.core.foundation.llm.output_parsers.output_parser import BaseOutputParser
from openjiuwen.core.foundation.llm.schema.generation_response import (
    ImageGenerationResponse,
    AudioGenerationResponse,
    VideoGenerationResponse
)
from openjiuwen.core.foundation.llm.model_clients.base_model_client import BaseModelClient
from openjiuwen.core.foundation.llm.model_clients.openai_model_client import OpenAIModelClient
from openjiuwen.core.foundation.llm.model_clients.siliconflow_model_client import SiliconFlowModelClient

_CLIENT_TYPE_REGISTRY: Dict[str, Type[BaseModelClient]] = {
    "OpenAI": OpenAIModelClient,
    "OpenRouter": OpenAIModelClient,
    "SiliconFlow": SiliconFlowModelClient,
    "DashScope": DashScopeModelClient,
}


class Model:
    """Unified LLM invocation entry point

    Responsibilities:
    1. Get/create ModelClient instance based on client_id or configuration
    2. Delegate to ModelClient to execute actual LLM calls
    3. Provide unified interface (ainvoke, astream)

    Usage:

    Method 1: Dynamic creation (pass configuration)
        model = Model(model_config, client_config)
        response = await model.ainvoke("Hello")
    """

    def __init__(
            self,
            model_client_config: Optional[ModelClientConfig],
            model_config: ModelRequestConfig = None,
    ):
        """Initialize Model instance

        Args:
            model_config: Model parameter configuration
            model_client_config: Client configuration
        """
        self.model_config = model_config
        self.model_client_config = model_client_config
        self._client: Optional[BaseModelClient] = None

        if model_client_config is not None:
            self._client = self._create_model_client(model_client_config)
        else:
            raise build_error(StatusCode.MODEL_SERVICE_CONFIG_ERROR,
                              error_msg="model client config is none")

    def _create_model_client(self, client_config: ModelClientConfig) -> BaseModelClient:
        """Create corresponding ModelClient instance based on client_type
        
        Args:
            client_config: Client configuration
            
        Returns:
            BaseModelClient: ModelClient instance
            
        Raises:
            ValueError: When client_provider is not supported
        """
        if client_config.client_provider is None:
            raise build_error(StatusCode.MODEL_SERVICE_CONFIG_ERROR,
                              error_msg="model client config client_provider is none")
        if client_config.client_id is None:
            raise build_error(StatusCode.MODEL_SERVICE_CONFIG_ERROR,
                              error_msg="model client config client_id is none")
        client_provider = client_config.client_provider

        client_class = _CLIENT_TYPE_REGISTRY.get(client_provider)

        if client_class is None:
            supported_types = ", ".join(_CLIENT_TYPE_REGISTRY.keys())

            raise build_error(
                StatusCode.MODEL_SERVICE_CONFIG_ERROR,
                error_msg=f"Unsupported client_type: '{client_provider}', Supported types: {supported_types}"
            )

        return client_class(self.model_config, client_config)

    async def invoke(
            self,
            messages: Union[str, List[BaseMessage], List[dict]],
            *,
            tools: Union[List[ToolInfo], List[dict], None] = None,
            temperature: Optional[float] = None,
            top_p: Optional[float] = None,
            max_tokens: Optional[int] = None,
            stop: Union[Optional[str], None] = None,
            model: str = None,
            output_parser: Optional[BaseOutputParser] = None,
            timeout: float = None,
            **kwargs
    ) -> AssistantMessage:
        """Asynchronous LLM invocation

        Args:
            :param output_parser:
            :param model:
            :param stop:
            :param temperature:
            :param tools:
            :param messages:
            :param top_p:
            :param max_tokens:
            :param timeout:
            **kwargs: Other parameters

        Returns:
            AssistantMessage
        """
        return await self._client.invoke(
            messages=messages,
            stop=stop,
            model=model,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            output_parser=output_parser,
            timeout=timeout,
            **kwargs
        )

    async def stream(
            self,
            messages: Union[str, List[BaseMessage], List[dict]],
            *,
            tools: Union[List[ToolInfo], List[dict], None] = None,
            temperature: Optional[float] = None,
            top_p: Optional[float] = None,
            max_tokens: Optional[int] = None,
            stop: Union[Optional[str], None] = None,
            model: str = None,
            output_parser: Optional[BaseOutputParser] = None,
            timeout: float = None,
            **kwargs
    ) -> AsyncIterator[AssistantMessageChunk]:
        """Asynchronous streaming LLM invocation

        Args:
            :param output_parser:
            :param model:
            :param stop:
            :param temperature:
            :param tools:
            :param messages:
            :param top_p:
            :param max_tokens:
            :param timeout:
            **kwargs: Other parameters

        Yields:
            AssistantMessageChunk
        """
        async for chunk in self._client.stream(
                messages=messages,
                stop=stop,
                model=model,
                tools=tools,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                output_parser=output_parser,
                timeout=timeout,
                **kwargs
        ):
            yield chunk

    async def generate_image(
            self,
            messages: List[UserMessage],
            *,
            model: Optional[str] = None,
            size: Optional[str] = "1664*928",
            negative_prompt: Optional[str] = None,
            n: Optional[int] = 1,
            prompt_extend: bool = True,
            watermark: bool = False,
            seed: int = 0,
            **kwargs
    ) -> ImageGenerationResponse:
        """Generate image from text prompt (text-to-image or text+image-to-image)

        Args:
            messages: List of UserMessage containing text descriptions and optional image URLs
            model: Model to use for generation
            size: Size of the generated image (e.g., "1664*928", "1024*1024")
            negative_prompt: Optional negative prompt to guide what not to generate
            n: Number of images to generate
            prompt_extend: Whether to automatically extend/enhance the prompt
            watermark: Whether to add watermark to generated images
            seed: Random seed for reproducible generation (0 for random)
            **kwargs: Additional parameters

        Returns:
            ImageGenerationResponse: Generated image response
        """
        return await self._client.generate_image(
            messages=messages,
            model=model,
            negative_prompt=negative_prompt,
            size=size,
            n=n,
            prompt_extend=prompt_extend,
            watermark=watermark,
            seed=seed,
            **kwargs
        )

    async def generate_speech(
            self,
            messages: List[UserMessage],
            *,
            model: Optional[str] = None,
            voice: Optional[str] = "Cherry",
            language_type: Optional[str] = "Auto",
            **kwargs
    ) -> AudioGenerationResponse:
        """Generate speech audio from text

        Args:
            messages: List of UserMessage containing text to convert to speech
            model: Model to use for generation
            voice: Voice to use for speech synthesis (required), refer to supported voices
            language_type: Language type for synthesized audio, defaults to "Auto" for automatic detection
            **kwargs: Additional parameters

        Returns:
            AudioGenerationResponse: Generated audio response
        """
        return await self._client.generate_speech(
            messages=messages,
            model=model,
            voice=voice,
            language_type=language_type,
            **kwargs
        )

    async def generate_video(
            self,
            messages: List[UserMessage],
            *,
            img_url: Optional[str] = None,
            audio_url: Optional[str] = None,
            model: Optional[str] = None,
            size: Optional[str] = None,
            resolution: Optional[str] = None,
            duration: Optional[int] = 5,
            prompt_extend: bool = True,
            watermark: bool = False,
            negative_prompt: Optional[str] = None,
            seed: Optional[int] = None,
            **kwargs
    ) -> VideoGenerationResponse:
        """Generate video from text prompt (text-to-video or image-to-video)

        Args:
            messages: List of UserMessage containing text description of the video to generate
            img_url: Optional URL/path of the first frame image for image-to-video generation.
                     Supports: public URL, local file path (file:// prefix), or base64 encoded image
            audio_url: Optional URL of audio to add to the video
            model: Model to use for generation
            size: Video size (e.g., "1280*720"). Use '*' as separator.
            resolution: Video resolution (e.g., "720P", "1080P")
            duration: Duration of the video in seconds (default: 5)
            prompt_extend: Whether to automatically extend/enhance the prompt (default: True)
            watermark: Whether to add watermark to generated video (default: False)
            negative_prompt: Negative prompt to guide what not to generate
            seed: Random seed for reproducible generation
            **kwargs: Additional parameters

        Returns:
            VideoGenerationResponse: Generated video response
        """
        return await self._client.generate_video(
            messages=messages,
            img_url=img_url,
            audio_url=audio_url,
            model=model,
            size=size,
            resolution=resolution,
            duration=duration,
            prompt_extend=prompt_extend,
            watermark=watermark,
            negative_prompt=negative_prompt,
            seed=seed,
            **kwargs
        )


def init_model(
        provider: str,
        model_name: str,
        api_key: str,
        api_base: str,
        *,
        temperature: float = 0.95,
        top_p: float = 0.1,
        max_tokens: Optional[int] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        verify_ssl: bool = False,
        custom_headers: Optional[dict[str, str]] = None,
) -> Model:
    """Compatibility factory for code paths importing ``init_model``.

    The browser runtime MCP server imports ``openjiuwen.core.foundation.llm``
    from the local browser-move patch sources. That patched copy previously
    omitted the public ``init_model`` helper exported by the installed
    ``openjiuwen`` package, causing the subprocess to crash on import before
    any browser tools could be registered.
    """
    client_config = ModelClientConfig(
        client_provider=provider,
        api_key=api_key,
        api_base=api_base,
        timeout=timeout,
        max_retries=max_retries,
        verify_ssl=verify_ssl,
        custom_headers=custom_headers,
    )
    request_config = ModelRequestConfig(
        model=model_name,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
    )
    return Model(
        model_client_config=client_config,
        model_config=request_config,
    )
