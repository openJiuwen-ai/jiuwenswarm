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
    content: str = ""                     # 网页正文（petal / bocha 等 provider 携带，可能只是摘要）
    update_time: Optional[float] = None  # 网页更新时间 epoch 秒；None 表示缺失
    images: list[str] = field(default_factory=list)  # provider 直接返回的图片 URL（可空）


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
