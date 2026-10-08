# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Vendor-native image / video generation backends (MiniMax, BytePlus ModelArk,
Alibaba DashScope, self-deployed vLLM-Omni).

None of these generation APIs is OpenAI/OpenRouter-compatible, so the
OpenRouter-style request path in visual_gen_tools / video_gen_tools cannot drive
them. Those tools call the four entry points below, which dispatch on the
backend name returned by ``detect_backend``:

- ``detect_backend(protocol_env, api_base)`` -> ``"minimax"`` / ``"modelark"`` /
  ``"dashscope"`` / ``"vllm-omni"`` / None
- ``generate_image(backend, ...)``  (synchronous)
- ``submit_video(backend, ...)``    (submit, then poll for a while)
- ``check_video(backend, ...)``     (poll a submitted job, download when ready)

MiniMax (https://platform.minimax.io/docs/api-reference/image-generation-t2i and
.../video-generation-v2-create):

- image-01: ``POST {root}/v1/image_generation`` returning ``data.image_base64``
  plus a ``base_resp`` status.
- MiniMax-H3: ``POST {root}/v2/video_generation`` with a ``content`` array
  (returns a ``task_id``), then ``GET {root}/v2/query/video_generation/{id}``
  until ``task.status`` is ``succeeded`` (``task.content.url``) or ``failed``.
