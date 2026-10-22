# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Read the video format a user asked for in their first prompt.

A short structured LLM call reads aspect ratio, resolution, and total runtime,
including indirect wording ("抖音竖版", "a minute and a half"). The regexes
below only fill fields the model call could not answer. Fields the user did
not state stay ``None``; ``media_settings_from_intent`` turns those into the
video model's defaults. The runtime is a planning target for the storyboard
only and is not stored as a canvas setting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, replace

from jiuwenswarm.server.runtime.designer.media_settings import (
    ImageSettings,
    VideoSettings,
    snap_image_ratio,
)
from jiuwenswarm.server.runtime.designer.pipeline.image_capacity import (
    ImageModelCapacity,
    active_image_capacity,
)
from jiuwenswarm.server.runtime.designer.pipeline.model_capacity import (
    VideoModelCapacity,
    active_video_capacity,
    default_ratio,
    snap_ratio,
    snap_resolution,
)

logger = logging.getLogger(__name__)

INTENT_RATIOS = ("21:9", "16:9", "3:2", "4:3", "1:1", "3:4", "2:3", "9:16")
INTENT_RESOLUTIONS = ("480p", "720p", "768p", "1080p", "2k", "4k")
_MAX_FILM_SEC = 3600


@dataclass(frozen=True)
class MediaIntent:
    aspect_ratio: str | None = None
    resolution: str | None = None
    film_duration_sec: int | None = None


_RATIO_RE = re.compile(r"(?<![\d.])(21|16|9|4|3|2|1)\s*[:xX：]\s*(9|16|3|4|2|1)(?![\d.])")
_VERTICAL_RE = re.compile(r"\b(?:vertical|tiktok|reels|shorts|portrait\s+video)\b|竖屏|竖版|竖向", re.I)
_HORIZONTAL_RE = re.compile(r"\b(?:horizontal|landscape\s+video|widescreen)\b|横屏|横版", re.I)
_SQUARE_RE = re.compile(r"\bsquare\s+(?:frame|video|format)\b|方形|正方形", re.I)
_ULTRAWIDE_RE = re.compile(r"\b(?:cinemascope|ultrawide|ultra-wide)\b|宽银幕", re.I)
_RES_RE = re.compile(r"(?<![\d.])(480|720|768|1080)\s*[pP]\b|(?<![\d.])([24])\s*[kK]\b")
_MIN_RE = re.compile(r"(?P<n>\d{1,3}(?:\.\d+)?)\s*(?:-|–)?\s*(?:minutes?|mins?|min|分钟)", re.I)
_SEC_RE = re.compile(r"(?P<n>\d{1,4}(?:\.\d+)?)\s*(?:-|–)?\s*(?:seconds?|secs?|sec|秒钟?)", re.I)
_SEC_COMPACT_RE = re.compile(r"\b(?P<n>\d{1,3})\s*s\b", re.I)
_RANGE_SEC_RE = re.compile(
    r"(?P<a>\d{1,4}(?:\.\d+)?)\s*(?:-|–|to)\s*(?P<b>\d{1,4}(?:\.\d+)?)\s*(?:seconds?|secs?|sec|秒钟?)",
    re.I,
)


def prompt_ratio(text: str) -> str | None:
    """Aspect ratio stated in ``text`` by number or by format name."""
    body = str(text or "")
    match = _RATIO_RE.search(body)
    if match:
        candidate = f"{match.group(1)}:{match.group(2)}"
        if candidate in INTENT_RATIOS:
            return candidate
    if _VERTICAL_RE.search(body):
        return "9:16"
    if _ULTRAWIDE_RE.search(body):
        return "21:9"
    if _SQUARE_RE.search(body):
        return "1:1"
    if _HORIZONTAL_RE.search(body):
        return "16:9"
    return None


def prompt_resolution(text: str) -> str | None:
    match = _RES_RE.search(str(text or ""))
    if not match:
        return None
    return f"{match.group(1)}p" if match.group(1) else f"{match.group(2)}k"


def prompt_duration_sec(text: str) -> int | None:
    """Total runtime stated in ``text`` in seconds, or None."""
    body = str(text or "").strip()
    if not body:
        return None
    found: list[int] = []
    for m in _MIN_RE.finditer(body):
        sec = int(round(float(m.group("n")) * 60.0))
        if 60 <= sec <= _MAX_FILM_SEC:
            found.append(sec)
    for m in _RANGE_SEC_RE.finditer(body):
        a, b = float(m.group("a")), float(m.group("b"))
        sec = int(round((a + b) / 2.0)) if b >= a else int(round(b))
        if 1 <= sec <= _MAX_FILM_SEC:
            found.append(sec)
    for m in _SEC_RE.finditer(body):
        sec = int(round(float(m.group("n"))))
        if 1 <= sec <= _MAX_FILM_SEC:
            found.append(sec)
    if not found:
        for m in _SEC_COMPACT_RE.finditer(body):
            sec = int(m.group("n"))
            if 2 <= sec <= 120:
                found.append(sec)
    return max(found) if found else None


