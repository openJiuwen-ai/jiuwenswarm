# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""配额 API 请求 Schema。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PolicyUpsertRequest(BaseModel):
    """工作区配额策略按 policy_id 全量 upsert。未传的可选字段使用缺省。

    不含 cluster_id：一套 Gateway 对应一个集群。
    """

    model_config = ConfigDict(extra="forbid")

    policy_id: str = Field(min_length=1, max_length=64)
    policy_name: str = Field(min_length=1, max_length=128)
    policy_desc: str | None = Field(default=None, max_length=512)
    match_expr: Any = None
    priority: int
    # -1=无限制；0=零配额；>0=正常限额
    limit_bytes: int = Field(ge=-1)
    soft_percent: int = Field(ge=0, le=100, default=80)
    hard_percent: int = Field(ge=0, le=100, default=100)
    source: str = Field(default="manual", max_length=32)
    source_order_num: str | None = Field(default=None, max_length=64)
    enabled: bool

    @model_validator(mode="after")
    def _percents(self) -> PolicyUpsertRequest:
        if self.hard_percent <= self.soft_percent:
            raise ValueError("hard_percent must be greater than soft_percent")
        return self

    @model_validator(mode="after")
    def _normalize_name_desc(self) -> PolicyUpsertRequest:
        name = self.policy_name.strip()
        if not name:
            raise ValueError("policy_name is required")
        self.policy_name = name
        desc = (self.policy_desc or "").strip()
        self.policy_desc = desc or None
        return self

    @model_validator(mode="after")
    def _normalize_source(self) -> PolicyUpsertRequest:
        raw = (self.source or "").strip().lower() or "manual"
        if raw not in {"manual", "approval"}:
            raise ValueError("source must be manual or approval")
        self.source = raw
        order = (self.source_order_num or "").strip() or None
        if self.source != "approval":
            order = None
        self.source_order_num = order
        return self


class UsageListQuery(BaseModel):
    """本集群用量缓存查询（只读，不访问 Agent）。"""

    user_id: str | None = Field(default=None, max_length=64)
    group_id: str | None = Field(
        default=None,
        max_length=64,
        description="有则过滤；无组织行传空串",
    )
    bot_id: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=20, ge=1, le=100)