- The configured "API URL" is the OpenAI-style base (``https://api.minimax.io/v1``
  global, ``https://api.minimaxi.com/v1`` China); the v2 endpoints live at the host
  root, so the trailing ``/v1`` is stripped. Keys are region-bound: a global key is
  rejected by the China host and vice versa.

ModelArk (Volcengine Ark / BytePlus):

- Seedream: ``POST {base}/images/generations`` with
  ``{model, prompt, size, response_format, watermark}`` returning
  ``data[].b64_json`` / ``data[].url``.
- Seedance: ``POST {base}/contents/generations/tasks`` with a ``content`` array plus
  ``resolution`` / ``ratio`` / ``duration`` / ``generate_audio`` / ``watermark``
  (returns ``id``), then ``GET .../tasks/{id}`` until ``status`` is ``succeeded``
  (``content.video_url``) or a failure status (``error``).
- ``{base}`` is e.g. ``https://ark.ap-southeast.bytepluses.com/api/v3``. Keys and
  model activation are region-bound, so a key rejected on the configured host is
  retried on the other known hosts. A model the account has not activated comes back
  as ``ModelNotOpen``; that is surfaced with the console step needed to fix it.

DashScope (Alibaba Model Studio, Wan video / Qwen-Image):

- Qwen-Image: ``POST {base}/services/aigc/multimodal-generation/generation`` with a
  ``messages`` input (reference images then text) returning
  ``output.choices[].message.content[].image`` URLs.
- Wan: ``POST {base}/services/aigc/video-generation/video-synthesis`` with
  ``X-DashScope-Async: enable`` (returns ``output.task_id``), then
  ``GET {base}/tasks/{id}`` until ``output.task_status`` is ``SUCCEEDED``
  (``output.video_url``). Wan 3.0 takes a ``media`` list (``reference_image`` /
  ``file``); Wan 2.x takes ``img_url`` or ``reference_urls``.
- ``{base}`` is ``https://dashscope.aliyuncs.com/api/v1`` (China) or
  ``https://dashscope-intl.aliyuncs.com/api/v1``; the OpenAI ``compatible-mode``
  URL a chat preset carries is mapped onto it. Keys are region-bound, so a key
  rejected on one region is retried on the other.

vLLM-Omni (self-deployed, see vllm_omni_gen): ``POST {base}/videos`` then
``GET {base}/videos/{id}`` and ``.../content``; images via
``/images/generations`` or ``/images/edits``. The API key and model name are
optional (the served model is discovered via ``GET {base}/models``).

Video references (``VideoRequest.reference_image_uris``, reference mode) are
supported by every backend here; the OpenRouter path has first-frame only.

Return strings follow the same conventions as the OpenRouter path
("[ERROR]: ...", "Saved to: ...", "Video job {id} submitted and still ...")
so callers such as the chat agent and check_video_status need no changes.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx
import requests

from jiuwenswarm.agents.harness.common.tools import vllm_omni_gen
from jiuwenswarm.agents.harness.common.tools.ssl_config import get_requests_verify
from jiuwenswarm.common.utils import get_agent_workspace_dir

logger = logging.getLogger(__name__)

MINIMAX = "minimax"
MODELARK = "modelark"
DASHSCOPE = "dashscope"
VLLM_OMNI = "vllm-omni"
NATIVE_BACKENDS = (MINIMAX, MODELARK, DASHSCOPE, VLLM_OMNI)
# Backends that also serve self-deployed endpoints: no API key or model name required.
KEYLESS_BACKENDS = (VLLM_OMNI,)

_POLL_INTERVAL_SECONDS = 10
_PENDING_STATUSES = ("queued", "running")

_MINIMAX_IMAGE_ASPECT_RATIOS = {"1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9"}
_MINIMAX_VIDEO_RATIOS = {"16:9", "21:9", "4:3", "1:1", "3:4", "9:16"}
_MINIMAX_MAX_POLL_SECONDS = 120
_MINIMAX_IMAGE_PROMPT_LIMIT = 1500
_MINIMAX_VIDEO_PROMPT_LIMIT = 7000
_MINIMAX_HOST = re.compile(r"(^|\.)minimax(i)?\.(io|com)$", re.IGNORECASE)

# MiniMax keys are bound to one region: a global key (platform.minimax.io) is
# rejected by the China host with "invalid api key (2049)" and vice versa. The
# built-in provider preset points at the China host while most keys are global,
# so a misconfigured region is the common failure - try the other region's host
# before reporting the key as bad.
_MINIMAX_REGION_ROOTS = ("https://api.minimax.io", "https://api.minimaxi.com")
_MINIMAX_AUTH_STATUS_CODES = (1004, 2049)

_MODELARK_KNOWN_BASES = (
    "https://ark.ap-southeast.bytepluses.com/api/v3",
    "https://ark.eu-west.bytepluses.com/api/v3",
    "https://ark.cn-beijing.volces.com/api/v3",
)
_MODELARK_HOST = re.compile(r"^ark\.[\w-]+\.(bytepluses\.com|volces\.com)$", re.IGNORECASE)
_MODELARK_VIDEO_RATIOS = {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9"}
_MODELARK_VIDEO_RESOLUTIONS = {"480p", "720p", "1080p"}
# A 5 s Seedance clip takes ~2.5 min to render; wait long enough that one call usually finishes.
_MODELARK_MAX_POLL_SECONDS = 300
_MODELARK_IMAGE_AREA = 2048 * 2048
_MODELARK_IMAGE_MAX_SIDE = 4096
_MODELARK_IMAGE_SIDE_STEP = 8
_MODELARK_MAX_REFERENCE_IMAGES = 14

_DASHSCOPE_HOST = re.compile(r"^dashscope(-intl)?\.aliyuncs\.com$", re.IGNORECASE)
_DASHSCOPE_CN_BASE = "https://dashscope.aliyuncs.com/api/v1"
_DASHSCOPE_INTL_BASE = "https://dashscope-intl.aliyuncs.com/api/v1"
_DASHSCOPE_REGION_BASES = (_DASHSCOPE_CN_BASE, _DASHSCOPE_INTL_BASE)
_DASHSCOPE_AUTH_CODES = ("InvalidApiKey",)
_DASHSCOPE_MAX_POLL_SECONDS = 300
_DASHSCOPE_MAX_IMAGE_REFERENCES = 3
_DASHSCOPE_MAX_WAN2_REFERENCES = 5
# Output sizes per resolution tier and aspect ratio (Wan's documented sizes), for
# backends that take explicit dimensions.
_VIDEO_SIZES = {
    "480p": {"16:9": "832*480", "9:16": "480*832", "1:1": "624*624"},
    "720p": {"16:9": "1280*720", "9:16": "720*1280", "1:1": "960*960", "4:3": "1088*832", "3:4": "832*1088"},
    "1080p": {
        "16:9": "1920*1080", "9:16": "1080*1920", "1:1": "1440*1440", "4:3": "1632*1248", "3:4": "1248*1632",
    },
}
_IMAGE_SIZES = {
    "1:1": "1328*1328", "16:9": "1664*928", "9:16": "928*1664", "4:3": "1472*1140", "3:4": "1140*1472",
}
# The SDK's OSS upload (the one SDK call made here) reads the module-global base URL.
_DASHSCOPE_SDK_LOCK = threading.Lock()

_VLLM_OMNI_MAX_POLL_SECONDS = 300

# Account-side failures: nothing the agent can change by retrying, switching
# models or searching config files for another key - it should tell the user.
_MODELARK_ACCOUNT_HINTS = {
    "ModelNotOpen": (
        "Activate this model for your account in the ModelArk console "
        "(https://console.byteplus.com/ark, Model activation) and try again."
    ),
    "SetLimitExceeded": (
        "The account's usage limit for this model has been reached (Safe Experience Mode / Free Credits Only "
        "Mode pauses the model once the free quota runs out). On the Model Activation page of the ModelArk "
        "console (https://console.byteplus.com/ark) adjust or close \"Safe Experience Mode\" (or add credits) "
        "and try again."
    ),
}


# --------------------------------------------------------------------------- #
# Backend detection and public entry points
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class GenerationTarget:
    """The configured backend, credentials and model a generation call is sent to."""

    backend: str
    api_key: str
    api_base: str
    model: str


@dataclass(frozen=True)
class VideoRequest:
    """What to render: the prompt plus the video options the tools expose.

    ``reference_image_uris`` are http(s) URLs or data: URIs of stills the clip must
    stay consistent with (reference-to-video). ``reference_mode`` also turns a first
    frame into a reference instead of the literal opening frame. ``size`` ("W*H" or
    "WxH") is honoured by DashScope and vLLM-Omni; the other backends use
    ``aspect_ratio`` / ``resolution``. ``reference_file_path`` is a local document
    (e.g. a storyboard) that Wan 3.0 takes as a ``file`` reference. ``extra`` holds
    vLLM-Omni request fields (see ``vllm_omni_gen.submit_vllm_omni_video_sync``).
    """

    prompt: str
    aspect_ratio: str
    resolution: str
    duration_seconds: int
    generate_audio: bool = False
    first_frame_data_uri: str | None = None
    size: str | None = None
    reference_image_uris: tuple[str, ...] = ()
    reference_mode: bool = False
    reference_file_path: str | None = None
    extra: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ImageOptions:
    """Optional image inputs beyond prompt and aspect ratio.

    ``size`` ("W*H" / "WxH" / "1K"/"2K") overrides the aspect-ratio default where the
    backend takes explicit dimensions. ``reference_image_uris`` (http(s) URLs or data:
    URIs) are honoured by ModelArk, DashScope and vLLM-Omni and ignored by MiniMax.
    ``extra`` holds vLLM-Omni request fields.
    """

    size: str | None = None
    reference_image_uris: tuple[str, ...] = ()
    extra: Mapping[str, Any] | None = None


def _host_of(api_base: str) -> str:
    return re.sub(r"^https?://", "", api_base.strip(), flags=re.IGNORECASE).split("/", 1)[0].split(":", 1)[0]


def detect_backend(protocol_env: str, api_base: str) -> str | None:
    """Return the vendor-native backend for the configured slot, or None.

    None means the OpenRouter-style path. The backend is named by the saved 协议
    (read from ``protocol_env``), or implied by the API URL being one of the
    vendor's hosts.
    """
    protocol = os.environ.get(protocol_env, "").strip().lower()
    if protocol in NATIVE_BACKENDS:
        return protocol
    host = _host_of(api_base)
    if _MINIMAX_HOST.search(host):
        return MINIMAX
    if _MODELARK_HOST.match(host):
        return MODELARK
    if _DASHSCOPE_HOST.match(host):
        return DASHSCOPE
    return None


async def generate_image(
    target: GenerationTarget,
    prompt: str,
    aspect_ratio: str,
    save_dir: str | None,
    options: ImageOptions | None = None,
) -> str:
    opts = options or ImageOptions()
    if target.backend == MINIMAX:
        if opts.reference_image_uris:
            logger.warning("[generate_visual] MiniMax image-01 ignores %d reference image(s)",
                           len(opts.reference_image_uris))
        return await _minimax_generate_image(target, prompt, aspect_ratio, save_dir)
    if target.backend == MODELARK:
        return await _modelark_generate_image(target, prompt, aspect_ratio, save_dir, opts)
    if target.backend == DASHSCOPE:
        return await _dashscope_generate_image(target, prompt, aspect_ratio, save_dir, opts)
    if target.backend == VLLM_OMNI:
        return await _vllm_omni_generate_image(target, prompt, aspect_ratio, save_dir, opts)
    return f"[ERROR]: unknown generation backend: {target.backend!r}"


async def submit_video(target: GenerationTarget, request: VideoRequest, save_dir: str | None) -> str:
    if request.extra and target.backend != VLLM_OMNI:
        return "[ERROR]: extra video request options are only supported by vLLM-Omni."
    if target.backend == MINIMAX:  # H3 has no audio switch
        return await _minimax_submit_video(target, request, save_dir)
    if target.backend == MODELARK:
        return await _modelark_submit_video(target, request, save_dir)
    if target.backend == DASHSCOPE:
        return await _dashscope_submit_video(target, request, save_dir)
    if target.backend == VLLM_OMNI:
        return await _vllm_omni_submit_video(target, request, save_dir)
    return f"[ERROR]: unknown generation backend: {target.backend!r}"


async def check_video(target: GenerationTarget, task_id: str, save_dir: str | None) -> str:
    if target.backend == MINIMAX:
        return await _minimax_check_video(target, task_id, save_dir)
    if target.backend == MODELARK:
        return await _modelark_check_video(target, task_id, save_dir)
    if target.backend == DASHSCOPE:
        return await _dashscope_check_video(target, task_id, save_dir)
    if target.backend == VLLM_OMNI:
        return await _vllm_omni_check_video(target, task_id, save_dir)
    return f"[ERROR]: unknown generation backend: {target.backend!r}"


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _save_root(save_dir: str | None, default_sub: str) -> Path:
    root = Path(save_dir).expanduser() if save_dir else (get_agent_workspace_dir() / default_sub)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _save_path(save_dir: str | None, default_sub: str, filename: str) -> Path:
    return _save_root(save_dir, default_sub) / filename


def _image_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def _image_filename(index: int, data: bytes) -> str:
    return f"image_{int(time.time())}_{index}_{secrets.token_hex(4)}.{_image_extension(data)}"


def _snap_request_duration(model: str, duration_seconds: int | float | None) -> int:
    """Clamp duration to the catalog for ``model`` (same table as resolution)."""
    from jiuwenswarm.server.runtime.designer.pipeline.model_capacity import (
        capacity_for_model,
        snap_duration,
    )

    return snap_duration(duration_seconds, capacity_for_model(model))


def parse_size(size: str | None) -> tuple[int, int] | None:
    """(width, height) of a "W*H" / "WxH" size, or None."""
    match = re.fullmatch(r"\s*(\d+)\s*[x*]\s*(\d+)\s*", str(size or ""), flags=re.IGNORECASE)
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    return (width, height) if width > 0 and height > 0 else None


def aspect_ratio_for_size(size: str | None, allowed: tuple[str, ...], default: str) -> str:
    """The ratio in ``allowed`` ("W:H") closest to ``size``'s, or ``default``."""
    parsed = parse_size(size)
    if parsed is None:
        return default
    target = parsed[0] / parsed[1]

    def error(ratio: str) -> float:
        width, height = (int(part) for part in ratio.split(":", 1))
        return abs(target - width / height)

    return min(allowed, key=error)


def _video_references(request: VideoRequest) -> tuple[str | None, list[str]]:
    """(first frame, reference stills) after folding the frame in for reference mode."""
    refs = list(dict.fromkeys(uri for uri in request.reference_image_uris if uri))
    frame = request.first_frame_data_uri
    if frame and (request.reference_mode or frame in refs):
        if frame not in refs:
            refs.append(frame)
        frame = None
    return frame, refs


def _video_content(prompt: str, frame: str | None, refs: list[str]) -> list[dict[str, Any]]:
    """MiniMax / Seedance ``content``: text, optional first frame, then reference stills."""
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if frame:
        content.append({"type": "image_url", "image_url": {"url": frame}, "role": "first_frame"})
    content.extend({"type": "image_url", "image_url": {"url": url}, "role": "reference_image"} for url in refs)
    return content


def _pending_submit_message(task_id: str, status: str, elapsed: int, wait_hint: str = "") -> str:
    return (
        f"Video job {task_id} submitted and still {status} after {elapsed}s - generation can "
        f"take several minutes. Call check_video_status with job_id={task_id} to check progress "
        f"and download it once ready{wait_hint}."
    )


async def _download_video(
    client: httpx.AsyncClient, task_id: str, url: str, save_dir: str | None, expiry_note: str
) -> str:
    if not url:
        return f"[ERROR]: video job {task_id} succeeded but returned no video URL."
    try:
        # The download URL is pre-signed - it must not carry the API key.
        content = await client.get(url, follow_redirects=True, timeout=300)
    except httpx.HTTPError as exc:
        return f"[ERROR]: downloading video job {task_id} failed: {exc!r}"
    if content.status_code != 200:
        return f"[ERROR]: video job {task_id} completed but downloading content failed: {content.status_code}"
    try:
        target = _save_path(save_dir, "generated_videos", f"video_{task_id}.mp4")
        target.write_bytes(content.content)
    except OSError as exc:
        return f"[ERROR]: failed to save video for job {task_id}: {exc!r}"
    return (
        "Video generated successfully!\n"
        f"Saved to: {target}\n"
        f"(job {task_id} - {expiry_note}, so this local file is the durable copy.)"
    )


# --------------------------------------------------------------------------- #
# MiniMax
# --------------------------------------------------------------------------- #

def _minimax_root(api_base: str) -> str:
    return re.sub(r"/v[0-9]+/?$", "", api_base.strip().rstrip("/"))


def _minimax_candidate_roots(api_base: str) -> list[str]:
    primary = _minimax_root(api_base)
    if primary not in _MINIMAX_REGION_ROOTS:
        return [primary]
    return [primary, *[root for root in _MINIMAX_REGION_ROOTS if root != primary]]


def _minimax_is_auth_failure(http_status: int, payload: Any) -> bool:
    if http_status == 401:
        return True
    if not isinstance(payload, dict):
        return False
    base = payload.get("base_resp")
    if isinstance(base, dict) and base.get("status_code") in _MINIMAX_AUTH_STATUS_CODES:
        return True
    err = payload.get("error")
    return isinstance(err, dict) and (err.get("type") == "authorized_error" or str(err.get("http_code")) == "401")


def _minimax_auth_failure_message(roots: list[str], detail: str) -> str:
    hosts = " and ".join(re.sub(r"^https?://", "", root) for root in roots)
    return (
        f"[ERROR]: MiniMax rejected the API key on {hosts} ({detail}). The key is wrong, expired or revoked - "
        "update it in Settings > Agent (Image/Video generation). Do not search other config files for a different key."
    )


def _minimax_base_error(payload: Any) -> str | None:
    """Return a readable error for a MiniMax response, or None on success.

    MiniMax reports failures in ``base_resp`` (HTTP 200) or in an ``error`` object.
    """
    if not isinstance(payload, dict):
        return f"unexpected response: {payload!r}"
    base = payload.get("base_resp")
    if isinstance(base, dict) and base.get("status_code") not in (0, None):
        return f"{base.get('status_code')} {base.get('status_msg', '')}".strip()
    err = payload.get("error")
    if isinstance(err, dict):
        return f"{err.get('type', 'error')}: {err.get('message', '')}".strip()
    return None


async def _minimax_generate_image(
    target: GenerationTarget, prompt: str, aspect_ratio: str, save_dir: str | None
) -> str:
    api_key, api_base, model = target.api_key, target.api_base, target.model
    aspect = aspect_ratio if aspect_ratio in _MINIMAX_IMAGE_ASPECT_RATIOS else "1:1"
    body = {
        "model": model,
        "prompt": prompt[:_MINIMAX_IMAGE_PROMPT_LIMIT],
        "aspect_ratio": aspect,
        "response_format": "base64",
        "n": 1,
    }
    roots = _minimax_candidate_roots(api_base)
    payload: Any = None
    try:
        async with httpx.AsyncClient(timeout=120, verify=get_requests_verify()) as client:
            for index, root in enumerate(roots):
                logger.info("[generate_visual] MiniMax model: %s (root: %s, aspect_ratio: %s)", model, root, aspect)
                resp = await client.post(
                    f"{root}/v1/image_generation", headers={"Authorization": f"Bearer {api_key}"}, json=body
                )
                try:
                    payload = resp.json()
                except ValueError:
                    return (
                        "[ERROR]: MiniMax image generation returned a non-JSON response: "
                        f"{resp.status_code} {resp.text[:300]}"
                    )
                if _minimax_is_auth_failure(resp.status_code, payload):
                    if index + 1 < len(roots):
                        logger.warning(
                            "[generate_visual] MiniMax rejected the key on %s, trying %s", root, roots[index + 1]
                        )
                        continue
                    return _minimax_auth_failure_message(roots, _minimax_base_error(payload) or str(resp.status_code))
                break
    except httpx.HTTPError as exc:
        return f"[ERROR]: MiniMax image generation request failed: {exc!r}"
    if resp.status_code != 200 or _minimax_base_error(payload):
        return f"[ERROR]: MiniMax image generation failed: {_minimax_base_error(payload) or resp.status_code}"

    images = ((payload.get("data") or {}).get("image_base64")) or []
    if not images:
        return f"[ERROR]: MiniMax returned no images. Response: {str(payload)[:300]}"
    saved: list[str] = []
    for index, encoded in enumerate(images):
        try:
            data = base64.b64decode(encoded)
            dest = _save_path(save_dir, "generated_images", _image_filename(index, data))
            dest.write_bytes(data)
        except (OSError, ValueError) as exc:
            return f"[ERROR]: failed to save MiniMax image: {exc!r}"
        saved.append(str(dest))
    return "Image generated successfully!\nSaved to: " + ", ".join(saved)


def _minimax_video_resolution(resolution: str) -> str:
    """Map the tool's resolution ("480p"/"720p"/"1080p") to H3's "768P" / "2K"."""
    text = (resolution or "").strip().upper()
    if text == "2K":
        return "2K"
    digits = re.sub(r"\D", "", text)
    return "768P" if not digits or int(digits) <= 768 else "2K"


async def _minimax_submit_video(target: GenerationTarget, request: VideoRequest, save_dir: str | None) -> str:
    api_key, api_base, model = target.api_key, target.api_base, target.model
    prompt, aspect_ratio, resolution = request.prompt, request.aspect_ratio, request.resolution
    duration_seconds = request.duration_seconds
    frame, refs = _video_references(request)
    content = _video_content(prompt[:_MINIMAX_VIDEO_PROMPT_LIMIT], frame, refs)
    body = {
        "model": model,
        "content": content,
        "resolution": _minimax_video_resolution(resolution),
        "duration": _snap_request_duration(model, duration_seconds),
        "ratio": aspect_ratio if aspect_ratio in _MINIMAX_VIDEO_RATIOS else "16:9",
    }
    roots = _minimax_candidate_roots(api_base)
    root = roots[0]
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=60, verify=get_requests_verify()) as client:
            for index, candidate in enumerate(roots):
                root = candidate
                logger.info(
                    "[generate_video] MiniMax model: %s (root: %s, body: %s)",
                    model, root, {k: v for k, v in body.items() if k != "content"},
                )
                submit = await client.post(f"{root}/v2/video_generation", headers=headers, json=body)
                try:
                    payload = submit.json()
                except ValueError:
                    return (
                        "[ERROR]: MiniMax video submit returned a non-JSON response: "
                        f"{submit.status_code} {submit.text[:300]}"
                    )
                if _minimax_is_auth_failure(submit.status_code, payload):
                    if index + 1 < len(roots):
                        logger.warning(
                            "[generate_video] MiniMax rejected the key on %s, trying %s", root, roots[index + 1]
                        )
                        continue
                    return _minimax_auth_failure_message(roots, _minimax_base_error(payload) or str(submit.status_code))
                break
            if submit.status_code not in (200, 201, 202) or _minimax_base_error(payload):
                detail = _minimax_base_error(payload) or submit.status_code
                return f"[ERROR]: MiniMax video generation submit failed: {detail}"
            task_id = str(payload.get("task_id") or ((payload.get("task") or {}).get("id")) or "")
            if not task_id:
                return f"[ERROR]: MiniMax video submit returned no task id: {str(payload)[:300]}"

            status, task, elapsed = "queued", {}, 0
            while status in _PENDING_STATUSES and elapsed < _MINIMAX_MAX_POLL_SECONDS:
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                elapsed += _POLL_INTERVAL_SECONDS
                status, task, error = await _minimax_query(client, root, headers, task_id)
                if status == "auth_failed":
                    return _minimax_auth_failure_message([root], error or "")
                if error:
                    return error
            if status in _PENDING_STATUSES:
                return _pending_submit_message(task_id, status, elapsed)
            return await _minimax_finish(client, task_id, status, task, save_dir)
    except httpx.HTTPError as exc:
        return f"[ERROR]: MiniMax video generation request failed: {exc!r}"


async def _minimax_query(
    client: httpx.AsyncClient, root: str, headers: dict[str, str], task_id: str
) -> tuple[str, dict[str, Any], str | None]:
    resp = await client.get(f"{root}/v2/query/video_generation/{task_id}", headers=headers)
    try:
        payload = resp.json()
    except ValueError:
        return "", {}, f"[ERROR]: polling MiniMax video job {task_id} returned a non-JSON response: {resp.status_code}"
    if _minimax_is_auth_failure(resp.status_code, payload):
        return "auth_failed", {}, _minimax_base_error(payload) or str(resp.status_code)
    if resp.status_code != 200 or _minimax_base_error(payload):
        detail = _minimax_base_error(payload) or resp.status_code
        return "", {}, f"[ERROR]: polling MiniMax video job {task_id} failed: {detail}"
    task = payload.get("task") or {}
    return str(task.get("status") or "").lower(), task, None


async def _minimax_finish(
    client: httpx.AsyncClient, task_id: str, status: str, task: dict[str, Any], save_dir: str | None
) -> str:
    if status != "succeeded":
        error = task.get("error") or {}
        if isinstance(error, dict):
            detail = f"{error.get('code', '')} {error.get('message', '')}".strip()
        else:
            detail = str(error)
        return f"[ERROR]: video job {task_id} ended with status {status}: {detail or 'no error detail provided'}"
    url = ((task.get("content") or {}).get("url")) or ""
    return await _download_video(client, task_id, url, save_dir, "the remote source URL is time-limited")


async def _minimax_check_video(target: GenerationTarget, task_id: str, save_dir: str | None) -> str:
    api_key, api_base = target.api_key, target.api_base
    roots = _minimax_candidate_roots(api_base)
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=60, verify=get_requests_verify()) as client:
            for index, root in enumerate(roots):
                status, task, error = await _minimax_query(client, root, headers, task_id)
                if status == "auth_failed":
                    if index + 1 < len(roots):
                        continue
                    return _minimax_auth_failure_message(roots, error or "")
                break
            if error:
                return error
            if status in _PENDING_STATUSES:
                return f"Video job {task_id} is still {status}."
            return await _minimax_finish(client, task_id, status, task, save_dir)
    except httpx.HTTPError as exc:
        return f"[ERROR]: checking MiniMax video job {task_id} failed: {exc!r}"


