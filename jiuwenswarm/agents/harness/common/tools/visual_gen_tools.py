# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""
Text-to-image generation tool via an OpenRouter-style chat-completions image
modality (e.g. google/gemini-3.1-flash-image).

Unlike video generation (video_gen_tools.py), this call is synchronous - the
model returns the generated image(s) directly in the chat-completions
response (as data: URIs or remote URLs), with no submit-then-poll job to
track.

Credentials come from a dedicated "Visual processing" config panel slot
(VISUAL_GEN_API_KEY/VISUAL_GEN_API_BASE/VISUAL_GEN_MODEL_NAME/
VISUAL_GEN_PROVIDER/VISUAL_GEN_PROTOCOL) - independent of both:
- visual_question_answering's VISION_* slot (image understanding, a
  different capability), and
- image_tools.py's generate_image (IMAGE_GEN_* slot), whose implementation
  is hard-locked to a DashScope-only code path (see
  OpenAIModelClient._require_dashscope_media_profile in openjiuwen) and
  cannot serve an OpenRouter/Gemini-style model no matter what provider or
  key is configured there.

There is no built-in default endpoint/model: whatever's configured in the
dedicated slot above is what this tool uses, and api_key/api_base/model
must all be set.
"""
from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.agents.harness.common.tools.ssl_config import get_requests_verify
from jiuwenswarm.common.utils import get_agent_workspace_dir

logger = logging.getLogger(__name__)


def visual_gen_enabled() -> bool:
    """Whether the "Visual processing" switch in configuration settings is on.

    Mirrors video_gen_tools.video_gen_enabled's env-var gate: VISUAL_GEN_ENABLED
    is set from that switch and checked independently of whether the Visual
    processing config itself is complete, so a user can leave valid
    credentials in place while still turning generation off.
    """
    return str(os.environ.get("VISUAL_GEN_ENABLED", "")).strip().lower() in ("1", "true", "yes", "on")


def _get_visual_gen_api_credentials() -> tuple[str, str, str]:
    """Resolve image-generation credentials from the dedicated "Visual
    processing" config panel slot only (VISUAL_GEN_API_KEY/VISUAL_GEN_API_BASE/
    VISUAL_GEN_MODEL_NAME). No fallback env vars, no built-in default endpoint
    or model - an empty string means unconfigured.
    """
    api_key = os.environ.get("VISUAL_GEN_API_KEY", "").strip()
    api_base = os.environ.get("VISUAL_GEN_API_BASE", "").strip()
    model = os.environ.get("VISUAL_GEN_MODEL_NAME", "").strip()
    return api_key, api_base, model


def visual_gen_configured() -> bool:
    """Whether the Visual processing config (key/base/model) is complete.

    Public entry point for callers outside this module (interface_deep.py,
    swarm/providers/tools.py) that only need a yes/no gate and shouldn't
    depend on _get_visual_gen_api_credentials' private tuple shape.
    """
    api_key, api_base, model = _get_visual_gen_api_credentials()
    return bool(api_key and api_base and model)


def _resolve_save_path(save_dir: str | None, filename: str) -> Path:
    root = Path(save_dir).expanduser() if save_dir else (get_agent_workspace_dir() / "generated_images")
    root.mkdir(parents=True, exist_ok=True)
    return root / filename


def _extension_for_mime(mime: str) -> str:
    if "png" in mime:
        return "png"
    if "webp" in mime:
        return "webp"
    if "gif" in mime:
        return "gif"
    return "jpg"


@tool(
    name="generate_visual",
    description=(
        "Generate an image from a text prompt using AI image generation models. "
        "Use this tool when the user wants to create or generate an image based "
        "on a text description. Returns the path to the saved generated image file."
    ),
)
async def generate_visual(
    prompt: str,
    aspect_ratio: str = "16:9",
    resolution: str = "512",
    save_dir: str | None = None,
) -> str:
    """
    Generate an image from a text prompt.

    Args:
        prompt: Text description of the image to generate.
        aspect_ratio: e.g. "16:9", "9:16", "1:1".
        resolution: e.g. "512", "1024" (short-edge pixel size).
        save_dir: Optional directory to save the image (defaults to the agent
            workspace's generated_images/ folder).

    Returns:
        Path to the generated image file, or an error message.
    """
    api_key, api_base, model = _get_visual_gen_api_credentials()
    if not (api_key and api_base and model):
        return (
            "[ERROR]: image generation is not configured - set the Visual processing "
            "API key, API URL, and model name in configuration settings."
        )
    prompt = (prompt or "").strip()
    if not prompt:
        return "[ERROR]: prompt is required."

    # Not every provider/model honors aspect_ratio/resolution as separate
    # request-body fields, so the hint is also folded into the prompt text
    # itself as a best-effort fallback the model can act on directly.
    full_prompt = f"{prompt}\n\n(Aspect ratio: {aspect_ratio}, resolution: {resolution}px)"
    body: dict[str, Any] = {
        "model": model,
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": full_prompt}],
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    logger.info(
        "[generate_visual] using model: %s (api_base: %s, aspect_ratio: %s, resolution: %s)",
        model, api_base, aspect_ratio, resolution,
    )

    try:
        async with httpx.AsyncClient(timeout=120, verify=get_requests_verify()) as client:
            resp = await client.post(f"{api_base}/chat/completions", headers=headers, json=body)
            if resp.status_code != 200:
                return f"[ERROR]: image generation request failed: {resp.status_code} {resp.text}"
            try:
                data = resp.json()
            except ValueError as exc:
                return f"[ERROR]: image generation request returned invalid JSON: {exc!r}"
    except httpx.HTTPError as exc:
        return f"[ERROR]: image generation request failed: {exc!r}"

    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return f"[ERROR]: unexpected response shape from provider: {data}"

    images = message.get("images") or []
    if not images:
        content = message.get("content", "")
        return f"[ERROR]: no images returned. Model response: {content}"

    saved_paths: list[str] = []
    for index, image in enumerate(images):
        url_field = ((image.get("image_url") or {}).get("url")) or image.get("url") or ""
        if not url_field.startswith("data:"):
            saved_paths.append(url_field)  # already a remote URL, nothing to download
            continue
        header, _, b64data = url_field.partition(",")
        mime = header[len("data:"):].split(";")[0] if header.startswith("data:") else "image/png"
        ext = _extension_for_mime(mime)
        filename = f"image_{int(time.time())}_{index}.{ext}"
        try:
            target = _resolve_save_path(save_dir, filename)
            target.write_bytes(base64.b64decode(b64data))
        except OSError as exc:
            return f"[ERROR]: failed to save image ({filename}) to {save_dir or 'agent workspace'}: {exc!r}"
        saved_paths.append(str(target))

    return "Image generated successfully!\nSaved to: " + ", ".join(saved_paths)
