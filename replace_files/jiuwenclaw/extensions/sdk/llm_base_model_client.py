# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from abc import abstractmethod, ABC
from typing import List, AsyncIterator, Union, Dict, Optional, Type
from openjiuwen.core.foundation.llm import BaseModelClient, ModelRequestConfig, ModelClientConfig, UserMessage, \
    AssistantMessageChunk, BaseOutputParser, BaseMessage, AssistantMessage
from openjiuwen.core.foundation.llm.schema import VideoGenerationResponse, AudioGenerationResponse, \
    ImageGenerationResponse
from openjiuwen.core.foundation.tool import ToolInfo


class LlmModelClientConfig(ModelClientConfig):
    pass


class LlmModelRequestConfig(ModelRequestConfig):
    pass


class LlmAssistantMessageChunk(AssistantMessageChunk):
    pass


class LlmBaseOutputParser(BaseOutputParser):
    pass


class LlmUserMessage(UserMessage):
    pass


class LlmBaseMessage(BaseMessage):
    pass


class LlmAssistantMessage(AssistantMessage):
    pass


class LlmVideoGenerationResponse(VideoGenerationResponse):
    pass


class LlmAudioGenerationResponse(AudioGenerationResponse):
    pass


class LlmImageGenerationResponse(ImageGenerationResponse):
    pass


class LlmToolInfo(ToolInfo):
    pass


