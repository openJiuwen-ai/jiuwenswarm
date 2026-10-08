# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Designer image / video generation on the shared generation backends.

Design reads the same Settings > Agent slots as the chat tools
(``VIDEO_GEN_*`` / ``VISUAL_GEN_*``, including ``*_PROTOCOL`` and the
``*_ENABLED`` switch) and calls ``gen_toolkits`` directly. Unlike the chat
tool, a clip waits for its video job: the submit / check_video pair is polled
here until the file is saved or the deadline passes.

Only the vendor-native backends are driven (MiniMax, ModelArk, DashScope,
vLLM-Omni): Design relies on reference images, which the OpenRouter path does
not take.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from jiuwenswarm.agents.harness.common.tools import gen_toolkits
from jiuwenswarm.agents.harness.common.tools.video_gen_tools import video_gen_enabled
from jiuwenswarm.agents.harness.common.tools.visual_gen_tools import visual_gen_enabled

logger = logging.getLogger(__name__)

Kind = Literal["image", "video"]

_ENV_PREFIX: dict[Kind, str] = {"image": "VISUAL_GEN", "video": "VIDEO_GEN"}
_SETTINGS_LABEL: dict[Kind, str] = {"image": "Image generation", "video": "Video generation"}
_IMAGE_RATIOS = ("1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9")
_VIDEO_RATIOS = ("16:9", "21:9", "4:3", "1:1", "3:4", "9:16")
_VIDEO_POLL_SECONDS = 15.0
_VIDEO_TIMEOUT_SECONDS = 1800.0
# The agent tool budget stays under the node cap so a cloud job can still be
# cancelled with a little time left on the node.
_VIDEO_TOOL_TIMEOUT_SECONDS = 1500.0
# A 50-step MiniMax-H3 render on a self-deployed server can take far longer.
_VLLM_OMNI_VIDEO_TIMEOUT_SECONDS = 7200.0
_SAVED_TO = re.compile(r"^Saved to: (.+)$", re.MULTILINE)
_PENDING_JOB = re.compile(r"^Video job (\S+) (?:submitted and still|is still) ")


@dataclass(frozen=True)
class SlotSettings:
    """One generation slot as saved in Settings > Agent."""

    api_key: str
    api_base: str
    model: str
    backend: str | None

    @property
    def complete(self) -> bool:
        if self.backend in gen_toolkits.KEYLESS_BACKENDS:
            return bool(self.api_base)
        return bool(self.api_key and self.api_base and self.model)


def slot_settings(kind: Kind) -> SlotSettings:
    prefix = _ENV_PREFIX[kind]
    api_base = os.environ.get(f"{prefix}_API_BASE", "").strip()
    return SlotSettings(
        api_key=os.environ.get(f"{prefix}_API_KEY", "").strip(),
        api_base=api_base,
        model=os.environ.get(f"{prefix}_MODEL_NAME", "").strip(),
        backend=gen_toolkits.detect_backend(f"{prefix}_PROTOCOL", api_base),
    )


def configured_model(kind: Kind) -> str:
    """User-configured model only; never a fallback model id."""
    return slot_settings(kind).model


def video_tool_timeout_seconds() -> float:
    """How long one clip video call may run.

    vLLM-Omni shares the long poll budget. Other backends keep the shorter
    tool budget that sits inside the 1800s node cap.
    """
    if slot_settings("video").backend == gen_toolkits.VLLM_OMNI:
        return _VLLM_OMNI_VIDEO_TIMEOUT_SECONDS
    return _VIDEO_TOOL_TIMEOUT_SECONDS


def generation_enabled(kind: Kind) -> bool:
    return visual_gen_enabled() if kind == "image" else video_gen_enabled()


def generation_problem(kind: Kind) -> str | None:
    """Why Design cannot generate this media kind, or None when it can."""
    label = _SETTINGS_LABEL[kind]
    if not generation_enabled(kind):
        return f"{label} is switched off in Settings > Agent."
    settings = slot_settings(kind)
    if not settings.complete:
        return f"{label} is not configured: set the API URL, API key and model in Settings > Agent."
    if settings.backend is None:
        return (
            f"{label} for Design needs a MiniMax, BytePlus ModelArk / Volcengine, Alibaba DashScope or "
            f"vLLM-Omni endpoint; {settings.api_base} is not one of them. Change it in Settings > Agent."
        )
    return None


def _local_path(value: str) -> Path | None:
    if value.startswith("file:"):
        local = unquote(urlparse(value).path)
        if len(local) >= 3 and local[0] == "/" and local[2] == ":":
            local = local[1:]
        candidate = Path(local)
    else:
        candidate = Path(value).expanduser()
    return candidate.resolve() if candidate.is_file() else None


def image_uri(path_or_url: str | None) -> str | None:
    """An http(s) URL / data: URI as-is, a readable local image as a data: URI, else None."""
    value = str(path_or_url or "").strip()
    if not value:
        return None
    if value.startswith(("http://", "https://", "data:")):
        return value
    local = _local_path(value)
    if local is None:
        return None
    mime, _ = mimetypes.guess_type(str(local))
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    return f"data:{mime};base64,{base64.b64encode(local.read_bytes()).decode('ascii')}"


