# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""OpenAI 兼容的自定义 ModelClient 示例。

使用方式：
1. 将本文件复制到 ``<EXTENSION_DIRS>/model_clients/u_openai_model_client.py``；
2. 设置 ``AGENT_EXTRA_MODEL_CLIENTS=u_openai_model_client``；
3. 模型配置使用 ``client_provider: UOpenAI``（Manager 中对应
   ``model_provider: UOpenAI``），其余模型地址、名称和密钥照常配置；
4. 重启 AgentServer。多个源码文件名使用逗号分隔。

本示例在正常响应和异常响应中额外收集四个业务字段，并放入
``provider_metadata``，供 Rail 读取。字段范围由白名单限制。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.foundation.llm.model_clients.openai_model_client import (
    OpenAIModelClient,
)


PROVIDER_METADATA_KEYS = (
    "servRespCd",
    "resCode",
    "servRespDescInfo",
    "globalBusiTrackNo",
)
_SCALAR_TYPES = (str, int, float, bool)


def extract_provider_metadata(source: Any) -> dict[str, Any]:
    """从字典或 SDK 响应对象中读取白名单内的业务字段。"""
    metadata: dict[str, Any] = {}
    for key in PROVIDER_METADATA_KEYS:
        if isinstance(source, Mapping):
            value = source.get(key)
        else:
            value = getattr(source, key, None)
            if value is None:
                extra = getattr(source, "model_extra", None)
                value = extra.get(key) if isinstance(extra, Mapping) else None
        if isinstance(value, _SCALAR_TYPES) and value != "":
            metadata[key] = value
    return metadata


def metadata_from_exception(exc: BaseException | None) -> dict[str, Any]:
    """从模型异常及其原因链中读取白名单内的业务字段。"""
    metadata: dict[str, Any] = {}
    seen: set[int] = set()
    current = exc

    while current is not None and id(current) not in seen:
        seen.add(id(current))

        details = getattr(current, "details", None)
        if isinstance(details, Mapping):
            provider_metadata = details.get("provider_metadata")
            if isinstance(provider_metadata, Mapping):
                metadata.update(extract_provider_metadata(provider_metadata))
            metadata.update(extract_provider_metadata(details))

        body = getattr(current, "body", None)
        if isinstance(body, Mapping):
            metadata.update(extract_provider_metadata(body))

        response = getattr(current, "response", None)
        if response is not None:
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001 - 保留原始模型异常
                payload = None
            if isinstance(payload, Mapping):
                metadata.update(extract_provider_metadata(payload))

        current = current.__cause__ or current.__context__

    return metadata


def _attach_metadata(exc: Exception, metadata: Mapping[str, Any]) -> None:
    if not metadata:
        return
    details = getattr(exc, "details", None)
    details = dict(details) if isinstance(details, Mapping) else {}
    details["provider_metadata"] = dict(metadata)
    exc.details = details  # type: ignore[attr-defined]


class UOpenAIModelClient(OpenAIModelClient):
    """以 ``UOpenAI`` 为配置名注册的 OpenAI 兼容客户端。"""

    __client_name__: ClassVar[list[str]] = ["UOpenAI"]

    @staticmethod
    def _response_provider_metadata(response: Any) -> dict[str, Any]:
        metadata = OpenAIModelClient._response_provider_metadata(response)
        metadata.update(extract_provider_metadata(response))
        return metadata

    async def _parse_response(self, response: Any, parser: Any = None):
        if not getattr(response, "choices", None):
            raise build_error(
                StatusCode.MODEL_CALL_FAILED,
                details={
                    "provider_metadata": self._response_provider_metadata(response)
                },
                error_msg="U OpenAI gateway returned no choices.",
            )
        return await super()._parse_response(response, parser)

    async def invoke(self, *args: Any, **kwargs: Any):
        try:
            return await super().invoke(*args, **kwargs)
        except Exception as exc:
            _attach_metadata(exc, metadata_from_exception(exc))
            raise

    async def stream(self, *args: Any, **kwargs: Any):
        try:
            async for chunk in super().stream(*args, **kwargs):
                yield chunk
        except Exception as exc:
            _attach_metadata(exc, metadata_from_exception(exc))
            raise


__all__ = [
    "PROVIDER_METADATA_KEYS",
    "UOpenAIModelClient",
    "extract_provider_metadata",
    "metadata_from_exception",
]