class LlmBaseModelClient(ABC):
    def __init__(self, model_config: LlmModelRequestConfig, model_client_config: LlmModelClientConfig):
        """初始化 LLM 基础客户端。

        Args:
            model_config: 模型请求配置，包含模型名称、参数等。
            model_client_config: 客户端配置，如超时、重试等。
        """
        self.model_config = model_config
        self.model_client_config = model_client_config

    def _get_client_name(self) -> str:
        """获取客户端名称（类名）。

        Returns:
            当前客户端类的名称。
        """
        return self.__class__.__name__

    @abstractmethod
    async def invoke(
            self,
            messages: Union[str, List[LlmBaseMessage], List[dict]],
            *,
            tools: Union[List[LlmToolInfo], List[dict], None] = None,
            temperature: Optional[float] = None,
            top_p: Optional[float] = None,
            model: str = None,
            max_tokens: Optional[int] = None,
            stop: Union[Optional[str], None] = None,
            output_parser: Optional[LlmBaseOutputParser] = None,
            timeout: float = None, **kwargs
    ) -> LlmAssistantMessage:
        """同步调用 LLM 模型（非流式）。

        Args:
            messages: 输入消息，可以是字符串、消息对象列表或字典列表。
            tools: 可供模型调用的工具列表，可以是 ToolInfo 对象或字典。
            temperature: 采样温度，控制随机性，范围通常 0-2。
            top_p: 核采样参数，替代温度。
            model: 要使用的具体模型名称，覆盖配置中的默认模型。
            max_tokens: 生成的最大 token 数。
            stop: 停止生成的字符串序列。
            output_parser: 输出解析器，用于结构化输出。
            timeout: 请求超时时间（秒）。
            **kwargs: 其他传递给底层 API 的参数。

        Returns:
            模型生成的完整助手消息。
        """
        pass

    @abstractmethod
    async def stream(
            self,
            messages: Union[str, List[LlmBaseMessage], List[dict]],
            *,
            tools: Union[List[LlmToolInfo], List[dict], None] = None,
            temperature: Optional[float] = None,
            top_p: Optional[float] = None,
            model: str = None,
            max_tokens: Optional[int] = None,
            stop: Union[Optional[str], None] = None,
            output_parser: Optional[LlmBaseOutputParser] = None,
            timeout: float = None, **kwargs
    ) -> AsyncIterator[LlmAssistantMessageChunk]:
        """流式调用 LLM 模型。

        Args:
            messages: 输入消息，可以是字符串、消息对象列表或字典列表。
            tools: 可供模型调用的工具列表。
            temperature: 采样温度。
            top_p: 核采样参数。
            model: 要使用的具体模型名称。
            max_tokens: 生成的最大 token 数。
            stop: 停止生成的字符串序列。
            output_parser: 输出解析器。
            timeout: 请求超时时间（秒）。
            **kwargs: 其他参数。

        Yields:
            模型生成的助手消息块（流式片段）。
        """
        pass

    @abstractmethod
    async def generate_image(
            self,
            messages: List[LlmUserMessage],
            *,
            model: Optional[str] = None,
            size: Optional[str] = "1920*1080",
            negative_prompt: Optional[str] = None,
            n: Optional[int] = 1,
            prompt_extend: bool = True,
            watermark: bool = False,
            seed: int = 0, **kwargs
    ) -> LlmImageGenerationResponse:
        """根据文本描述生成图像。

        Args:
            messages: 包含图像生成提示的用户消息列表。
            model: 图像生成模型名称。
            size: 输出图像尺寸，格式如 "宽*高"，默认 "1920*1080"。
            negative_prompt: 反向提示词，避免生成的内容。
            n: 生成图像数量。
            prompt_extend: 是否自动扩展提示词。
            watermark: 是否添加水印。
            seed: 随机种子，用于复现结果。
            **kwargs: 其他参数。

        Returns:
            图像生成响应，包含生成的图像数据或 URL。
        """
        pass

    @abstractmethod
    async def generate_speech(
            self,
            messages: List[LlmUserMessage],
            *,
            model: Optional[str] = None,
            voice: Optional[str] = "",
            language_type: Optional[str] = "", **kwargs
    ) -> LlmAudioGenerationResponse:
        """根据文本生成语音（文本转语音）。

        Args:
            messages: 包含要合成语音的文本的用户消息列表。
            model: 语音合成模型名称。
            voice: 音色标识，默认 "Cherry"。
            language_type: 语言类型，如 "Auto" 自动检测。
            **kwargs: 其他参数。

        Returns:
            音频生成响应，包含音频数据或 URL。
        """
        pass

    @abstractmethod
    async def generate_video(
            self,
            messages: List[LlmUserMessage],
            *,
            img_url: Optional[str] = None,
            audio_url: Optional[str] = None,
            model: Optional[str] = None,
            size: Optional[str] = None,
            resolution: Optional[str] = None,
            duration: Optional[int] = 10,
            prompt_extend: bool = True,
            watermark: bool = False,
            negative_prompt: Optional[str] = None,
            seed: Optional[int] = None, **kwargs
    ) -> LlmVideoGenerationResponse:
        """根据文本、图片或音频生成视频。

        Args:
            messages: 包含视频生成提示的用户消息列表。
            img_url: 作为视频输入的首帧图片 URL。
            audio_url: 作为视频背景音频的 URL。
            model: 视频生成模型名称。
            size: 视频尺寸（宽高）。
            resolution: 分辨率，如 "1080p"。
            duration: 视频时长（秒），默认 10。
            prompt_extend: 是否自动扩展提示词。
            watermark: 是否添加水印。
            negative_prompt: 反向提示词。
            seed: 随机种子。
            **kwargs: 其他参数。

        Returns:
            视频生成响应，包含生成的视频数据或 URL。
        """
        pass


