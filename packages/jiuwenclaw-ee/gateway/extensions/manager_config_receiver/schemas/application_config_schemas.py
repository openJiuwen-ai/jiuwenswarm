# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LoggingConfigUpsertRequest(BaseModel):
    """对齐 Manager ``LoggingConfigUpsertRequest``。"""

    level: str = Field(default="INFO", max_length=16)
    console_level: str | None = Field(default=None, max_length=16)
    gateway: str | None = Field(default=None, max_length=16)
    channel: str | None = Field(default=None, max_length=16)
    agent_server: str | None = Field(default=None, max_length=16)
    full: str | None = Field(default=None, max_length=16)


class TaskMemoryUpsertRequest(BaseModel):
    """对齐 Manager ``TaskMemoryUpsertRequest``。"""

    enabled: bool = Field(default=False)
    llm_model: str = Field(default="", max_length=256)
    embedding_model: str = Field(default="", max_length=256)
    api_key: str = Field(default="", max_length=512)
    api_base: str = Field(default="", max_length=1024)
    retrieval_algo: str | None = Field(default=None, max_length=64)
    summary_algo: str | None = Field(default=None, max_length=64)


class MemoryConfigUpsertRequest(BaseModel):
    """对齐 Manager ``MemoryConfigUpsertRequest``。"""

    body: dict[str, Any] = Field(
        ...,
        description=(
            "memory 段配置，结构与 config.yaml::memory 一致"
            "（mode/engine/forbidden_memory_definition/external）"
        ),
    )


class LogMaskingRuleCreateRequest(BaseModel):
    """创建日志脱敏规则（对齐 Manager ``LogMaskingRuleCreateBody``）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    rule_id: str = Field(..., min_length=1, max_length=64)
    rule_name: str = Field(..., min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    pattern: str = Field(..., min_length=1)
    replacement: str | None = Field(default=None, max_length=64)
    priority: int = 0
    with_fingerprint: bool = False
    source: str = Field(default="custom", max_length=16)
    enabled: bool = True
    data: dict[str, Any] | None = None


class LogMaskingRuleUpdateRequest(BaseModel):
    """更新日志脱敏规则（对齐 Manager ``LogMaskingRuleUpdateBody``）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    rule_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    pattern: str | None = None
    replacement: str | None = Field(default=None, max_length=64)
    priority: int | None = None
    with_fingerprint: bool | None = None
    source: str | None = Field(default=None, max_length=16)
    enabled: bool | None = None
    data: dict[str, Any] | None = None


class InstanceDataLifecycleRequest(BaseModel):
    """实例数据生命周期（默认 purge）。

    ``op=purge`` 会清空本 Gateway 库内全部实例级数据（不可逆；每网关独立 DB）。
    """

    op: str = Field(default="purge", max_length=32)