# --------------------------------------------------------------------------- #
# ModelArk
# --------------------------------------------------------------------------- #

def _modelark_normalize_base(api_base: str) -> str:
    base = api_base.strip().rstrip("/")
    return base if re.search(r"/api/v\d+$", base) else f"{base}/api/v3"


def _modelark_candidate_bases(api_base: str) -> list[str]:
    primary = _modelark_normalize_base(api_base)
    if primary not in _MODELARK_KNOWN_BASES:
        return [primary]
    return [primary, *[base for base in _MODELARK_KNOWN_BASES if base != primary]]


def _modelark_account_hint(code: str) -> str:
    hint = _MODELARK_ACCOUNT_HINTS.get(code)
    return (
        f" -> {hint} This is an account-side problem: report it to the user; retrying or searching config "
        "files for another key will not help."
        if hint
        else ""
    )


def _modelark_error_detail(payload: Any) -> str | None:
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        err = payload["error"]
        code = str(err.get("code", "error"))
        return f"{code}: {err.get('message', '')}".strip() + _modelark_account_hint(code)
    return None


def _modelark_is_auth_failure(http_status: int, payload: Any) -> bool:
    """Return whether a request failed because of the key or the region it went to.

    That is a rejected key (401 / AuthenticationError), or "model not found", which
    is how a host answers when the key/model belongs to a different region.
    """
    if http_status == 401:
        return True
    err = payload.get("error") if isinstance(payload, dict) else None
    code = str(err.get("code", "")) if isinstance(err, dict) else ""
    return code.startswith("Authentication") or code == "InvalidEndpointOrModel.NotFound"


