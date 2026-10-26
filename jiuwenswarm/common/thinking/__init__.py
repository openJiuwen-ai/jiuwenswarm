# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Thinking control: semantic depth / toggle → vendor kwargs."""

from __future__ import annotations

from jiuwenswarm.common.thinking.adapter import adapt_thinking, resolve_effective_depth
from jiuwenswarm.common.thinking.rail import ThinkingInjectRail
from jiuwenswarm.common.thinking.types import (
    THINKING_VALUES,
    ThinkingProfile,
    normalize_thinking,
)
from jiuwenswarm.common.thinking.vendor_map import DEFAULT_THINKING_DEPTH

__all__ = [
    "DEFAULT_THINKING_DEPTH",
    "THINKING_VALUES",
    "ThinkingInjectRail",
    "ThinkingProfile",
    "adapt_thinking",
    "normalize_thinking",
    "resolve_effective_depth",
]
