# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Prompt-length limits for configured image/video backends (domain-agnostic).

Resolves limits from the user's VISUAL_GEN / VIDEO_GEN model + provider hints.
Never forces a specific model. Unknown backends get a soft advisory only.

Sources are vendor docs / common API contracts (chars unless noted). Token
limits are converted with a conservative chars≈tokens*4 estimate for LLM
guidance. Override at runtime with VISUAL_GEN_PROMPT_MAX_CHARS /
VIDEO_GEN_PROMPT_MAX_CHARS (and the IMAGE_* / VIDEO_* twins).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Literal


Kind = Literal["image", "video"]


@dataclass(frozen=True)
class PromptLimit:
    """Resolved prompt budget for one media tool call."""

    kind: Kind
    model: str
    provider: str
    max_chars: int | None
    """Hard or documented character budget. None = no known hard cap."""

    unit: str
    """'chars' or 'tokens' (tokens already converted into max_chars)."""

    known: bool
    """True when matched a documented family/model entry."""

    soft: bool
    """True when this is only advisory (unknown model / soft family default)."""

    source: str
    """Short provenance note for logs / Director gate."""

    def guidance(self) -> str:
        label = "IMAGE" if self.kind == "image" else "VIDEO"
        tool = "call_image_model" if self.kind == "image" else "call_video_model"
        model_bit = self.model or "configured backend"
        if self.max_chars and self.max_chars > 0:
            tone = "soft advisory" if self.soft or not self.known else "documented"
            return (
                f"{label} PROMPT LIMIT ({tone}, model={model_bit}): keep every "
                f"{tool} prompt under {self.max_chars} characters. Dense shot-ready "
                "prose only — no lock banners, forbid lists, or padding."
            )
        return (
            f"{label} PROMPT LIMIT (no documented hard cap for model={model_bit}): "
            f"write a full production-ready {tool} prompt; stay concise and avoid padding."
        )


# (kind, pattern, max_chars, unit_note, source)
# Patterns match normalized model id OR provider OR api_base substring.
# Order: more specific first within each family.
_REGISTRY: tuple[tuple[str, str, int, str, str], ...] = (
    # —— Image ——
    ("image", r"(?i)\bimage-01\b|minimax.*image|image.*minimax", 1500, "chars", "MiniMax image-01 docs"),
    ("image", r"(?i)qwen-image-3\.0|qwen-image-max|qwen-image-plus|qwen-image-edit", 18000, "≈4500 tokens×4", "Qwen-Image 3.x / DashScope (~4500 tokens)"),
    ("image", r"(?i)qwen-image-2\.0|qwen-image-2\b", 5200, "≈1300 tokens×4", "Qwen-Image 2.x (~1300 tokens)"),
    ("image", r"(?i)qwen-image\b|qwen.*image|dashscope.*image", 3200, "≈800 tokens×4", "Qwen-Image legacy / DashScope (~800 tokens|chars)"),
    ("image", r"(?i)\bdall-?e-3\b|gpt-image|openai.*image", 4000, "chars", "OpenAI image API common cap"),
    ("image", r"(?i)\bflux\b|blackforest|bfl\.|schnell|dev\b.*flux", 1200, "≈300 tokens×4", "FLUX.1 ~300-token encoder"),
    ("image", r"(?i)stable-?diffusion|sdxl|sd3|stability", 800, "chars", "SD/SDXL common prompt practice"),
    ("image", r"(?i)ideogram", 2000, "chars", "Ideogram common API practice"),
    ("image", r"(?i)recraft|imagen|gemini.*image|nano-?banana|seedream", 4000, "chars", "common hosted image APIs"),
    ("image", r"(?i)midjourney", 2000, "chars", "Midjourney prompt practice"),
    # —— Video ——
    ("video", r"(?i)minimax-h3|hailuo-?h3|h3-max|video.?generation.?v2", 7000, "chars", "MiniMax H3 / video-generation v2"),
    ("video", r"(?i)minimax|hailuo|t2v-01|i2v-01|video-01", 2000, "chars", "MiniMax Hailuo T2V/I2V (pre-H3)"),
    ("video", r"(?i)wan2\.|wan3\.|wan-?video|wanx|tongyi.*wan", 20000, "chars", "Wan / DashScope video common max"),
    ("video", r"(?i)seedance.?2\.5|seedance@2\.5", 10000, "chars", "Seedance 2.5 API max"),
    ("video", r"(?i)seedance|doubao.*video|bytedance.*video", 5000, "chars", "Seedance / ByteDance video common"),
    ("video", r"(?i)\bkling\b|kwai.*video", 2500, "chars", "Kling API common practice"),
    ("video", r"(?i)runway|gen-3|gen3|gen-4|act-one", 3000, "chars", "Runway Gen common practice"),
    ("video", r"(?i)\bluma\b|dream-?machine|ray-", 3000, "chars", "Luma Dream Machine common"),
    ("video", r"(?i)pika|vidu|hunyuan.?video|cogvideo|mochi|ltx-?video", 4000, "chars", "common video APIs"),
    ("video", r"(?i)veo|sora|openai.*video", 4000, "chars", "hosted video APIs common"),
)