def _modelark_auth_failure_message(bases: list[str], detail: str) -> str:
    hosts = ", ".join(re.sub(r"^https?://|/api/v\d+$", "", base) for base in bases)
    return (
        f"[ERROR]: ModelArk request failed on every region host tried ({hosts}): {detail}. Check that the API key "
        "is valid, that key and model belong to one of these regions, and that the model name is correct and "
        "activated for your account. Update the settings in Settings > Agent (Image/Video generation). "
        "Do not search other config files for a different key."
    )


async def _modelark_post(
    client: httpx.AsyncClient, bases: list[str], path: str, headers: dict[str, str], body: dict[str, Any]
) -> tuple[str, httpx.Response | None, Any, str | None]:
    """POST to the first base that accepts the key.

    Returns (base, response, payload, error); a non-None error is the finished
    tool result.
    """
    resp: httpx.Response | None = None
    payload: Any = None
    base = bases[0]
    for index, candidate in enumerate(bases):
        base = candidate
        logger.info("ModelArk POST %s%s", base, path)
        resp = await client.post(f"{base}{path}", headers=headers, json=body)
        try:
            payload = resp.json()
        except ValueError:
            return base, resp, None, (
                f"[ERROR]: ModelArk returned a non-JSON response: {resp.status_code} {resp.text[:300]}"
            )
        if _modelark_is_auth_failure(resp.status_code, payload):
            if index + 1 < len(bases):
                logger.warning("ModelArk rejected the key on %s, trying %s", base, bases[index + 1])
                continue
            return base, resp, payload, _modelark_auth_failure_message(
                bases, _modelark_error_detail(payload) or str(resp.status_code)
            )
        break
    return base, resp, payload, None


