# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class WebSearchRecord:
    title: str
    url: str
    snippet: str
    source: str
    content: str = ""                     # 网页正文（仅 petal content=True 时携带；不进给模型的响应）
    update_time: Optional[float] = None  # 网页更新时间 epoch 秒；None 表示缺失


@dataclass
class ProviderRun:
    provider: str
    records: list[WebSearchRecord] = field(default_factory=list)
    answer: str = ""
    error: str = ""
    quality_passed: bool = False
    quality_reason: str = ""


@dataclass(frozen=True)
class WebSearchSettings:
    timeout_seconds: int
    max_results: int
    paid_provider_order: tuple[str, ...]
