# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class LoggingConfigUpsertRequest(BaseModel):
    """对齐 logging_config：``level`` NOT NULL；其余 level 列可空。"""

    level: str = Field(default="INFO", min_length=1, max_length=16)
    console_level: str | None = Field(default=None, max_length=16)
    gateway: str | None = Field(default=None, max_length=16)
    channel: str | None = Field(default=None, max_length=16)
    agent_server: str | None = Field(default=None, max_length=16)
    full: str | None = Field(default=None, max_length=16)


class TaskMemoryUpsertRequest(BaseModel):
    """对齐 task_memory_config：仅 ``enabled`` NOT NULL；模型/密钥等列可空。"""

    enabled: bool = Field(default=False)
    llm_model: str | None = Field(default=None, max_length=256)
    embedding_model: str | None = Field(default=None, max_length=256)
    api_key: str | None = Field(default=None, max_length=512)
    api_base: str | None = Field(default=None, max_length=1024)
    retrieval_algo: str | None = Field(default=None, max_length=64)
    summary_algo: str | None = Field(default=None, max_length=64)


class MemoryConfigUpsertRequest(BaseModel):
    """对齐 memory_config upsert：接口要求 body；落库 body 可空由服务层处理。"""

    body: dict[str, Any] = Field(
        ...,
        description=(
            "memory 段配置，结构与 config.yaml::memory 一致"
            "（mode/engine/forbidden_memory_definition/external）"
        ),
    )


class LogMaskingRuleCreateRequest(BaseModel):
    """创建日志脱敏规则（对齐 log_masking_rule / Manager ``LogMaskingRuleCreateBody``）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    rule_id: str = Field(..., min_length=1, max_length=64)
    rule_name: str = Field(..., min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    pattern: str = Field(..., min_length=1, max_length=512)
    # 可省略/null：服务层 normalize_replacement → 默认 ******
    replacement: str | None = Field(default=None, max_length=64)
    priority: int = 0
    with_fingerprint: bool = False
    source: Literal["builtin", "custom"] = Field(default="custom")
    enabled: bool = True
    data: dict[str, Any] | None = None


class LogMaskingRuleUpdateRequest(BaseModel):
    """PATCH：未传=不更新；NOT NULL 列用 ``T = Field(default=None)`` 拒绝显式 null。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    rule_name: str = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    pattern: str = Field(default=None, min_length=1, max_length=512)
    # 可省略；显式 null 仍交给 normalize_replacement 回落到默认值
    replacement: str | None = Field(default=None, max_length=64)
    priority: int = Field(default=None)
    with_fingerprint: bool = Field(default=None)
    source: Literal["builtin", "custom"] = Field(default=None)
    enabled: bool = Field(default=None)
    data: dict[str, Any] | None = None


class InstanceDataLifecycleRequest(BaseModel):
    """实例数据生命周期（默认 purge）。

    ``op=purge`` 会清空本 Gateway 库内全部实例级数据（不可逆；每网关独立 DB）。
    """

    op: str = Field(default="purge", max_length=32)