def _modelark_image_size(size: str | None) -> str | None:
    """An explicit WxH scaled onto the "2k" pixel budget (same aspect ratio), or None."""
    parsed = parse_size(size)
    if parsed is None:
        return None
    scale = (_MODELARK_IMAGE_AREA / (parsed[0] * parsed[1])) ** 0.5
    step = _MODELARK_IMAGE_SIDE_STEP

    def fit(side: int) -> int:
        return min(_MODELARK_IMAGE_MAX_SIDE, max(step, round(side * scale / step) * step))

    return f"{fit(parsed[0])}x{fit(parsed[1])}"


async def _modelark_generate_image(
    target: GenerationTarget, prompt: str, aspect_ratio: str, save_dir: str | None, options: ImageOptions
) -> str:
    api_key, api_base, model = target.api_key, target.api_base, target.model
    explicit_size = _modelark_image_size(options.size)
    body: dict[str, Any] = {
        "model": model,
        # Seedream takes a size tier rather than an aspect ratio, so the ratio is stated in the
        # prompt. "2k" is the one tier every Seedream 5.x model accepts (the lite model rejects
        # "1K": size must be WIDTHxHEIGHT, 2k, 3k or 4k), so it is used regardless of the
        # tool's resolution argument. An explicit WxH is kept at the same pixel budget.
        "prompt": prompt if explicit_size else f"{prompt}\n\n(Aspect ratio: {aspect_ratio})",
        "size": explicit_size or "2k",
        "response_format": "b64_json",
        "watermark": False,
    }
    if options.reference_image_uris:
        body["image"] = list(options.reference_image_uris[:_MODELARK_MAX_REFERENCE_IMAGES])
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=180, verify=get_requests_verify()) as client:
            _, resp, payload, error = await _modelark_post(
                client, _modelark_candidate_bases(api_base), "/images/generations", headers, body
            )
            if error:
                return error
            if resp is None or resp.status_code != 200 or _modelark_error_detail(payload):
                detail = _modelark_error_detail(payload) or (resp.status_code if resp else "no response")
                return f"[ERROR]: ModelArk image generation failed: {detail}"
            items = (payload or {}).get("data") or []
            saved: list[str] = []
            for index, item in enumerate(items):
                if item.get("b64_json"):
                    data = base64.b64decode(item["b64_json"])
                elif item.get("url"):
                    download = await client.get(item["url"], follow_redirects=True, timeout=120)
                    if download.status_code != 200:
                        return (
                            "[ERROR]: ModelArk image was generated but downloading it failed: "
                            f"{download.status_code}"
                        )
                    data = download.content
                else:
                    continue
                dest = _save_path(save_dir, "generated_images", _image_filename(index, data))
                dest.write_bytes(data)
                saved.append(str(dest))
    except httpx.HTTPError as exc:
        return f"[ERROR]: ModelArk image generation request failed: {exc!r}"
    except (OSError, ValueError) as exc:
        return f"[ERROR]: failed to save ModelArk image: {exc!r}"
    if not saved:
        return f"[ERROR]: ModelArk returned no images. Response: {str(payload)[:300]}"
    return "Image generated successfully!\nSaved to: " + ", ".join(saved)