def _reference_uris(paths: list[str] | None) -> tuple[tuple[str, ...], str | None]:
    """(data URIs / URLs, error) for the readable references."""
    uris = [uri for uri in (image_uri(item) for item in paths or []) if uri]
    if paths and not uris:
        return (), "[ERROR]: reference images were provided but none could be read as local files or URLs."
    if len(uris) < len(paths or []):
        logger.warning("Designer generation skipped %d unreadable reference(s)", len(paths or []) - len(uris))
    return tuple(dict.fromkeys(uris)), None


def _target(kind: Kind, model_override: str | None = None) -> gen_toolkits.GenerationTarget | str:
    problem = generation_problem(kind)
    if problem:
        return f"[ERROR]: {problem}"
    settings = slot_settings(kind)
    model = (model_override or "").strip() or settings.model
    return gen_toolkits.GenerationTarget(settings.backend or "", settings.api_key, settings.api_base, model)


def _saved_path(result: str) -> str | None:
    match = _SAVED_TO.search(result)
    return match.group(1).split(", ")[0].strip() if match else None


async def generate_image(
    prompt: str,
    *,
    size: str | None = None,
    reference_images: list[str] | None = None,
    save_dir: str | None = None,
) -> dict[str, str]:
    """Generate one still; returns ``{"image_path": ...}`` or ``{"error": ...}``."""
    target = _target("image")
    if isinstance(target, str):
        return {"error": target}
    refs, error = _reference_uris(reference_images)
    if error:
        return {"error": error}
    aspect_ratio = gen_toolkits.aspect_ratio_for_size(size, _IMAGE_RATIOS, "1:1")
    options = gen_toolkits.ImageOptions(size=size, reference_image_uris=refs)
    logger.info(
        "Designer image generation backend=%s model=%s size=%s references=%d",
        target.backend, target.model, size, len(refs),
    )
    result = await gen_toolkits.generate_image(target, prompt, aspect_ratio, save_dir, options)
    path = None if result.startswith("[ERROR]") else _saved_path(result)
    return {"image_path": path} if path else {"error": result}


@dataclass(frozen=True)
class DesignerVideoRequest:
    """One clip for ``generate_video``; paths may be local files or URLs."""

    prompt: str
    duration: int = 5
    size: str | None = None
    resolution: str | None = None
    first_frame: str | None = None
    reference_images: tuple[str, ...] = ()
    reference_file: str | None = None
    audio: bool = False
    reference_mode: bool = False
    model: str | None = None


async def generate_video(request: DesignerVideoRequest, *, save_dir: str | None = None) -> dict[str, str]:
    """Generate one clip and wait for it; returns ``{"video_path": ...}`` or ``{"error": ...}``."""
    target = _target("video", request.model)
    if isinstance(target, str):
        return {"error": target}
    refs, error = _reference_uris(list(request.reference_images))
    if error:
        return {"error": error}
    first_frame = image_uri(request.first_frame)
    if request.first_frame and not first_frame:
        return {"error": f"[ERROR]: first frame {request.first_frame!r} is not a readable image file or URL."}
    video_request = gen_toolkits.VideoRequest(
        prompt=request.prompt,
        aspect_ratio=gen_toolkits.aspect_ratio_for_size(request.size, _VIDEO_RATIOS, "16:9"),
        resolution=(request.resolution or "720p").strip().lower(),
        duration_seconds=int(request.duration),
        generate_audio=bool(request.audio),
        first_frame_data_uri=first_frame,
        size=request.size,
        reference_image_uris=refs,
        reference_mode=request.reference_mode,
        reference_file_path=request.reference_file,
    )
    logger.info(
        "Designer video generation backend=%s model=%s size=%s resolution=%s first_frame=%s references=%d "
        "reference_mode=%s file=%s",
        target.backend, target.model, request.size, video_request.resolution, bool(first_frame), len(refs),
        request.reference_mode, bool(request.reference_file),
    )
    result = await gen_toolkits.submit_video(target, video_request, save_dir)
    timeout = (
        _VLLM_OMNI_VIDEO_TIMEOUT_SECONDS if target.backend == gen_toolkits.VLLM_OMNI else _VIDEO_TIMEOUT_SECONDS
    )
    deadline = time.monotonic() + timeout
    while (pending := _PENDING_JOB.match(result)) and time.monotonic() < deadline:
        await asyncio.sleep(_VIDEO_POLL_SECONDS)
        result = await gen_toolkits.check_video(target, pending.group(1), save_dir)
    if pending:
        return {"error": f"[ERROR]: video job {pending.group(1)} did not finish within {int(timeout)}s."}
    path = None if result.startswith("[ERROR]") else _saved_path(result)
    return {"video_path": path} if path else {"error": result}


def vllm_omni_endpoint(kind: Kind, api_base: str | None, model: str | None) -> tuple[str, str, str]:
    """(api_key, api_base, model) for a ComfyUI node pinned to vLLM-Omni.

    The node's own URL / model win; the Settings slot fills the gaps only when it
    is itself a vLLM-Omni endpoint, so another vendor's key never reaches the node's server.
    """
    settings = slot_settings(kind)
    if settings.backend != gen_toolkits.VLLM_OMNI:
        settings = SlotSettings("", "", "", None)
    return (
        settings.api_key,
        (api_base or "").strip() or settings.api_base,
        (model or "").strip() or settings.model,
    )