def regex_media_intent(prompt: str) -> MediaIntent:
    return MediaIntent(
        aspect_ratio=prompt_ratio(prompt),
        resolution=prompt_resolution(prompt),
        film_duration_sec=prompt_duration_sec(prompt),
    )


def _intent_system() -> str:
    return (
        "You read a video request and report only the output format the user asked for. "
        "Output ONLY one JSON object: "
        '{"aspect_ratio": null, "resolution": null, "film_duration_sec": null}. '
        f"aspect_ratio is one of {list(INTENT_RATIOS)} or null. "
        f"resolution is one of {list(INTENT_RESOLUTIONS)} or null. "
        "film_duration_sec is the total runtime in whole seconds or null. "
        "Use null unless the user states the value or names a format that fixes it: "
        "vertical / 竖屏 / TikTok / Reels / Shorts / phone full screen = 9:16; "
        "横屏 / landscape = 16:9; cinemascope / 宽银幕 = 21:9; square = 1:1. "
        "'a minute and a half' = 90; 'half a minute' = 30. "
        "Never infer a format from the story, subject, or genre."
    )


def _parse_llm_intent(text: str) -> MediaIntent:
    from jiuwenswarm.server.runtime.designer.script_analysis import _extract_json_object

    parsed = _extract_json_object(text) or {}
    ratio = str(parsed.get("aspect_ratio") or "").strip()
    resolution = str(parsed.get("resolution") or "").strip().lower()
    raw_duration = parsed.get("film_duration_sec")
    duration = None
    if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool):
        duration = int(round(raw_duration))
        if not 1 <= duration <= _MAX_FILM_SEC:
            duration = None
    return MediaIntent(
        aspect_ratio=ratio if ratio in INTENT_RATIOS else None,
        resolution=resolution if resolution in INTENT_RESOLUTIONS else None,
        film_duration_sec=duration,
    )


def _merge(primary: MediaIntent, fallback: MediaIntent) -> MediaIntent:
    return replace(
        primary,
        aspect_ratio=primary.aspect_ratio or fallback.aspect_ratio,
        resolution=primary.resolution or fallback.resolution,
        film_duration_sec=primary.film_duration_sec or fallback.film_duration_sec,
    )


async def extract_media_intent(prompt: str, *, timeout_sec: float = 30.0) -> MediaIntent:
    """Format the user asked for. Model call first, regexes for unanswered fields."""
    text = str(prompt or "").strip()
    fallback = regex_media_intent(text)
    if not text:
        return fallback
    from jiuwenswarm.server.runtime.designer.model_tools import call_model_tool, model_text_or_raise

    try:
        result = await asyncio.wait_for(
            call_model_tool(
                prompt=json.dumps({"user_prompt": text[:3000]}, ensure_ascii=False),
                system=_intent_system(),
                max_tokens=512,
            ),
            timeout=timeout_sec,
        )
        llm = _parse_llm_intent(model_text_or_raise(result))
    except Exception:  # noqa: BLE001
        logger.info("Media intent model call failed; using prompt patterns only", exc_info=True)
        return fallback
    return _merge(llm, fallback)


def media_settings_from_intent(
    intent: MediaIntent,
    capacity: VideoModelCapacity | None = None,
    image_capacity: ImageModelCapacity | None = None,
) -> tuple[VideoSettings, ImageSettings]:
    """Canvas settings for a new graph. Unstated fields take the model defaults.

    Stills start at the video ratio so scene plates frame like the clips; the
    two settings are edited separately afterwards.
    """
    cap = capacity or active_video_capacity()
    image_cap = image_capacity or active_image_capacity()
    video = VideoSettings(
        aspect_ratio=snap_ratio(intent.aspect_ratio, cap) if intent.aspect_ratio else default_ratio(cap),
        resolution=snap_resolution(intent.resolution or "", cap),
    )
    return video, ImageSettings(
        aspect_ratio=snap_image_ratio(video.aspect_ratio),
        resolution=image_cap.default_resolution,
    )
