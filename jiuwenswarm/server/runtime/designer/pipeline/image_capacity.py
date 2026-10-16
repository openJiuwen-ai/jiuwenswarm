# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Resolution tiers for the configured image model and the pixel size of each tier.

The toolbar offers a ratio and a tier; the backend sends ``IMAGE_SIZES[tier][ratio]``
as the model's width x height. Which tiers a model gets follows the vendor limits:
Qwen-Image 3.0 and MiniMax image-01 accept 512-2048 px per side, Seedream models take
their documented tiers, vLLM-Omni serves 0.5K-2K, and anything else gets 1K.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

IMAGE_RATIOS: tuple[str, ...] = ("16:9", "4:3", "1:1", "3:4", "9:16")
DEFAULT_IMAGE_RATIO = "16:9"

IMAGE_SIZES: dict[str, dict[str, str]] = {
    "0.5K": {"1:1": "512x512", "4:3": "576x432", "3:4": "432x576", "16:9": "704x400", "9:16": "400x704"},
    "1K": {"1:1": "1024x1024", "4:3": "1152x864", "3:4": "864x1152", "16:9": "1424x800", "9:16": "800x1424"},
    "1.5K": {"1:1": "1536x1536", "4:3": "1792x1344", "3:4": "1344x1792", "16:9": "2048x1152", "9:16": "1152x2048"},
    "2K": {"1:1": "2048x2048", "4:3": "2368x1776", "3:4": "1776x2368", "16:9": "2816x1584", "9:16": "1584x2816"},
    "3K": {"1:1": "3072x3072", "4:3": "3456x2592", "3:4": "2592x3456", "16:9": "4096x2304", "9:16": "2304x4096"},
    "4K": {"1:1": "4096x4096", "4:3": "4704x3520", "3:4": "3520x4704", "16:9": "5504x3040", "9:16": "3040x5504"},
}
_TIER_ORDER = tuple(IMAGE_SIZES)


@dataclass(frozen=True)
class ImageModelCapacity:
    resolutions: tuple[str, ...]
    default_resolution: str


_ONE_K = ImageModelCapacity(resolutions=("1K",), default_resolution="1K")
_QWEN_MINIMAX = ImageModelCapacity(resolutions=("1K", "1.5K"), default_resolution="1K")
_SEEDREAM_50_PRO_FLASH = ImageModelCapacity(resolutions=("1K", "1.5K", "2K"), default_resolution="1K")
_SEEDREAM_50 = ImageModelCapacity(resolutions=("2K", "3K", "4K"), default_resolution="2K")
_SEEDREAM_45 = ImageModelCapacity(resolutions=("2K", "4K"), default_resolution="2K")
_SEEDREAM_40 = ImageModelCapacity(resolutions=("1K", "2K", "4K"), default_resolution="1K")
_VLLM_OMNI = ImageModelCapacity(resolutions=("0.5K", "1K", "1.5K", "2K"), default_resolution="1K")


def image_capacity_for(model_id: str, backend: str | None) -> ImageModelCapacity:
    """Tiers for a configured image model on its backend."""
    from jiuwenswarm.agents.harness.common.tools.gen_toolkits import VLLM_OMNI

    if backend == VLLM_OMNI:
        return _VLLM_OMNI
    if backend is None:
        return _ONE_K
    key = re.sub(r"[^a-z0-9]+", "", (model_id or "").lower())
    if "qwenimage30" in key or key.startswith("image01"):
        return _QWEN_MINIMAX
    if "seedream50pro" in key or "seedream50flash" in key:
        return _SEEDREAM_50_PRO_FLASH
    if "seedream50" in key:
        return _SEEDREAM_50
    if "seedream45" in key:
        return _SEEDREAM_45
    if "seedream40" in key:
        return _SEEDREAM_40
    return _ONE_K


def active_image_capacity() -> ImageModelCapacity:
    """Tiers for the image model saved in Settings > Agent."""
    from jiuwenswarm.server.runtime.designer.media_generation import slot_settings

    settings = slot_settings("image")
    return image_capacity_for(settings.model, settings.backend)


def snap_image_resolution(requested: str, capacity: ImageModelCapacity) -> str:
    """The model's tier closest to ``requested``; unknown values take the model default."""
    token = str(requested or "").strip().upper()
    if token not in _TIER_ORDER:
        return capacity.default_resolution
    asked = _TIER_ORDER.index(token)
    return min(capacity.resolutions, key=lambda tier: (abs(_TIER_ORDER.index(tier) - asked), _TIER_ORDER.index(tier)))