# Soft defaults when nothing matches — advisory only, never forces a vendor.
_SOFT_DEFAULT: dict[Kind, int] = {"image": 4000, "video": 4000}


def _env_int(*names: str) -> int | None:
    for name in names:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def _slot_env(kind: Kind, field: str) -> str:
    prefix = "VISUAL_GEN" if kind == "image" else "VIDEO_GEN"
    return os.environ.get(f"{prefix}_{field}") or ""


def _provider_blob(*, kind: Kind, model: str = "", provider: str = "", api_base: str = "") -> str:
    provider = provider or _slot_env(kind, "PROVIDER")
    api_base = api_base or _slot_env(kind, "API_BASE")
    protocol = _slot_env(kind, "PROTOCOL")
    model = model or _configured_model(kind)
    return " ".join(_norm(x) for x in (model, provider, api_base, protocol) if _norm(x))


def _configured_model(kind: Kind) -> str:
    from jiuwenswarm.server.runtime.designer.media_generation import configured_model

    return configured_model(kind)


def _match_registry(kind: Kind, blob: str) -> tuple[int, str, str] | None:
    text = blob or ""
    if not text:
        return None
    for reg_kind, pattern, max_chars, unit, source in _REGISTRY:
        if reg_kind != kind:
            continue
        if re.search(pattern, text):
            return max_chars, unit, source
    return None


def resolve_prompt_limit(
    kind: Kind,
    *,
    model: str | None = None,
    provider: str | None = None,
    api_base: str | None = None,
) -> PromptLimit:
    """Resolve prompt budget for the configured (or passed) media backend."""
    kind_l: Kind = "image" if str(kind).lower().startswith("image") else "video"
    model_s = _norm(model or _configured_model(kind_l))
    blob = _provider_blob(
        kind=kind_l,
        model=model_s,
        provider=_norm(provider or ""),
        api_base=_norm(api_base or ""),
    )
    override = _env_int(
        *(
            ("VISUAL_GEN_PROMPT_MAX_CHARS", "IMAGE_PROMPT_MAX_CHARS")
            if kind_l == "image"
            else ("VIDEO_GEN_PROMPT_MAX_CHARS", "VIDEO_PROMPT_MAX_CHARS")
        )
    )
    provider_s = _norm(provider or _slot_env(kind_l, "PROVIDER"))

    if override:
        return PromptLimit(
            kind=kind_l,
            model=model_s,
            provider=provider_s,
            max_chars=override,
            unit="chars",
            known=True,
            soft=False,
            source="env override",
        )

    hit = _match_registry(kind_l, blob)
    if hit:
        max_chars, unit, source = hit
        return PromptLimit(
            kind=kind_l,
            model=model_s,
            provider=provider_s,
            max_chars=max_chars,
            unit=unit,
            known=True,
            soft=False,
            source=source,
        )

    soft_max = _SOFT_DEFAULT[kind_l]
    return PromptLimit(
        kind=kind_l,
        model=model_s or "(unspecified)",
        provider=provider_s,
        max_chars=soft_max,
        unit="chars",
        known=False,
        soft=True,
        source="soft default for unknown backend",
    )


def image_prompt_limit_guidance(
    *,
    model: str | None = None,
    provider: str | None = None,
) -> str:
    return resolve_prompt_limit("image", model=model, provider=provider).guidance()


def video_prompt_limit_guidance(
    *,
    model: str | None = None,
    provider: str | None = None,
) -> str:
    return resolve_prompt_limit("video", model=model, provider=provider).guidance()


def media_prompt_limit_packet() -> dict[str, Any]:
    """Structured limits for Director lock gate / leaf JSON context."""
    image = resolve_prompt_limit("image")
    video = resolve_prompt_limit("video")
    return {
        "image": {
            "model": image.model,
            "provider": image.provider,
            "max_chars": image.max_chars,
            "known": image.known,
            "soft": image.soft,
            "source": image.source,
            "guidance": image.guidance(),
        },
        "video": {
            "model": video.model,
            "provider": video.provider,
            "max_chars": video.max_chars,
            "known": video.known,
            "soft": video.soft,
            "source": video.source,
            "guidance": video.guidance(),
        },
    }


def prompt_within_limit(text: str, limit: PromptLimit) -> bool:
    if not limit.max_chars or limit.max_chars <= 0:
        return True
    return len(str(text or "")) <= int(limit.max_chars)


def trim_prompt_to_limit(text: str, limit: PromptLimit) -> tuple[str, bool]:
    """Optional soft trim only when a known hard cap is exceeded.

    Prefer leaf self-limit; this is a last-resort safety for documented caps.
    Soft/unknown limits are never truncated here.
    """
    raw = str(text or "").strip()
    if not limit.known or limit.soft or not limit.max_chars:
        return raw, False
    if len(raw) <= limit.max_chars:
        return raw, False
    budget = max(1, int(limit.max_chars) - 1)
    cut = raw[:budget]
    if budget >= 40:
        for sep in (". ", "。", "! ", "? ", "\n"):
            idx = cut.rfind(sep)
            if idx >= budget // 2:
                cut = cut[: idx + len(sep)].rstrip()
                break
    return cut.strip(), True