async def _modelark_submit_video(target: GenerationTarget, request: VideoRequest, save_dir: str | None) -> str:
    api_key, api_base, model = target.api_key, target.api_base, target.model
    prompt, aspect_ratio, resolution = request.prompt, request.aspect_ratio, request.resolution
    duration_seconds = request.duration_seconds
    generate_audio = request.generate_audio
    frame, refs = _video_references(request)
    content = _video_content(prompt, frame, refs)
    res = (resolution or "").strip().lower()
    body = {
        "model": model,
        "content": content,
        "resolution": res if res in _MODELARK_VIDEO_RESOLUTIONS else "720p",
        # With a first frame the output must follow that frame's aspect ratio.
        "ratio": (
            "adaptive" if frame else (aspect_ratio if aspect_ratio in _MODELARK_VIDEO_RATIOS else "16:9")
        ),
        "duration": _snap_request_duration(model, duration_seconds),
        "generate_audio": bool(generate_audio),
        "watermark": False,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=60, verify=get_requests_verify()) as client:
            base, resp, payload, error = await _modelark_post(
                client, _modelark_candidate_bases(api_base), "/contents/generations/tasks", headers, body
            )
            if error:
                return error
            if resp is None or resp.status_code not in (200, 201, 202) or _modelark_error_detail(payload):
                detail = _modelark_error_detail(payload) or (resp.status_code if resp else "no response")
                return f"[ERROR]: ModelArk video generation submit failed: {detail}"
            task_id = str((payload or {}).get("id") or "")
            if not task_id:
                return f"[ERROR]: ModelArk video submit returned no task id: {str(payload)[:300]}"

            status, task, elapsed = "queued", {}, 0
            while status in _PENDING_STATUSES and elapsed < _MODELARK_MAX_POLL_SECONDS:
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                elapsed += _POLL_INTERVAL_SECONDS
                status, task, query_error = await _modelark_query(client, base, headers, task_id)
                if query_error:
                    return query_error
            if status in _PENDING_STATUSES:
                return _pending_submit_message(
                    task_id, status, elapsed, " (call the tool again to wait; do not use shell sleep)"
                )
            return await _modelark_finish(client, task_id, status, task, save_dir)
    except httpx.HTTPError as exc:
        return f"[ERROR]: ModelArk video generation request failed: {exc!r}"


async def _modelark_query(
    client: httpx.AsyncClient, base: str, headers: dict[str, str], task_id: str
) -> tuple[str, dict[str, Any], str | None]:
    resp = await client.get(f"{base}/contents/generations/tasks/{task_id}", headers=headers)
    try:
        payload = resp.json()
    except ValueError:
        return "", {}, f"[ERROR]: polling ModelArk video job {task_id} returned a non-JSON response: {resp.status_code}"
    if _modelark_is_auth_failure(resp.status_code, payload):
        return "auth_failed", {}, _modelark_error_detail(payload) or str(resp.status_code)
    if resp.status_code != 200:
        detail = _modelark_error_detail(payload) or resp.status_code
        return "", {}, f"[ERROR]: polling ModelArk video job {task_id} failed: {detail}"
    return str(payload.get("status") or "").lower(), payload, None


async def _modelark_finish(
    client: httpx.AsyncClient, task_id: str, status: str, task: dict[str, Any], save_dir: str | None
) -> str:
    if status != "succeeded":
        error = task.get("error") or {}
        if isinstance(error, dict):
            code = str(error.get("code", ""))
            detail = f"{code} {error.get('message', '')}".strip() + _modelark_account_hint(code)
        else:
            detail = str(error)
        return f"[ERROR]: video job {task_id} ended with status {status}: {detail or 'no error detail provided'}"
    url = ((task.get("content") or {}).get("video_url")) or ""
    return await _download_video(client, task_id, url, save_dir, "the remote source URL expires after 24h")


async def _modelark_check_video(target: GenerationTarget, task_id: str, save_dir: str | None) -> str:
    api_key, api_base = target.api_key, target.api_base
    bases = _modelark_candidate_bases(api_base)
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=60, verify=get_requests_verify()) as client:
            for index, base in enumerate(bases):
                status, task, error = await _modelark_query(client, base, headers, task_id)
                if status == "auth_failed":
                    if index + 1 < len(bases):
                        continue
                    return _modelark_auth_failure_message(bases, error or "")
                break
            if error:
                return error
            if status in _PENDING_STATUSES:
                return (
                    f"Video job {task_id} is still {status}. Call check_video_status again to keep waiting "
                    "(do not use shell sleep)."
                )
            return await _modelark_finish(client, task_id, status, task, save_dir)
    except httpx.HTTPError as exc:
        return f"[ERROR]: checking ModelArk video job {task_id} failed: {exc!r}"


# --------------------------------------------------------------------------- #
# DashScope
# --------------------------------------------------------------------------- #

def _dashscope_base(api_base: str) -> str:
    """The native ``/api/v1`` base; chat presets carry the OpenAI ``compatible-mode`` URL."""
    base = api_base.strip().rstrip("/")
    if not base:
        return _DASHSCOPE_CN_BASE
    if _DASHSCOPE_HOST.match(_host_of(base)) and not re.search(r"/api/v\d+$", base):
        return _DASHSCOPE_INTL_BASE if "dashscope-intl" in base.lower() else _DASHSCOPE_CN_BASE
    return base


def _dashscope_candidate_bases(api_base: str) -> list[str]:
    primary = _dashscope_base(api_base)
    if primary not in _DASHSCOPE_REGION_BASES:
        return [primary]
    return [primary, *[base for base in _DASHSCOPE_REGION_BASES if base != primary]]


def _dashscope_error_detail(payload: Any) -> str | None:
    if isinstance(payload, dict) and payload.get("code"):
        return f"{payload.get('code')}: {payload.get('message', '')}".strip()
    return None


def _dashscope_is_auth_failure(http_status: int, payload: Any) -> bool:
    return http_status == 401 or (isinstance(payload, dict) and payload.get("code") in _DASHSCOPE_AUTH_CODES)


def _dashscope_auth_failure_message(bases: list[str], detail: str) -> str:
    hosts = " and ".join(re.sub(r"^https?://|/api/v\d+$", "", base) for base in bases)
    return (
        f"[ERROR]: DashScope rejected the API key on {hosts} ({detail}). The key is wrong, expired or revoked, "
        "or belongs to another region - update it in Settings > Agent (Image/Video generation). "
        "Do not search other config files for a different key."
    )


