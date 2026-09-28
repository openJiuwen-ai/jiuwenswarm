# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Vendor-native image / video generation backends (MiniMax, BytePlus ModelArk).

Neither vendor's generation API is OpenAI/OpenRouter-compatible, so the
OpenRouter-style request path in visual_gen_tools / video_gen_tools cannot drive
them. Those tools call the four entry points below, which dispatch on the
backend name returned by ``detect_backend``:

- ``detect_backend(protocol_env, api_base)`` -> ``"minimax"`` / ``"modelark"`` / None
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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from jiuwenswarm.agents.harness.common.tools.ssl_config import get_requests_verify
from jiuwenswarm.common.utils import get_agent_workspace_dir

logger = logging.getLogger(__name__)

MINIMAX = "minimax"
MODELARK = "modelark"

_VIDEO_MIN_SECONDS = 4
_VIDEO_MAX_SECONDS = 15
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
    """What to render: the prompt plus the video options the tools expose."""

    prompt: str
    aspect_ratio: str
    resolution: str
    duration_seconds: int
    generate_audio: bool = False
    first_frame_data_uri: str | None = None


def _host_of(api_base: str) -> str:
    return re.sub(r"^https?://", "", api_base.strip(), flags=re.IGNORECASE).split("/", 1)[0].split(":", 1)[0]


def detect_backend(protocol_env: str, api_base: str) -> str | None:
    """Return the vendor-native backend for the configured slot, or None.

    None means the OpenRouter-style path. The backend is named by the saved 协议
    (read from ``protocol_env``), or implied by the API URL being one of the
    vendor's hosts.
    """
    protocol = os.environ.get(protocol_env, "").strip().lower()
    if protocol in (MINIMAX, MODELARK):
        return protocol
    host = _host_of(api_base)
    if _MINIMAX_HOST.search(host):
        return MINIMAX
    if _MODELARK_HOST.match(host):
        return MODELARK
    return None


async def generate_image(target: GenerationTarget, prompt: str, aspect_ratio: str, save_dir: str | None) -> str:
    if target.backend == MINIMAX:
        return await _minimax_generate_image(target, prompt, aspect_ratio, save_dir)
    if target.backend == MODELARK:
        return await _modelark_generate_image(target, prompt, aspect_ratio, save_dir)
    return f"[ERROR]: unknown generation backend: {target.backend!r}"


async def submit_video(target: GenerationTarget, request: VideoRequest, save_dir: str | None) -> str:
    if target.backend == MINIMAX:  # H3 has no audio switch
        return await _minimax_submit_video(target, request, save_dir)
    if target.backend == MODELARK:
        return await _modelark_submit_video(target, request, save_dir)
    return f"[ERROR]: unknown generation backend: {target.backend!r}"


async def check_video(target: GenerationTarget, task_id: str, save_dir: str | None) -> str:
    if target.backend == MINIMAX:
        return await _minimax_check_video(target, task_id, save_dir)
    if target.backend == MODELARK:
        return await _modelark_check_video(target, task_id, save_dir)
    return f"[ERROR]: unknown generation backend: {target.backend!r}"


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _save_path(save_dir: str | None, default_sub: str, filename: str) -> Path:
    root = Path(save_dir).expanduser() if save_dir else (get_agent_workspace_dir() / default_sub)
    root.mkdir(parents=True, exist_ok=True)
    return root / filename


def _image_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def _image_filename(index: int, data: bytes) -> str:
    return f"image_{int(time.time())}_{index}_{secrets.token_hex(4)}.{_image_extension(data)}"


def _clamp_duration(duration_seconds: int) -> int:
    return max(_VIDEO_MIN_SECONDS, min(_VIDEO_MAX_SECONDS, int(duration_seconds)))


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
    duration_seconds, first_frame_data_uri = request.duration_seconds, request.first_frame_data_uri
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt[:_MINIMAX_VIDEO_PROMPT_LIMIT]}]
    if first_frame_data_uri:
        content.append({"type": "image_url", "image_url": {"url": first_frame_data_uri}, "role": "first_frame"})
    body = {
        "model": model,
        "content": content,
        "resolution": _minimax_video_resolution(resolution),
        "duration": _clamp_duration(duration_seconds),
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


async def _modelark_generate_image(
    target: GenerationTarget, prompt: str, aspect_ratio: str, save_dir: str | None
) -> str:
    api_key, api_base, model = target.api_key, target.api_base, target.model
    body = {
        "model": model,
        # Seedream takes a size tier rather than an aspect ratio, so the ratio is stated in the
        # prompt. "2k" is the one tier every Seedream 5.x model accepts (the lite model rejects
        # "1K": size must be WIDTHxHEIGHT, 2k, 3k or 4k), so it is used regardless of the
        # tool's resolution argument.
        "prompt": f"{prompt}\n\n(Aspect ratio: {aspect_ratio})",
        "size": "2k",
        "response_format": "b64_json",
        "watermark": False,
    }
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
    duration_seconds, first_frame_data_uri = request.duration_seconds, request.first_frame_data_uri
    generate_audio = request.generate_audio
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if first_frame_data_uri:
        content.append({"type": "image_url", "image_url": {"url": first_frame_data_uri}, "role": "first_frame"})
    res = (resolution or "").strip().lower()
    body = {
        "model": model,
        "content": content,
        "resolution": res if res in _MODELARK_VIDEO_RESOLUTIONS else "720p",
        # With a first frame the output must follow that frame's aspect ratio.
        "ratio": (
            "adaptive" if first_frame_data_uri else (aspect_ratio if aspect_ratio in _MODELARK_VIDEO_RATIOS else "16:9")
        ),
        "duration": _clamp_duration(duration_seconds),
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
