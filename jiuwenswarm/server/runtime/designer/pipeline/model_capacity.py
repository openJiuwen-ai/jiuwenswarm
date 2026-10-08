# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Duration, resolution, and reference limits for the configured video model.

Figures follow the vendor docs as of 2026-09 (Wan 3.0, Seedance 2.5, MiniMax-H3,
MiniMax-H3-Max). The director uses the active entry to size shots. Whichever
video model is configured with an API key is the one that applies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VideoModelCapacity:
    model_id: str
    provider: str
    min_sec: int
    max_sec: int
    default_sec: int
    resolutions: tuple[str, ...]
    default_resolution: str
    max_reference_images: int
    supports_reference_images: bool
    ratios: tuple[str, ...] = ("16:9", "9:16", "1:1", "4:3", "3:4")

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "min_sec": self.min_sec,
            "max_sec": self.max_sec,
            "default_sec": self.default_sec,
            "resolutions": list(self.resolutions),
            "default_resolution": self.default_resolution,
            "max_reference_images": self.max_reference_images,
            "supports_reference_images": self.supports_reference_images,
            "ratios": list(self.ratios),
        }


# Wan 3.0: 2–30s without a reference video; 480P/720P/1080P; up to 10 reference images.
# Official default resolution is 1080P.
_WAN3 = VideoModelCapacity(
    model_id="wan3.0-video",
    provider="dashscope",
    min_sec=2,
    max_sec=30,
    default_sec=5,
    resolutions=("480P", "720P", "1080P"),
    default_resolution="1080P",
    max_reference_images=10,
    supports_reference_images=True,
)

# Seedance 2.5 (BytePlus / Volcengine, 2026-09): 4–30s, 480p/720p/1080p, 30 reference images.
_SEEDANCE_25 = VideoModelCapacity(
    model_id="doubao-seedance-2-5-260628",
    provider="volcengine",
    min_sec=4,
    max_sec=30,
    default_sec=5,
    resolutions=("480p", "720p", "1080p"),
    default_resolution="720p",
    max_reference_images=30,
    supports_reference_images=True,
    ratios=("21:9", "16:9", "4:3", "1:1", "3:4", "9:16"),
)

# Seedance 2.0 series: 4–15s. 2.5 is the 30s model above.
_SEEDANCE_20 = VideoModelCapacity(
    model_id="doubao-seedance-2-0-260128",
    provider="volcengine",
    min_sec=4,
    max_sec=15,
    default_sec=5,
    resolutions=("480p", "720p", "1080p"),
    default_resolution="720p",
    max_reference_images=9,
    supports_reference_images=True,
    ratios=("21:9", "16:9", "4:3", "1:1", "3:4", "9:16"),
)

# MiniMax-H3: 4–15s, 768P/2K, up to 9 reference images.
_H3 = VideoModelCapacity(
    model_id="MiniMax-H3",
    provider="minimax",
    min_sec=4,
    max_sec=15,
    default_sec=5,
    resolutions=("768P", "2K"),
    default_resolution="768P",
    max_reference_images=9,
    supports_reference_images=True,
)

# MiniMax-H3-Max: 5–15s, 480P/768P. Reference images are rejected by the API.
_H3_MAX = VideoModelCapacity(
    model_id="MiniMax-H3-Max",
    provider="minimax",
    min_sec=5,
    max_sec=15,
    default_sec=6,
    resolutions=("480P", "768P"),
    default_resolution="768P",
    max_reference_images=0,
    supports_reference_images=False,
)

_CATALOG: tuple[VideoModelCapacity, ...] = (
    _WAN3,
    _SEEDANCE_25,
    _SEEDANCE_20,
    _H3,
    _H3_MAX,
)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def capacity_for_model(model_id: str) -> VideoModelCapacity:
    """Match a configured model id. Unknown ids fall back to Wan 3.0."""
    key = _norm(model_id)
    if not key:
        return _WAN3
    if "h3max" in key or ("minimax" in key and key.endswith("max")):
        return _H3_MAX
    if "minimaxh3" in key or key in {"h3", "minimaxh3"}:
        return _H3
    if "seedance" in key and ("25" in key or "260628" in key):
        return _SEEDANCE_25
    if key.startswith("wan3") or "wan30" in key:
        return _WAN3
    for item in _CATALOG:
        if _norm(item.model_id) == key:
            return item
    if "seedance" in key:
        return _SEEDANCE_20
    if "minimax" in key or "hailuo" in key:
        return _H3_MAX if "max" in key else _H3
    if "wan" in key:
        return _WAN3
    return _WAN3