async def _dashscope_post(
    client: httpx.AsyncClient,
    bases: list[str],
    path: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> tuple[str, httpx.Response | None, Any, str | None]:
    """POST to the first region that accepts the key; see ``_modelark_post``."""
    resp: httpx.Response | None = None
    payload: Any = None
    base = bases[0]
    for index, candidate in enumerate(bases):
        base = candidate
        logger.info("DashScope POST %s%s (model: %s)", base, path, body.get("model"))
        resp = await client.post(f"{base}{path}", headers=headers, json=body)
        try:
            payload = resp.json()
        except ValueError:
            return base, resp, None, (
                f"[ERROR]: DashScope returned a non-JSON response: {resp.status_code} {resp.text[:300]}"
            )
        if _dashscope_is_auth_failure(resp.status_code, payload):
            if index + 1 < len(bases):
                logger.warning("DashScope rejected the key on %s, trying %s", base, bases[index + 1])
                continue
            return base, resp, payload, _dashscope_auth_failure_message(
                bases, _dashscope_error_detail(payload) or str(resp.status_code)
            )
        break
    return base, resp, payload, None


def _dashscope_image_size(size: str | None, aspect_ratio: str) -> str:
    text = (size or "").strip().upper()
    if text in ("1K", "1024"):
        return "1024*1024"
    if text == "2K":
        return "2048*2048"
    parsed = parse_size(size)
    if parsed:
        return f"{parsed[0]}*{parsed[1]}"
    return _IMAGE_SIZES.get(aspect_ratio, _IMAGE_SIZES["1:1"])


def _dashscope_image_urls(payload: Any) -> list[str]:
    output = payload.get("output") if isinstance(payload, dict) else None
    choices = output.get("choices") if isinstance(output, dict) else None
    urls: list[str] = []
    for choice in choices or []:
        content = ((choice or {}).get("message") or {}).get("content") or []
        urls.extend(str(item["image"]) for item in content if isinstance(item, dict) and item.get("image"))
    return urls


async def _dashscope_generate_image(
    target: GenerationTarget, prompt: str, aspect_ratio: str, save_dir: str | None, options: ImageOptions
) -> str:
    content: list[dict[str, str]] = [
        {"image": uri} for uri in options.reference_image_uris[:_DASHSCOPE_MAX_IMAGE_REFERENCES]
    ]
    content.append({"text": prompt})
    body = {
        "model": target.model,
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": {"size": _dashscope_image_size(options.size, aspect_ratio), "n": 1, "watermark": False},
    }
    headers = {"Authorization": f"Bearer {target.api_key}"}
    try:
        async with httpx.AsyncClient(timeout=300, verify=get_requests_verify()) as client:
            _, resp, payload, error = await _dashscope_post(
                client, _dashscope_candidate_bases(target.api_base),
                "/services/aigc/multimodal-generation/generation", headers, body,
            )
            if error:
                return error
            if resp is None or resp.status_code != 200 or _dashscope_error_detail(payload):
                detail = _dashscope_error_detail(payload) or (resp.status_code if resp else "no response")
                return f"[ERROR]: DashScope image generation failed: {detail}"
            saved: list[str] = []
            for index, url in enumerate(_dashscope_image_urls(payload)):
                download = await client.get(url, follow_redirects=True, timeout=120)
                if download.status_code != 200:
                    return f"[ERROR]: DashScope image was generated but downloading it failed: {download.status_code}"
                dest = _save_path(save_dir, "generated_images", _image_filename(index, download.content))
                dest.write_bytes(download.content)
                saved.append(str(dest))
    except httpx.HTTPError as exc:
        return f"[ERROR]: DashScope image generation request failed: {exc!r}"
    except OSError as exc:
        return f"[ERROR]: failed to save DashScope image: {exc!r}"
    if not saved:
        return f"[ERROR]: DashScope returned no images. Response: {str(payload)[:300]}"
    return "Image generated successfully!\nSaved to: " + ", ".join(saved)


def _is_wan3(model: str) -> bool:
    return "wan3" in model.strip().lower().replace("-", "").replace("_", "")


def _wan2_supports_shot_type(model: str) -> bool:
    return any(version in model for version in ("2.2", "2.5", "2.6", "2.7"))


def _dashscope_resolution_tier(request: VideoRequest) -> str:
    digits = re.sub(r"\D", "", request.resolution or "")
    if digits and int(digits) <= 480:
        return "480p"
    if digits and int(digits) >= 1080:
        return "1080p"
    return "720p"


def _dashscope_video_size(request: VideoRequest) -> str:
    """Explicit size wins, except that 480P is snapped to a documented Wan 480P size."""
    tier = _dashscope_resolution_tier(request)
    parsed = parse_size(request.size)
    if parsed and not (tier == "480p" and request.resolution):
        return f"{parsed[0]}*{parsed[1]}"
    sizes = _VIDEO_SIZES[tier]
    ratio = aspect_ratio_for_size(request.size, tuple(sizes), "16:9") if parsed else request.aspect_ratio
    return sizes.get(ratio, sizes["16:9"])


def _dashscope_video_body(model: str, request: VideoRequest, file_url: str | None) -> dict[str, Any]:
    """Map the request onto Wan's input: references, a lone first frame, or text only."""
    frame, refs = _video_references(request)
    if refs and frame:
        refs, frame = [*refs, frame], None
    size = _dashscope_video_size(request)
    resolution = _dashscope_resolution_tier(request).upper()
    inputs: dict[str, Any] = {"prompt": request.prompt}
    parameters: dict[str, Any] = {
        "duration": _snap_request_duration(model, request.duration_seconds),
    }
    if _is_wan3(model):
        parameters["audio"] = bool(request.generate_audio)
        if refs or file_url:
            media = [{"type": "reference_image", "url": url} for url in refs]
            if file_url:
                media.append({"type": "file", "url": file_url})
            inputs["media"] = media
            ratios = ("16:9", "4:3", "1:1", "3:4", "9:16")
            parameters.update(size=size, ratio=aspect_ratio_for_size(size, ratios, "16:9"))
        elif frame:
            inputs["img_url"] = frame
            parameters["resolution"] = resolution
        else:
            parameters["size"] = size
    elif refs:
        inputs["reference_urls"] = refs[:_DASHSCOPE_MAX_WAN2_REFERENCES]
        parameters["size"] = size
        if _wan2_supports_shot_type(model):
            parameters["shot_type"] = "multi"
    elif frame:
        inputs["img_url"] = frame
        parameters["resolution"] = resolution
        if _wan2_supports_shot_type(model):
            parameters["shot_type"] = "single"
    else:
        parameters["size"] = size
    return {"model": model, "input": inputs, "parameters": parameters}


def _dashscope_upload_file(model: str, path: str, api_key: str, base: str) -> str:
    """Upload a local file to DashScope's temporary OSS storage and return its oss:// URL."""
    import dashscope  # pylint: disable=import-outside-toplevel
    from dashscope.utils.oss_utils import OssUtils  # pylint: disable=import-outside-toplevel

    with _DASHSCOPE_SDK_LOCK:
        previous = dashscope.base_http_api_url
        dashscope.base_http_api_url = base
        try:
            url, _ = OssUtils.upload(model=model, file_path=path, api_key=api_key)
        finally:
            dashscope.base_http_api_url = previous
    if not url:
        raise OSError(f"DashScope upload of {path} returned no URL")
    return str(url)


async def _dashscope_query(
    client: httpx.AsyncClient, base: str, headers: dict[str, str], task_id: str
) -> tuple[str, dict[str, Any], str | None]:
    resp = await client.get(f"{base}/tasks/{task_id}", headers=headers)
    try:
        payload = resp.json()
    except ValueError:
        return "", {}, (
            f"[ERROR]: polling DashScope video job {task_id} returned a non-JSON response: {resp.status_code}"
        )
    if _dashscope_is_auth_failure(resp.status_code, payload):
        return "auth_failed", {}, _dashscope_error_detail(payload) or str(resp.status_code)
    if resp.status_code != 200:
        detail = _dashscope_error_detail(payload) or resp.status_code
        return "", {}, f"[ERROR]: polling DashScope video job {task_id} failed: {detail}"
    output = payload.get("output") or {}
    status = str(output.get("task_status") or "").lower()
    return ("running" if status in ("pending", "running") else status), output, None


async def _dashscope_finish(
    client: httpx.AsyncClient, task_id: str, status: str, output: dict[str, Any], save_dir: str | None
) -> str:
    if status != "succeeded":
        detail = f"{output.get('code', '')} {output.get('message', '')}".strip()
        return f"[ERROR]: video job {task_id} ended with status {status}: {detail or 'no error detail provided'}"
    return await _download_video(
        client, task_id, str(output.get("video_url") or ""), save_dir, "the remote source URL expires after 24h"
    )


async def _dashscope_submit_video(target: GenerationTarget, request: VideoRequest, save_dir: str | None) -> str:
    bases = _dashscope_candidate_bases(target.api_base)
    headers = {"Authorization": f"Bearer {target.api_key}", "X-DashScope-Async": "enable"}
    file_url = None
    if request.reference_file_path:
        if not _is_wan3(target.model):
            logger.info("[generate_video] %s takes no file reference; ignoring it", target.model)
        elif re.match(r"^(https?|oss)://", request.reference_file_path):
            file_url = request.reference_file_path
        else:
            try:
                file_url = await asyncio.to_thread(
                    _dashscope_upload_file, target.model, request.reference_file_path, target.api_key, bases[0]
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught  # SDK-specific exception types
                return f"[ERROR]: uploading reference file {request.reference_file_path} to DashScope failed: {exc}"
    if file_url and file_url.startswith("oss://"):
        headers["X-DashScope-OssResourceResolve"] = "enable"
    body = _dashscope_video_body(target.model, request, file_url)
    try:
        async with httpx.AsyncClient(timeout=60, verify=get_requests_verify()) as client:
            base, resp, payload, error = await _dashscope_post(
                client, bases, "/services/aigc/video-generation/video-synthesis", headers, body
            )
            if error:
                return error
            if resp is None or resp.status_code != 200 or _dashscope_error_detail(payload):
                detail = _dashscope_error_detail(payload) or (resp.status_code if resp else "no response")
                return f"[ERROR]: DashScope video generation submit failed: {detail}"
            task_id = str(((payload or {}).get("output") or {}).get("task_id") or "")
            if not task_id:
                return f"[ERROR]: DashScope video submit returned no task id: {str(payload)[:300]}"

            poll_headers = {"Authorization": f"Bearer {target.api_key}"}
            status, output, elapsed = "queued", {}, 0
            while status in _PENDING_STATUSES and elapsed < _DASHSCOPE_MAX_POLL_SECONDS:
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                elapsed += _POLL_INTERVAL_SECONDS
                status, output, query_error = await _dashscope_query(client, base, poll_headers, task_id)
                if query_error:
                    return query_error
            if status in _PENDING_STATUSES:
                return _pending_submit_message(
                    task_id, status, elapsed, " (call the tool again to wait; do not use shell sleep)"
                )
            return await _dashscope_finish(client, task_id, status, output, save_dir)
    except httpx.HTTPError as exc:
        return f"[ERROR]: DashScope video generation request failed: {exc!r}"


async def _dashscope_check_video(target: GenerationTarget, task_id: str, save_dir: str | None) -> str:
    bases = _dashscope_candidate_bases(target.api_base)
    headers = {"Authorization": f"Bearer {target.api_key}"}
    try:
        async with httpx.AsyncClient(timeout=60, verify=get_requests_verify()) as client:
            for index, base in enumerate(bases):
                status, output, error = await _dashscope_query(client, base, headers, task_id)
                if status == "auth_failed":
                    if index + 1 < len(bases):
                        continue
                    return _dashscope_auth_failure_message(bases, error or "")
                break
            if error:
                return error
            if status in _PENDING_STATUSES:
                return (
                    f"Video job {task_id} is still {status}. Call check_video_status again to keep waiting "
                    "(do not use shell sleep)."
                )
            return await _dashscope_finish(client, task_id, status, output, save_dir)
    except httpx.HTTPError as exc:
        return f"[ERROR]: checking DashScope video job {task_id} failed: {exc!r}"


# --------------------------------------------------------------------------- #
# vLLM-Omni
# --------------------------------------------------------------------------- #

def _vllm_omni_size(size: str | None, aspect_ratio: str, resolution: str) -> str | None:
    if parse_size(size):
        return size
    tier = re.sub(r"\D", "", resolution or "")
    sizes = _VIDEO_SIZES.get(f"{tier}p") if tier else None
    return (sizes or {}).get(aspect_ratio)


async def _vllm_omni_generate_image(
    target: GenerationTarget, prompt: str, aspect_ratio: str, save_dir: str | None, options: ImageOptions
) -> str:
    size = options.size if parse_size(options.size) else _IMAGE_SIZES.get(aspect_ratio)
    output_dir = _save_root(save_dir, "generated_images")
    try:
        result = await asyncio.to_thread(
            vllm_omni_gen.invoke_vllm_omni_image_generation_sync,
            prompt,
            api_key=target.api_key,
            api_base=target.api_base,
            model=target.model,
            size=size,
            reference_images=list(options.reference_image_uris) or None,
            output_dir=output_dir,
            **dict(options.extra or {}),
        )
    except (requests.RequestException, ValueError, OSError) as exc:
        return f"[ERROR]: vLLM-Omni image generation failed: {exc}"
    return "Image generated successfully!\nSaved to: " + str(result["image_path"])


async def _vllm_omni_submit_video(target: GenerationTarget, request: VideoRequest, save_dir: str | None) -> str:
    frame, refs = _video_references(request)
    try:
        video_id = await asyncio.to_thread(
            vllm_omni_gen.submit_vllm_omni_video_sync,
            request.prompt,
            api_key=target.api_key,
            api_base=target.api_base,
            model=target.model,
            size=_vllm_omni_size(request.size, request.aspect_ratio, request.resolution),
            duration=request.duration_seconds,
            resolution=request.resolution,
            first_frame=frame,
            reference_images=refs or None,
            **dict(request.extra or {}),
        )
    except (requests.RequestException, ValueError, OSError) as exc:
        return f"[ERROR]: vLLM-Omni video generation submit failed: {exc}"
    elapsed = 0
    while elapsed < _VLLM_OMNI_MAX_POLL_SECONDS:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS
        result = await _vllm_omni_poll(target, video_id, save_dir)
        if result is not None:
            return result
    return _pending_submit_message(
        video_id, "running", elapsed, " (call the tool again to wait; do not use shell sleep)"
    )


async def _vllm_omni_poll(target: GenerationTarget, video_id: str, save_dir: str | None) -> str | None:
    """The finished tool result, or None while the job is still running."""
    try:
        job = await asyncio.to_thread(
            vllm_omni_gen.query_vllm_omni_video_sync,
            api_key=target.api_key, api_base=target.api_base, video_id=video_id,
        )
        if job.failed:
            detail = job.error or "no error detail provided"
            return f"[ERROR]: video job {video_id} ended with status {job.status}: {detail}"
        if not job.completed:
            return None
        dest = _save_path(save_dir, "generated_videos", f"video_{video_id}.mp4")
        await asyncio.to_thread(
            vllm_omni_gen.download_vllm_omni_video_sync,
            api_key=target.api_key, api_base=target.api_base, video_id=video_id, output_path=dest,
        )
    except (requests.RequestException, ValueError, OSError) as exc:
        return f"[ERROR]: checking vLLM-Omni video job {video_id} failed: {exc}"
    return f"Video generated successfully!\nSaved to: {dest}\n(job {video_id} - this local file is the durable copy.)"


async def _vllm_omni_check_video(target: GenerationTarget, task_id: str, save_dir: str | None) -> str:
    result = await _vllm_omni_poll(target, task_id, save_dir)
    if result is None:
        return (
            f"Video job {task_id} is still running. Call check_video_status again to keep waiting "
            "(do not use shell sleep)."
        )
    return result
