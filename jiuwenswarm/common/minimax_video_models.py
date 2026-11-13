# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared MiniMax H3 variant recognition for video generation."""

import re
from enum import Enum


class MiniMaxH3Variant(Enum):
    """Variants with resolution limits distinct from the base H3 model."""

    MAX = "h3max"
    HIGHSPEED = "h3highspeed"
    LITE = "h3lite"


def minimax_h3_variant(model: str) -> MiniMaxH3Variant | None:
    """Recognize restricted H3 variants in configured model IDs."""
    key = re.sub(r"[^a-z0-9]+", "", model.lower())
    if "minimax" in key and key.endswith("max"):
        return MiniMaxH3Variant.MAX
    for variant in MiniMaxH3Variant:
        if variant.value in key:
            return variant
    return None