class LlmModelClientDelegate(BaseModelClient):
    def __init__(self, llm: LlmBaseModelClient):
        """使用具体的 LLM 客户端初始化委托类。

        Args:
            llm: 实现了 LlmBaseModelClient 接口的实例。
        """
        self.llm = llm

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
            timeout: float = None, **kwargs
    ) -> AssistantMessage:
        """委托给底层 LLM 客户端的 invoke 方法。"""
        return await self.llm.invoke(
            messages=messages,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            model=model,
            max_tokens=max_tokens,
            stop=stop,
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
            model: str = None,
            max_tokens: Optional[int] = None,
            stop: Union[Optional[str], None] = None,
            output_parser: Optional[BaseOutputParser] = None,
            timeout: float = None, **kwargs
    ) -> AsyncIterator[AssistantMessageChunk]:
        """委托给底层 LLM 客户端的 stream 方法。"""
        async for chunk in self.llm.stream(
            messages=messages,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            model=model,
            max_tokens=max_tokens,
            stop=stop,
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
            size: Optional[str] = "1920*1080",
            negative_prompt: Optional[str] = None,
            n: Optional[int] = 1,
            prompt_extend: bool = True,
            watermark: bool = False,
            seed: int = 0, **kwargs
    ) -> ImageGenerationResponse:
        """委托给底层 LLM 客户端的 generate_image 方法。"""
        return await self.llm.generate_image(
            messages=messages,
            model=model,
            size=size,
            negative_prompt=negative_prompt,
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
            voice: Optional[str] = "",
            language_type: Optional[str] = "", **kwargs
    ) -> AudioGenerationResponse:
        """委托给底层 LLM 客户端的 generate_speech 方法。"""
        return await self.llm.generate_speech(
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
            duration: Optional[int] = 10,
            prompt_extend: bool = True,
            watermark: bool = False,
            negative_prompt: Optional[str] = None,
            seed: Optional[int] = None, **kwargs
    ) -> VideoGenerationResponse:
        """委托给底层 LLM 客户端的 generate_video 方法。"""
        return await self.llm.generate_video(
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


def create_delegating_client(
    client_name: str,
    llm_client_class: Type,
    method_map: Optional[Dict[str, str]] = None,
    client_type: str = "llm",
    adapt_params: bool = True,
) -> Type:
    """
    动态创建一个继承自 BaseModelClient 的子类，将所有抽象方法委托给 llm_client_class 的实例。

    Args:
        client_name: 客户端名称（设置 __client_name__）
        llm_client_class: 实际执行调用的 LLM 客户端类
        method_map: 从 BaseModelClient 方法名到 llm_client_class 方法名的映射
        client_type: 客户端类型
        adapt_params: 是否自动将 BaseModelClient 的参数名适配为 llm_client_class 的参数名（通过 **kwargs 传递）

    Returns:
        动态生成的类
    """
    if method_map is None:
        # 默认同名映射
        method_map = {
            "invoke": "invoke",
            "stream": "stream",
            "generate_image": "generate_image",
            "generate_speech": "generate_speech",
            "generate_video": "generate_video",
        }

    # 委托方法生成器
    def make_delegator(base_method: str, target_method: str):
        if base_method == "stream":
            async def stream_delegator(self, *args, **kwargs):
                llm_client = self.llm_client
                target = getattr(llm_client, target_method)
                # 直接异步迭代目标返回的异步迭代器（或协程自动await）
                async for chunk in target(*args, **kwargs):
                    yield chunk

            return stream_delegator
        else:
            async def delegator(self, *args, **kwargs):
                llm_client = self.llm_client
                target = getattr(llm_client, target_method)
                return await target(*args, **kwargs)

            return delegator

    # 类属性
    attrs = {
        "__client_name__": client_name,
        "__client_type__": client_type,
    }

    # 添加委托方法
    for base_method, target_method in method_map.items():
        attrs[base_method] = make_delegator(base_method, target_method)

    # 自定义 __init__：创建内部 llm_client 实例
    def __init__(self, model_config, model_client_config, **extra_kwargs):
        # 调用父类 BaseModelClient 的 __init__（会进行配置验证等）
        super(self.__class__, self).__init__(model_config, model_client_config)
        # 创建委托目标实例（假设 llm_client_class 的构造函数接受相同参数）
        # 如果构造函数签名不同，可以在此处调整
        self.llm_client = llm_client_class(model_config, model_client_config, **extra_kwargs)

    attrs["__init__"] = __init__
    # 使用 type 动态创建类
    cls = type(
        f"{client_name}DelegatingClient",
        (BaseModelClient,),
        attrs
    )
    return cls