def configured_video_model_id() -> str:
    """Model name from Settings / env. Empty when nothing is configured."""
    from jiuwenswarm.server.runtime.designer.media_generation import configured_model

    return configured_model("video")


def active_video_capacity() -> VideoModelCapacity:
    """Limits for the video model whose API key and model id are configured."""
    return capacity_for_model(configured_video_model_id())


def snap_duration(requested: int | float | None, capacity: VideoModelCapacity) -> int:
    """Map a requested clip length onto seconds this model accepts.

    Mirrors ``snap_resolution``: missing or invalid values become the model
    default; otherwise the value is clamped to ``min_sec``–``max_sec``.
    """
    try:
        raw = int(round(float(requested))) if requested is not None else 0
    except (TypeError, ValueError):
        raw = 0
    if raw <= 0:
        return int(capacity.default_sec)
    lo = int(capacity.min_sec)
    hi = int(capacity.max_sec)
    if hi < lo:
        hi = lo
    return max(lo, min(hi, raw))


def snap_resolution(requested: str, capacity: VideoModelCapacity) -> str:
    """Map a free-form resolution onto a tier this model accepts."""
    raw = str(requested or "").strip()
    if not raw:
        return capacity.default_resolution
    token = raw.upper().replace(" ", "")
    allowed = {item.upper().replace(" ", ""): item for item in capacity.resolutions}
    if token in allowed:
        return allowed[token]
    aliases = {
        "1080": "1080P",
        "720": "720P",
        "480": "480P",
        "768": "768P",
        "2K": "2K",
        "4K": "1080P",
        "1K": "720P",
    }
    mapped = aliases.get(token, "")
    if mapped and mapped.upper() in allowed:
        return allowed[mapped.upper()]
    # A request above this model's top tier uses that top tier (4K on H3 is 2K).
    rank = {"480P": 1, "720P": 2, "768P": 3, "1080P": 4, "2K": 5, "4K": 6}
    asked_rank = rank.get(token) or rank.get(mapped) or 0
    if asked_rank:
        ranked = [
            (rank.get(key, 0), label)
            for key, label in allowed.items()
            if rank.get(key, 0)
        ]
        at_or_below = [item for item in ranked if item[0] <= asked_rank]
        if at_or_below:
            return max(at_or_below)[1]
        if ranked:
            return max(ranked)[1]
    return capacity.default_resolution


def size_for_resolution(resolution: str, ratio: str) -> str:
    """W*H companion for providers that still take a pixel size."""
    tier = str(resolution or "").upper().replace(" ", "")
    aspect = str(ratio or "16:9").strip()
    table = {
        ("480P", "16:9"): "832*480",
        ("480P", "9:16"): "480*832",
        ("480P", "1:1"): "480*480",
        ("720P", "16:9"): "1280*720",
        ("720P", "9:16"): "720*1280",
        ("720P", "1:1"): "960*960",
        ("720P", "4:3"): "1088*832",
        ("720P", "3:4"): "832*1088",
        ("768P", "16:9"): "1280*768",
        ("768P", "9:16"): "768*1280",
        ("768P", "1:1"): "768*768",
        ("1080P", "16:9"): "1920*1080",
        ("1080P", "9:16"): "1080*1920",
        ("1080P", "1:1"): "1440*1440",
        ("1080P", "4:3"): "1632*1248",
        ("1080P", "3:4"): "1248*1632",
        ("1080P", "21:9"): "2560*1080",
        ("2K", "16:9"): "2560*1440",
        ("2K", "9:16"): "1440*2560",
        ("2K", "1:1"): "1440*1440",
    }
    if aspect not in {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}:
        aspect = "16:9"
    return table.get((tier, aspect)) or table.get((tier, "16:9")) or "1920*1080"


_RES_RE = re.compile(
    r"(?P<tier>480\s*p|720\s*p|768\s*p|1080\s*p|2\s*k|4\s*k)",
    re.I,
)


def resolution_mentioned(text: str) -> str:
    """Resolution the user actually asked for, or empty."""
    match = _RES_RE.search(str(text or ""))
    if not match:
        return ""
    token = re.sub(r"\s+", "", match.group("tier")).upper()
    if token == "4K":
        return "4K"
    if token == "2K":
        return "2K"
    if token.endswith("P"):
        return token
    return ""
